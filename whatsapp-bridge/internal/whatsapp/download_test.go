package whatsapp

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func TestSenderJID(t *testing.T) {
	group := types.NewJID("120363145616168799", types.GroupServer)
	lidChat := types.NewJID("96941807521806", types.HiddenUserServer)
	cases := []struct {
		sender string
		chat   types.JID
		want   string
	}{
		{"5551999990000@s.whatsapp.net", group, "5551999990000@s.whatsapp.net"},
		{"245637920633041@lid", group, "245637920633041@lid"},
		{"5551999990000", group, "5551999990000@s.whatsapp.net"},
		{"96941807521806", lidChat, "96941807521806@lid"},
	}
	for _, c := range cases {
		got, err := senderJID(c.sender, c.chat)
		if err != nil {
			t.Fatalf("senderJID(%q): %v", c.sender, err)
		}
		if got.String() != c.want {
			t.Errorf("senderJID(%q) = %s, want %s", c.sender, got, c.want)
		}
	}
}
