#!/usr/bin/env bash
# deploy.sh — Deploy custom Telegram Mini App to hermes-agent installation.
# Copies web_server.py and web_dist/ from the standalone repo into the
# hermes-agent installation directory. Backs up existing files first.
#
# Usage: ./deploy.sh [--no-backup]
#
# Source:  /home/adam/projects/telegram-miniapp-v2/
# Target:  /home/adam/.hermes/hermes-agent/

set -euo pipefail

SOURCE_DIR="/home/adam/projects/telegram-miniapp-v2"
TARGET_DIR="/home/adam/.hermes/hermes-agent"
NO_BACKUP=false

[[ "${1:-}" == "--no-backup" ]] && NO_BACKUP=true

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[deploy]${NC} $*"; }
die()  { echo -e "${RED}[deploy]${NC} $*" >&2; exit 1; }

# --- Pre-checks ---
[[ -d "$SOURCE_DIR" ]] || die "Source dir not found: $SOURCE_DIR"
[[ -d "$TARGET_DIR" ]] || die "Target dir not found: $TARGET_DIR"
[[ -f "$SOURCE_DIR/hermes_cli/web_server.py" ]] || die "web_server.py not in source"
[[ -d "$SOURCE_DIR/hermes_cli/web_dist" ]] || die "web_dist/ not built yet — run 'cd web && npm run build' first"

# Check that the built frontend contains Telegram auth (not upstream)
if ! grep -q "X-Telegram-Init-Data" "$SOURCE_DIR/hermes_cli/web_dist/assets/"*.js 2>/dev/null; then
    die "Built frontend does not contain Telegram auth — rebuild with 'cd web && npm run build'"
fi

# Check that web_server.py has the CORRECT HMAC formula (not the swapped one)
if grep -q 'hmac.new(_TG_BOT_TOKEN.encode(), "WebAppData".encode()' "$SOURCE_DIR/hermes_cli/web_server.py" 2>/dev/null; then
    die "web_server.py still has the BUGGY HMAC formula (swapped args). Fix before deploying."
fi

# --- Backup ---
BACKUP_DIR=""
if [[ "$NO_BACKUP" == "false" ]]; then
    TIMESTAMP=$(date +%Y%m%d-%H%M%S)
    BACKUP_DIR="$TARGET_DIR/hermes_cli/backups/deploy-$TIMESTAMP"
    mkdir -p "$BACKUP_DIR"
    
    # Backup current files
    if [[ -f "$TARGET_DIR/hermes_cli/web_server.py" ]]; then
        cp -p "$TARGET_DIR/hermes_cli/web_server.py" "$BACKUP_DIR/"
        log "Backed up web_server.py → $BACKUP_DIR/"
    fi
    if [[ -d "$TARGET_DIR/hermes_cli/web_dist" ]]; then
        cp -rp "$TARGET_DIR/hermes_cli/web_dist" "$BACKUP_DIR/"
        log "Backed up web_dist/ → $BACKUP_DIR/"
    fi
fi

# --- Deploy ---
log "Deploying web_server.py..."
cp -p "$SOURCE_DIR/hermes_cli/web_server.py" "$TARGET_DIR/hermes_cli/web_server.py"

log "Deploying web_dist/..."
rm -rf "$TARGET_DIR/hermes_cli/web_dist"
cp -rp "$SOURCE_DIR/hermes_cli/web_dist" "$TARGET_DIR/hermes_cli/web_dist/"

# --- Verify ---
log "Verifying deployment..."
COMPILES=$(python3 -c "import py_compile; py_compile.compile('$TARGET_DIR/hermes_cli/web_server.py', doraise=True)" 2>&1 && echo "OK" || echo "FAIL")
if [[ "$COMPILES" == *"FAIL"* ]]; then
    die "Deployed web_server.py has syntax errors! Restoring backup..."
    [[ -n "$BACKUP_DIR" && -f "$BACKUP_DIR/web_server.py" ]] && cp -p "$BACKUP_DIR/web_server.py" "$TARGET_DIR/hermes_cli/web_server.py"
    exit 1
fi

HAS_TG_AUTH=$(grep -c "X-Telegram-Init-Data" "$TARGET_DIR/hermes_cli/web_dist/assets/"*.js 2>/dev/null || echo "0")
if [[ "$HAS_TG_AUTH" == "0" ]]; then
    die "Deployed frontend is missing Telegram auth headers!"
fi

log "Deployment complete."
echo ""
warn "NOTE: You must restart the web server for changes to take effect."
warn "  kill \$(pgrep -f 'web_server.*start_server.*9119')"
warn "  cd $TARGET_DIR && source venv/bin/activate"
warn "  nohup python -B -c \"from hermes_cli.web_server import start_server; start_server('127.0.0.1', 9119, False)\" > /tmp/hermes-dashboard.log 2>&1 &"
