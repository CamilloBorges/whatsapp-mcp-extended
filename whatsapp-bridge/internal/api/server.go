package api

import (
	"fmt"
	"net"
	"net/http"
	"os"

	"whatsapp-bridge/internal/database"
	"whatsapp-bridge/internal/webhook"
	"whatsapp-bridge/internal/whatsapp"
)

// Server is the HTTP REST API server for the WhatsApp bridge.
// It exposes endpoints for sending messages, managing webhooks,
// group operations, and other WhatsApp features.
type Server struct {
	client         *whatsapp.Client
	messageStore   *database.MessageStore
	webhookManager *webhook.Manager
	port           int
	bindHost       string
}

// NewServer creates a new API server with the given dependencies.
func NewServer(client *whatsapp.Client, messageStore *database.MessageStore, webhookManager *webhook.Manager, port int, bindHost string) *Server {
	if bindHost == "" {
		bindHost = "127.0.0.1"
	}
	return &Server{
		client:         client,
		messageStore:   messageStore,
		webhookManager: webhookManager,
		port:           port,
		bindHost:       bindHost,
	}
}

// Start launches the HTTP server in a background goroutine.
//
// When the API_SOCKET environment variable is set, the server binds a UNIX
// domain socket at that path (removing any stale socket file first) instead of
// listening on a TCP port. The socket is chmod'd to 0660 so a same-uid client
// (e.g. the Python MCP server) can connect. When API_SOCKET is unset, the
// server falls back to the configured TCP bind host and port.
func (s *Server) Start() {
	s.registerHandlers()

	if socketPath := os.Getenv("API_SOCKET"); socketPath != "" {
		fmt.Printf("Starting REST API server on UNIX socket %s...\n", socketPath)

		// Remove any stale socket left over from a previous run; a lingering
		// file would otherwise make net.Listen fail with "address in use".
		_ = os.Remove(socketPath)

		ln, err := net.Listen("unix", socketPath)
		if err != nil {
			fmt.Printf("REST API server error: failed to listen on UNIX socket %s: %v\n", socketPath, err)
			return
		}

		// Allow same-uid clients (e.g. the Python MCP server in our container)
		// to connect. A chmod failure is non-fatal — log and continue.
		if err := os.Chmod(socketPath, 0660); err != nil {
			fmt.Printf("REST API server warning: failed to chmod UNIX socket %s: %v\n", socketPath, err)
		}

		go func() {
			if err := http.Serve(ln, nil); err != nil {
				fmt.Printf("REST API server error: %v\n", err)
			}
		}()
		return
	}

	serverAddr := fmt.Sprintf("%s:%d", s.bindHost, s.port)
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	// Bind before backgrounding, so a failure to acquire the port is fatal
	// rather than logged and ignored. A bridge that cannot own its port is
	// unreachable by the MCP server, but still connects to WhatsApp — and that
	// connection evicts the previous bridge's device session, which *was*
	// serving the port. Carrying on would therefore leave the port dead with no
	// bridge behind it and nothing to notice: the process stays up, so the
	// container looks healthy while WhatsApp silently stops working. Exiting
	// instead lets the supervisor respawn us cleanly.
	ln, err := net.Listen("tcp", serverAddr)
	if err != nil {
		fmt.Printf("REST API server error: failed to listen on %s: %v\n", serverAddr, err)
		os.Exit(1)
	}

	go func() {
		if err := http.Serve(ln, nil); err != nil {
			fmt.Printf("REST API server error: %v\n", err)
		}
	}()
}

// registerHandlers sets up all API routes with security middleware.
// All endpoints are protected by SecureMiddleware which enforces:
// API key authentication, rate limiting, CORS, and security headers.
func (s *Server) registerHandlers() {
	// Health check - no auth (for Docker healthcheck / load balancers)
	http.HandleFunc("/api/health", CorsMiddleware(s.handleHealth))

	// Message operations endpoints
	http.HandleFunc("/api/send", SecureMiddleware(s.handleSendMessage))
	http.HandleFunc("/api/messages", SecureMiddleware(s.handleGetMessages))
	http.HandleFunc("/api/chats", SecureMiddleware(s.handleGetChats))

	// Webhook management endpoints
	http.HandleFunc("/api/webhooks", SecureMiddleware(s.handleWebhooks))
	http.HandleFunc("/api/webhooks/", SecureMiddleware(s.handleWebhookByID))
	http.HandleFunc("/api/webhook-logs", SecureMiddleware(s.handleWebhookLogs))

	// Phase 1 features: Reactions, Edit, Delete, Group Info, Mark Read
	http.HandleFunc("/api/reaction", SecureMiddleware(s.handleReaction))
	http.HandleFunc("/api/edit", SecureMiddleware(s.handleEditMessage))
	http.HandleFunc("/api/delete", SecureMiddleware(s.handleDeleteMessage))
	http.HandleFunc("/api/group/", SecureMiddleware(s.handleGetGroupInfo))
	http.HandleFunc("/api/read", SecureMiddleware(s.handleMarkRead))

	// Phase 2: Group Management
	http.HandleFunc("/api/group/create", SecureMiddleware(s.handleCreateGroup))
	http.HandleFunc("/api/group/add-members", SecureMiddleware(s.handleAddGroupMembers))
	http.HandleFunc("/api/group/remove-members", SecureMiddleware(s.handleRemoveGroupMembers))
	http.HandleFunc("/api/group/promote", SecureMiddleware(s.handlePromoteAdmin))
	http.HandleFunc("/api/group/demote", SecureMiddleware(s.handleDemoteAdmin))
	http.HandleFunc("/api/group/leave", SecureMiddleware(s.handleLeaveGroup))
	http.HandleFunc("/api/group/update", SecureMiddleware(s.handleUpdateGroup))

	// Phase 3: Polls
	http.HandleFunc("/api/poll/create", SecureMiddleware(s.handleCreatePoll))

	// Phase 4: History Sync
	http.HandleFunc("/api/history/request", SecureMiddleware(s.handleRequestHistory))

	// Phase 5: Advanced Features
	http.HandleFunc("/api/presence/set", SecureMiddleware(s.handleSetPresence))
	http.HandleFunc("/api/presence/subscribe", SecureMiddleware(s.handleSubscribePresence))
	http.HandleFunc("/api/profile-picture", SecureMiddleware(s.handleGetProfilePicture))
	http.HandleFunc("/api/blocklist", SecureMiddleware(s.handleGetBlocklist))
	http.HandleFunc("/api/blocklist/update", SecureMiddleware(s.handleUpdateBlocklist))
	http.HandleFunc("/api/newsletter/follow", SecureMiddleware(s.handleFollowNewsletter))
	http.HandleFunc("/api/newsletter/unfollow", SecureMiddleware(s.handleUnfollowNewsletter))
	http.HandleFunc("/api/newsletter/create", SecureMiddleware(s.handleCreateNewsletter))

	// Phase 6: Chat Features
	http.HandleFunc("/api/typing", SecureMiddleware(s.handleSendTyping))
	http.HandleFunc("/api/set-about", SecureMiddleware(s.handleSetAbout))
	http.HandleFunc("/api/disappearing", SecureMiddleware(s.handleSetDisappearingTimer))
	http.HandleFunc("/api/privacy", SecureMiddleware(s.handleGetPrivacySettings))
	http.HandleFunc("/api/pin", SecureMiddleware(s.handlePinChat))
	http.HandleFunc("/api/mute", SecureMiddleware(s.handleMuteChat))
	http.HandleFunc("/api/archive", SecureMiddleware(s.handleArchiveChat))

	// Phase 7: Phone Number Pairing
	http.HandleFunc("/api/qr", SecureMiddleware(s.handleGetQR))
	http.HandleFunc("/api/logout", SecureMiddleware(s.handleLogout))
	http.HandleFunc("/api/pair", SecureMiddleware(s.handlePairPhone))
	http.HandleFunc("/api/pairing", SecureMiddleware(s.handlePairingStatus))
	http.HandleFunc("/api/connection", SecureMiddleware(s.handleConnectionStatus))

	// Connection management
	http.HandleFunc("/api/reconnect", SecureMiddleware(s.handleReconnect))

	// Media download
	http.HandleFunc("/api/download", SecureMiddleware(s.handleDownload))

	// Sync status monitoring
	http.HandleFunc("/api/sync-status", SecureMiddleware(s.handleSyncStatus))
}
