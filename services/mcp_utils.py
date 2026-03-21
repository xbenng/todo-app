"""MCP (Model Context Protocol) utility functions.

Handles registry loading, OAuth token refresh, server config building,
manager lifecycle, tool parsing, and permission checks.
"""

import os
import json
import time
import hmac
import hashlib
import tempfile
import threading
import uuid
import shutil

from datetime import datetime, timedelta, timezone

import state
from state import (
    _mcp_managers,
    _mcp_managers_lock,
    _pending_approvals,
    _approvals_lock,
    _jobs,
    _user_temp_dirs,
)
from services.mcp_manager import MCPManager
import db as _db
import sys
import logging
log = logging.getLogger("services.mcp_utils")


def _resolve_mcp_command(command: str, server_name: str, app_dir: str) -> str:
    """Resolve an MCP server command to an absolute path.

    Checks (in order): shutil.which, Python's own bin dir (for pip-installed
    commands like mcp-caldav), app dir, mcp-servers dir, node_modules/.bin.
    """
    python_bin_dir = os.path.dirname(sys.executable)
    mcp_dir = os.path.join(app_dir, "mcp-servers")
    for candidate in [
        shutil.which(command),
        os.path.join(python_bin_dir, command),
        os.path.join(app_dir, command),
        os.path.join(mcp_dir, command),
        os.path.join(mcp_dir, server_name, "node_modules", ".bin", command),
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return command  # fallback to original (may fail at runtime)


def _load_mcp_registry() -> dict:
    """Load MCP server registry from mcp-servers/registry.json."""
    registry_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "mcp-servers", "registry.json")
    if not os.path.exists(registry_path):
        return {}
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _refresh_oauth_token(oauth_token: dict) -> str | None:
    """Refresh an OAuth access token. Returns new access token or None on failure."""
    import requests as _req
    try:
        resp = _req.post(oauth_token["token_uri"], data={
            "client_id": oauth_token["client_id"],
            "client_secret": oauth_token["client_secret"],
            "refresh_token": oauth_token["refresh_token"],
            "grant_type": "refresh_token",
        })
        if resp.status_code == 200:
            data = resp.json()
            return data.get("access_token")
    except Exception as exc:
        log.warning("OAuth token refresh failed: %s", exc)
    return None


def _write_server_accounts(user_id: str, server_name: str, entry: dict) -> dict:
    """Write per-user account configs to temp files. Returns extra env vars to set."""
    if not user_id or user_id == "local":
        return {}
    if "account_fields" not in entry:
        return {}
    accounts = _db.get_server_accounts(user_id, server_name)
    if not accounts:
        return {}

    uid = user_id
    if uid not in _user_temp_dirs:
        _user_temp_dirs[uid] = tempfile.mkdtemp(prefix=f"mcp-{uid[:8]}-")

    config_format = entry.get("config_format", "")
    config_env = entry.get("config_env", "")
    extra_env = {}

    if config_format == "imap":
        # IMAP: write accounts.json + .key file under a fake HOME
        home_dir = os.path.join(_user_temp_dirs[uid], f"imap-{server_name}")
        imap_dir = os.path.join(home_dir, ".imap-mcp")
        os.makedirs(imap_dir, exist_ok=True)
        # Generate encryption key if not exists
        key_path = os.path.join(imap_dir, ".key")
        if not os.path.exists(key_path):
            import secrets as _secrets
            with open(key_path, "w") as f:
                f.write(_secrets.token_hex(32))
        # Read the key for encrypting passwords
        with open(key_path) as f:
            enc_key = bytes.fromhex(f.read().strip())
        # Write accounts with encrypted passwords
        import hashlib
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        imap_accounts = []
        for acct in accounts:
            cfg = dict(acct["config"])
            cfg.setdefault("id", acct["id"])
            cfg.setdefault("tls", True)
            cfg.setdefault("port", 993)
            # Handle OAuth accounts
            oauth_token = cfg.pop("oauth_token", None)
            if oauth_token and cfg.get("auth_type") == "oauth":
                # Refresh the access token and write as accessToken
                fresh_token = _refresh_oauth_token(oauth_token)
                if fresh_token:
                    cfg["accessToken"] = fresh_token
                    cfg.pop("password", None)  # Don't need password for OAuth
                else:
                    log.warning("IMAP OAuth token refresh failed for %s", cfg.get("name"))
                    continue  # Skip account if refresh fails
            # Encrypt password for non-OAuth accounts
            elif "password" in cfg and cfg["password"]:
                iv = os.urandom(16)
                pw = cfg["password"].encode()
                pad_len = 16 - (len(pw) % 16)
                pw_padded = pw + bytes([pad_len] * pad_len)
                cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
                enc = cipher.encryptor()
                encrypted = enc.update(pw_padded) + enc.finalize()
                cfg["password"] = iv.hex() + ":" + encrypted.hex()
            imap_accounts.append(cfg)
        with open(os.path.join(imap_dir, "accounts.json"), "w") as f:
            json.dump(imap_accounts, f)
        extra_env["HOME"] = home_dir

    elif config_format == "caldav":
        # CalDAV: write accounts.json + OAuth token files, set CALDAV_ACCOUNTS_CONFIG
        caldav_dir = os.path.join(_user_temp_dirs[uid], f"caldav-{server_name}")
        os.makedirs(caldav_dir, exist_ok=True)
        caldav_accounts = []
        for acct in accounts:
            cfg = dict(acct["config"])
            # Write OAuth token file if present
            oauth_token = cfg.pop("oauth_token", None)
            if oauth_token and cfg.get("auth_type") == "oauth":
                acct_name = cfg.get("name", acct["id"][:8])
                token_path = os.path.join(caldav_dir, f"{acct_name}_token.json")
                with open(token_path, "w") as tf:
                    json.dump(oauth_token, tf)
                cfg["google_token_path"] = token_path
            caldav_accounts.append(cfg)
        config_path = os.path.join(caldav_dir, "accounts.json")
        with open(config_path, "w") as f:
            json.dump({"accounts": caldav_accounts}, f)
        extra_env[config_env] = config_path

    return extra_env


def _build_mcp_configs_from_registry(registry: dict, tokens: dict,
                                     enabled_servers: set | None = None,
                                     user_id: str | None = None) -> dict:
    """Convert registry.json entries + user tokens into MCPManager-compatible server_configs."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    server_configs = {}
    for name, entry in registry.items():
        if enabled_servers is not None and name not in enabled_servers:
            continue
        server_type = entry.get("type", "stdio")
        if server_type == "stdio":
            env = dict(entry.get("static_env", {}))
            for field in entry.get("credential_fields", []):
                key = field["key"]
                if key in tokens:
                    env[key] = tokens[key]
            # Write per-user account configs if needed
            extra_env = _write_server_accounts(user_id, name, entry)
            env.update(extra_env)
            # Resolve relative paths in args against app directory
            args = []
            for arg in entry.get("args", []):
                if not arg.startswith("-") and not os.path.isabs(arg):
                    resolved = os.path.join(app_dir, arg)
                    if os.path.exists(resolved):
                        args.append(resolved)
                    else:
                        args.append(arg)
                else:
                    args.append(arg)
            # Resolve command to absolute path so it works regardless of subprocess PATH
            command = entry["command"]
            if not os.path.isabs(command):
                command = _resolve_mcp_command(command, name, app_dir)
            server_configs[name] = {
                "type": "stdio",
                "command": command,
                "args": args,
                "env": env,
            }
        elif server_type in ("sse", "http", "streamable-http"):
            cfg = {
                "type": server_type,
                "url": entry["url"],
            }
            # Add Bearer token header if configured
            bearer_key = entry.get("bearer_token")
            if bearer_key and bearer_key in tokens:
                oauth_token = tokens[bearer_key]
                access_token = None
                if isinstance(oauth_token, dict):
                    # Full oauth_token object — refresh only if expired
                    access_token = oauth_token.get("token")
                    expiry_str = oauth_token.get("expiry", "")
                    is_expired = False
                    if expiry_str:
                        try:
                            expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                            is_expired = datetime.now(timezone.utc) >= expiry
                        except Exception:
                            pass
                    if is_expired and oauth_token.get("refresh_token"):
                        fresh = _refresh_oauth_token(oauth_token)
                        if fresh:
                            access_token = fresh
                            oauth_token["token"] = fresh
                            oauth_token["expiry"] = (datetime.now(timezone.utc) +
                                                     timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                            if user_id and user_id != "local":
                                config = _db.get_config(user_id)
                                db_tokens = config.get("tokens", {})
                                db_tokens[bearer_key] = oauth_token
                                _db.save_config(user_id, tokens=db_tokens)
                elif isinstance(oauth_token, str):
                    access_token = oauth_token
                if access_token:
                    cfg["headers"] = {"Authorization": f"Bearer {access_token}"}
            # Add Basic auth header as fallback (only if no bearer token set)
            if "headers" not in cfg:
                basic_key = entry.get("basic_auth_token")
                if basic_key and basic_key in tokens and tokens[basic_key]:
                    cfg["headers"] = {"Authorization": f"Basic {tokens[basic_key]}"}
            server_configs[name] = cfg
    return server_configs


def _build_cli_mcp_config(user_id: str | None) -> dict | None:
    """Build an mcpServers dict for Claude CLI --mcp-config from the registry + user credentials."""
    registry = _load_mcp_registry()
    if not registry:
        return None
    if user_id and user_id != "local":
        config = _db.get_config(user_id)
        prefs = _db.get_mcp_preferences(user_id)
        enabled = {name for name, p in prefs.items() if p["enabled"]}
    else:
        config = {}
        enabled = None
    tokens = config.get("tokens", {})
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    servers = {}
    for name, entry in registry.items():
        if enabled is not None and name not in enabled:
            continue
        server_type = entry.get("type", "stdio")
        if server_type == "stdio":
            env = dict(entry.get("static_env", {}))
            for field in entry.get("credential_fields", []):
                key = field["key"]
                if key in tokens:
                    env[key] = tokens[key]
            # Write per-user account configs if needed
            extra_env = _write_server_accounts(user_id, name, entry)
            env.update(extra_env)
            # Resolve relative paths
            args = []
            for arg in entry.get("args", []):
                if not arg.startswith("-") and not os.path.isabs(arg):
                    resolved = os.path.join(app_dir, arg)
                    if os.path.exists(resolved):
                        args.append(resolved)
                    else:
                        args.append(arg)
                else:
                    args.append(arg)
            command = entry["command"]
            if not os.path.isabs(command):
                command = _resolve_mcp_command(command, name, app_dir)
            servers[name] = {"type": "stdio", "command": command, "args": args, "env": env}
        elif server_type in ("sse", "http"):
            cfg = {"type": "http", "url": entry["url"]}
            # Bearer token
            bearer_key = entry.get("bearer_token")
            if bearer_key and bearer_key in tokens:
                oauth_token = tokens[bearer_key]
                if isinstance(oauth_token, dict):
                    cfg["headers"] = {"Authorization": f"Bearer {oauth_token.get('token', '')}"}
                elif isinstance(oauth_token, str):
                    cfg["headers"] = {"Authorization": f"Bearer {oauth_token}"}
            # Basic auth fallback
            if "headers" not in cfg:
                basic_key = entry.get("basic_auth_token")
                if basic_key and basic_key in tokens and tokens[basic_key]:
                    cfg["headers"] = {"Authorization": f"Basic {tokens[basic_key]}"}
            servers[name] = cfg
    # Add the todo-tools MCP server (proxies back to our HTTP API)
    todo_tools_script = os.path.join(app_dir, "mcp-servers", "todo-tools.py")
    if os.path.exists(todo_tools_script):
        todo_env = {"TODO_API_BASE": "http://localhost:5222"}
        # Create a short-lived session token so the MCP proxy can call our API as this user
        if user_id and user_id != "local":
            proxy_token = _db.create_session(user_id, expires_hours=1)
            todo_env["TODO_AUTH_TOKEN"] = proxy_token
        python_bin = shutil.which("python3") or "python3"
        servers["todo-tools"] = {
            "type": "stdio",
            "command": python_bin,
            "args": [todo_tools_script],
            "env": todo_env,
        }
    if not servers:
        return None
    return {"mcpServers": servers}


def _get_mcp_manager(user_id: str | None = None) -> MCPManager | None:
    """Get or lazily create an MCPManager for the given user.

    Loads server definitions from the registry and credentials from the
    user's DB config (or file config in non-DB mode).
    """
    try:
        from mcp import ClientSessionGroup
    except ImportError:
        ClientSessionGroup = None
    if not ClientSessionGroup:
        return None
    uid = user_id or "local"
    with _mcp_managers_lock:
        if uid in _mcp_managers:
            return _mcp_managers[uid]
    # Load registry + user tokens outside the lock (IO)
    registry = _load_mcp_registry()
    if not registry:
        return None
    if user_id and user_id != "local":
        config = _db.get_config(user_id)
    else:
        config = {}
    tokens = config.get("tokens", {})
    # Only connect servers the user has enabled
    enabled_servers = None
    if user_id and user_id != "local":
        prefs = _db.get_mcp_preferences(user_id)
        enabled_servers = {name for name, p in prefs.items() if p["enabled"]}
        if not enabled_servers:
            return None  # No servers enabled
    mcp_configs = _build_mcp_configs_from_registry(registry, tokens, enabled_servers, user_id)
    if not mcp_configs:
        return None
    mgr = MCPManager()
    mgr.start(mcp_configs, tokens)
    with _mcp_managers_lock:
        # Double-check another thread didn't create one while we were building
        if uid in _mcp_managers:
            mgr.stop()
            return _mcp_managers[uid]
        _mcp_managers[uid] = mgr
    return mgr


def _get_mcp_tools(user_id: str | None = None) -> list[dict]:
    """Get filtered MCP tool definitions, excluding registry + per-user disabled tools."""
    mgr = _get_mcp_manager(user_id)
    if not mgr:
        return []
    tools = mgr.get_tool_definitions()
    registry = _load_mcp_registry()
    # Build set of excluded tool names (registry-level)
    excluded = set()
    if registry:
        for server_name, entry in registry.items():
            for tool_name in entry.get("exclude_tools", []):
                excluded.add(f"mcp__{server_name}__{tool_name}")
    # Add per-user disabled tools
    if user_id and user_id != "local":
        prefs = _db.get_mcp_preferences(user_id)
        for server_name, pref in prefs.items():
            for tool_name in pref.get("disabled_tools", []):
                excluded.add(f"mcp__{server_name}__{tool_name}")
    if not excluded:
        return tools
    return [t for t in tools if t["name"] not in excluded]


def _parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Parse 'mcp__{server}__{tool}' -> (server, tool), or None."""
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    if len(parts) == 3:
        return parts[1], parts[2]
    return None


def _check_tool_permission(user_id: str, tool_name: str, input_data: dict,
                           agent_context: dict | None) -> str:
    """Check if an MCP tool is auto-approved or needs user confirmation.

    Returns 'approved', 'denied', or blocks until user responds.
    """
    # Global bypass
    config = _db.get_config(user_id)
    if config.get("auto_approve_all"):
        return "approved"

    parsed = _parse_mcp_tool_name(tool_name)
    if not parsed:
        return "approved"  # Not an MCP tool, always allow
    server_name, bare_tool = parsed

    # Check per-tool auto-approval
    prefs = _db.get_mcp_preferences(user_id)
    server_pref = prefs.get(server_name, {})
    if bare_tool in server_pref.get("auto_approved_tools", []):
        return "approved"

    # Need user approval — emit SSE event and block
    approval_id = str(uuid.uuid4())[:8]
    event = threading.Event()
    with _approvals_lock:
        _pending_approvals[approval_id] = {
            "event": event,
            "approved": None,
            "server_name": server_name,
            "tool_name": bare_tool,
        }

    # Emit approval request to the job's SSE stream
    job_id = agent_context.get("job_id") if agent_context else None
    if job_id and job_id in _jobs:
        approval_obj = {
            "__tool_approval__": True,
            "approval_id": approval_id,
            "tool": tool_name,
            "tool_display": bare_tool,
            "server": server_name,
            "args": input_data,
        }
        _jobs[job_id]["output_lines"].append(approval_obj)

    # Block until user responds (timeout after 5 minutes)
    event.wait(timeout=300)

    with _approvals_lock:
        result = _pending_approvals.pop(approval_id, {})

    if result.get("approved") is True:
        return "approved"
    return "denied"


def _redact_key(key: str) -> str:
    """Redact an API key for display."""
    if not key:
        return ""
    if len(key) > 12:
        return key[:8] + "..." + key[-4:]
    return "***"
