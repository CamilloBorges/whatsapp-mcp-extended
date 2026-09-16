# Deploy: WhatsApp MCP (extended) on EasyPanel

Repo: https://github.com/CamilloBorges/whatsapp-mcp-extended

This replaces an earlier deployment based on `Malaccamaxgit/whatsapp-mcp-docker`
(`CamilloBorges/whatsapp-mcp-docker`), which had a persistent, unresolved `websocket not
connected` pairing failure across two different networks and two phone number formats. This
project (`domdomegg/whatsapp-mcp-extended`) is a more actively maintained fork with a much
more robust pairing flow (phone-code pairing via a documented REST endpoint, a QR web UI
fallback, self-service reconnect/status tools) and — importantly — **native Streamable HTTP
transport**, so we no longer need the `supergateway` stdio-to-HTTP wrapper the old setup
required.

## Architecture
```
Claude Code / any MCP client
        │  HTTPS + Cloudflare Access headers
        ▼
Cloudflare Tunnel (cloudflare-tunnel container, already on the `easypanel` network)
        │
        ▼
EasyPanel host, service "whatsapp-mcp" (port 8081, internal only)
        │  BRIDGE_HOST=whatsapp-bridge
        ▼
service "whatsapp-bridge" (Go, whatsmeow, port 8080, internal only)
        │
        ▼
WhatsApp (device-linked account; own session, separate from any other deployment)

service "web-ui" also runs (QR pairing fallback) but gets NO public domain — see Pairing below.
```

## Tool scope
`WHATSAPP_MCP_TOOLSETS=core` + `WHATSAPP_MCP_TOOLS=get_setup_qr,setup_status,download_media`
(all `read_only` per the upstream code's own tool annotations). No send/edit/delete/group
management/presence/blocklist/newsletter tools are registered at all — not just hidden,
actually absent from the MCP tool list the server advertises.

## Deploy steps

### 1. EasyPanel
1. New service → **App from Git**, this repo, path to compose file:
   `deploy/easypanel/docker-compose.yml`.
2. Set as env vars in EasyPanel's UI (deploy fails without these, on purpose):
   - `API_KEY` — generate with `openssl rand -hex 32`
   - `MCP_TRUSTED_HOSTS` — the public hostname you're about to create in step 2 below (e.g.
     `mcp-whatsapp-ext.bomgado.net`). **This has to match exactly what you set the domain to**
     — decide the hostname first, then fill this in, then deploy.
   - `TZ` (optional, defaults to America/Sao_Paulo)
3. Deploy. First build compiles the Go bridge, the Python MCP server, and the React web UI —
   expect several minutes.

### 2. Domain (only for the `whatsapp-mcp` service)
In the service's Domains tab: add a domain — same hostname as `MCP_TRUSTED_HOSTS` above —
**Serviço Compose = `whatsapp-mcp`** (not `whatsapp-bridge`, not `web-ui` — this field is easy
to leave empty, which silently breaks routing; we hit exactly that on the previous
deployment), port **8081**, path `/mcp`.

**Why `MCP_TRUSTED_HOSTS` exists at all:** upstream's MCP server rejects any request whose
`Host` header isn't `localhost`/`127.0.0.1` — a DNS-rebinding protection that only
auto-configures for loopback deployments, with no built-in way to allowlist a real domain.
Fixed in this fork (`whatsapp-mcp-server/main.py`) by reading `MCP_TRUSTED_HOSTS`. Without it
set to the right value, every request 421s, tunnel and Access notwithstanding.

Do **not** add a public domain for `whatsapp-bridge` (8080) or `web-ui` (8090) — the bridge's
REST API and the web UI have no auth of their own beyond the internal `API_KEY`, and pairing
is done via SSH instead (see below), so there's no reason to expose either publicly.

### 3. Cloudflare Access (mandatory — the MCP endpoint has no auth of its own)
Same as before: Zero Trust dashboard → Access → Applications → Self-hosted, pointed at the
domain from step 2. One policy for your own login, one Service Auth policy with a Service
Token for automated MCP clients (Claude Code).

**Do not skip this.** `API_KEY` in this compose only guards the internal bridge↔MCP-server
channel — it does not require anything from a client hitting the public `/mcp` endpoint
directly. Confirmed by reading `whatsapp-mcp-server/main.py`: no auth middleware on the
inbound HTTP side.

## Pairing (via SSH, no QR image needed)

SSH into the server, then either use the Makefile (from the repo checkout path EasyPanel used,
usually `/etc/easypanel/projects/<project>/<service>/code`) or the bridge's REST API directly:

```bash
# Using the Makefile (reads API_KEY from .env in that directory):
make pair PHONE=+555193272563

# Or directly (BRIDGE is the bridge container's internal port — SSH doesn't reach it
# externally since it's not published; exec into a container on the same Docker network,
# or docker exec into the bridge container itself and curl its own localhost:8080):
docker exec whatsapp-bridge wget -qO- --header="X-API-Key: $API_KEY" \
  --post-data='{"phone":"+555193272563"}' \
  http://localhost:8080/api/pair
```

This returns an 8-digit code — enter it in **WhatsApp → Settings → Linked Devices → Link a
Device → Link with phone number instead**. Check `make pairing-status` (or
`GET /api/pairing` with the same header) to confirm it connected.

If phone-code pairing fails, `web-ui` (internal-only, not published) can be reached
temporarily for QR pairing by exec'ing into its container or SSH port-forwarding — ask if
this is needed, it's not the default path.

## Notes
- No `ports:` mappings anywhere in this compose — EasyPanel's automatic tunnel routing
  (Serviço Compose selection) plus the separate `cloudflare-tunnel` container (already
  attached to the `easypanel` network from the previous deployment) are what reach it.
- Session store is a named volume (`whatsapp-store`) shared between `whatsapp-bridge` and
  `whatsapp-mcp` — losing it means re-pairing the device.
- `PRESENCE_PING_ENABLED`/`PRESENCE_PING_INTERVAL` env vars exist upstream for anti-
  fingerprinting; not set here (defaults apply — enabled, 20 minutes). Not part of our
  enabled toolset anyway (`presence` toolset is disabled), this only affects the bridge's own
  background behavior, not a tool Claude Code can trigger.
