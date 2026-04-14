# Hermes Telegram Mini App

A sleek, terminal-style web interface for your Hermes agent that runs inside Telegram as a Mini App. Chat with your agent, manage cron jobs, and monitor system health — all from a dark-mode TUI that feels like home.

## What you get

- **Terminal chat** — streaming responses, slash commands, file attachments (images, PDFs, text)
- **Context bar** — live model name, token usage bar, session duration (like the Hermes CLI)
- **Status tab** — CPU/mem/disk gauges, process list, quick actions
- **Cron tab** — create, edit, delete, pause, and trigger scheduled jobs
- **Agent spawning** — spawn independent Hermes instances in the background, monitor live output, send follow-up messages (interactive or one-shot mode, max 5 concurrent)
- **File attachments** — attach images, PDFs, CSVs; agent uses vision_analyze or OCR automatically
- **Local vision & OCR** — optional local LLM servers for private image analysis and document OCR
- **Rock-solid auth** — dual HMAC-SHA256 + Ed25519 validation (Telegram's recommended method + third-party fallback)
- **Security hardened** — CSP headers, XSS sanitization, auth rate limiting, SRI, CSPRNG session IDs (see [Security](#security))

## Prerequisites

Before you start, you'll need:

1. **Hermes Agent** installed and working (`hermes` CLI runs successfully)
   - [Hermes Agent on GitHub](https://github.com/NousResearch/hermes-agent)
   - Version 0.9.0 or later
2. **A Telegram bot** — created via [@BotFather](https://t.me/BotFather)
3. **Your Telegram user ID** — a number, not your username. Get it from [@userinfobot](https://t.me/userinfobot)
4. **A publicly accessible URL** — either a Cloudflare tunnel, ngrok, or your own domain with SSL
5. **Python `cryptography` package** — for Ed25519 signature validation
   ```bash
   pip install cryptography
   ```

## Setup

### Step 1: Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram
2. Send `/newbot`
3. Pick a name (e.g. "My Hermes Agent")
4. Pick a username ending in `bot` (e.g. `my_hermes_agent_bot`)
5. **Save the bot token** — you'll need it. It looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

### Step 2: Get your Telegram user ID

1. Open [@userinfobot](https://t.me/userinfobot)
2. Send `/start`
3. It replies with your numeric ID (e.g. `9876543210`)
4. **Save this number**

### Step 3: Clone and build

```bash
# Clone the miniapp repo
git clone https://github.com/clawvader-tech/hermes-telegram-miniapp.git
cd hermes-telegram-miniapp

# Build the frontend
cd web && npm install && npm run build && cd ..
```

### Step 4: Deploy to your Hermes installation

```bash
# Deploy and install the auto-update hook (first time)
./deploy.sh --install-hook
```

This does three things:
1. **Deploys** `web_server.py` and `web_dist/` to your hermes-agent installation (with backup)
2. **Installs a post-merge git hook** that auto-redeploys the mini app after every `hermes update`
3. **Marks `web_server.py` as assume-unchanged** so `git status` stays clean

**Why the hook?** `hermes update` pulls from the upstream NousResearch repo, which has its own `web_server.py`. Without the hook, upstream changes silently replace the mini app's custom Telegram auth. The hook detects the update and re-deploys automatically.

If you prefer manual control (no hook):
```bash
./deploy.sh                           # Deploy with backup
./deploy.sh --no-backup               # Deploy without backup
```

**Custom target directory:**
```bash
HERMES_AGENT_DIR=/path/to/hermes-agent ./deploy.sh --install-hook
```

### Step 5: Configure environment variables

Add these to `~/.hermes/.env` (create it if it doesn't exist):

```bash
# Required
TELEGRAM_BOT_TOKEN=123456...wxyz
TELEGRAM_OWNER_ID=9876543210

# Generate a random API key for Bearer auth fallback:
# python3 -c "import secrets; print(secrets.token_urlsafe(32))"
API_SERVER_KEY=your_generated_key_here
```

If you're using systemd to run the gateway, also add these to your service file. See `systemd/hermes-gateway.service` for a template.

### Step 6: Expose the gateway to the internet

The mini app needs to be accessible from Telegram's servers. The Hermes gateway runs on port 9119 by default.

**Option A: Cloudflare Quick Tunnel (fastest, but URL changes on restart)**

```bash
cloudflared tunnel --url http://localhost:9119
```

This gives you a URL like `https://random-words.trycloudflare.com`. It works, but the URL changes every time you restart. Fine for testing.

**Option B: Named Cloudflare Tunnel (recommended for production)**

```bash
# Login to Cloudflare
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create hermes

# Route your domain to it
cloudflared tunnel route dns hermes miniapp.yourdomain.com

# Run the tunnel
cloudflared tunnel run hermes
```

See `tunnel/cloudflared-config.yml` for a sample config. Save it as `~/.cloudflared/config.yml`.

**Option C: Any other reverse proxy**

Just forward HTTPS traffic to `localhost:9119`. You need a valid SSL certificate — Telegram requires HTTPS.

### Step 7: Set the bot's Mini App URL

1. Go back to [@BotFather](https://t.me/BotFather)
2. Send `/setmenubutton`
3. Pick your bot
4. Send the URL: `https://your-tunnel-url/`

This adds a "menu" button in the chat that opens the mini app. Users tap it to launch the interface.

### Step 8: Start the server

```bash
cd ~/.hermes/hermes-agent && source venv/bin/activate
nohup python -B -c "from hermes_cli.web_server import start_server; start_server('127.0.0.1', 9119, False)" > /tmp/hermes-dashboard.log 2>&1 &

# Verify
curl -s http://localhost:9119/api/status
```

### Step 9: Open it

1. Open your bot in Telegram
2. Tap the menu button (left of the text input)
3. The mini app opens — you should see "Hermes Agent" with the context bar

If it asks for an API key, that means Telegram initData isn't reaching the server. See troubleshooting below.

## How auth works

The mini app uses **dual validation**: HMAC-SHA256 (primary) + Ed25519 (fallback). Here's the flow:

```
Telegram Client                    Your Server
     │                                │
     │  1. User opens mini app        │
     │  Telegram generates initData   │
     │  (contains hash + signature)   │
     │                                │
     │  2. Mini app sends initData ──>│
     │     via X-Telegram-Init-Data   │
     │                                │
     │                         3. Try HMAC-SHA256 (primary)
     │                            secret = HMAC(key="WebAppData", msg=bot_token)
     │                            verify hash field
     │                                │
     │                         4. If HMAC fails, try Ed25519 (fallback)
     │                            verify signature field with Telegram public key
     │                                │
     │                         5. Check user ID matches
     │                            TELEGRAM_OWNER_ID
     │                                │
     │  <── 6. Authenticated ──────── │
     │                                │
```

**HMAC-SHA256** (primary) uses the bot token to derive a secret key. Per [Telegram docs](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app): `secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)`.

**Ed25519** (fallback) uses Telegram's published public key — no bot token needed for verification. Useful for third-party validation.

If initData isn't available (e.g. you're testing in a regular browser), the server falls back to Bearer token auth using `API_SERVER_KEY`.

## API endpoints used by the mini app

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /api/status` | None | Server status, gateway state, platform connections |
| `GET /api/health` | None | System health (CPU, memory, uptime) |
| `GET /api/auth/session-token` | Telegram auth or localhost | Ephemeral session token for write ops |
| `GET /api/model-info` | Yes | Active model name, provider, context length |
| `GET /api/session-usage` | Yes | Cumulative token usage for session |
| `GET /api/sessions` | Yes | Paginated session list |
| `GET /api/sessions/{id}/messages` | Yes | Session messages |
| `DELETE /api/sessions/{id}` | Yes | Delete a session |
| `GET /api/cron/jobs` | Yes | List cron jobs |
| `POST /api/cron/jobs` | Yes | Create a new cron job |
| `POST /api/cron/jobs/{id}/pause` | Yes | Pause a cron job |
| `POST /api/cron/jobs/{id}/resume` | Yes | Resume a paused cron job |
| `POST /api/cron/jobs/{id}/trigger` | Yes | Trigger immediate execution |
| `DELETE /api/cron/jobs/{id}` | Yes | Delete a cron job |
| `POST /api/command` | Yes | Execute a slash command |
| `POST /v1/chat/completions` | Yes | Streaming chat (SSE), supports multimodal content |
| `GET /api/agents` | Yes | List spawned agents with live status |
| `POST /api/agents` | Yes | Spawn a new agent (interactive or one-shot) |
| `GET /api/agents/{name}` | Yes | Agent details + tmux output |
| `DELETE /api/agents/{name}` | Yes | Kill agent and remove from registry |
| `POST /api/agents/{name}/message` | Yes | Send message to agent's tmux session |

## Troubleshooting

### "Error 401" when sending a message

This means Telegram initData validation is failing. Check:

1. **Is `TELEGRAM_BOT_TOKEN` set correctly?** It's needed for HMAC-SHA256 validation (primary method) and bot ID extraction (Ed25519 fallback). Verify with: `curl https://api.telegram.org/bot<TOKEN>/getMe`
2. **Are you opening the mini app from Telegram?** initData is only generated inside Telegram's built-in browser. If you're opening the URL in Chrome/Safari directly, there's no initData.
3. **Is `TELEGRAM_OWNER_ID` your numeric ID?** Not your username — a number like `9876543210`.
4. **HMAC argument order correct?** The server code must use `hmac.new(b"WebAppData", bot_token, sha256)` — NOT `hmac.new(bot_token, b"WebAppData", sha256)`. The Telegram docs use non-standard `HMAC_SHA256(msg, key)` notation which is easy to misread. See [this skill](https://github.com/clawvader-tech/hermes-telegram-miniapp/tree/main/.hermes/skills/hermes-telegram-miniapp/SKILL.md) for details.

### "Invalid API key" on the cron/status tab

The cron tab uses Bearer token auth as a fallback. If you see this:

1. Check that `API_SERVER_KEY` is set in your environment
2. Make sure it matches what the mini app has stored (it auto-saves after first successful auth)
3. Try clearing the mini app's local storage: open in Telegram → ... → Clear storage

### Mini app loads but feels choppy

The keyboard animation uses `visualViewport` events for smooth transitions. This works in Telegram's built-in browser on iOS and Android. If you're testing in a desktop browser, the visual behavior may differ.

### initData keeps expiring

Telegram generates initData once when the mini app opens. It's valid for 24 hours. If you leave the app open overnight, you'll need to close and reopen it to get fresh initData.

### Cloudflare tunnel URL changed

Free `cloudflared tunnel --url` tunnels get a random URL each restart. For a stable URL, set up a named tunnel with your own domain (see Step 6, Option B).

### Mini app broke after `hermes update`

If you run `hermes update` and the mini app stops working (401 errors, missing features):

1. This happens because the upstream `hermes update` replaces `web_server.py` with the stock version (no Telegram auth)
2. **Fix:** Redeploy and install the hook: `./deploy.sh --install-hook`
3. The post-merge hook prevents this — it auto-redeploys after every `hermes update`
4. If the hook is already installed but didn't fire, check: `cat ~/.hermes/hermes-agent/.git/hooks/post-merge`

### Upstream git pull overwrote custom files

This should not happen if the post-merge hook is installed. If it does:

1. Redeploy: `cd ~/projects/telegram-miniapp-v2 && ./deploy.sh`
2. Reinstall the hook: `./deploy.sh --install-hook`
3. Restart the server after redeploying

## Architecture

```
Telegram Client
    │
    ├── Mini App (React SPA — Vite + TypeScript + Tailwind)
    │   ├─ Sends initData via X-Telegram-Init-Data header
    │   ├─ Falls back to Bearer token for non-Telegram browsers
    │   └─ Built SPA served from hermes_cli/web_dist/
    │
    ▼
Cloudflare Tunnel (or any HTTPS reverse proxy)
    │
    ▼
FastAPI Web Server (port 9119)
    ├─ Dual auth: HMAC-SHA256 (primary) + Ed25519 (fallback)
    ├─ Owner-only access control
    ├─ Serves mini app static files from hermes_cli/web_dist/
    ├─ Multimodal chat (images, PDFs, text files)
    ├─ Attachment handling: saves to /tmp, injects tool hints
    ├─ Agent spawning: tmux-backed independent Hermes instances
    │   ├─ Interactive mode (full session, send follow-ups)
    │   ├─ One-shot mode (hermes chat -q, auto-detects completion)
    │   ├─ Worktree mode (-w) for parallel code work without conflicts
    │   └─ Max 5 concurrent, auto-cleanup after 1 hour
    └─ SSE streaming for chat responses

Standalone Project Repo
    ├─ Source: ~/projects/telegram-miniapp-v2/
    ├─ Deploy: ./deploy.sh → copies to hermes-agent installation
    └─ Protected: assume-unchanged flag prevents git pull overwrites

Optional Local Models (CPU)
    ├─ LFM2-VL-450M (port 8080) — image description & analysis
    └─ GLM-OCR (port 8081) — OCR, tables, formulas, structured extraction
```

## Security

v2.0.1 addresses a critical HMAC validation bug. v2.0.0/v1.0.3 addressed 11 vulnerabilities from a full security audit. Here's what's protected:

| Layer | Protection |
|-------|-----------|
| **Auth validation** | Dual HMAC-SHA256 + Ed25519 initData validation with correct key/message argument order |
| **XSS** | All user-generated content (markdown, URLs, image sources, command names) sanitized via `esc()` before rendering. Only `http://`, `https://`, `mailto:` URL schemes allowed in links |
| **CSP** | Strict Content-Security-Policy via `<meta>` tag — blocks inline eval, external scripts (except Telegram SDK), unauthorized connections, and all framing (`frame-ancestors 'none'`) |
| **Auth brute-force** | Per-IP rate limiter: 10 failed auth attempts per 60s triggers a 15-minute lockout (HTTP 429). Tracks failures across all authenticated endpoints |
| **Token replay** | initData freshness reduced from 24h to 5 min, limiting replay window even if intercepted |
| **Credential storage** | Bearer tokens stored in `sessionStorage` (clears on tab close), not `localStorage`. Telegram context uses native `CloudStorage` |
| **Session IDs** | Generated with `crypto.randomUUID()` (CSPRNG), not `Math.random()` |
| **Error disclosure** | Auth errors return generic messages; exception details logged server-side only |
| **CDN integrity** | Telegram SDK loaded with Subresource Integrity (`integrity` + `crossorigin="anonymous"`) |

### Reporting

Found a vulnerability? Please disclose responsibly by opening a private issue or contacting the maintainer directly.

## Contributing

Found a bug? Have an idea? Contributions are welcome.

1. Fork the repo
2. Make your changes (frontend in `web/`, backend in `hermes_cli/web_server.py`)
3. Build the frontend: `cd web && npm run build`
4. Open a PR

## License

MIT — see [LICENSE](LICENSE).
