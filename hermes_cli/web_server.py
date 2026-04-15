"""
Hermes Agent — Web UI server.

Provides a FastAPI backend serving the Vite/React frontend and REST API
endpoints for managing configuration, environment variables, and sessions.

Usage:
    python -m hermes_cli.main web          # Start on http://127.0.0.1:9119
    python -m hermes_cli.main web --port 8080
"""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli import __version__, __release_date__
from hermes_cli.config import (
    DEFAULT_CONFIG,
    OPTIONAL_ENV_VARS,
    get_config_path,
    get_env_path,
    get_hermes_home,
    load_config,
    load_env,
    save_config,
    save_env_value,
    remove_env_value,
    check_config_version,
    redact_key,
)
from gateway.status import get_running_pid, read_runtime_status

try:
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError:
    raise SystemExit(
        "Web UI requires fastapi and uvicorn.\n"
        "Run 'hermes web' to auto-install, or: pip install hermes-agent[web]"
    )

WEB_DIST = Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)
_log.setLevel(logging.DEBUG)

app = FastAPI(title="Hermes Agent", version=__version__)

# ---------------------------------------------------------------------------
# Session token for protecting sensitive endpoints (reveal).
# Generated fresh on every server start — dies when the process exits.
# Injected into the SPA HTML so only the legitimate web UI can use it.
# ---------------------------------------------------------------------------
_SESSION_TOKEN = secrets.token_urlsafe(32)

# Simple rate limiter for the reveal endpoint
_reveal_timestamps: List[float] = []
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30

# Request size limit (1 MB)
_MAX_REQUEST_SIZE = 1024 * 1024

# ---------------------------------------------------------------------------
# Auth dependency for write/destructive endpoints.
# Uses lazy import of _validate_telegram_init_data to avoid forward-reference
# issues — the function is defined later in this module but called at runtime.
# ---------------------------------------------------------------------------
async def _require_auth(request: Request):
    """FastAPI dependency: reject unauthenticated write requests.

    Checks (in order):
      1. Bearer token matching _SESSION_TOKEN (ephemeral, per-process)
      2. Bearer token matching API_SERVER_KEY (from .env)
      3. Valid Telegram initData (Ed25519 signature)
    """
    auth = request.headers.get("authorization", "")
    # 1. Ephemeral session token (SPA sends this after loading)
    if auth == f"Bearer {_SESSION_TOKEN}":
        return
    # 2. API server key (from .env, used by CLI/cron)
    api_key = os.getenv("API_SERVER_KEY", "")
    if api_key and auth == f"Bearer {api_key}":
        return
    # 3. Telegram Ed25519 initData (resolved at call time to avoid forward ref)
    tg_init = request.headers.get("x-telegram-init-data", "").strip()
    if tg_init:
        _validate = globals().get("_validate_telegram_init_data")
        if _validate and _validate(tg_init) is not None:
            return
    raise HTTPException(status_code=401, detail="Authentication required")


async def _require_auth_local_ok(request: Request):
    """Like _require_auth but also allows true localhost (no forwarded-for)."""
    client_host = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if client_host in ("127.0.0.1", "::1", "localhost") and not forwarded_for:
        return
    return await _require_auth(request)

# CORS: restrict to known origins (localhost for dev, Telegram webview, tunnel).
# Sensitive endpoints (reveal, OAuth) are additionally protected by the
# ephemeral session token.

# Load .env before reading module-level config — standalone web_server
# invocation (python -B -c "from hermes_cli.web_server import ...") doesn't
# auto-load .env, so we do it here so auth middleware has the keys it needs.
for _k, _v in load_env().items():
    os.environ.setdefault(_k, _v)

_TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "") or os.getenv("TELEGRAM_ALLOWED_USERS", "")
_GATEWAY_PORT = int(os.getenv("HERMES_GATEWAY_PORT", "8642"))

# Prompt blacklist for agent spawning (prevents shell injection)
_PROMPT_BLACKLIST = re.compile(r'[;&|`$]')

_CORS_ORIGINS = [
    "http://localhost:9119",
    "http://127.0.0.1:9119",
    "https://web.telegram.org",
]
# Allow additional origins via env var (comma-separated)
_EXTRA_ORIGINS = os.getenv("HERMES_CORS_ORIGINS", "").split(",")
_CORS_ORIGINS.extend([o.strip() for o in _EXTRA_ORIGINS if o.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Telegram-Init-Data", "X-Hermes-Session-Id"],
)


# ---------------------------------------------------------------------------
# Config schema — auto-generated from DEFAULT_CONFIG
# ---------------------------------------------------------------------------

# Manual overrides for fields that need select options or custom types
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "singularity"],
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "neutts"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        "options": ["local", "openai", "mistral"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["queue", "interrupt", "block"],
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": ["builtin", "honcho"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["ask", "yolo", "deny"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "low", "medium", "high"],
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "smart_model_routing": "agent",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict."""
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in ("_config_version",):
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            # Recurse into nested dicts
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            # Apply manual overrides
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            # Merge small categories
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


CONFIG_SCHEMA = _build_schema_from_config(DEFAULT_CONFIG)


class ConfigUpdate(BaseModel):
    config: dict


class EnvVarUpdate(BaseModel):
    key: str
    value: str


class EnvVarDelete(BaseModel):
    key: str


class EnvVarReveal(BaseModel):
    key: str


@app.get("/api/status")
async def get_status():
    current_ver, latest_ver = check_config_version()

    gateway_pid = get_running_pid()
    gateway_running = gateway_pid is not None

    gateway_state = None
    gateway_platforms: dict = {}
    gateway_exit_reason = None
    gateway_updated_at = None
    configured_gateway_platforms: set[str] | None = None
    try:
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
        configured_gateway_platforms = {
            platform.value for platform in gateway_config.get_connected_platforms()
        }
    except Exception:
        configured_gateway_platforms = None

    runtime = read_runtime_status()
    if runtime:
        gateway_state = runtime.get("gateway_state")
        gateway_platforms = runtime.get("platforms") or {}
        if configured_gateway_platforms is not None:
            gateway_platforms = {
                key: value
                for key, value in gateway_platforms.items()
                if key in configured_gateway_platforms
            }
        gateway_exit_reason = runtime.get("exit_reason")
        gateway_updated_at = runtime.get("updated_at")
        if not gateway_running:
            gateway_state = gateway_state if gateway_state in ("stopped", "startup_failed") else "stopped"
            gateway_platforms = {}

    active_sessions = 0
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=50)
            now = time.time()
            active_sessions = sum(
                1 for s in sessions
                if s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0))) < 300
            )
        finally:
            db.close()
    except Exception:
        pass

    return {
        "version": __version__,
        "release_date": __release_date__,
        "hermes_home": str(get_hermes_home()),
        "config_path": str(get_config_path()),
        "env_path": str(get_env_path()),
        "config_version": current_ver,
        "latest_config_version": latest_ver,
        "gateway_running": gateway_running,
        "gateway_pid": gateway_pid,
        "gateway_state": gateway_state,
        "gateway_platforms": gateway_platforms,
        "gateway_exit_reason": gateway_exit_reason,
        "gateway_updated_at": gateway_updated_at,
        "active_sessions": active_sessions,
    }


@app.get("/api/sessions")
async def get_sessions(limit: int = 20, offset: int = 0):
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=limit, offset=offset)
            total = db.session_count()
            now = time.time()
            for s in sessions:
                s["is_active"] = (
                    s.get("ended_at") is None
                    and (now - s.get("last_active", s.get("started_at", 0))) < 300
                )
            return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}
        finally:
            db.close()
    except Exception as e:
        _log.exception("GET /api/sessions failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 20):
    """Full-text search across session message content using FTS5."""
    if not q or not q.strip():
        return {"results": []}
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            # Auto-add prefix wildcards so partial words match
            # e.g. "nimb" → "nimb*" matches "nimby"
            # Preserve quoted phrases and existing wildcards as-is
            import re
            terms = []
            for token in re.findall(r'"[^"]*"|\S+', q.strip()):
                if token.startswith('"') or token.endswith("*"):
                    terms.append(token)
                else:
                    terms.append(token + "*")
            prefix_query = " ".join(terms)
            matches = db.search_messages(query=prefix_query, limit=limit)
            # Group by session_id — return unique sessions with their best snippet
            seen: dict = {}
            for m in matches:
                sid = m["session_id"]
                if sid not in seen:
                    seen[sid] = {
                        "session_id": sid,
                        "snippet": m.get("snippet", ""),
                        "role": m.get("role"),
                        "source": m.get("source"),
                        "model": m.get("model"),
                        "session_started": m.get("session_started"),
                    }
            return {"results": list(seen.values())}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions/search failed")
        raise HTTPException(status_code=500, detail="Search failed")


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize config for the web UI.

    Hermes supports ``model`` as either a bare string (``"anthropic/claude-sonnet-4"``)
    or a dict (``{default: ..., provider: ..., base_url: ...}``).  The schema is built
    from DEFAULT_CONFIG where ``model`` is a string, but user configs often have the
    dict form.  Normalize to the string form so the frontend schema matches.
    """
    config = dict(config)  # shallow copy
    model_val = config.get("model")
    if isinstance(model_val, dict):
        config["model"] = model_val.get("default", model_val.get("name", ""))
    return config


@app.get("/api/config")
async def get_config():
    config = _normalize_config_for_web(load_config())
    # Strip internal keys that the frontend shouldn't see or send back
    return {k: v for k, v in config.items() if not k.startswith("_")}


@app.get("/api/config/defaults")
async def get_defaults():
    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_schema():
    return {"fields": CONFIG_SCHEMA, "category_order": _CATEGORY_ORDER}


def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving.

    Reconstructs ``model`` as a dict by reading the current on-disk config
    to recover model subkeys (provider, base_url, api_mode, etc.) that were
    stripped from the GET response.  The frontend only sees model as a flat
    string; the rest is preserved transparently.
    """
    config = dict(config)
    # Remove any _model_meta that might have leaked in (shouldn't happen
    # with the stripped GET response, but be defensive)
    config.pop("_model_meta", None)

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        # Read the current disk config to recover model subkeys
        try:
            disk_config = load_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                # Preserve all subkeys, update default with the new value
                disk_model["default"] = model_val
                config["model"] = disk_model
        except Exception:
            pass  # can't read disk config — just use the string form
    return config


@app.put("/api/config")
async def update_config(body: ConfigUpdate, _: None = Depends(_require_auth)):
    try:
        save_config(_denormalize_config_from_web(body.config))
        return {"ok": True}
    except Exception as e:
        _log.exception("PUT /api/config failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/auth/session-token")
async def get_session_token(request: Request):
    """Return the ephemeral session token for this server instance.

    The token protects sensitive endpoints (reveal).  It's served to the SPA
    which stores it in memory — it's never persisted and dies when the server
    process exits.

    Accessible from:
      - True localhost (no forwarded-for header)
      - Valid Telegram initData (Ed25519 signature verified)
    """
    client_host = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    is_local = client_host in ("127.0.0.1", "::1", "localhost") and not forwarded_for
    if not is_local:
        # Check Telegram Ed25519 initData
        tg_init = request.headers.get("x-telegram-init-data", "").strip()
        if not tg_init:
            raise HTTPException(status_code=403, detail="Session token requires localhost or Telegram auth")
        _validate = globals().get("_validate_telegram_init_data")
        if not _validate or _validate(tg_init) is None:
            raise HTTPException(status_code=403, detail="Invalid Telegram auth")
    return {"token": _SESSION_TOKEN}


@app.get("/api/env")
async def get_env_vars():
    env_on_disk = load_env()
    result = {}
    for var_name, info in OPTIONAL_ENV_VARS.items():
        value = env_on_disk.get(var_name)
        result[var_name] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description", ""),
            "url": info.get("url"),
            "category": info.get("category", ""),
            "is_password": info.get("password", False),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", False),
        }
    return result


@app.put("/api/env")
async def set_env_var(body: EnvVarUpdate, _: None = Depends(_require_auth)):
    try:
        save_env_value(body.key, body.value)
        return {"ok": True, "key": body.key}
    except Exception as e:
        _log.exception("PUT /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/env")
async def remove_env_var(body: EnvVarDelete, _: None = Depends(_require_auth)):
    try:
        removed = remove_env_value(body.key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")
        return {"ok": True, "key": body.key}
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("DELETE /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env/reveal")
async def reveal_env_var(body: EnvVarReveal, request: Request):
    """Return the real (unredacted) value of a single env var.

    Protected by:
    - Ephemeral session token (generated per server start, injected into SPA)
    - Rate limiting (max 5 reveals per 30s window)
    - Audit logging
    """
    # --- Token check ---
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {_SESSION_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    # --- Rate limit ---
    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _reveal_timestamps[:] = [t for t in _reveal_timestamps if t > cutoff]
    if len(_reveal_timestamps) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many reveal requests. Try again shortly.")
    _reveal_timestamps.append(now)

    # --- Reveal ---
    env_on_disk = load_env()
    value = env_on_disk.get(body.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")

    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# ---------------------------------------------------------------------------
# OAuth provider endpoints — status + disconnect (Phase 1)
# ---------------------------------------------------------------------------
#
# Phase 1 surfaces *which OAuth providers exist* and whether each is
# connected, plus a disconnect button. The actual login flow (PKCE for
# Anthropic, device-code for Nous/Codex) still runs in the CLI for now;
# Phase 2 will add in-browser flows. For unconnected providers we return
# the canonical ``hermes auth add <provider>`` command so the dashboard
# can surface a one-click copy.


def _truncate_token(value: Optional[str], visible: int = 6) -> str:
    """Return ``...XXXXXX`` (last N chars) for safe display in the UI.

    We never expose more than the trailing ``visible`` characters of an
    OAuth access token. JWT prefixes (the part before the first dot) are
    stripped first when present so the visible suffix is always part of
    the signing region rather than a meaningless header chunk.
    """
    if not value:
        return ""
    s = str(value)
    if "." in s and s.count(".") >= 2:
        # Looks like a JWT — show the trailing piece of the signature only.
        s = s.rsplit(".", 1)[-1]
    if len(s) <= visible:
        return s
    return f"…{s[-visible:]}"


def _anthropic_oauth_status() -> Dict[str, Any]:
    """Combined status across the three Anthropic credential sources we read.

    Hermes resolves Anthropic creds in this order at runtime:
    1. ``~/.hermes/.anthropic_oauth.json`` — Hermes-managed PKCE flow
    2. ``~/.claude/.credentials.json`` — Claude Code CLI credentials (auto)
    3. ``ANTHROPIC_TOKEN`` / ``ANTHROPIC_API_KEY`` env vars
    The dashboard reports the highest-priority source that's actually present.
    """
    try:
        from agent.anthropic_adapter import (
            read_hermes_oauth_credentials,
            read_claude_code_credentials,
            _HERMES_OAUTH_FILE,
        )
    except ImportError:
        read_claude_code_credentials = None  # type: ignore
        read_hermes_oauth_credentials = None  # type: ignore
        _HERMES_OAUTH_FILE = None  # type: ignore

    hermes_creds = None
    if read_hermes_oauth_credentials:
        try:
            hermes_creds = read_hermes_oauth_credentials()
        except Exception:
            hermes_creds = None
    if hermes_creds and hermes_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "hermes_pkce",
            "source_label": f"Hermes PKCE ({_HERMES_OAUTH_FILE})",
            "token_preview": _truncate_token(hermes_creds.get("accessToken")),
            "expires_at": hermes_creds.get("expiresAt"),
            "has_refresh_token": bool(hermes_creds.get("refreshToken")),
        }

    cc_creds = None
    if read_claude_code_credentials:
        try:
            cc_creds = read_claude_code_credentials()
        except Exception:
            cc_creds = None
    if cc_creds and cc_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code",
            "source_label": "Claude Code (~/.claude/.credentials.json)",
            "token_preview": _truncate_token(cc_creds.get("accessToken")),
            "expires_at": cc_creds.get("expiresAt"),
            "has_refresh_token": bool(cc_creds.get("refreshToken")),
        }

    env_token = os.getenv("ANTHROPIC_TOKEN") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return {
            "logged_in": True,
            "source": "env_var",
            "source_label": "ANTHROPIC_TOKEN environment variable",
            "token_preview": _truncate_token(env_token),
            "expires_at": None,
            "has_refresh_token": False,
        }
    return {"logged_in": False, "source": None}


def _claude_code_only_status() -> Dict[str, Any]:
    """Surface Claude Code CLI credentials as their own provider entry.

    Independent of the Anthropic entry above so users can see whether their
    Claude Code subscription tokens are actively flowing into Hermes even
    when they also have a separate Hermes-managed PKCE login.
    """
    try:
        from agent.anthropic_adapter import read_claude_code_credentials
        creds = read_claude_code_credentials()
    except Exception:
        creds = None
    if creds and creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code_cli",
            "source_label": "~/.claude/.credentials.json",
            "token_preview": _truncate_token(creds.get("accessToken")),
            "expires_at": creds.get("expiresAt"),
            "has_refresh_token": bool(creds.get("refreshToken")),
        }
    return {"logged_in": False, "source": None}


# Provider catalog. The order matters — it's how we render the UI list.
# ``cli_command`` is what the dashboard surfaces as the copy-to-clipboard
# fallback while Phase 2 (in-browser flows) isn't built yet.
# ``flow`` describes the OAuth shape so the future modal can pick the
# right UI: ``pkce`` = open URL + paste callback code, ``device_code`` =
# show code + verification URL + poll, ``external`` = read-only (delegated
# to a third-party CLI like Claude Code or Qwen).
_OAUTH_PROVIDER_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "anthropic",
        "name": "Anthropic (Claude API)",
        "flow": "pkce",
        "cli_command": "hermes auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
        "status_fn": _anthropic_oauth_status,
    },
    {
        "id": "claude-code",
        "name": "Claude Code (subscription)",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
        "status_fn": _claude_code_only_status,
    },
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "hermes auth add nous",
        "docs_url": "https://portal.nousresearch.com",
        "status_fn": None,  # dispatched via auth.get_nous_auth_status
    },
    {
        "id": "openai-codex",
        "name": "OpenAI Codex (ChatGPT)",
        "flow": "device_code",
        "cli_command": "hermes auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
        "status_fn": None,  # dispatched via auth.get_codex_auth_status
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "hermes auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
        "status_fn": None,  # dispatched via auth.get_qwen_auth_status
    },
)


def _resolve_provider_status(provider_id: str, status_fn) -> Dict[str, Any]:
    """Dispatch to the right status helper for an OAuth provider entry."""
    if status_fn is not None:
        try:
            return status_fn()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
    try:
        from hermes_cli import auth as hauth
        if provider_id == "nous":
            raw = hauth.get_nous_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "nous_portal",
                "source_label": raw.get("portal_base_url") or "Nous Portal",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "openai-codex":
            raw = hauth.get_codex_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "openai_codex",
                "source_label": raw.get("auth_mode") or "OpenAI Codex",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": False,
                "last_refresh": raw.get("last_refresh"),
            }
        if provider_id == "qwen-oauth":
            raw = hauth.get_qwen_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "qwen_cli",
                "source_label": raw.get("auth_store_path") or "Qwen CLI",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
    return {"logged_in": False}


@app.get("/api/providers/oauth")
async def list_oauth_providers():
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external"
        cli_command     fallback CLI command for users to run manually
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("hermes_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool
    """
    providers = []
    for p in _OAUTH_PROVIDER_CATALOG:
        status = _resolve_provider_status(p["id"], p.get("status_fn"))
        providers.append({
            "id": p["id"],
            "name": p["name"],
            "flow": p["flow"],
            "cli_command": p["cli_command"],
            "docs_url": p["docs_url"],
            "status": status,
        })
    return {"providers": providers}


@app.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(provider_id: str, _: None = Depends(_require_auth)):
    """Disconnect an OAuth provider. Requires auth."""

    valid_ids = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider_id}. "
                   f"Available: {', '.join(sorted(valid_ids))}",
        )

    # Anthropic and claude-code clear the same Hermes-managed PKCE file
    # AND forget the Claude Code import. We don't touch ~/.claude/* directly
    # — that's owned by the Claude Code CLI; users can re-auth there if they
    # want to undo a disconnect.
    if provider_id in ("anthropic", "claude-code"):
        try:
            from agent.anthropic_adapter import _HERMES_OAUTH_FILE
            if _HERMES_OAUTH_FILE.exists():
                _HERMES_OAUTH_FILE.unlink()
        except Exception:
            pass
        # Also clear the credential pool entry if present.
        try:
            from hermes_cli.auth import clear_provider_auth
            clear_provider_auth("anthropic")
        except Exception:
            pass
        _log.info("oauth/disconnect: %s", provider_id)
        return {"ok": True, "provider": provider_id}

    try:
        from hermes_cli.auth import clear_provider_auth
        cleared = clear_provider_auth(provider_id)
        _log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
        return {"ok": bool(cleared), "provider": provider_id}
    except Exception as e:
        _log.exception("disconnect %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# OAuth Phase 2 — in-browser PKCE & device-code flows
# ---------------------------------------------------------------------------
#
# Two flow shapes are supported:
#
#   PKCE (Anthropic):
#     1. POST /api/providers/oauth/anthropic/start
#          → server generates code_verifier + challenge, builds claude.ai
#            authorize URL, stashes verifier in _oauth_sessions[session_id]
#          → returns { session_id, flow: "pkce", auth_url }
#     2. UI opens auth_url in a new tab. User authorizes, copies code.
#     3. POST /api/providers/oauth/anthropic/submit { session_id, code }
#          → server exchanges (code + verifier) → tokens at console.anthropic.com
#          → persists to ~/.hermes/.anthropic_oauth.json AND credential pool
#          → returns { ok: true, status: "approved" }
#
#   Device code (Nous, OpenAI Codex):
#     1. POST /api/providers/oauth/{nous|openai-codex}/start
#          → server hits provider's device-auth endpoint
#          → gets { user_code, verification_url, device_code, interval, expires_in }
#          → spawns background poller thread that polls the token endpoint
#            every `interval` seconds until approved/expired
#          → stores poll status in _oauth_sessions[session_id]
#          → returns { session_id, flow: "device_code", user_code,
#                      verification_url, expires_in, poll_interval }
#     2. UI opens verification_url in a new tab and shows user_code.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          every 2s until status != "pending".
#     4. On "approved" the background thread has already saved creds; UI
#        refreshes the providers list.
#
# Sessions are kept in-memory only (single-process FastAPI) and time out
# after 15 minutes. A periodic cleanup runs on each /start call to GC
# expired sessions so the dict doesn't grow without bound.

_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_oauth_sessions: Dict[str, Dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()

# Import OAuth constants from canonical source instead of duplicating.
# Guarded so hermes web still starts if anthropic_adapter is unavailable;
# Phase 2 endpoints will return 501 in that case.
try:
    from agent.anthropic_adapter import (
        _OAUTH_CLIENT_ID as _ANTHROPIC_OAUTH_CLIENT_ID,
        _OAUTH_TOKEN_URL as _ANTHROPIC_OAUTH_TOKEN_URL,
        _OAUTH_REDIRECT_URI as _ANTHROPIC_OAUTH_REDIRECT_URI,
        _OAUTH_SCOPES as _ANTHROPIC_OAUTH_SCOPES,
        _generate_pkce as _generate_pkce_pair,
    )
    _ANTHROPIC_OAUTH_AVAILABLE = True
except ImportError:
    _ANTHROPIC_OAUTH_AVAILABLE = False
_ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"


def _gc_oauth_sessions() -> None:
    """Drop expired sessions. Called opportunistically on /start."""
    cutoff = time.time() - _OAUTH_SESSION_TTL_SECONDS
    with _oauth_sessions_lock:
        stale = [sid for sid, sess in _oauth_sessions.items() if sess["created_at"] < cutoff]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def _new_oauth_session(provider_id: str, flow: str) -> tuple[str, Dict[str, Any]]:
    """Create + register a new OAuth session, return (session_id, session_dict)."""
    sid = secrets.token_urlsafe(16)
    sess = {
        "session_id": sid,
        "provider": provider_id,
        "flow": flow,
        "created_at": time.time(),
        "status": "pending",  # pending | approved | denied | expired | error
        "error_message": None,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = sess
    return sid, sess


def _save_anthropic_oauth_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist Anthropic PKCE creds to both Hermes file AND credential pool.

    Mirrors what auth_commands.add_command does so the dashboard flow leaves
    the system in the same state as ``hermes auth add anthropic``.
    """
    from agent.anthropic_adapter import _HERMES_OAUTH_FILE
    payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    _HERMES_OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HERMES_OAUTH_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Best-effort credential-pool insert. Failure here doesn't invalidate
    # the file write — pool registration only matters for the rotation
    # strategy, not for runtime credential resolution.
    try:
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid
        pool = load_pool("anthropic")
        # Avoid duplicate entries: delete any prior dashboard-issued OAuth entry
        existing = [e for e in pool.entries() if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_pkce")]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("anthropic pool add (dashboard) failed: %s", e)


def _start_anthropic_pkce() -> Dict[str, Any]:
    """Begin PKCE flow. Returns the auth URL the UI should open."""
    if not _ANTHROPIC_OAUTH_AVAILABLE:
        raise HTTPException(status_code=501, detail="Anthropic OAuth not available (missing adapter)")
    verifier, challenge = _generate_pkce_pair()
    sid, sess = _new_oauth_session("anthropic", "pkce")
    sess["verifier"] = verifier
    sess["state"] = verifier  # Anthropic round-trips verifier as state
    params = {
        "code": "true",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": _ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    auth_url = f"{_ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "session_id": sid,
        "flow": "pkce",
        "auth_url": auth_url,
        "expires_in": _OAUTH_SESSION_TTL_SECONDS,
    }


def _submit_anthropic_pkce(session_id: str, code_input: str) -> Dict[str, Any]:
    """Exchange authorization code for tokens. Persists on success."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess or sess["provider"] != "anthropic" or sess["flow"] != "pkce":
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if sess["status"] != "pending":
        return {"ok": False, "status": sess["status"], "message": sess.get("error_message")}

    # Anthropic's redirect callback page formats the code as `<code>#<state>`.
    # Strip the state suffix if present (we already have the verifier server-side).
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    if not code:
        return {"ok": False, "status": "error", "message": "No code provided"}
    state_from_callback = parts[1] if len(parts) > 1 else ""

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_from_callback or sess["state"],
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()
    req = urllib.request.Request(
        _ANTHROPIC_OAUTH_TOKEN_URL,
        data=exchange_data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-dashboard/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        sess["status"] = "error"
        sess["error_message"] = f"Token exchange failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        sess["status"] = "error"
        sess["error_message"] = "No access token returned"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    try:
        _save_anthropic_oauth_creds(access_token, refresh_token, expires_at_ms)
    except Exception as e:
        sess["status"] = "error"
        sess["error_message"] = f"Save failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}
    sess["status"] = "approved"
    _log.info("oauth/pkce: anthropic login completed (session=%s)", session_id)
    return {"ok": True, "status": "approved"}


async def _start_device_code_flow(provider_id: str) -> Dict[str, Any]:
    """Initiate a device-code flow (Nous or OpenAI Codex).

    Calls the provider's device-auth endpoint via the existing CLI helpers,
    then spawns a background poller. Returns the user-facing display fields
    so the UI can render the verification page link + user code.
    """
    from hermes_cli import auth as hauth
    if provider_id == "nous":
        from hermes_cli.auth import _request_device_code, PROVIDER_REGISTRY
        import httpx
        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("HERMES_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        client_id = pconfig.client_id
        scope = pconfig.scope
        def _do_nous_device_request():
            with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
                return _request_device_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=client_id,
                    scope=scope,
                )
        device_data = await asyncio.get_event_loop().run_in_executor(None, _do_nous_device_request)
        sid, sess = _new_oauth_session("nous", "device_code")
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = client_id
        threading.Thread(
            target=_nous_poller, args=(sid,), daemon=True, name=f"oauth-poll-{sid[:6]}"
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    if provider_id == "openai-codex":
        # Codex uses fixed OpenAI device-auth endpoints; reuse the helper.
        sid, _ = _new_oauth_session("openai-codex", "device_code")
        # Use the helper but in a thread because it polls inline.
        # We can't extract just the start step without refactoring auth.py,
        # so we run the full helper in a worker and proxy the user_code +
        # verification_url back via the session dict. The helper prints
        # to stdout — we capture nothing here, just status.
        threading.Thread(
            target=_codex_full_login_worker, args=(sid,), daemon=True,
            name=f"oauth-codex-{sid[:6]}",
        ).start()
        # Block briefly until the worker has populated the user_code, OR error.
        deadline = time.time() + 10
        while time.time() < deadline:
            with _oauth_sessions_lock:
                s = _oauth_sessions.get(sid)
            if s and (s.get("user_code") or s["status"] != "pending"):
                break
            await asyncio.sleep(0.1)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(sid, {})
        if s.get("status") == "error":
            raise HTTPException(status_code=500, detail=s.get("error_message") or "device-auth failed")
        if not s.get("user_code"):
            raise HTTPException(status_code=504, detail="device-auth timed out before returning a user code")
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": s["user_code"],
            "verification_url": s["verification_url"],
            "expires_in": int(s.get("expires_in") or 900),
            "poll_interval": int(s.get("interval") or 5),
        }

    raise HTTPException(status_code=400, detail=f"Provider {provider_id} does not support device-code flow")


def _nous_poller(session_id: str) -> None:
    """Background poller that drives a Nous device-code flow to completion."""
    from hermes_cli.auth import _poll_for_token, refresh_nous_oauth_from_state
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    device_code = sess["device_code"]
    interval = sess["interval"]
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        # Same post-processing as _nous_device_code_login (mint agent key)
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": portal_base_url,
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": client_id,
            "scope": token_data.get("scope"),
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(now.timestamp() + token_ttl, tz=timezone.utc).isoformat()
                if token_ttl else None
            ),
            "expires_in": token_ttl,
        }
        full_state = refresh_nous_oauth_from_state(
            auth_state, min_key_ttl_seconds=300, timeout_seconds=15.0,
            force_refresh=False, force_mint=True,
        )
        # Save into credential pool same as auth_commands.py does
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        pool = load_pool("nous")
        entry = PooledCredential.from_dict("nous", {
            **full_state,
            "label": "dashboard device_code",
            "auth_type": AUTH_TYPE_OAUTH,
            "source": f"{SOURCE_MANUAL}:dashboard_device_code",
            "base_url": full_state.get("inference_base_url"),
        })
        pool.add_entry(entry)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: nous login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("nous device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _codex_full_login_worker(session_id: str) -> None:
    """Run the complete OpenAI Codex device-code flow.

    Codex doesn't use the standard OAuth device-code endpoints; it has its
    own ``/api/accounts/deviceauth/usercode`` (JSON body, returns
    ``device_auth_id``) and ``/api/accounts/deviceauth/token`` (JSON body
    polled until 200). On success the response carries an
    ``authorization_code`` + ``code_verifier`` that get exchanged at
    CODEX_OAUTH_TOKEN_URL with grant_type=authorization_code.

    The flow is replicated inline (rather than calling
    _codex_device_code_login) because that helper prints/blocks/polls in a
    single function — we need to surface the user_code to the dashboard the
    moment we receive it, well before polling completes.
    """
    try:
        import httpx
        from hermes_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
            DEFAULT_CODEX_BASE_URL,
        )
        issuer = "https://auth.openai.com"

        # Step 1: request device code
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"deviceauth/usercode returned {resp.status_code}")
        device_data = resp.json()
        user_code = device_data.get("user_code", "")
        device_auth_id = device_data.get("device_auth_id", "")
        poll_interval = max(3, int(device_data.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("device-code response missing user_code or device_auth_id")
        verification_url = f"{issuer}/codex/device"
        with _oauth_sessions_lock:
            sess = _oauth_sessions.get(session_id)
            if not sess:
                return
            sess["user_code"] = user_code
            sess["verification_url"] = verification_url
            sess["device_auth_id"] = device_auth_id
            sess["interval"] = poll_interval
            sess["expires_in"] = 15 * 60  # OpenAI's effective limit
            sess["expires_at"] = time.time() + sess["expires_in"]

        # Step 2: poll until authorized
        deadline = time.time() + sess["expires_in"]
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.time() < deadline:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in (403, 404):
                    continue  # user hasn't authorized yet
                raise RuntimeError(f"deviceauth/token poll returned {poll.status_code}")

        if code_resp is None:
            with _oauth_sessions_lock:
                sess["status"] = "expired"
                sess["error_message"] = "Device code expired before approval"
            return

        # Step 3: exchange authorization_code for tokens
        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device-auth response missing authorization_code/code_verifier")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            raise RuntimeError(f"token exchange returned {token_resp.status_code}")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("token exchange did not return access_token")

        # Persist via credential pool — same shape as auth_commands.add_command
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid as _uuid
        pool = load_pool("openai-codex")
        base_url = (
            os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
            or DEFAULT_CODEX_BASE_URL
        )
        entry = PooledCredential(
            provider="openai-codex",
            id=_uuid.uuid4().hex[:6],
            label="dashboard device_code",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_device_code",
            access_token=access_token,
            refresh_token=refresh_token,
            base_url=base_url,
        )
        pool.add_entry(entry)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: openai-codex login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("codex device-code worker failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(e)


@app.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(provider_id: str, request: Request):
    """Initiate an OAuth login flow. Token-protected."""
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {_SESSION_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    _gc_oauth_sessions()
    valid = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in _OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        if catalog_entry["flow"] == "pkce":
            return _start_anthropic_pkce()
        if catalog_entry["flow"] == "device_code":
            return await _start_device_code_flow(provider_id)
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("oauth/start %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=400, detail="Unsupported flow")


class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


@app.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(provider_id: str, body: OAuthSubmitBody, request: Request):
    """Submit the auth code for PKCE flows. Token-protected."""
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {_SESSION_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    if provider_id == "anthropic":
        return await asyncio.get_event_loop().run_in_executor(
            None, _submit_anthropic_pkce, body.session_id, body.code,
        )
    raise HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")


@app.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(provider_id: str, session_id: str):
    """Poll a device-code session's status (no auth — read-only state)."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }


@app.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(session_id: str, request: Request):
    """Cancel a pending OAuth session. Token-protected."""
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {_SESSION_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    with _oauth_sessions_lock:
        sess = _oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Session detail endpoints
# ---------------------------------------------------------------------------


@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: str):
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = db.get_messages(sid)
        return {"session_id": sid, "messages": messages}
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str, _: None = Depends(_require_auth)):
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        if not db.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Log viewer endpoint
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_logs(
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    from hermes_cli.logs import _read_tail, LOG_FILES

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise HTTPException(status_code=400, detail=f"Unknown log file: {file}")
    log_path = get_hermes_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    try:
        from hermes_logging import COMPONENT_PREFIXES
    except ImportError:
        COMPONENT_PREFIXES = {}

    # Normalize "ALL" / "all" / empty → no filter. _matches_filters treats an
    # empty tuple as "must match a prefix" (startswith(()) is always False),
    # so passing () instead of None silently drops every line.
    min_level = level if level and level.upper() != "ALL" else None
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component)
        if comp_prefixes is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown component: {component}. "
                       f"Available: {', '.join(sorted(COMPONENT_PREFIXES))}",
            )
    else:
        comp_prefixes = None

    has_filters = bool(min_level or comp_prefixes or search)
    result = _read_tail(
        log_path, min(lines, 500) if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )
    # Post-filter by search term (case-insensitive substring match).
    # _read_tail doesn't support free-text search, so we filter here and
    # trim to the requested line count afterward.
    if search:
        needle = search.lower()
        result = [l for l in result if needle in l.lower()][-min(lines, 500):]
    return {"file": file, "lines": result}


# ---------------------------------------------------------------------------
# Cron job management endpoints
# ---------------------------------------------------------------------------


class CronJobCreate(BaseModel):
    prompt: str
    schedule: str
    name: str = ""
    deliver: str = "local"


class CronJobUpdate(BaseModel):
    updates: dict


@app.get("/api/cron/jobs")
async def list_cron_jobs():
    from cron.jobs import list_jobs
    return list_jobs(include_disabled=True)


@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str):
    from cron.jobs import get_job
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate, _: None = Depends(_require_auth)):
    from cron.jobs import create_job
    try:
        job = create_job(prompt=body.prompt, schedule=body.schedule,
                         name=body.name, deliver=body.deliver)
        return job
    except Exception as e:
        _log.exception("POST /api/cron/jobs failed")
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate, _: None = Depends(_require_auth)):
    from cron.jobs import update_job
    job = update_job(job_id, body.updates)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str, _: None = Depends(_require_auth)):
    from cron.jobs import pause_job
    job = pause_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str, _: None = Depends(_require_auth)):
    from cron.jobs import resume_job
    job = resume_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, _: None = Depends(_require_auth)):
    from cron.jobs import trigger_job
    job = trigger_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str, _: None = Depends(_require_auth)):
    from cron.jobs import remove_job
    if not remove_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Skills & Tools endpoints
# ---------------------------------------------------------------------------


class SkillToggle(BaseModel):
    name: str
    enabled: bool


@app.get("/api/skills")
async def get_skills():
    from tools.skills_tool import _find_all_skills
    from hermes_cli.skills_config import get_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    skills = _find_all_skills(skip_disabled=True)
    for s in skills:
        s["enabled"] = s["name"] not in disabled
    return skills


@app.put("/api/skills/toggle")
async def toggle_skill(body: SkillToggle, _: None = Depends(_require_auth)):
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    if body.enabled:
        disabled.discard(body.name)
    else:
        disabled.add(body.name)
    save_disabled_skills(config, disabled)
    return {"ok": True, "name": body.name, "enabled": body.enabled}


@app.get("/api/tools/toolsets")
async def get_toolsets():
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
    )
    from toolsets import resolve_toolset

    config = load_config()
    enabled_toolsets = _get_platform_tools(
        config,
        "cli",
        include_default_mcp_servers=False,
    )
    result = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        is_enabled = name in enabled_toolsets
        result.append({
            "name": name, "label": label, "description": desc,
            "enabled": is_enabled,
            "available": is_enabled,
            "configured": _toolset_has_keys(name, config),
            "tools": tools,
        })
    return result


# ---------------------------------------------------------------------------
# Raw YAML config endpoint
# ---------------------------------------------------------------------------


class RawConfigUpdate(BaseModel):
    yaml_text: str


@app.get("/api/config/raw")
async def get_config_raw():
    path = get_config_path()
    if not path.exists():
        return {"yaml": ""}
    return {"yaml": path.read_text(encoding="utf-8")}


@app.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate, _: None = Depends(_require_auth)):
    try:
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        save_config(parsed)
        return {"ok": True}
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


# ---------------------------------------------------------------------------
# Token / cost analytics endpoint
# ---------------------------------------------------------------------------


@app.get("/api/analytics/usage")
async def get_usage_analytics(days: int = 30):
    from hermes_state import SessionDB
    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)
        cur = db._conn.execute("""
            SELECT date(started_at, 'unixepoch') as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
        """, (cutoff,))
        daily = [dict(r) for r in cur.fetchall()]

        cur2 = db._conn.execute("""
            SELECT model,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COUNT(*) as sessions
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        by_model = [dict(r) for r in cur2.fetchall()]

        cur3 = db._conn.execute("""
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions
            FROM sessions WHERE started_at > ?
        """, (cutoff,))
        totals = dict(cur3.fetchone())

        return {"daily": daily, "by_model": by_model, "totals": totals, "period_days": days}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Telegram initData validation — HMAC-SHA256 (primary) + Ed25519 (fallback)
# ---------------------------------------------------------------------------

def _parse_init_data(init_data: str) -> tuple:
    """Parse initData query string into (raw_pairs, hash_value, signature_b64).
    
    raw_pairs excludes 'hash' and 'signature' fields.
    Values are URL-decoded once via urllib.parse.unquote.
    """
    from urllib.parse import unquote
    raw_pairs: Dict[str, str] = {}
    hash_value: Optional[str] = None
    signature_b64: Optional[str] = None
    for part in init_data.split("&"):
        if "=" in part:
            key, val = part.split("=", 1)
            key = key.strip()
            if key == "hash":
                hash_value = val
            elif key == "signature":
                signature_b64 = val
            else:
                raw_pairs[key] = unquote(val)
    return raw_pairs, hash_value, signature_b64


def _check_owner_and_expiry(raw_pairs: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Validate auth_date freshness and owner identity. Returns user dict or None."""
    auth_date_str = raw_pairs.get("auth_date", "0")
    auth_date = int(auth_date_str) if auth_date_str else 0
    # 5 minute expiry to match README claims (prevent replay attacks)
    if time.time() - auth_date > 300:
        _log.warning("Telegram initData expired: auth_date=%d age=%ds", auth_date, int(time.time() - auth_date))
        return None

    user_json = raw_pairs.get("user", "{}")
    user = json.loads(user_json)
    user_id = str(user.get("id", ""))

    if _TG_OWNER_ID and user_id != _TG_OWNER_ID:
        _log.warning("Telegram owner check failed: user_id=%s expected=%s", user_id, _TG_OWNER_ID)
        return None

    return user


def _validate_hmac_sha256(raw_pairs: Dict[str, str], hash_value: Optional[str]) -> bool:
    """Validate initData via HMAC-SHA256 (Telegram's primary method).
    
    Per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app:
      secret_key = HMAC_SHA256(bot_token, "WebAppData")
      data_check_string = sorted key=value pairs joined by \\n  (NO prefix)
      verify: hex(HMAC_SHA256(data_check_string, secret_key)) == hash
    """
    if not hash_value or not _TG_BOT_TOKEN:
        return False
    
    check_items = [f"{k}={raw_pairs[k]}" for k in sorted(raw_pairs.keys())]
    check_string = "\n".join(check_items)
    
    import hashlib
    secret_key = hmac.new("WebAppData".encode(), _TG_BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    
    if hmac.compare_digest(computed, hash_value):
        return True
    _log.debug("HMAC-SHA256 mismatch: computed=%s... hash=%s...", computed[:16], hash_value[:16])
    return False


def _validate_ed25519(raw_pairs: Dict[str, str], signature_b64: Optional[str]) -> bool:
    """Validate initData via Ed25519 signature (Telegram's third-party method).
    
    Per https://core.telegram.org/bots/webapps#validating-data-for-third-party-use:
      data_check_string = "bot_id:WebAppData\\n" + sorted key=value pairs joined by \\n
      signature = base64url-encoded Ed25519 signature
      public_key (prod) = e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d
    """
    if not signature_b64 or not _TG_BOT_TOKEN:
        return False
    
    import base64
    bot_id = _TG_BOT_TOKEN.split(":")[0] if ":" in _TG_BOT_TOKEN else ""
    check_items = [f"{k}={raw_pairs[k]}" for k in sorted(raw_pairs.keys())]
    prefix = f"{bot_id}:WebAppData"
    check_string = prefix + "\n" + "\n".join(check_items)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    # Telegram Ed25519 production public key (64 hex chars = 32 bytes).
    # MUST match https://core.telegram.org/bots/webapps exactly.
    TG_ED25519_PUBKEY = "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d"
    assert len(TG_ED25519_PUBKEY) == 64 and all(c in "0123456789abcdef" for c in TG_ED25519_PUBKEY), \
        f"TG_ED25519_PUBKEY corrupt: expected 64 hex chars, got {len(TG_ED25519_PUBKEY)}: {TG_ED25519_PUBKEY}"
    pub_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(TG_ED25519_PUBKEY))
    sig_padded = signature_b64 + "=" * (-len(signature_b64) % 4)
    sig_bytes = base64.urlsafe_b64decode(sig_padded)
    
    try:
        pub_key.verify(sig_bytes, check_string.encode())
        return True
    except Exception as e:
        _log.debug("Ed25519 validation failed: %s", e)
        return False


def _validate_telegram_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    """Validate Telegram WebApp initData via HMAC-SHA256 (primary) then Ed25519 (fallback).

    Tries HMAC-SHA256 first (uses bot token — Telegram's recommended method for the
    bot's own server). Falls back to Ed25519 if HMAC fails (uses Telegram's public key
    — designed for third-party validation but works too).

    Returns parsed user dict on success, None on failure.
    """
    if not init_data or not _TG_BOT_TOKEN:
        return None
    try:
        raw_pairs, hash_value, signature_b64 = _parse_init_data(init_data)
        
        # Try HMAC-SHA256 first (primary, per Telegram docs)
        if hash_value and _validate_hmac_sha256(raw_pairs, hash_value):
            _log.debug("Auth: HMAC-SHA256 valid")
            return _check_owner_and_expiry(raw_pairs)
        
        # Fall back to Ed25519 (third-party method)
        if signature_b64 and _validate_ed25519(raw_pairs, signature_b64):
            _log.debug("Auth: Ed25519 valid")
            return _check_owner_and_expiry(raw_pairs)
        
        _log.warning("Auth: both HMAC and Ed25519 failed for initData (%d chars)", len(init_data))
        return None
    except Exception as e:
        _log.warning("Telegram initData validation error: %s", e)
        return None


@app.middleware("http")
async def request_size_middleware(request: Request, call_next):
    """Check request Content-Length against max size limit."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            size = int(content_length)
            if size > _MAX_REQUEST_SIZE:
                return JSONResponse(
                    {"error": "Request too large"},
                    status_code=413
                )
        except ValueError:
            pass
    return await call_next(request)


@app.middleware("http")
async def telegram_auth_middleware(request: Request, call_next):
    """Allow localhost without auth ONLY for same-machine access.
    
    IMPORTANT: Cloudflare tunnel traffic arrives as 127.0.0.1 (proxied locally).
    We cannot rely on client.host alone — check X-Forwarded-For to detect tunnel.
    Static assets (SPA HTML/JS/CSS/fonts) are always served without auth so the
    frontend can load before the user authenticates.
    """
    path = request.url.path
    client_host = request.client.host if request.client else ""
    # Check if request came through the Cloudflare tunnel (forwarded header)
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    # True localhost = no proxy headers AND client is localhost
    is_local = (
        client_host in ("127.0.0.1", "::1", "localhost")
        and not forwarded_for
    )
    if is_local:
        return await call_next(request)
    # Whitelist static/frontend paths — the frontend must load before auth
    path = request.url.path
    if path in ("", "/", "/index.html", "/favicon.ico") or path.startswith("/assets/") or path.startswith("/miniapp") or path.startswith("/fonts/"):
        return await call_next(request)
    # Check Telegram Ed25519
    tg_init = request.headers.get("x-telegram-init-data", "").strip()
    if tg_init:
        user = _validate_telegram_init_data(tg_init)
        if user is not None:
            _log.debug("Auth: Ed25519 valid for user_id=%s", user.get("id"))
            return await call_next(request)
        else:
            _log.warning("Auth: Ed25519 validation FAILED for initData (%d chars)", len(tg_init))
    # Check Bearer token
    auth = request.headers.get("authorization", "")
    api_key = os.getenv("API_SERVER_KEY", "")
    if api_key and auth == f"Bearer {api_key}":
        return await call_next(request)
    _log.warning("Auth failed: path=%s client=%s forwarded=%s", path, client_host, bool(forwarded_for))
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


# ---------------------------------------------------------------------------
# Chat — uses tmux agent spawning, not gateway proxy
# See /api/agents endpoints below
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Session usage — GET /api/session-usage
# ---------------------------------------------------------------------------

@app.get("/api/session-usage")
async def get_session_usage():
    """Cumulative token usage from the session store."""
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            cur = db._conn.execute("""
                SELECT SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(cache_read_tokens) as total_cache_read,
                       SUM(reasoning_tokens) as total_reasoning,
                       COUNT(*) as total_sessions
                FROM sessions
            """)
            row = cur.fetchone()
            return dict(row) if row else {}
        finally:
            db.close()
    except Exception as e:
        _log.warning("session-usage failed: %s", e)
        return {"total_input": 0, "total_output": 0}


# ---------------------------------------------------------------------------
# Chat completions proxy → gateway
# ---------------------------------------------------------------------------

_GW_BASE = "http://127.0.0.1:8642"


def _get_gw_api_key() -> str:
    """Load API_SERVER_KEY from .env (dashboard doesn't auto-load env)."""
    env = load_env()
    return env.get("API_SERVER_KEY", "") or os.getenv("API_SERVER_KEY", "")


@app.post("/v1/chat/completions")
async def chat_completions_proxy(request: Request):
    """Proxy chat requests to the gateway API server on :8642."""
    import httpx as _httpx

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    gw_key = _get_gw_api_key()
    if gw_key:
        headers["Authorization"] = f"Bearer {gw_key}"

    sid = request.headers.get("X-Hermes-Session-Id")
    if sid:
        headers["X-Hermes-Session-Id"] = sid

    stream = body.get("stream", False)

    try:
        if stream:
            from starlette.responses import StreamingResponse as _SR

            async def _stream_chunks():
                async with _httpx.AsyncClient(timeout=_httpx.Timeout(300.0)) as client:
                    async with client.stream(
                        "POST", f"{_GW_BASE}/v1/chat/completions",
                        json=body, headers=headers,
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            yield chunk

            return _SR(
                _stream_chunks(),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )
        else:
            async with _httpx.AsyncClient(timeout=_httpx.Timeout(300.0)) as client:
                resp = await client.post(
                    f"{_GW_BASE}/v1/chat/completions",
                    json=body, headers=headers,
                )
                response = JSONResponse(content=resp.json(), status_code=resp.status_code)
                gw_sid = resp.headers.get("X-Hermes-Session-Id")
                if gw_sid:
                    response.headers["X-Hermes-Session-Id"] = gw_sid
                return response
    except Exception as e:
        _log.exception("Chat proxy error")
        raise HTTPException(status_code=502, detail=f"Gateway error: {e}")


# ---------------------------------------------------------------------------
# System health endpoint
# ---------------------------------------------------------------------------

@app.get("/api/system-health")
async def get_system_health():
    """System health metrics (CPU, memory, disk, uptime)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        uptime = int(time.time() - psutil.boot_time())
        load_avg = None
        try:
            load_avg = list(psutil.getloadavg())
        except Exception:
            pass
        return {
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "memory_total_gb": round(mem.total / (1024**3), 1),
            "memory_used_gb": round(mem.used / (1024**3), 1),
            "disk_percent": disk.percent,
            "disk_total_gb": round(disk.total / (1024**3), 1),
            "disk_used_gb": round(disk.used / (1024**3), 1),
            "uptime": uptime,
            "load_avg": load_avg,
        }
    except ImportError:
        raise HTTPException(status_code=501, detail="psutil not installed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Model info endpoint
# ---------------------------------------------------------------------------

@app.get("/api/model-info")
async def get_model_info():
    """Current model, provider, and context length."""
    config = load_config()
    model_val = config.get("model", "")
    if isinstance(model_val, dict):
        model_name = model_val.get("default", "")
        provider = model_val.get("provider", "")
    else:
        model_name = str(model_val)
        provider = ""

    context_length = 0
    try:
        from agent.model_metadata import get_context_length
        context_length = get_context_length(model_name)
    except Exception:
        pass

    return {
        "model": model_name,
        "model_short": model_name.split("/")[-1] if "/" in model_name else model_name,
        "provider": provider,
        "context_length": context_length,
    }


# ---------------------------------------------------------------------------
# Command execution endpoint
# ---------------------------------------------------------------------------

class CommandBody(BaseModel):
    command: str
    args: str = ""


@app.post("/api/command")
async def execute_command(body: CommandBody, _: None = Depends(_require_auth)):
    """Execute a slash command by proxying to gateway chat."""
    import httpx as _httpx

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    gw_key = _get_gw_api_key()
    if gw_key:
        headers["Authorization"] = f"Bearer {gw_key}"

    cmd_text = body.command + (" " + body.args if body.args else "")
    chat_body = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": cmd_text}],
        "stream": False,
    }

    try:
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(120.0)) as client:
            resp = await client.post(
                f"{_GW_BASE}/v1/chat/completions",
                json=chat_body, headers=headers,
            )
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            sid = resp.headers.get("X-Hermes-Session-Id", "")
            return {"output": content, "command": body.command, "session_id": sid}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gateway error: {e}")


# ---------------------------------------------------------------------------
# Agent management endpoints — tmux-backed hermes instances
# ---------------------------------------------------------------------------

_agent_registry: Dict[str, Dict[str, Any]] = {}


def _make_agent_name() -> str:
    i = len(_agent_registry) + 1
    while f"agent-{i}" in _agent_registry:
        i += 1
    return f"agent-{i}"


@app.get("/api/agents")
async def list_agents():
    """List all spawned agent instances."""
    alive = []
    for name, info in list(_agent_registry.items()):
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", f"h-{name}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            info["status"] = "running"
            info["uptime"] = int(time.time() - info.get("started_at", time.time()))
            info["display_name"] = info.get("display_name", name)
            alive.append(info)
        else:
            _agent_registry.pop(name, None)
    return {"agents": alive}


class SpawnBody(BaseModel):
    prompt: str
    name: str = ""
    mode: str = "interactive"
    worktree: bool = False
    model: str = ""


@app.post("/api/agents")
async def spawn_agent(body: SpawnBody, _: None = Depends(_require_auth)):
    """Spawn a new agent instance in a tmux session."""
    # Validate prompt for shell injection attempts
    if _PROMPT_BLACKLIST.search(body.prompt):
        raise HTTPException(status_code=400, detail="Invalid characters in prompt")
    
    # Sanitize name: only allow alphanumeric, hyphens, underscores
    name = body.name or _make_agent_name()
    name = _validate_agent_name(name)
    session_name = f"h-{name}"

    proc = await asyncio.create_subprocess_exec(
        "tmux", "has-session", "-t", session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode == 0:
        raise HTTPException(status_code=409, detail=f"Agent '{name}' already exists")

    hermes_cmd = "hermes"
    if body.worktree:
        hermes_cmd += " -w"
    if body.model:
        hermes_cmd += f" -m {shlex.quote(body.model)}"

    proc = await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", session_name, "-x", "120", "-y", "40",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    if body.mode == "interactive":
        # Sanitize prompt: strip tmux control sequences and non-printable chars
        safe_prompt = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', body.prompt)
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, "-l", hermes_cmd,
        )
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, "Enter",
        )
        await asyncio.sleep(8)
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, "-l", safe_prompt,
        )
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, "Enter",
        )
    else:
        cmd = f"{hermes_cmd} chat -q {shlex.quote(body.prompt)}"
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, "-l", cmd,
        )
        await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", session_name, "Enter",
        )

    now = time.time()
    _agent_registry[name] = {
        "name": name,
        "display_name": name,
        "status": "running",
        "mode": body.mode,
        "worktree": body.worktree,
        "model": body.model or "default",
        "started_at": now,
        "prompt": body.prompt[:100],
    }
    return _agent_registry[name]


def _validate_agent_name(name: str) -> str:
    """Sanitize and validate agent name for tmux session safety."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise HTTPException(status_code=400, detail="Invalid agent name")
    return name


@app.get("/api/agents/{name}")
async def get_agent(name: str):
    """Get agent details and output."""
    name = _validate_agent_name(name)
    if name not in _agent_registry:
        raise HTTPException(status_code=404, detail="Agent not found")

    info = dict(_agent_registry[name])
    proc = await asyncio.create_subprocess_exec(
        "tmux", "has-session", "-t", f"h-{name}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    if proc.returncode != 0:
        info["status"] = "dead"
        return info

    proc = await asyncio.create_subprocess_exec(
        "tmux", "capture-pane", "-t", f"h-{name}", "-p", "-S", "-120",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()

    info["status"] = "running"
    info["uptime"] = int(time.time() - info.get("started_at", time.time()))
    info["output"] = stdout.decode(errors="replace")
    return info


@app.delete("/api/agents/{name}")
async def kill_agent(name: str, _: None = Depends(_require_auth)):
    """Kill an agent and remove from registry."""
    name = _validate_agent_name(name)
    if name not in _agent_registry:
        raise HTTPException(status_code=404, detail="Agent not found")

    session_name = f"h-{name}"
    await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", session_name, "/exit", "Enter",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(2)
    await asyncio.create_subprocess_exec(
        "tmux", "kill-session", "-t", session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    _agent_registry.pop(name, None)
    return {"status": "killed", "name": name}


class AgentMessageBody(BaseModel):
    message: str


@app.post("/api/agents/{name}/message")
async def send_agent_message(name: str, body: AgentMessageBody, _: None = Depends(_require_auth)):
    """Send a message to a running agent's tmux session."""
    name = _validate_agent_name(name)
    if name not in _agent_registry:
        raise HTTPException(status_code=404, detail="Agent not found")

    session_name = f"h-{name}"
    proc = await asyncio.create_subprocess_exec(
        "tmux", "has-session", "-t", session_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise HTTPException(status_code=410, detail="Agent session no longer exists")

    await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", session_name, "-l",
        re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', body.message),
    )
    await asyncio.create_subprocess_exec(
        "tmux", "send-keys", "-t", session_name, "Enter",
    )
    return {"status": "sent", "name": name}


@app.get("/api/processes")
async def list_processes():
    """List background processes."""
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                info = p.info
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"],
                    "cpu": round(info["cpu_percent"] or 0, 1),
                    "mem": round(info["memory_percent"] or 0, 1),
                    "running": info["status"] == "running",
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return {"processes": procs[:20]}
    except ImportError:
        return {"processes": []}


@app.get("/api/debug-auth")
async def debug_auth(request: Request, _: None = Depends(_require_auth_local_ok)):
    """Debug endpoint to troubleshoot Telegram auth. Requires auth."""
    client_host = request.client.host if request.client else ""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    is_local = client_host in ("127.0.0.1", "::1", "localhost") and not forwarded_for
    tg_init = request.headers.get("x-telegram-init-data", "")
    auth = request.headers.get("authorization", "")
    return {
        "client_host": client_host,
        "forwarded_for": forwarded_for or None,
        "is_local": is_local,
        "tg_init_data_present": bool(tg_init),
        "auth_present": bool(auth),
    }


def mount_spa(application: FastAPI):
    """Mount the built SPA. Falls back to index.html for client-side routing."""
    if not WEB_DIST.exists():
        @application.get("/{full_path:path}")
        async def no_frontend(full_path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run: cd web && npm run build"},
                status_code=404,
            )
        return

    application.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = WEB_DIST / full_path
        # Prevent path traversal via url-encoded sequences (%2e%2e/)
        if (
            full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists()
            and file_path.is_file()
        ):
            return FileResponse(file_path)
        return FileResponse(
            WEB_DIST / "index.html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )


mount_spa(app)


def start_server(host: str = "127.0.0.1", port: int = 9119, open_browser: bool = True):
    """Start the web UI server."""
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        import logging
        logging.warning(
            "Binding to %s — the web UI exposes config and API keys. "
            "Only bind to non-localhost if you trust all users on the network.", host,
        )

    if open_browser:
        import threading
        import webbrowser

        def _open():
            import time as _t
            _t.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()

    print(f"  Hermes Web UI → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
