package whatsapp

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"

	"whatsapp-bridge/internal/database"
)

// ErrNoMedia is returned when the message exists but carries no media.
var ErrNoMedia = errors.New("message has no media")

// ErrMessageNotFound is returned when the message id+jid is not in the store.
var ErrMessageNotFound = errors.New("message not found")

// Fork-local: media that WhatsApp already removed from its CDN (404/410, usually after a few
// weeks) can still come back if the phone re-uploads it. We ask with a media retry receipt and
// wait for the phone's answer (it must be online and still have the file).
const mediaRetryWait = 45 * time.Second

var (
	mediaRetryOnce    sync.Once
	mediaRetryWaiters sync.Map // message ID -> chan *events.MediaRetry
)

func downloadable(mediaType, url, directPath string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) (whatsmeow.DownloadableMessage, error) {
	switch mediaType {
	case "image":
		return &waE2E.ImageMessage{URL: &url, DirectPath: &directPath, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &fileLength}, nil
	case "video":
		return &waE2E.VideoMessage{URL: &url, DirectPath: &directPath, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &fileLength}, nil
	case "audio":
		return &waE2E.AudioMessage{URL: &url, DirectPath: &directPath, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &fileLength}, nil
	case "document":
		return &waE2E.DocumentMessage{URL: &url, DirectPath: &directPath, MediaKey: mediaKey,
			FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256, FileLength: &fileLength}, nil
	}
	return nil, fmt.Errorf("unsupported media type %q", mediaType)
}

// DownloadMessageMedia decrypts and saves media for a stored message.
// Returns the absolute path on disk and the media type ("image"/"video"/"audio"/"document").
func (c *Client) DownloadMessageMedia(ctx context.Context, store *database.MessageStore, mediaDir, messageID, chatJID string) (string, string, error) {
	mediaType, filename, url, directPath, mediaKey, fileSHA256, fileEncSHA256, fileLength, err := store.GetMessageMedia(messageID, chatJID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return "", "", ErrMessageNotFound
		}
		return "", "", fmt.Errorf("lookup message: %w", err)
	}
	if mediaType == "" {
		return "", "", ErrNoMedia
	}

	msg, err := downloadable(mediaType, url, directPath, mediaKey, fileSHA256, fileEncSHA256, fileLength)
	if err != nil {
		return "", "", err
	}
	data, err := c.Client.Download(ctx, msg)
	if errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) || errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410) {
		newPath, retryErr := c.requestMediaReupload(ctx, store, messageID, chatJID, mediaKey)
		if retryErr != nil {
			return "", "", fmt.Errorf("whatsmeow download: %w; phone re-upload: %v", err, retryErr)
		}
		if updErr := store.UpdateMediaDirectPath(messageID, chatJID, newPath); updErr != nil {
			c.logger.Warnf("could not store the new direct path for %s: %v", messageID, updErr)
		}
		msg, _ = downloadable(mediaType, "", newPath, mediaKey, fileSHA256, fileEncSHA256, fileLength)
		data, err = c.Client.Download(ctx, msg)
	}
	if err != nil {
		return "", "", fmt.Errorf("whatsmeow download: %w", err)
	}

	// Strip path separators defensively before joining.
	safeJID := strings.ReplaceAll(chatJID, "/", "_")
	safeName := strings.ReplaceAll(filename, "/", "_")
	if safeName == "" {
		safeName = messageID
	}
	dir := filepath.Join(mediaDir, safeJID)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", "", fmt.Errorf("mkdir %s: %w", dir, err)
	}
	out := filepath.Join(dir, safeName)
	if err := os.WriteFile(out, data, 0o644); err != nil {
		return "", "", fmt.Errorf("write %s: %w", out, err)
	}
	abs, err := filepath.Abs(out)
	if err != nil {
		return out, mediaType, nil
	}
	return abs, mediaType, nil
}

// senderJID turns the stored sender (bare user or full JID) into a JID. A bare user takes the
// chat's server in direct chats (phone number or LID) and the phone-number server in groups.
func senderJID(sender string, chat types.JID) (types.JID, error) {
	if strings.Contains(sender, "@") {
		return types.ParseJID(sender)
	}
	server := types.DefaultUserServer
	if chat.Server == types.HiddenUserServer {
		server = types.HiddenUserServer
	}
	return types.NewJID(sender, server), nil
}

// requestMediaReupload asks the phone to re-upload expired media and returns the new direct path.
func (c *Client) requestMediaReupload(ctx context.Context, store *database.MessageStore, messageID, chatJID string, mediaKey []byte) (string, error) {
	mediaRetryOnce.Do(func() {
		c.AddEventHandler(func(evt interface{}) {
			if retry, ok := evt.(*events.MediaRetry); ok {
				if ch, found := mediaRetryWaiters.Load(string(retry.MessageID)); found {
					select {
					case ch.(chan *events.MediaRetry) <- retry:
					default:
					}
				}
			}
		})
	})

	chat, err := types.ParseJID(chatJID)
	if err != nil {
		return "", fmt.Errorf("chat jid: %w", err)
	}
	sender, fromMe, err := store.GetMessageOrigin(messageID, chatJID)
	if err != nil {
		return "", fmt.Errorf("lookup sender: %w", err)
	}
	info := &types.MessageInfo{
		MessageSource: types.MessageSource{Chat: chat, IsFromMe: fromMe, IsGroup: chat.Server == types.GroupServer},
		ID:            types.MessageID(messageID),
	}
	if info.IsGroup && !fromMe && sender != "" {
		if info.Sender, err = senderJID(sender, chat); err != nil {
			return "", fmt.Errorf("sender jid: %w", err)
		}
	}

	answer := make(chan *events.MediaRetry, 1)
	mediaRetryWaiters.Store(messageID, answer)
	defer mediaRetryWaiters.Delete(messageID)
	if err := c.Client.SendMediaRetryReceipt(ctx, info, mediaKey); err != nil {
		return "", fmt.Errorf("send media retry receipt: %w", err)
	}

	select {
	case retry := <-answer:
		if retry.Error != nil {
			return "", fmt.Errorf("the phone refused the re-upload (code %d)", retry.Error.Code)
		}
		result, err := whatsmeow.DecryptMediaRetryNotification(retry, mediaKey)
		if err != nil {
			return "", fmt.Errorf("decrypt media retry: %w", err)
		}
		if result.GetResult() != waMmsRetry.MediaRetryNotification_SUCCESS {
			return "", fmt.Errorf("the phone no longer has the media (%s)", result.GetResult())
		}
		return result.GetDirectPath(), nil
	case <-time.After(mediaRetryWait):
		return "", errors.New("the phone did not answer the re-upload request (it must be online)")
	case <-ctx.Done():
		return "", ctx.Err()
	}
}
