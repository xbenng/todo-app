#!/usr/bin/env python3
"""
Simple Todo App — A lightweight web UI for managing a markdown-based todo list.

Usage:
    python app.py [path/to/todos.md]

If no file is specified, defaults to 'todos.md' in the current directory.
The file will be created if it doesn't exist.
"""

import sys
import os

# Load .env file if present (before any other env lookups)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

import re
import json
import uuid
import copy
import shutil
import signal
import subprocess
import threading
import time
import pty
import fcntl
import termios
import struct
import select as _select
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, Response
from flask_sock import Sock

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import openai as openai_mod
except ImportError:
    openai_mod = None

try:
    import asyncio
    from mcp import ClientSessionGroup, StdioServerParameters
    from mcp.client.session_group import SseServerParameters
except ImportError:
    ClientSessionGroup = None

import db as _db

app = Flask(__name__)
sock = Sock(app)
TODO_FILE = "todos.md"

# Database mode flag — set to True when DATABASE_URL is configured
_USE_DB = False

# Undo stack: per-user when DB mode, global when file mode
_undo_stack: deque[tuple[list[dict], list[dict]]] = deque(maxlen=30)
_undo_stacks: dict[str, deque] = {}  # user_id -> deque

# job_id -> {id, label, job_key, status, output_lines, proc, created_at, user_id}
_jobs: dict[str, dict] = {}


def get_current_user() -> dict | None:
    """Extract user from session cookie or Authorization header.

    Returns {"id": ..., "email": ..., "name": ...} or None.
    In file mode (_USE_DB=False), returns a stub user.
    """
    if not _USE_DB:
        return {"id": "local", "email": "local", "name": "Local User"}

    # Check cookie first
    token = request.cookies.get("session_token")
    # Fall back to Authorization header
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    return _db.get_session_user(token)


def require_user():
    """Get current user or abort with 401."""
    user = get_current_user()
    if not user:
        return None
    return user
# session_id -> {id, todo_id, title, tmux_target, alive, created_at, needs_auto_send, resume_id}
_pty_sessions: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# MCP Server Manager
# ---------------------------------------------------------------------------

class MCPManager:
    """Manages MCP server connections in a background asyncio event loop."""

    def __init__(self):
        self._loop: object = None  # asyncio event loop
        self._thread: threading.Thread | None = None
        self._group: object = None  # ClientSessionGroup
        self._tool_defs: list[dict] = []  # cached Anthropic API format
        self._mcp_tool_names: set[str] = set()
        self._started = threading.Event()
        self._server_status: dict[str, dict] = {}  # name -> {connected, tool_count}

    def start(self, server_configs: dict, tokens: dict | None = None):
        """Start background event loop thread and connect to all configured MCP servers."""
        if not ClientSessionGroup:
            return
        self._tokens = tokens or {}
        self._server_configs = server_configs
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)
        # Connect to servers (blocking wait)
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._connect_all(server_configs), self._loop
            )
            future.result(timeout=60)
        except Exception as exc:
            print(f"MCP startup error: {exc}")

    def stop(self):
        """Shutdown all MCP connections and stop the event loop."""
        if self._loop and self._loop.is_running():
            async def _shutdown():
                if self._group:
                    await self._group.__aexit__(None, None, None)
            try:
                future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
                future.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)

    def get_tool_definitions(self) -> list[dict]:
        """Return MCP tools in Anthropic API tool definition format."""
        return list(self._tool_defs)

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return name in self._mcp_tool_names

    def call_tool(self, name: str, arguments: dict, timeout: float = 120.0) -> str:
        """Execute an MCP tool call synchronously from a Flask thread."""
        if not self._loop or not self._group:
            return json.dumps({"error": "MCP not initialized"})
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_call_tool(name, arguments), self._loop
            )
            return future.result(timeout=timeout)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def get_status(self) -> dict:
        """Return status of all MCP servers."""
        return dict(self._server_status)

    # -- internal --

    def _run_loop(self):
        """Background thread target: run an asyncio event loop forever."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    async def _connect_all(self, server_configs: dict):
        """Connect to all configured MCP servers."""
        # Track which server_info.name maps to which config key.
        # We populate this before connecting so the name_hook can use it.
        self._name_map = {}  # server_impl_name -> config_key
        self._pending_config_key = None  # set before each connect

        def name_hook(name, server_info):
            # Register mapping on first tool encountered
            if server_info.name not in self._name_map and self._pending_config_key:
                self._name_map[server_info.name] = self._pending_config_key
            config_key = self._name_map.get(server_info.name, server_info.name)
            return f"mcp__{config_key}__{name}"

        self._group = ClientSessionGroup(component_name_hook=name_hook)
        await self._group.__aenter__()

        for server_name, config in server_configs.items():
            try:
                self._pending_config_key = server_name
                await self._connect_server(server_name, config)
                tool_count = sum(1 for t in self._group.tools
                                 if t.startswith(f"mcp__{server_name}__"))
                self._server_status[server_name] = {
                    "connected": True, "tool_count": tool_count
                }
                print(f"MCP connected: {server_name} ({tool_count} tools)")
            except Exception as exc:
                self._server_status[server_name] = {
                    "connected": False, "error": str(exc)
                }
                print(f"MCP failed: {server_name}: {exc}")
        self._pending_config_key = None

        # Rebuild cached tool definitions
        self._rebuild_tool_cache()

    async def _connect_server(self, server_name: str, config: dict):
        """Connect to a single MCP server. Returns the ClientSession."""
        server_type = config.get("type", "stdio")

        if server_type == "stdio":
            # Start with user's full shell environment so npx/node/python are on PATH
            shell_env = _get_user_shell_env()
            # Merge server-specific env vars on top
            server_env = self._resolve_env(config.get("env", {}))
            full_env = dict(shell_env)
            full_env.update(server_env)
            # Resolve command via shell PATH if not absolute
            command = config["command"]
            if not os.path.isabs(command):
                resolved = shutil.which(command, path=full_env.get("PATH", os.defpath))
                if resolved:
                    command = resolved
            params = StdioServerParameters(
                command=command,
                args=config.get("args", []),
                env=full_env,
            )
        elif server_type in ("sse", "http"):
            params = SseServerParameters(url=config["url"])
        else:
            raise ValueError(f"Unknown MCP server type: {server_type}")

        return await self._group.connect_to_server(params)

    def _resolve_env(self, env: dict) -> dict:
        """Replace ${tokens.X} placeholders with actual values from config tokens."""
        resolved = {}
        for key, value in env.items():
            if isinstance(value, str) and value.startswith("${tokens.") and value.endswith("}"):
                token_key = value[9:-1]
                resolved[key] = self._tokens.get(token_key, value)
            else:
                resolved[key] = value
        return resolved

    def _rebuild_tool_cache(self):
        """Convert MCP Tool objects to Anthropic API tool definition dicts."""
        self._tool_defs = []
        self._mcp_tool_names = set()
        if not self._group:
            return
        for name, tool in self._group.tools.items():
            self._tool_defs.append({
                "name": name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            })
            self._mcp_tool_names.add(name)

    async def _async_call_tool(self, name: str, arguments: dict) -> str:
        """Execute an MCP tool and return the result as a JSON string."""
        result = await self._group.call_tool(name, arguments)
        parts = []
        for block in (result.content or []):
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(f"[binary data: {getattr(block, 'mimeType', 'unknown')}]")
        if result.isError:
            return json.dumps({"error": "\n".join(parts) if parts else "Tool call failed"})
        return "\n".join(parts) if parts else json.dumps({"result": "ok"})


_mcp_managers: dict[str, MCPManager] = {}  # user_id -> MCPManager
_mcp_managers_lock = threading.Lock()

# Runtime tool approval: approval_id -> {event, approved, server_name, tool_name}
_pending_approvals: dict[str, dict] = {}
_approvals_lock = threading.Lock()


def _get_mcp_manager(user_id: str | None = None) -> MCPManager | None:
    """Get or lazily create an MCPManager for the given user.

    Loads server definitions from the registry and credentials from the
    user's DB config (or file config in non-DB mode).
    """
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
    if _USE_DB and user_id and user_id != "local":
        config = _db.get_config(user_id)
    else:
        config = _load_config()
    tokens = config.get("tokens", {})
    # Only connect servers the user has enabled
    enabled_servers = None
    if _USE_DB and user_id and user_id != "local":
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
    if _USE_DB and user_id and user_id != "local":
        prefs = _db.get_mcp_preferences(user_id)
        for server_name, pref in prefs.items():
            for tool_name in pref.get("disabled_tools", []):
                excluded.add(f"mcp__{server_name}__{tool_name}")
    if not excluded:
        return tools
    return [t for t in tools if t["name"] not in excluded]


def _parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Parse 'mcp__{server}__{tool}' → (server, tool), or None."""
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


def _completed_file_path(path: str) -> str:
    """Derive the completed-todos file path from the main todo file path.
    e.g. todos.md -> todos-completed.md
    """
    base, ext = os.path.splitext(path)
    return f"{base}-completed{ext}"


def _chats_file_path(path: str) -> str:
    """Derive the chats file path from the main todo file path.
    e.g. todos.md -> todos-chats.json
    """
    base, _ = os.path.splitext(path)
    return f"{base}-chats.json"


def _load_chats() -> dict:
    """Load chat sessions from disk."""
    path = _chats_file_path(TODO_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_chats(data: dict) -> None:
    """Save chat sessions to disk."""
    path = _chats_file_path(TODO_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


_DEFAULT_SYSTEM_PROMPT = """\
You are a helpful assistant managing a todo list.
You have tools to read, create, update, and search todos.
Use them when the user asks about their tasks or wants to make changes.
"""

# Source files to seed into the config context/ directory on first run.
_CONTEXT_SEED_FILES = [
    ("claude.md", "~/.claude/CLAUDE.md"),
    ("communication-style.md", "~/.claude/communication-style.md"),
    ("org.md", "~/.claude/org.md"),
]
_MEMORY_INDEX_PATH = ""  # Disabled — memory context is not seeded


def _config_dir_path() -> str:
    """Return the config directory path derived from the todo file."""
    base, _ = os.path.splitext(TODO_FILE)
    return f"{base}-config"


def _ensure_config_dir() -> str:
    """Create config dir structure. Migrate from flat file if needed. Return dir path."""
    config_dir = _config_dir_path()
    context_dir = os.path.join(config_dir, "context")
    os.makedirs(context_dir, exist_ok=True)
    os.makedirs(os.path.join(config_dir, "users"), exist_ok=True)

    # Migrate from old flat config file
    old_flat = f"{os.path.splitext(TODO_FILE)[0]}-config.json"
    new_json = os.path.join(config_dir, "config.json")
    if os.path.exists(old_flat) and not os.path.exists(new_json):
        shutil.move(old_flat, new_json)

    # Seed system-prompt.md if missing
    prompt_path = os.path.join(config_dir, "system-prompt.md")
    if not os.path.exists(prompt_path):
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_SYSTEM_PROMPT)

    # Seed context files from ~/.claude/ if context dir is empty
    if not any(f.endswith(".md") for f in os.listdir(context_dir)):
        for dest_name, src_path in _CONTEXT_SEED_FILES:
            src = os.path.expanduser(src_path)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(context_dir, dest_name))
                except OSError:
                    pass
        # Assemble memory from MEMORY.md + referenced files
        _seed_memory_context(context_dir)

    return config_dir


def _seed_memory_context(context_dir: str) -> None:
    """Read MEMORY.md index, resolve relative .md links, assemble into context/memory.md."""
    index_path = os.path.expanduser(_MEMORY_INDEX_PATH)
    if not os.path.exists(index_path):
        return
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_content = f.read()
        parts = [index_content.strip()]
        # Resolve relative .md links: [name](file.md)
        memory_dir = os.path.dirname(index_path)
        for match in re.finditer(r'\[.*?\]\(([^)]+\.md)\)', index_content):
            ref_path = os.path.join(memory_dir, match.group(1))
            if os.path.exists(ref_path):
                with open(ref_path, "r", encoding="utf-8") as rf:
                    parts.append(rf.read().strip())
        with open(os.path.join(context_dir, "memory.md"), "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(parts))
    except OSError:
        pass


def _load_config() -> dict:
    """Load server config from disk."""
    path = os.path.join(_config_dir_path(), "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_mcp_registry() -> dict:
    """Load MCP server registry from mcp-servers/registry.json."""
    registry_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "mcp-servers", "registry.json")
    if not os.path.exists(registry_path):
        return {}
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


import tempfile as _tempfile

# Track per-user temp dirs for config files (cleaned up on manager stop)
_user_temp_dirs: dict[str, str] = {}


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
        print(f"[oauth] Token refresh failed: {exc}")
    return None


def _write_server_accounts(user_id: str, server_name: str, entry: dict) -> dict:
    """Write per-user account configs to temp files. Returns extra env vars to set."""
    if not _USE_DB or not user_id or user_id == "local":
        return {}
    if "account_fields" not in entry:
        return {}
    accounts = _db.get_server_accounts(user_id, server_name)
    if not accounts:
        return {}

    uid = user_id
    if uid not in _user_temp_dirs:
        _user_temp_dirs[uid] = _tempfile.mkdtemp(prefix=f"mcp-{uid[:8]}-")

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
                    print(f"[imap] OAuth token refresh failed for {cfg.get('name')}")
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
    app_dir = os.path.dirname(os.path.abspath(__file__))
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
            server_configs[name] = {
                "type": "stdio",
                "command": entry["command"],
                "args": args,
                "env": env,
            }
        elif server_type in ("sse", "http"):
            server_configs[name] = {
                "type": "sse",
                "url": entry["url"],
            }
    return server_configs


def _save_config(data: dict) -> None:
    """Save server config to disk."""
    _ensure_config_dir()
    path = os.path.join(_config_dir_path(), "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# File format parser / writer
# ---------------------------------------------------------------------------

VALID_PRIORITIES = {"low", "medium", "high", "none"}
DEFAULT_PRIORITY = "medium"
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3}


def _parse_todo_file(path: str) -> list[dict]:
    """Parse a todos.md file into a list of todo dicts.

    Tracks ## headers as section names and assigns them to subsequent todos.
    """
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        file_lines = f.readlines()

    todos: list[dict] = []
    current_section = ""
    i = 0
    while i < len(file_lines):
        line = file_lines[i].rstrip("\n")

        # Track section headers (## level only)
        if line.startswith("## "):
            current_section = line[3:].strip()
            i += 1
            continue

        # Skip h1 headers and blank lines
        if line.startswith("# ") or not line.strip():
            i += 1
            continue

        # Match todo item
        m = re.match(r"^- \[([ xX])\] (.+)", line)
        if m:
            checked = m.group(1).lower() == "x"
            first_line = m.group(2).strip()

            # Collect continuation lines (indented or blank)
            desc_raw_lines: list[str] = []
            i += 1
            while i < len(file_lines):
                cl = file_lines[i].rstrip("\n")
                if cl.startswith("  ") or cl.strip() == "":
                    desc_raw_lines.append(cl)
                    i += 1
                else:
                    break

            # Strip trailing blank lines from description
            while desc_raw_lines and not desc_raw_lines[-1].strip():
                desc_raw_lines.pop()

            # Extract id if present: <!-- id:xxxx -->
            id_match = re.search(r"<!-- id:(\S+?) -->", first_line)
            todo_id = id_match.group(1) if id_match else str(uuid.uuid4())[:8]
            if id_match:
                first_line = first_line.replace(id_match.group(0), "").strip()

            # Extract status tag: [critical], [in-progress], etc.
            status = "completed" if checked else "open"
            # Strip legacy status tags from title
            status_match = re.match(r"^\[(\S+?)\]\s*", first_line)
            if status_match:
                first_line = first_line[status_match.end() :].strip()

            # Extract priority tag: {high}, {medium}, {low}
            priority = DEFAULT_PRIORITY
            priority_match = re.search(r"\{(high|medium|low|none)\}", first_line, re.IGNORECASE)
            if priority_match:
                priority = priority_match.group(1).lower()
                first_line = first_line.replace(priority_match.group(0), "").strip()

            # Title is the remainder of first line (strip bold markers)
            title = first_line.strip("*").strip()

            # Description is remaining lines, de-indented
            desc_lines = []
            for dl in desc_raw_lines:
                stripped = dl.strip()
                if stripped:
                    desc_lines.append(re.sub(r"^  ", "", dl.rstrip()))
                else:
                    desc_lines.append("")
            description = "\n".join(desc_lines).strip()

            todos.append(
                {
                    "id": todo_id,
                    "title": title,
                    "description": description,
                    "status": status,
                    "priority": priority,
                    "section": current_section,
                }
            )
        else:
            i += 1

    return todos


def _write_todo_file(path: str, todos: list[dict]) -> None:
    """Write todos across two files: active items in the main file, completed in a -completed file."""
    active = [t for t in todos if t["status"] != "completed"]
    completed = [t for t in todos if t["status"] == "completed"]

    # Write active file — group by section, preserving order of first appearance
    lines = ["# Todo List", ""]
    sections_order: list[str] = []
    seen_sections: set[str] = set()
    for t in active:
        s = t.get("section", "")
        if s not in seen_sections:
            sections_order.append(s)
            seen_sections.add(s)

    for section in sections_order:
        if section:
            lines.append(f"## {section}")
            lines.append("")
        items = [t for t in active if t.get("section", "") == section]
        for t in items:
            lines.extend(_format_todo(t, checked=False))
            lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Write completed file
    comp_path = _completed_file_path(path)
    comp_lines = ["# Completed Todos", ""]
    for t in completed:
        comp_lines.extend(_format_todo(t, checked=True))
        comp_lines.append("")

    with open(comp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(comp_lines))


def _format_todo(t: dict, checked: bool) -> list[str]:
    """Format a single todo as markdown lines."""
    checkbox = "[x]" if checked else "[ ]"
    priority = t.get("priority", DEFAULT_PRIORITY)
    priority_tag = f" {{{priority}}}" if priority != DEFAULT_PRIORITY else ""
    id_tag = f" <!-- id:{t['id']} -->"

    title_line = f"- {checkbox} **{t['title']}**{priority_tag}{id_tag}"
    result = [title_line]

    if t.get("description"):
        for dline in t["description"].split("\n"):
            result.append(f"  {dline}")

    return result


def _snapshot_and_write(path: str, todos: list[dict]) -> None:
    """Snapshot current state for undo, then write new state."""
    old_active = _parse_todo_file(path)
    old_completed = _parse_todo_file(_completed_file_path(path))
    _undo_stack.append((copy.deepcopy(old_active), copy.deepcopy(old_completed)))
    _write_todo_file(path, todos)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    if not _USE_DB:
        return jsonify({"error": "Auth not available in file mode"}), 400
    data = request.json or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    name = (data.get("name") or "").strip()
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    try:
        user = _db.create_user(email, password, name or None)
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return jsonify({"error": "Email already registered"}), 409
        return jsonify({"error": str(exc)}), 500
    token = _db.create_session(user["id"])
    resp = jsonify({"user": user})
    resp.set_cookie("session_token", token, httponly=True, samesite="Lax",
                     max_age=60 * 60 * 24 * 30)  # 30 days
    return resp


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    if not _USE_DB:
        return jsonify({"error": "Auth not available in file mode"}), 400
    data = request.json or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    user = _db.verify_user(email, password)
    if not user:
        return jsonify({"error": "Invalid email or password"}), 401
    token = _db.create_session(user["id"])
    resp = jsonify({"user": user})
    resp.set_cookie("session_token", token, httponly=True, samesite="Lax",
                     max_age=60 * 60 * 24 * 30)
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = request.cookies.get("session_token")
    if token and _USE_DB:
        _db.delete_session(token)
    resp = jsonify({"ok": True})
    resp.delete_cookie("session_token")
    return resp


@app.route("/api/auth/me")
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify({"user": user})


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    user = get_current_user()
    if _USE_DB and not user:
        return Response(LOGIN_PAGE, mimetype="text/html")
    return Response(HTML_PAGE, mimetype="text/html")


@app.route("/api/todos", methods=["GET"])
def get_todos():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    if _USE_DB:
        return jsonify(_db.get_todos(user["id"]))
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    return jsonify(active + completed)


@app.route("/api/todos", methods=["POST"])
def add_todo():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    if _USE_DB:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "Title is required"}), 400
        todo = _db.create_todo(
            user["id"], title,
            description=(data.get("description") or "").strip(),
            priority=data.get("priority", DEFAULT_PRIORITY),
            section=(data.get("section") or "").strip(),
        )
        return jsonify(todo), 201

    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    new_todo = {
        "id": str(uuid.uuid4())[:8],
        "title": data.get("title", "").strip(),
        "description": data.get("description", "").strip(),
        "status": "open",
        "priority": data.get("priority", DEFAULT_PRIORITY),
        "section": data.get("section", "").strip(),
    }
    if not new_todo["title"]:
        return jsonify({"error": "Title is required"}), 400

    before_id = data.get("before_id")
    if before_id:
        idx = next((i for i, t in enumerate(active) if t["id"] == before_id), None)
        if idx is not None:
            active.insert(idx, new_todo)
        else:
            active.append(new_todo)
        todos = active + completed
    else:
        todos.append(new_todo)
    _snapshot_and_write(TODO_FILE, todos)
    return jsonify(new_todo), 201


@app.route("/api/todos/<todo_id>", methods=["PUT"])
def update_todo_route(todo_id):
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    if _USE_DB:
        fields = {}
        for k in ("title", "description", "status", "priority", "section"):
            if k in data:
                val = data[k]
                if k == "status" and val not in ("open", "completed"):
                    continue
                if k == "priority" and val not in VALID_PRIORITIES:
                    continue
                fields[k] = val.strip() if isinstance(val, str) else val
        result = _db.update_todo(user["id"], todo_id, **fields)
        if not result:
            return jsonify({"error": "Not found"}), 404
        return jsonify(result)

    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    for t in todos:
        if t["id"] == todo_id:
            if "title" in data:
                t["title"] = data["title"].strip()
            if "description" in data:
                t["description"] = data["description"].strip()
            if "status" in data and data["status"] in ("open", "completed"):
                t["status"] = data["status"]
            if "priority" in data and data["priority"] in VALID_PRIORITIES:
                t["priority"] = data["priority"]
            if "section" in data:
                t["section"] = data["section"].strip()
            _snapshot_and_write(TODO_FILE, todos)
            return jsonify(t)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/todos/<todo_id>/mark-read", methods=["POST"])
def mark_read(todo_id):
    """Replace `updated ...` tag with `read ...` and current timestamp."""
    import re as _re
    from datetime import datetime as _dt
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    for t in todos:
        if t["id"] == todo_id:
            now = _dt.now().strftime("%Y-%m-%d %H:%M")
            t["title"] = _re.sub(r'`updated[^`]*`', f'`read {now}`', t["title"])
            _snapshot_and_write(TODO_FILE, todos)
            return jsonify(t)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/todos/<todo_id>", methods=["DELETE"])
def delete_todo_route(todo_id):
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    if _USE_DB:
        if not _db.delete_todo(user["id"], todo_id):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"ok": True})
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    new_todos = [t for t in todos if t["id"] != todo_id]
    if len(new_todos) == len(todos):
        return jsonify({"error": "Not found"}), 404
    _snapshot_and_write(TODO_FILE, new_todos)
    return jsonify({"ok": True})


@app.route("/api/todos/reorder", methods=["POST"])
def reorder_todo():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    todo_id = data.get("id")
    direction = data.get("direction")  # "up" or "down"
    if not todo_id or direction not in ("up", "down"):
        return jsonify({"error": "id and direction (up/down) required"}), 400

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    # Find the item in active list
    idx = next((i for i, t in enumerate(active) if t["id"] == todo_id), None)
    if idx is None:
        return jsonify({"error": "Not found or not an active item"}), 404

    item = active[idx]
    item_section = item.get("section", "")

    # Build ordered list of sections (preserving first-appearance order)
    sections_order: list[str] = []
    seen: set[str] = set()
    for t in active:
        s = t.get("section", "")
        if s not in seen:
            sections_order.append(s)
            seen.add(s)

    # Get items in the same section
    section_items = [t for t in active if t.get("section", "") == item_section]
    pos_in_section = next(i for i, t in enumerate(section_items) if t["id"] == todo_id)

    if direction == "down":
        if pos_in_section < len(section_items) - 1:
            # Swap within section: find next same-section item in the flat list
            cur_flat = idx
            nxt_flat = cur_flat + 1
            while nxt_flat < len(active) and active[nxt_flat].get("section", "") != item_section:
                nxt_flat += 1
            if nxt_flat < len(active):
                active[cur_flat], active[nxt_flat] = active[nxt_flat], active[cur_flat]
            else:
                return jsonify({"ok": True, "moved": False})
        else:
            # At bottom of section — move to adjacent section below
            sec_idx = sections_order.index(item_section)
            if sec_idx + 1 >= len(sections_order):
                return jsonify({"ok": True, "moved": False})
            new_section = sections_order[sec_idx + 1]
            item["section"] = new_section
            # Move item to the top of the new section
            active.pop(idx)
            first_in_new = next((i for i, t in enumerate(active) if t.get("section", "") == new_section), len(active))
            active.insert(first_in_new, item)
    else:  # direction == "up"
        if pos_in_section > 0:
            # Swap within section: find previous same-section item in the flat list
            cur_flat = idx
            prev_flat = cur_flat - 1
            while prev_flat >= 0 and active[prev_flat].get("section", "") != item_section:
                prev_flat -= 1
            if prev_flat >= 0:
                active[cur_flat], active[prev_flat] = active[prev_flat], active[cur_flat]
            else:
                return jsonify({"ok": True, "moved": False})
        else:
            # At top of section — move to adjacent section above
            sec_idx = sections_order.index(item_section)
            if sec_idx - 1 < 0:
                return jsonify({"ok": True, "moved": False})
            new_section = sections_order[sec_idx - 1]
            item["section"] = new_section
            # Move item to the bottom of the new section
            active.pop(idx)
            # Find last item in the new section
            last_in_new = -1
            for i, t in enumerate(active):
                if t.get("section", "") == new_section:
                    last_in_new = i
            active.insert(last_in_new + 1, item)

    if _USE_DB:
        for i, t in enumerate(active):
            t["position"] = i
        _db.bulk_update_todos(user["id"], active)
    else:
        _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True, "moved": True})


@app.route("/api/todos/move-to-top", methods=["POST"])
def move_to_top():
    """Move a todo to the top of its section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    todo_id = data.get("id")
    if not todo_id:
        return jsonify({"error": "id required"}), 400

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    idx = next((i for i, t in enumerate(active) if t["id"] == todo_id), None)
    if idx is None:
        return jsonify({"error": "Not found or not an active item"}), 404

    item = active[idx]
    section = item.get("section", "")

    # Find the first item in the same section
    first_idx = next(i for i, t in enumerate(active) if t.get("section", "") == section)
    if idx == first_idx:
        return jsonify({"ok": True, "moved": False})

    # Remove from current position, insert at the top of the section
    active.pop(idx)
    active.insert(first_idx, item)

    if _USE_DB:
        for i, t in enumerate(active):
            t["position"] = i
        _db.bulk_update_todos(user["id"], active)
    else:
        _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True, "moved": True})


@app.route("/api/todos/sort-priority", methods=["POST"])
def sort_by_priority():
    """Sort todos by priority within a given section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    section = data.get("section", "")

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    # Separate items in the target section from others, preserving order
    section_items = []
    other_items = []
    for t in active:
        if t.get("section", "") == section:
            section_items.append(t)
        else:
            other_items.append(t)

    # Sort the section items by priority
    section_items.sort(key=lambda t: PRIORITY_ORDER.get(t.get("priority", DEFAULT_PRIORITY), 1))

    # Rebuild active list: insert sorted section items back in position
    rebuilt = []
    inserted = False
    for t in active:
        if t.get("section", "") == section:
            if not inserted:
                rebuilt.extend(section_items)
                inserted = True
        else:
            rebuilt.append(t)
    if not inserted:
        rebuilt.extend(section_items)

    if _USE_DB:
        for i, t in enumerate(rebuilt):
            t["position"] = i
        _db.bulk_update_todos(user["id"], rebuilt)
    else:
        _snapshot_and_write(TODO_FILE, rebuilt + completed)
    return jsonify({"ok": True})


@app.route("/api/todos/drop", methods=["POST"])
def drop_todo():
    """Move a todo to a specific position: before another item, or to the end of a section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    todo_id = data.get("id")
    before_id = data.get("before_id")  # insert before this item (None = end of section)
    target_section = data.get("section")  # required if before_id is None

    if not todo_id:
        return jsonify({"error": "id required"}), 400

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    idx = next((i for i, t in enumerate(active) if t["id"] == todo_id), None)
    if idx is None:
        return jsonify({"error": "Not found or not an active item"}), 404
    item = active.pop(idx)

    if before_id:
        target_idx = next((i for i, t in enumerate(active) if t["id"] == before_id), None)
        if target_idx is not None:
            item["section"] = active[target_idx].get("section", "")
            active.insert(target_idx, item)
        else:
            active.append(item)
    elif target_section is not None:
        item["section"] = target_section
        last_in_section = -1
        for i, t in enumerate(active):
            if t.get("section", "") == target_section:
                last_in_section = i
        active.insert(last_in_section + 1, item)
    else:
        active.append(item)

    if _USE_DB:
        for i, t in enumerate(active):
            t["position"] = i
        _db.bulk_update_todos(user["id"], active)
    else:
        _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True})


@app.route("/api/sections/rename", methods=["POST"])
def rename_section():
    """Rename a section header across all todos."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "old_name and new_name required"}), 400
    if old_name == new_name:
        return jsonify({"ok": True})

    if _USE_DB:
        todos = _db.get_todos(user["id"])
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))
        todos = active + completed
    changed = False
    for t in todos:
        if t.get("section", "") == old_name:
            t["section"] = new_name
            changed = True
    if not changed:
        return jsonify({"error": "Section not found"}), 404
    if _USE_DB:
        _db.bulk_update_todos(user["id"], [t for t in todos if t.get("section") == new_name])
    else:
        _snapshot_and_write(TODO_FILE, todos)
    return jsonify({"ok": True})


@app.route("/api/sections/reorder", methods=["POST"])
def reorder_section():
    """Move a section (and all its todos) before another section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    section = (data.get("section") or "").strip()
    before_section = data.get("before_section")  # None = move to end

    if not section:
        return jsonify({"error": "section required"}), 400

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    # Build current section order
    sections_order = []
    seen = set()
    for t in active:
        s = t.get("section", "")
        if s not in seen:
            sections_order.append(s)
            seen.add(s)

    if section not in sections_order:
        return jsonify({"error": "Section not found"}), 404

    # Remove the section from its current position
    sections_order.remove(section)

    # Insert before the target section, or at the end
    if before_section is not None:
        before_section = before_section.strip()
        if before_section in sections_order:
            idx = sections_order.index(before_section)
            sections_order.insert(idx, section)
        else:
            sections_order.append(section)
    else:
        sections_order.append(section)

    # Rebuild the active list in the new section order
    section_groups = {}
    for t in active:
        s = t.get("section", "")
        section_groups.setdefault(s, []).append(t)

    rebuilt = []
    for s in sections_order:
        rebuilt.extend(section_groups.get(s, []))

    if _USE_DB:
        for i, t in enumerate(rebuilt):
            t["position"] = i
        _db.bulk_update_todos(user["id"], rebuilt)
    else:
        _snapshot_and_write(TODO_FILE, rebuilt + completed)
    return jsonify({"ok": True})


@app.route("/api/todos/mtime", methods=["GET"])
def get_mtime():
    """Return the max modification time across both files for change detection."""
    mtime = 0
    for p in (TODO_FILE, _completed_file_path(TODO_FILE)):
        try:
            mtime = max(mtime, os.path.getmtime(p))
        except OSError:
            pass
    return jsonify({"mtime": mtime})


@app.route("/api/undo", methods=["POST"])
def undo():
    """Restore the previous file state from the undo stack."""
    if not _undo_stack:
        return jsonify({"error": "Nothing to undo"}), 400
    old_active, old_completed = _undo_stack.pop()
    _write_todo_file(TODO_FILE, old_active + old_completed)
    return jsonify({"ok": True})


def _run_claude_job(job_id: str, prompt: str, cwd: str):
    """Thread target: run claude -p as a subprocess and parse stream-json output."""
    claude_bin, env = _resolve_claude_bin()
    if not claude_bin:
        _jobs[job_id]["output_lines"].append("error: claude binary not found")
        _jobs[job_id]["status"] = "error"
        return

    cmd = [claude_bin, "-p", prompt, "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose",
           "--effort", "low", "--include-partial-messages"]
    _jobs[job_id]["status"] = "running"

    def emit(line: str) -> None:
        if line.strip():
            _jobs[job_id]["output_lines"].append(line)

    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                start_new_session=True)
        _jobs[job_id]["proc"] = proc

        text_buf = ""       # accumulates text_delta fragments until a newline or block end
        got_streaming = False  # True if we receive content_block_delta events

        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                emit(raw_line[:200])
                continue

            t = data.get("type", "")

            if t == "content_block_start":
                block = data.get("content_block", {})
                if block.get("type") == "tool_use":
                    if text_buf.strip():
                        emit(text_buf.strip())
                        text_buf = ""
                    emit(f"▶ {block.get('name', '?')}...")
                elif block.get("type") == "text" and text_buf.strip():
                    emit(text_buf.strip())
                    text_buf = ""

            elif t == "content_block_delta":
                got_streaming = True
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_buf += delta.get("text", "")
                    while "\n" in text_buf:
                        line, text_buf = text_buf.split("\n", 1)
                        emit(line)

            elif t == "content_block_stop":
                if text_buf.strip():
                    emit(text_buf.strip())
                    text_buf = ""

            elif t == "assistant" and not got_streaming:
                # Fallback: no streaming events, parse the complete assistant message
                parts = []
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text = block["text"].strip()
                        if text:
                            parts.append(text)
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))[:80]
                        parts.append(f"▶ {name}({inp})")
                for part in parts:
                    for line in part.splitlines():
                        emit(line)

            elif t == "result":
                result = data.get("result", "").strip()
                cost = data.get("cost_usd")
                cost_str = f" — ${cost:.4f}" if cost else ""
                emit(f"✓ Done{cost_str}" + (f": {result}" if result else ""))

            # Skip: user, tool_result, system, debug, rate_limit_event

        if text_buf.strip():
            emit(text_buf.strip())

        proc.wait()
        if _jobs[job_id]["status"] != "killed":
            _jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        _jobs[job_id]["output_lines"].append(f"error: {exc}")
        _jobs[job_id]["status"] = "error"


# ---------------------------------------------------------------------------
# Server-side agentic loop (Anthropic SDK)
# ---------------------------------------------------------------------------

def _get_tool_definitions(depth: int = 0, user_id: str | None = None) -> list[dict]:
    """Return Claude API tool definitions for server-side tools."""
    tools = [
        {
            "name": "read_todos",
            "description": "Read all todo items (active and completed). Returns a JSON array of todo objects with id, title, description, status, priority, and section fields.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "enum": ["all", "open", "completed"],
                        "description": "Filter by status. Default: all"
                    }
                },
                "required": []
            }
        },
        {
            "name": "update_todo",
            "description": "Update a todo item's fields (title, description, status, priority, section).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The todo item ID"},
                    "title": {"type": "string", "description": "New title"},
                    "description": {"type": "string", "description": "New description (markdown)"},
                    "status": {"type": "string", "enum": ["open", "completed"]},
                    "priority": {"type": "string", "enum": ["high", "medium", "low", "none"]},
                    "section": {"type": "string", "description": "Section/category name"}
                },
                "required": ["todo_id"]
            }
        },
        {
            "name": "create_todo",
            "description": "Create a new todo item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Todo title"},
                    "description": {"type": "string", "description": "Todo description (markdown)"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low", "none"]},
                    "section": {"type": "string", "description": "Section/category name"}
                },
                "required": ["title"]
            }
        },
        {
            "name": "search_todos",
            "description": "Search todos by text query across titles and descriptions.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        },
        {
            "name": "read_chat_history",
            "description": "Read the chat history for a specific todo item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The todo item ID"}
                },
                "required": ["todo_id"]
            }
        },
    ]
    if _USE_DB and user_id and user_id != "local":
        config = _db.get_config(user_id)
    else:
        config = _load_config()
    if depth < 2 and config.get("subagents_enabled", True):
        tools.append({
            "name": "spawn_agents",
            "description": "Launch multiple subagents in parallel in a SINGLE call. Pass ALL agents in the 'agents' array — "
                           "they run concurrently via a thread pool. Do NOT call this tool multiple times sequentially; "
                           "instead, batch all independent tasks into one call. Each subagent gets its own prompt, "
                           "full tool access (including MCP), and returns results. "
                           "This is the 'Agent tool' referenced in the EA skill instructions.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string", "description": "Complete instructions for this subagent. Include all context it needs — it has no access to the parent conversation."},
                                "label": {"type": "string", "description": "Short label for progress output (e.g., 'Slack sweep')."},
                                "model": {"type": "string", "description": "Optional model override. Defaults to the parent's model."}
                            },
                            "required": ["prompt"]
                        },
                        "description": "Array of agent specs to launch in parallel."
                    }
                },
                "required": ["agents"]
            }
        })
    return tools


def _execute_tool(name: str, input_data: dict, todo_id: str | None,
                  agent_context: dict | None = None) -> str:
    """Execute a server-side tool and return the result as a string.

    agent_context: {job_id, provider, depth} — passed when called from ChatAgent.
    """
    depth = agent_context.get("depth", 0) if agent_context else 0
    prefix = f"[tool d={depth}]"
    print(f"{prefix} {name}({json.dumps(input_data)[:200]})")

    # Resolve user_id from agent context for DB-aware operations
    user_id = None
    if agent_context and agent_context.get("job_id"):
        user_id = _jobs.get(agent_context["job_id"], {}).get("user_id")

    # In DB mode, require user_id — never fall through to file-based operations
    if _USE_DB and not user_id and name not in ("spawn_agents",):
        return json.dumps({"error": "Not authenticated"})

    try:
        if name == "read_todos":
            if _USE_DB and user_id:
                todos = _db.get_todos(user_id)
            else:
                active = _parse_todo_file(TODO_FILE)
                completed = _parse_todo_file(_completed_file_path(TODO_FILE))
                todos = active + completed
            status_filter = input_data.get("status_filter", "all")
            if status_filter == "open":
                todos = [t for t in todos if t["status"] != "completed"]
            elif status_filter == "completed":
                todos = [t for t in todos if t["status"] == "completed"]
            return json.dumps(todos, ensure_ascii=False)

        elif name == "update_todo":
            tid = input_data["todo_id"]
            if _USE_DB and user_id:
                fields = {}
                for k in ("title", "description", "status", "priority", "section"):
                    if k in input_data:
                        val = input_data[k]
                        if k == "status" and val not in ("open", "completed"):
                            continue
                        if k == "priority" and val not in VALID_PRIORITIES:
                            continue
                        fields[k] = val.strip() if isinstance(val, str) else val
                result = _db.update_todo(user_id, tid, **fields)
                if not result:
                    return json.dumps({"error": f"Todo {tid} not found"})
                return json.dumps(result, ensure_ascii=False)
            else:
                active = _parse_todo_file(TODO_FILE)
                completed = _parse_todo_file(_completed_file_path(TODO_FILE))
                todos = active + completed
                for t in todos:
                    if t["id"] == tid:
                        for field in ("title", "description", "status", "priority", "section"):
                            if field in input_data:
                                val = input_data[field]
                                if field == "status" and val not in ("open", "completed"):
                                    continue
                                if field == "priority" and val not in VALID_PRIORITIES:
                                    continue
                                t[field] = val.strip() if isinstance(val, str) else val
                        _snapshot_and_write(TODO_FILE, todos)
                        return json.dumps(t, ensure_ascii=False)
                return json.dumps({"error": f"Todo {tid} not found"})

        elif name == "create_todo":
            title = (input_data.get("title") or "").strip()
            if not title:
                return json.dumps({"error": "Title is required"})
            if _USE_DB and user_id:
                new_todo = _db.create_todo(
                    user_id, title,
                    description=(input_data.get("description") or "").strip(),
                    priority=input_data.get("priority", DEFAULT_PRIORITY),
                    section=(input_data.get("section") or "").strip(),
                )
                return json.dumps(new_todo, ensure_ascii=False)
            else:
                active = _parse_todo_file(TODO_FILE)
                completed = _parse_todo_file(_completed_file_path(TODO_FILE))
                todos = active + completed
                new_todo = {
                    "id": str(uuid.uuid4())[:8],
                    "title": title,
                    "description": (input_data.get("description") or "").strip(),
                    "status": "open",
                    "priority": input_data.get("priority", DEFAULT_PRIORITY),
                    "section": (input_data.get("section") or "").strip(),
                }
                todos.append(new_todo)
                _snapshot_and_write(TODO_FILE, todos)
                return json.dumps(new_todo, ensure_ascii=False)

        elif name == "search_todos":
            query = input_data.get("query", "").lower()
            if _USE_DB and user_id:
                results = _db.search_todos(user_id, query)
            else:
                active = _parse_todo_file(TODO_FILE)
                completed = _parse_todo_file(_completed_file_path(TODO_FILE))
                results = [t for t in active + completed
                           if query in t.get("title", "").lower()
                           or query in t.get("description", "").lower()]
            return json.dumps(results, ensure_ascii=False)

        elif name == "read_chat_history":
            tid = input_data.get("todo_id", todo_id)
            if _USE_DB:
                messages = _db.get_messages(tid)
                return json.dumps(messages[-20:], ensure_ascii=False)
            else:
                chats = _load_chats()
                chat = chats.get(tid, {"messages": []})
                return json.dumps(chat.get("messages", [])[-20:], ensure_ascii=False)

        elif name == "spawn_agents":
            if not agent_context:
                return json.dumps({"error": "spawn_agents requires agent context"})
            agents = input_data.get("agents", [])
            print(f"[spawn_agents] Received {len(agents)} agent(s): {[a.get('label', '?') for a in agents]}")
            if not agents:
                return json.dumps({"error": "No agents specified"})
            if _USE_DB and user_id:
                config = _db.get_config(user_id)
            else:
                config = _load_config()
            max_subagents = config.get("max_subagents", 10)
            if len(agents) > max_subagents:
                return json.dumps({"error": f"Maximum {max_subagents} parallel agents"})
            depth = agent_context.get("depth", 0)
            if depth >= 2:
                return json.dumps({"error": "Maximum agent nesting depth reached"})
            return _execute_spawn_agents(agents, agent_context, todo_id)

        else:
            # Delegate to MCP if it's an MCP tool
            if not user_id and agent_context and agent_context.get("job_id"):
                user_id = _jobs.get(agent_context["job_id"], {}).get("user_id")
            mgr = _get_mcp_manager(user_id)
            if mgr and mgr.is_mcp_tool(name):
                # Check runtime permission before executing
                if _USE_DB and user_id and user_id != "local":
                    permission = _check_tool_permission(user_id, name, input_data, agent_context)
                    if permission == "denied":
                        return json.dumps({"error": f"Tool '{name}' was denied by the user"})
                return mgr.call_tool(name, input_data)
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as exc:
        print(f"{prefix} {name} ERROR: {exc}")
        return json.dumps({"error": str(exc)})



def _execute_spawn_agents(agents: list[dict], agent_context: dict, todo_id: str | None) -> str:
    """Launch subagents in parallel and return their results."""
    job_id = agent_context["job_id"]
    provider = agent_context["provider"]
    depth = agent_context.get("depth", 0)

    _jobs[job_id]["output_lines"].append(f"⚡ Launching {len(agents)} subagent(s)...")

    results = []
    config = _load_config()
    max_workers = config.get("max_subagents", 10)
    with ThreadPoolExecutor(max_workers=min(len(agents), max_workers)) as executor:
        futures = {}
        for i, spec in enumerate(agents):
            label = spec.get("label", f"agent-{i+1}")
            future = executor.submit(
                _run_subagent,
                job_id=job_id, todo_id=todo_id, provider=provider,
                prompt=spec["prompt"], label=label, depth=depth + 1,
            )
            futures[future] = label

        for future in as_completed(futures, timeout=600):
            try:
                result = future.result(timeout=300)
                results.append(result)
            except Exception as exc:
                results.append({
                    "label": futures[future], "result": "",
                    "error": str(exc), "input_tokens": 0, "output_tokens": 0,
                })

    total_in = sum(r.get("input_tokens", 0) for r in results)
    total_out = sum(r.get("output_tokens", 0) for r in results)
    _jobs[job_id]["output_lines"].append(
        f"✓ All {len(results)} subagent(s) complete (tokens: {total_in}+{total_out})"
    )
    return json.dumps({"agents": results}, ensure_ascii=False)


def _run_subagent(job_id: str, todo_id: str | None, provider: dict,
                  prompt: str, label: str, depth: int) -> dict:
    """Run a single subagent to completion. Returns {label, result, error, tokens}."""
    ptype = provider.get("type", "local")
    model = provider.get("model", "claude-sonnet-4-20250514")
    job = _jobs[job_id]

    def emit(line: str):
        if line.strip():
            job["output_lines"].append(f"[{label}] {line}")

    emit(f"Starting ({model})...")
    result_lines = []
    total_input = 0
    total_output = 0

    try:
        system_prompt = _build_system_prompt(todo_id, job.get("user_id"))
        messages_api = [{"role": "user", "content": prompt}]
        tools = _get_tool_definitions(depth, job.get("user_id"))
        tools = tools + _get_mcp_tools(job.get("user_id"))
        agent_ctx = {"job_id": job_id, "provider": provider, "depth": depth}

        if ptype == "anthropic":
            api_key = provider.get("api_key")
            if not api_key or not anthropic:
                return {"label": label, "result": "", "error": "Anthropic API not configured",
                        "input_tokens": 0, "output_tokens": 0}
            client = anthropic.Anthropic(api_key=api_key)

            for _ in range(20):
                if job["status"] == "killed":
                    return {"label": label, "result": "\n".join(result_lines),
                            "error": "killed", "input_tokens": total_input, "output_tokens": total_output}
                response = client.messages.create(
                    model=model, system=system_prompt, messages=messages_api,
                    max_tokens=8192, tools=tools,
                )
                if response.usage:
                    total_input += response.usage.input_tokens
                    total_output += response.usage.output_tokens

                for block in response.content:
                    if block.type == "text" and block.text.strip():
                        for ln in block.text.strip().splitlines():
                            emit(ln)
                            result_lines.append(ln)

                if response.stop_reason == "tool_use":
                    tool_results = []
                    assistant_content = []
                    for block in response.content:
                        if block.type == "text":
                            assistant_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            assistant_content.append({
                                "type": "tool_use", "id": block.id,
                                "name": block.name, "input": block.input
                            })
                            emit(f"▶ {block.name}...")
                            result = _execute_tool(block.name, block.input, todo_id, agent_ctx)
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": block.id, "content": result
                            })
                    messages_api.append({"role": "assistant", "content": assistant_content})
                    messages_api.append({"role": "user", "content": tool_results})
                    continue
                break

        elif ptype == "openai_compat":
            base_url = provider.get("base_url")
            api_key = provider.get("api_key", "none")
            if not base_url or not openai_mod:
                return {"label": label, "result": "", "error": "OpenAI endpoint not configured",
                        "input_tokens": 0, "output_tokens": 0}
            client = openai_mod.OpenAI(base_url=base_url, api_key=api_key)
            oai_messages = [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt}]
            oai_tools = [{
                "type": "function",
                "function": {"name": t["name"], "description": t.get("description", ""),
                             "parameters": t.get("input_schema", {"type": "object", "properties": {}})}
            } for t in tools]
            max_tokens = min(provider.get("max_tokens", 4096), 4096)

            for _ in range(20):
                if job["status"] == "killed":
                    return {"label": label, "result": "\n".join(result_lines),
                            "error": "killed", "input_tokens": total_input, "output_tokens": total_output}
                kwargs = {"model": model, "messages": oai_messages, "max_tokens": max_tokens}
                if oai_tools and provider.get("tool_use", True):
                    kwargs["tools"] = oai_tools
                resp = client.chat.completions.create(**kwargs)
                if resp.usage:
                    total_input += resp.usage.prompt_tokens or 0
                    total_output += resp.usage.completion_tokens or 0
                choice = resp.choices[0]
                msg = choice.message
                if msg.content:
                    cleaned = _strip_think_tags(msg.content)
                    if cleaned:
                        for ln in cleaned.splitlines():
                            emit(ln)
                            result_lines.append(ln)
                if msg.tool_calls:
                    assistant_msg = {"role": "assistant", "content": msg.content or ""}
                    assistant_msg["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ]
                    oai_messages.append(assistant_msg)
                    for tc in msg.tool_calls:
                        emit(f"▶ {tc.function.name}...")
                        try:
                            args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            args = {}
                        result = _execute_tool(tc.function.name, args, todo_id, agent_ctx)
                        oai_messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                    continue
                break

        emit("Done.")
        return {"label": label, "result": "\n".join(result_lines), "error": None,
                "input_tokens": total_input, "output_tokens": total_output}

    except Exception as exc:
        emit(f"Error: {str(exc)[:200]}")
        return {"label": label, "result": "\n".join(result_lines), "error": str(exc),
                "input_tokens": total_input, "output_tokens": total_output}


def _build_system_prompt(todo_id: str | None, user_id: str | None = None) -> str:
    """Build a system prompt from per-user DB config or file-based fallback."""
    parts = []

    if _USE_DB and user_id:
        config = _db.get_config(user_id)
        # 1. Base system prompt from DB
        sp = config.get("system_prompt")
        if sp and sp.strip():
            parts.append(sp.strip())
        else:
            parts.append(_DEFAULT_SYSTEM_PROMPT.strip())
        # 2. Context files from user_context_files table
        ctx = _db.get_context_files(user_id)
        for name in sorted(ctx.keys()):
            content = ctx[name]
            if content and content.strip():
                parts.append(f"# {name}\n{content.strip()}")
    else:
        # File-based fallback
        config_dir = _config_dir_path()
        # 1. Base system prompt
        prompt_path = os.path.join(config_dir, "system-prompt.md")
        if os.path.exists(prompt_path):
            try:
                with open(prompt_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append(content)
            except OSError:
                pass
        if not parts:
            parts.append(_DEFAULT_SYSTEM_PROMPT.strip())
        # 2. Context files from disk
        context_dir = os.path.join(config_dir, "context")
        if os.path.isdir(context_dir):
            for name in sorted(os.listdir(context_dir)):
                if name.endswith(".md"):
                    fp = os.path.join(context_dir, name)
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                        if content:
                            parts.append(f"# {name}\n{content}")
                    except OSError:
                        pass

    # 3. Current date
    parts.append(f"Today's date is {datetime.now().strftime('%Y-%m-%d')}.")

    # 4. Todo context
    if todo_id:
        parts.append(f"Current conversation is for todo item ID: {todo_id}")

    return "\n\n---\n\n".join(parts)


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from model output."""
    return re.sub(r'<think>[\s\S]*?</think>\s*', '', text).strip()


def _openai_tool_defs(depth: int = 0, user_id: str | None = None) -> list[dict]:
    """Convert Anthropic-format tool definitions to OpenAI function-calling format."""
    tools = _get_tool_definitions(depth, user_id)
    tools = tools + _get_mcp_tools(user_id)
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        }
    } for t in tools]


# ---------------------------------------------------------------------------
# Agent Architecture
# ---------------------------------------------------------------------------
#
# The agentic system has three layers:
#
#   Browser ──SSE──> Server (Flask)
#                      │
#                      ├── ChatAgent (parent, depth=0)
#                      │     ├── Calls Claude API or OpenAI-compat API
#                      │     ├── Streams text to job["output_lines"] → SSE → browser
#                      │     ├── Executes tools: read_todos, update_todo, MCP, etc.
#                      │     └── Can call spawn_agents tool → launches subagents
#                      │
#                      └── _run_subagent (child, depth=1)
#                            ├── Fresh messages (no parent history)
#                            ├── Same system prompt + tools + MCP access
#                            ├── Emits progress as [label] prefixed lines
#                            ├── Returns {label, result, error, tokens}
#                            └── Cannot spawn further subagents (depth >= 2)
#
# Flow for /ea update:
#   1. User sends "/ea update" → ChatAgent starts (depth=0)
#   2. Model reads EA skill instructions from system prompt
#   3. Model calls spawn_agents with 5 agents: Slack, Email, Calendar, Notes, Jira
#   4. _execute_spawn_agents launches 5 threads via ThreadPoolExecutor
#   5. Each _run_subagent makes its own API calls, uses MCP tools (Slack, IMAP, etc.)
#   6. Progress streams to parent job: [Slack sweep] Checking unreads...
#   7. All subagents complete → results returned as tool_result to parent
#   8. Parent model merges results, updates todos.md, commits
#
# Key properties:
#   - Provider-agnostic: both Anthropic and OpenAI-compat supported at all levels
#   - Kill propagation: subagents check job["status"] == "killed" each iteration
#   - Thread safety: output_lines.append() is GIL-safe; file writes serialized
#   - No persistence: subagent conversations are ephemeral (parent persists)
#   - Configurable: subagents_enabled toggle in config; model override per subagent
# ---------------------------------------------------------------------------


class ChatAgent:
    """Unified agentic loop for both Anthropic and OpenAI-compatible providers.

    Handles: message history, system prompt, streaming, tool execution,
    output emission, persistence. Provider-specific logic is in _call_anthropic
    and _call_openai.
    """

    def __init__(self, job_id: str, todo_id: str | None, provider: dict, depth: int = 0):
        self.job_id = job_id
        self.todo_id = todo_id
        self.provider = provider
        self.ptype = provider.get("type", "local")
        self.model = provider.get("model", "claude-sonnet-4-20250514")
        self.depth = depth
        self.user_id = _jobs.get(job_id, {}).get("user_id")
        self.assistant_text_lines: list[str] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    @property
    def job(self):
        return _jobs[self.job_id]

    def emit(self, line: str, is_text: bool = False) -> None:
        if line.strip():
            self.job["output_lines"].append(line)
            if is_text:
                self.assistant_text_lines.append(line)

    def is_killed(self) -> bool:
        return self.job["status"] == "killed"

    def _build_history(self, message: str) -> list[dict]:
        """Build messages array from persisted history + new message."""
        chats = _load_chats()
        chat = chats.get(self.todo_id, {"messages": []}) if self.todo_id else {"messages": []}
        messages = []
        for m in chat.get("messages", []):
            role = m.get("role", "user")
            content = m.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        return messages

    def _get_tools_anthropic(self) -> list[dict]:
        tools = _get_tool_definitions(self.depth, self.user_id)
        tools = tools + _get_mcp_tools(self.user_id)
        return tools

    def _get_tools_openai(self) -> list[dict]:
        return _openai_tool_defs(self.depth, self.user_id)

    def _persist_response(self) -> None:
        """Persist assistant response to chats/DB and mark unread."""
        if self.todo_id and self.assistant_text_lines:
            try:
                content = "\n".join(self.assistant_text_lines)
                if _USE_DB:
                    user_id = self.job.get("user_id")
                    _db.add_message(self.todo_id, user_id, "assistant", content)
                else:
                    chats = _load_chats()
                    chat = chats.get(self.todo_id, {"conversationId": None, "messages": []})
                    chat["messages"].append({"role": "assistant", "content": content})
                    chat["unread"] = True
                    chats[self.todo_id] = chat
                    _save_chats(chats)
            except Exception as exc:
                print(f"[persist] ERROR saving chat for {self.todo_id}: {exc}")

    # ------------------------------------------------------------------
    # Anthropic provider
    # ------------------------------------------------------------------

    def _run_anthropic(self, message: str) -> None:
        api_key = self.provider.get("api_key")
        if not api_key or not anthropic:
            self.emit("error: Anthropic API not configured")
            self.job["status"] = "error"
            return

        client = anthropic.Anthropic(api_key=api_key)
        messages = self._build_history(message)
        tools = self._get_tools_anthropic()
        system_prompt = _build_system_prompt(self.todo_id, self.user_id)

        while not self.is_killed():
            text_buf = ""
            with client.messages.stream(
                model=self.model,
                system=system_prompt,
                messages=messages,
                max_tokens=8192,
                tools=tools,
            ) as stream:
                self.job["_stream"] = stream
                for event in stream:
                    if self.is_killed():
                        stream.close()
                        return
                    if event.type == "content_block_start":
                        if hasattr(event, "content_block"):
                            block = event.content_block
                            if block.type == "tool_use":
                                if text_buf.strip():
                                    for ln in text_buf.strip().splitlines():
                                        self.emit(ln, is_text=True)
                                    text_buf = ""
                                self.emit(f"▶ {block.name}...")
                            elif block.type == "text" and text_buf.strip():
                                for ln in text_buf.strip().splitlines():
                                    self.emit(ln, is_text=True)
                                text_buf = ""
                    elif event.type == "content_block_delta":
                        if hasattr(event, "delta") and event.delta.type == "text_delta":
                            text_buf += event.delta.text
                            while "\n" in text_buf:
                                line, text_buf = text_buf.split("\n", 1)
                                self.emit(line, is_text=True)
                    elif event.type == "content_block_stop":
                        if text_buf.strip():
                            for ln in text_buf.strip().splitlines():
                                self.emit(ln, is_text=True)
                            text_buf = ""
                if text_buf.strip():
                    for ln in text_buf.strip().splitlines():
                        self.emit(ln, is_text=True)

            self.job.pop("_stream", None)
            response = stream.get_final_message()

            if response.usage:
                self.total_input_tokens += response.usage.input_tokens
                self.total_output_tokens += response.usage.output_tokens

            if response.stop_reason == "tool_use":
                tool_results = []
                assistant_content = []
                for block in response.content:
                    if block.type == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        assistant_content.append({
                            "type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input
                        })
                        result = _execute_tool(block.name, block.input, self.todo_id,
                                              agent_context={"job_id": self.job_id, "provider": self.provider, "depth": self.depth})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result
                        })
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": tool_results})
                continue
            break  # end_turn or max_tokens

    # ------------------------------------------------------------------
    # OpenAI-compatible provider
    # ------------------------------------------------------------------

    def _run_openai(self, message: str) -> None:
        base_url = self.provider.get("base_url")
        api_key = self.provider.get("api_key", "none")
        if not base_url or not openai_mod:
            self.emit("error: OpenAI-compatible endpoint not configured")
            self.job["status"] = "error"
            return

        client = openai_mod.OpenAI(base_url=base_url, api_key=api_key)
        system_prompt = _build_system_prompt(self.todo_id, self.user_id)
        messages = [{"role": "system", "content": system_prompt}] + self._build_history(message)
        tools = self._get_tools_openai()
        max_tokens = min(self.provider.get("max_tokens", 4096), 4096)

        for _ in range(10):  # max iterations
            if self.is_killed():
                return
            kwargs = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
            if tools and self.provider.get("tool_use", True):
                kwargs["tools"] = tools

            response = client.chat.completions.create(**kwargs)

            if response.usage:
                self.total_input_tokens += response.usage.prompt_tokens or 0
                self.total_output_tokens += response.usage.completion_tokens or 0

            choice = response.choices[0]
            msg = choice.message

            if msg.content:
                cleaned = _strip_think_tags(msg.content)
                if cleaned:
                    for line in cleaned.splitlines():
                        self.emit(line, is_text=True)

            if msg.tool_calls:
                assistant_msg = {"role": "assistant", "content": msg.content or ""}
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
                messages.append(assistant_msg)
                for tc in msg.tool_calls:
                    self.emit(f"▶ {tc.function.name}...")
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    result = _execute_tool(tc.function.name, args, self.todo_id,
                                          agent_context={"job_id": self.job_id, "provider": self.provider, "depth": self.depth})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue
            break  # No tool calls — done

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, message: str) -> None:
        """Run the agentic loop. Called from a thread."""
        self.job["status"] = "running"
        try:
            if self.ptype == "anthropic":
                self._run_anthropic(message)
            elif self.ptype == "openai_compat":
                self._run_openai(message)
            else:
                self.emit(f"error: unknown provider type '{self.ptype}'")
                self.job["status"] = "error"
                return

            if self.is_killed():
                return

            # Cost/token summary
            if self.ptype == "anthropic":
                cost = (self.total_input_tokens * 3.0 + self.total_output_tokens * 15.0) / 1_000_000
                self.emit(f"✓ Done — ${cost:.4f}" if cost > 0 else "✓ Done")
            else:
                self.emit(f"✓ Done (tokens: {self.total_input_tokens}+{self.total_output_tokens})")

            self.job["status"] = "done"
            self._persist_response()

        except Exception as exc:
            self.job.pop("_stream", None)
            if not self.is_killed():
                exc_str = str(exc)
                # Provide actionable detail for common errors
                if "max_tokens" in exc_str or "context_length" in exc_str or "too long" in exc_str.lower() or "maximum" in exc_str.lower():
                    hist_count = len(self.assistant_text_lines)
                    self.emit(f"error: Context length exceeded. The conversation history + system prompt is too large for the model. "
                              f"({self.total_input_tokens} input tokens so far, {hist_count} assistant lines accumulated). "
                              f"Try /compact or Restart to reduce context. Raw: {exc_str[:200]}")
                elif "401" in exc_str or "auth" in exc_str.lower() or "api_key" in exc_str.lower():
                    self.emit(f"error: Authentication failed. Check your API key in Settings. Raw: {exc_str[:200]}")
                elif "429" in exc_str or "rate" in exc_str.lower():
                    self.emit(f"error: Rate limited. Too many requests — wait a moment and try again. Raw: {exc_str[:200]}")
                elif "connection" in exc_str.lower() or "timeout" in exc_str.lower() or "refused" in exc_str.lower():
                    self.emit(f"error: Connection failed. Is the endpoint reachable? Provider: {self.ptype}, model: {self.model}. Raw: {exc_str[:200]}")
                else:
                    self.emit(f"error: {self.ptype}/{self.model} — {exc_str[:300]}")
                self.job["status"] = "error"


def _run_chat_local(job_id: str, message: str, cwd: str,
                    conversation_id: str | None = None,
                    todo_id: str | None = None):
    """Thread target: run claude -p for chat with optional --resume (local CLI fallback)."""
    claude_bin, env = _resolve_claude_bin()
    if not claude_bin:
        _jobs[job_id]["output_lines"].append("error: claude binary not found")
        _jobs[job_id]["status"] = "error"
        return

    cmd = [claude_bin, "-p", message, "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose"]
    if conversation_id:
        cmd.extend(["--resume", conversation_id])
    _jobs[job_id]["status"] = "running"

    assistant_text_lines = []  # collect plain text lines for persistence

    def emit(line: str, is_text: bool = False) -> None:
        if line.strip():
            _jobs[job_id]["output_lines"].append(line)
            if is_text:
                assistant_text_lines.append(line)

    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                start_new_session=True)
        _jobs[job_id]["proc"] = proc

        text_buf = ""
        got_streaming = False

        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                emit(raw_line[:200])
                continue

            t = data.get("type", "")

            if t == "content_block_start":
                block = data.get("content_block", {})
                if block.get("type") == "tool_use":
                    if text_buf.strip():
                        emit(text_buf.strip(), is_text=True)
                        text_buf = ""
                    emit(f"▶ {block.get('name', '?')}...")
                elif block.get("type") == "text" and text_buf.strip():
                    emit(text_buf.strip(), is_text=True)
                    text_buf = ""

            elif t == "content_block_delta":
                got_streaming = True
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_buf += delta.get("text", "")
                    while "\n" in text_buf:
                        line, text_buf = text_buf.split("\n", 1)
                        emit(line, is_text=True)

            elif t == "content_block_stop":
                if text_buf.strip():
                    emit(text_buf.strip(), is_text=True)
                    text_buf = ""

            elif t == "assistant" and not got_streaming:
                parts = []
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text = block["text"].strip()
                        if text:
                            parts.append(("text", text))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))[:80]
                        parts.append(("tool", f"▶ {name}({inp})"))
                for kind, part in parts:
                    for line in part.splitlines():
                        emit(line, is_text=(kind == "text"))

            elif t == "result":
                session_id = data.get("session_id")
                if session_id:
                    _jobs[job_id]["conversation_id"] = session_id
                result = data.get("result", "").strip()
                cost = data.get("cost_usd")
                cost_str = f" — ${cost:.4f}" if cost else ""
                emit(f"✓ Done{cost_str}" + (f": {result}" if result else ""))

        if text_buf.strip():
            emit(text_buf.strip(), is_text=True)

        proc.wait()
        if _jobs[job_id]["status"] != "killed":
            _jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"

        # Persist assistant response to chats file and mark unread
        if todo_id and assistant_text_lines:
            try:
                chats = _load_chats()
                chat = chats.get(todo_id, {"conversationId": None, "messages": []})
                chat["conversationId"] = _jobs[job_id].get("conversation_id")
                chat["messages"].append({"role": "assistant", "content": "\n".join(assistant_text_lines)})
                chat["unread"] = True
                chats[todo_id] = chat
                _save_chats(chats)
            except Exception:
                pass

    except Exception as exc:
        _jobs[job_id]["output_lines"].append(f"error: {exc}")
        _jobs[job_id]["status"] = "error"


def _get_active_provider(user_id: str | None = None) -> tuple[str, dict]:
    """Return (provider_name, provider_config) for the active provider."""
    if _USE_DB and user_id:
        config = _db.get_config(user_id)
    else:
        config = _load_config()
    active = config.get("active_provider", "")

    # Explicit local CLI selection
    if active == "local":
        return "local", {"type": "local"}

    # Named provider from providers dict
    providers = config.get("providers", {})
    if active and active in providers:
        return active, providers[active]

    # Migration: build provider from legacy flat config
    if config.get("openai_compat", {}).get("base_url"):
        oai = config["openai_compat"]
        return "openai_compat", {
            "type": "openai_compat",
            "base_url": oai.get("base_url"),
            "api_key": oai.get("api_key", "none"),
            "model": oai.get("model", "default"),
        }
    if config.get("anthropic_api_key"):
        return "anthropic", {
            "type": "anthropic",
            "api_key": config["anthropic_api_key"],
            "model": config.get("model", "claude-sonnet-4-20250514"),
        }
    return "none", {"type": "none"}


def _run_claude_chat_job(job_id: str, message: str, cwd: str,
                         conversation_id: str | None = None,
                         todo_id: str | None = None):
    """Dispatcher: route to the active provider. Only uses local CLI if explicitly selected."""
    user_id = _jobs.get(job_id, {}).get("user_id")
    name, provider = _get_active_provider(user_id)
    ptype = provider.get("type", "")
    if ptype in ("anthropic", "openai_compat"):
        agent = ChatAgent(job_id, todo_id, provider)
        agent.run(message)
    elif ptype == "local":
        _run_chat_local(job_id, message, cwd, conversation_id, todo_id)
    else:
        _jobs[job_id]["output_lines"].append("error: No provider configured. Go to Settings to set up a provider.")
        _jobs[job_id]["status"] = "error"


def _start_claude_chat_job(label: str, job_key: str, message: str, cwd: str,
                           conversation_id: str | None = None,
                           todo_id: str | None = None,
                           user_id: str | None = None) -> str:
    """Start a headless Claude chat job; return job_id. No dedup — each message is a new job."""
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "id": job_id,
        "label": label,
        "job_key": job_key,
        "status": "pending",
        "output_lines": [],
        "proc": None,
        "created_at": time.time(),
        "conversation_id": conversation_id,
        "todo_id": todo_id,
        "user_id": user_id,
    }
    t = threading.Thread(target=_run_claude_chat_job,
                         args=(job_id, message, cwd, conversation_id, todo_id), daemon=True)
    t.start()
    return job_id


def _kill_process_tree(pid: int) -> None:
    """Send SIGTERM to a process and all its descendants.

    Reads the full process tree snapshot first so children that create their
    own sessions (like claude subagents) are still caught before reparenting.
    """
    try:
        result = subprocess.run(["ps", "-o", "pid,ppid", "-ax"],
                                capture_output=True, text=True)
        children: dict[int, list[int]] = {}
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                p, pp = int(parts[0]), int(parts[1])
                children.setdefault(pp, []).append(p)

        # BFS from root to collect descendants
        to_kill: list[int] = []
        queue = [pid]
        while queue:
            p = queue.pop()
            to_kill.append(p)
            queue.extend(children.get(p, []))

        # Kill leaves first so parents don't spawn replacements
        for p in reversed(to_kill):
            try:
                os.kill(p, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _pty_set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set PTY window size via TIOCSWINSZ ioctl."""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _tmux_bin():
    return shutil.which("tmux") or "/opt/homebrew/bin/tmux"


def _tmux_session_exists(name: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run([_tmux_bin(), "has-session", "-t", name],
                            capture_output=True)
    return result.returncode == 0


def _tmux_list_sessions() -> list[str]:
    """List tmux session names matching our prefix."""
    result = subprocess.run(
        [_tmux_bin(), "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.strip().splitlines()
            if s.strip().startswith("t-")]


def _tmux_recover_sessions():
    """On startup, recover existing tmux sessions into _pty_sessions."""
    for sname in _tmux_list_sessions():
        session_id = sname[2:]
        if session_id in _pty_sessions:
            continue
        _pty_sessions[session_id] = {
            "id": session_id,
            "todo_id": None,
            "title": f"Recovered: {session_id}",
            "tmux_target": sname,
            "alive": True,
            "needs_auto_send": False,
            "resume_id": None,
            "created_at": time.time(),
        }
    print(f"[terminal] Recovered {len(_pty_sessions)} tmux sessions", flush=True)


def _get_user_shell_env():
    """Get the full user login shell environment (needed under launchctl)."""
    try:
        result = subprocess.run(
            ["zsh", "-l", "-c", "env -0"],
            capture_output=True, text=True, timeout=5,
        )
        env = {}
        for entry in result.stdout.split("\0"):
            if "=" in entry:
                k, v = entry.split("=", 1)
                env[k] = v
        if env:
            env.pop("CLAUDECODE", None)
            return env
    except Exception:
        pass
    # Fallback: current env minus CLAUDECODE
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}


def _resolve_claude_bin():
    """Resolve the claude binary path, stripping CLAUDECODE from env."""
    env = _get_user_shell_env()
    candidates = [
        shutil.which("claude", path=env.get("PATH", os.defpath)),
        os.path.expanduser("~/.local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    claude_bin = next((c for c in candidates if c and os.path.isfile(c)), None)
    return claude_bin, env


def _start_claude_job(label: str, job_key: str, prompt: str, cwd: str) -> str:
    """Start a headless Claude job; return job_id. Deduplicates by job_key."""
    for j in _jobs.values():
        if j["job_key"] == job_key and j["status"] == "running":
            return j["id"]
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "id": job_id,
        "label": label,
        "job_key": job_key,
        "status": "pending",
        "output_lines": [],
        "proc": None,
        "created_at": time.time(),
    }
    t = threading.Thread(target=_run_claude_job, args=(job_id, prompt, cwd), daemon=True)
    t.start()
    return job_id


@app.route("/api/todos/<todo_id>/start", methods=["POST"])
def start_in_tmux(todo_id):
    """Launch a headless Claude Code session working on the given todo."""
    todos = _parse_todo_file(TODO_FILE)
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Todo not found"}), 404

    todo_dir = os.path.dirname(os.path.abspath(TODO_FILE)) or os.getcwd()
    job_id = _start_claude_job(todo.get("title", todo_id), f"workon-{todo_id}",
                               f"/ea workon {todo_id}", todo_dir)
    return jsonify({"status": "started", "job_id": job_id})


@app.route("/api/todos/<todo_id>/chat", methods=["POST"])
def chat_with_todo(todo_id):
    """Send a chat message for a todo item, optionally resuming a conversation."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401

    if _USE_DB:
        todo = _db.get_todo(user["id"], todo_id)
    else:
        todos = _parse_todo_file(TODO_FILE)
        todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Todo not found"}), 404

    data = request.json or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    if _USE_DB:
        _db.add_message(todo_id, user["id"], "user", message)
        meta = _db.get_chat_meta(todo_id)
        conversation_id = meta["conversation_id"] if meta else None
    else:
        chats = _load_chats()
        chat = chats.get(todo_id, {"conversationId": None, "messages": []})
        conversation_id = chat.get("conversationId")
        chat["messages"].append({"role": "user", "content": message})
        chats[todo_id] = chat
        _save_chats(chats)

    todo_dir = os.path.dirname(os.path.abspath(TODO_FILE)) or os.getcwd()

    job_id = _start_claude_chat_job(
        label=f"chat: {todo.get('title', todo_id)[:40]}",
        job_key=f"chat-{todo_id}",
        message=message,
        cwd=todo_dir,
        conversation_id=conversation_id,
        todo_id=todo_id,
        user_id=user["id"] if user else None,
    )
    return jsonify({"job_id": job_id, "conversation_id": conversation_id})


@app.route("/api/chats/<todo_id>")
def get_chat(todo_id):
    """Return the persisted chat session for a todo item, plus any running job."""
    user = get_current_user()
    if _USE_DB:
        messages = _db.get_messages(todo_id) if user else []
        meta = _db.get_chat_meta(todo_id)
        chat = {
            "conversationId": meta["conversation_id"] if meta else None,
            "messages": messages,
        }
    else:
        chats = _load_chats()
        chat = chats.get(todo_id, {"conversationId": None, "messages": []})
    # Check for the most recent running chat job for this todo
    job_key = f"chat-{todo_id}"
    latest_job = None
    for j in _jobs.values():
        if j["job_key"] == job_key and j["status"] in ("pending", "running"):
            if not latest_job or j["created_at"] > latest_job["created_at"]:
                latest_job = j
    if latest_job:
        chat["running_job_id"] = latest_job["id"]
    return jsonify(chat)


@app.route("/api/chats/<todo_id>", methods=["DELETE"])
def delete_chat(todo_id):
    """Clear the persisted chat session for a todo item."""
    if _USE_DB:
        _db.delete_messages(todo_id)
    else:
        chats = _load_chats()
        chats.pop(todo_id, None)
        _save_chats(chats)
    return jsonify({"ok": True})


@app.route("/api/chats/unread")
def get_unread_chats():
    """Return list of todo_ids with unread chat responses."""
    user = get_current_user()
    if _USE_DB and user:
        return jsonify(list(_db.get_unread_todo_ids(user["id"])))
    chats = _load_chats()
    return jsonify([tid for tid, c in chats.items() if c.get("unread")])


@app.route("/api/chats/<todo_id>/read", methods=["POST"])
def mark_chat_read(todo_id):
    """Mark a chat as read."""
    if _USE_DB:
        _db.mark_chat_read(todo_id)
    else:
        chats = _load_chats()
        chat = chats.get(todo_id)
        if chat and chat.get("unread"):
            chat["unread"] = False
            _save_chats(chats)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------

def _redact_key(key: str) -> str:
    """Redact an API key for display."""
    if not key:
        return ""
    if len(key) > 12:
        return key[:8] + "..." + key[-4:]
    return "***"


@app.route("/api/config", methods=["GET"])
def get_config():
    """Return server config with API keys redacted."""
    user = get_current_user()
    if _USE_DB and user:
        config = _db.get_config(user["id"])
    else:
        config = _load_config()
    safe = dict(config)
    # Redact legacy flat keys
    if "anthropic_api_key" in safe and safe["anthropic_api_key"]:
        safe["anthropic_api_key"] = _redact_key(safe["anthropic_api_key"])
    if "openai_compat" in safe and isinstance(safe["openai_compat"], dict):
        oai = dict(safe["openai_compat"])
        if oai.get("api_key"):
            oai["api_key"] = _redact_key(oai["api_key"])
        safe["openai_compat"] = oai
    # Redact provider keys
    if "providers" in safe and isinstance(safe["providers"], dict):
        providers = {}
        for name, prov in safe["providers"].items():
            p = dict(prov)
            if p.get("api_key"):
                p["api_key"] = _redact_key(p["api_key"])
            providers[name] = p
        safe["providers"] = providers
    # Include active provider info
    active_name, active_prov = _get_active_provider()
    safe["_active_provider_name"] = active_name
    safe["_active_provider_type"] = active_prov.get("type", "local")
    return jsonify(safe)


@app.route("/api/config", methods=["PUT"])
def put_config():
    """Update server config."""
    user = get_current_user()
    data = request.json or {}
    if _USE_DB and user:
        config = _db.get_config(user["id"])
    else:
        config = _load_config()
    # Merge provided fields (legacy + new)
    for key in ("anthropic_api_key", "model", "mcp_servers", "openai_compat", "active_provider", "subagents_enabled", "max_subagents", "auto_approve_all"):
        if key in data:
            config[key] = data[key]
    if "providers" in data and isinstance(data["providers"], dict):
        existing = config.get("providers", {})
        for name, prov in data["providers"].items():
            if prov is None:
                existing.pop(name, None)  # Delete provider
            elif name in existing:
                # Merge: don't overwrite api_key with redacted value
                for k, v in prov.items():
                    if k == "api_key" and v and "..." in v:
                        continue  # Skip redacted key
                    existing[name][k] = v
            else:
                existing[name] = prov
        config["providers"] = existing
    if "tokens" in data and isinstance(data["tokens"], dict):
        config.setdefault("tokens", {}).update(data["tokens"])
    if _USE_DB and user:
        _db.save_config(user["id"], **{k: v for k, v in config.items()
                        if k in ("providers", "active_provider", "tokens",
                                 "subagents_enabled", "max_subagents",
                                 "auto_approve_all")})
    else:
        _save_config(config)
    return jsonify({"ok": True})


@app.route("/api/mcp/status")
def mcp_status():
    """Return status of all MCP servers with per-user preferences."""
    user = get_current_user()
    user_id = user["id"] if user else None
    registry = _load_mcp_registry()
    mgr = _get_mcp_manager(user_id)
    connected_status = mgr.get_status() if mgr else {}
    # Load user preferences
    prefs = {}
    auto_approve_all = False
    if _USE_DB and user_id and user_id != "local":
        prefs = _db.get_mcp_preferences(user_id)
        config = _db.get_config(user_id)
        auto_approve_all = config.get("auto_approve_all", False)
    # Get user tokens to show which credentials are set
    user_tokens = {}
    if _USE_DB and user_id and user_id != "local":
        user_tokens = config.get("tokens", {})
    servers = []
    for name, reg_entry in registry.items():
        pref = prefs.get(name, {})
        cred_fields = reg_entry.get("credential_fields", [])
        acct_fields = reg_entry.get("account_fields", [])
        acct_count = 0
        if acct_fields and _USE_DB and user_id and user_id != "local":
            acct_count = len(_db.get_server_accounts(user_id, name))
        entry = {
            "name": name,
            "label": reg_entry.get("label", name),
            "enabled": pref.get("enabled", False),
            "disabled_tools": pref.get("disabled_tools", []),
            "auto_approved_tools": pref.get("auto_approved_tools", []),
            "credential_fields": [
                {**f, "has_value": bool(user_tokens.get(f["key"]))}
                for f in cred_fields
            ],
            "account_fields": acct_fields,
            "account_count": acct_count,
            "oauth_providers": [
                {"id": p["id"], "label": p["label"]}
                for p in reg_entry.get("oauth_providers", [])
            ],
        }
        if name in connected_status:
            entry["connected"] = connected_status[name].get("connected", False)
            entry["tool_count"] = connected_status[name].get("tool_count", 0)
            if "error" in connected_status[name]:
                entry["error"] = connected_status[name]["error"]
            # Include tool names for this server
            if mgr and entry["connected"]:
                prefix = f"mcp__{name}__"
                # Get global excludes from registry
                global_excludes = set(reg_entry.get("exclude_tools", []))
                entry["tools"] = [
                    {"name": t["name"][len(prefix):],
                     "description": (t.get("description") or "")[:120]}
                    for t in mgr.get_tool_definitions()
                    if t["name"].startswith(prefix)
                    and t["name"][len(prefix):] not in global_excludes
                ]
        else:
            entry["connected"] = False
        servers.append(entry)
    return jsonify({
        "servers": servers,
        "available": bool(ClientSessionGroup),
        "auto_approve_all": auto_approve_all,
    })


@app.route("/api/mcp/reconnect", methods=["POST"])
def mcp_reconnect():
    """Restart MCP server connections for the current user."""
    user = get_current_user()
    user_id = user["id"] if user else None
    uid = user_id or "local"
    with _mcp_managers_lock:
        old = _mcp_managers.pop(uid, None)
    if old:
        old.stop()
    # Next call to _get_mcp_manager will lazy-create a fresh one
    mgr = _get_mcp_manager(user_id)
    return jsonify({"ok": True, "connected": mgr is not None})


@app.route("/api/mcp/approve", methods=["POST"])
def mcp_approve():
    """Approve or deny a pending tool execution."""
    data = request.json or {}
    approval_id = data.get("approval_id", "")
    approved = data.get("approved", False)
    always_allow = data.get("always_allow", False)

    with _approvals_lock:
        pending = _pending_approvals.get(approval_id)
    if not pending:
        return jsonify({"error": "No pending approval with that ID"}), 404

    pending["approved"] = approved

    # Persist auto-approval if requested
    if approved and always_allow:
        user = get_current_user()
        if user and _USE_DB:
            _db.set_tool_auto_approved(
                user["id"], pending["server_name"], pending["tool_name"], True
            )

    # Unblock the waiting thread
    pending["event"].set()
    return jsonify({"ok": True})


@app.route("/api/mcp/servers", methods=["PUT"])
def mcp_set_server():
    """Enable or disable an MCP server for the current user."""
    user = get_current_user()
    if not user or not _USE_DB:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    server = data.get("server", "")
    enabled = data.get("enabled", False)
    registry = _load_mcp_registry()
    if server not in registry:
        return jsonify({"error": f"Unknown server: {server}"}), 400
    _db.set_server_enabled(user["id"], server, enabled)
    # Reconnect with new server set
    uid = user["id"]
    with _mcp_managers_lock:
        old = _mcp_managers.pop(uid, None)
    if old:
        old.stop()
    return jsonify({"ok": True})


@app.route("/api/mcp/tools", methods=["PUT"])
def mcp_set_tool():
    """Set tool disabled or auto_approved state."""
    user = get_current_user()
    if not user or not _USE_DB:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    server = data.get("server", "")
    tool = data.get("tool", "")
    if not server or not tool:
        return jsonify({"error": "server and tool required"}), 400
    if "disabled" in data:
        _db.set_tool_disabled(user["id"], server, tool, data["disabled"])
    if "auto_approved" in data:
        _db.set_tool_auto_approved(user["id"], server, tool, data["auto_approved"])
    return jsonify({"ok": True})


@app.route("/api/mcp/accounts/<server_name>", methods=["GET"])
def mcp_get_accounts(server_name):
    """Get all accounts for a server."""
    user = get_current_user()
    if not user or not _USE_DB:
        return jsonify({"error": "Not authenticated"}), 401
    accounts = _db.get_server_accounts(user["id"], server_name)
    # Redact secrets, add oauth_connected flag
    for acct in accounts:
        cfg = acct["config"]
        if "oauth_token" in cfg:
            cfg["oauth_connected"] = True
            del cfg["oauth_token"]
        for k in list(cfg.keys()):
            if "password" in k.lower() and cfg[k]:
                cfg[k] = "***"
    return jsonify({"accounts": accounts})


@app.route("/api/mcp/accounts/<server_name>", methods=["POST"])
def mcp_add_account(server_name):
    """Add an account for a server."""
    user = get_current_user()
    if not user or not _USE_DB:
        return jsonify({"error": "Not authenticated"}), 401
    registry = _load_mcp_registry()
    if server_name not in registry:
        return jsonify({"error": f"Unknown server: {server_name}"}), 400
    data = request.json or {}
    acct = _db.add_server_account(user["id"], server_name, data)
    # Reconnect to pick up new account
    uid = user["id"]
    with _mcp_managers_lock:
        old = _mcp_managers.pop(uid, None)
    if old:
        old.stop()
    return jsonify(acct), 201


@app.route("/api/mcp/accounts/<server_name>/<account_id>", methods=["PUT"])
def mcp_update_account(server_name, account_id):
    """Update an account."""
    user = get_current_user()
    if not user or not _USE_DB:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    # Merge: don't overwrite password with redacted value
    existing = _db.get_server_accounts(user["id"], server_name)
    old = next((a for a in existing if a["id"] == account_id), None)
    if old:
        merged = dict(old["config"])
        for k, v in data.items():
            if "password" in k.lower() and v == "***":
                continue  # Skip redacted
            merged[k] = v
        data = merged
    if not _db.update_server_account(user["id"], account_id, data):
        return jsonify({"error": "Not found"}), 404
    # Reconnect
    uid = user["id"]
    with _mcp_managers_lock:
        old_mgr = _mcp_managers.pop(uid, None)
    if old_mgr:
        old_mgr.stop()
    return jsonify({"ok": True})


@app.route("/api/mcp/accounts/<server_name>/<account_id>", methods=["DELETE"])
def mcp_delete_account(server_name, account_id):
    """Delete an account."""
    user = get_current_user()
    if not user or not _USE_DB:
        return jsonify({"error": "Not authenticated"}), 401
    if not _db.delete_server_account(user["id"], account_id):
        return jsonify({"error": "Not found"}), 404
    # Reconnect
    uid = user["id"]
    with _mcp_managers_lock:
        old = _mcp_managers.pop(uid, None)
    if old:
        old.stop()
    return jsonify({"ok": True})


import hmac
import hashlib
import base64
import urllib.parse

_OAUTH_SECRET = os.environ.get("OAUTH_STATE_SECRET", "todo-app-oauth-state-secret")


def _resolve_oauth_creds(provider: dict) -> tuple[str, str]:
    """Resolve OAuth client_id and client_secret from env vars or direct values."""
    client_id = provider.get("client_id") or os.environ.get(provider.get("client_id_env", ""), "")
    client_secret = provider.get("client_secret") or os.environ.get(provider.get("client_secret_env", ""), "")
    return client_id, client_secret


@app.route("/api/mcp/oauth/start")
def mcp_oauth_start():
    """Start an OAuth flow. Returns {auth_url} for the UI to open in a popup."""
    user = get_current_user()
    if not user or not _USE_DB:
        return jsonify({"error": "Not authenticated"}), 401
    server = request.args.get("server", "")
    provider_id = request.args.get("provider", "")
    account_id = request.args.get("account_id", "")
    registry = _load_mcp_registry()
    if server not in registry:
        return jsonify({"error": f"Unknown server: {server}"}), 400
    providers = registry[server].get("oauth_providers", [])
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        return jsonify({"error": f"Unknown OAuth provider: {provider_id}"}), 400
    client_id, client_secret = _resolve_oauth_creds(provider)
    if not client_id:
        return jsonify({"error": "OAuth client_id not configured (check env vars)"}), 500
    # Build state (signed)
    state_data = json.dumps({"user_id": user["id"], "server": server,
                             "provider": provider_id, "account_id": account_id})
    sig = hmac.new(_OAUTH_SECRET.encode(), state_data.encode(), hashlib.sha256).hexdigest()[:16]
    state = base64.urlsafe_b64encode(f"{sig}:{state_data}".encode()).decode()
    # Build redirect URI from request host
    domain = os.environ.get("DOMAIN_NAME")
    if domain:
        redirect_uri = f"https://{domain}/api/mcp/oauth/callback"
    else:
        redirect_uri = f"{request.scheme}://{request.host}/api/mcp/oauth/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if provider.get("scope"):
        params["scope"] = provider["scope"]
    if provider.get("user_scope"):
        params["user_scope"] = provider["user_scope"]
    if not provider.get("user_scope"):
        # Google-style: request offline access for refresh tokens
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    auth_url = provider["auth_uri"] + "?" + urllib.parse.urlencode(params)
    return jsonify({"auth_url": auth_url})


@app.route("/api/mcp/oauth/callback")
def mcp_oauth_callback():
    """OAuth callback. Exchanges code for tokens, stores in DB, closes popup."""
    try:
        return _handle_oauth_callback()
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        return f"<html><body><h3>Error</h3><pre>{tb}</pre></body></html>", 500


def _handle_oauth_callback():
    code = request.args.get("code", "")
    state_b64 = request.args.get("state", "")
    error = request.args.get("error", "")
    if error:
        return f"<html><body><h3>OAuth Error: {error}</h3><script>window.close()</script></body></html>"
    if not code or not state_b64:
        return "Missing code or state", 400
    # Validate state
    try:
        state_raw = base64.urlsafe_b64decode(state_b64).decode()
        sig, state_data = state_raw.split(":", 1)
        expected_sig = hmac.new(_OAUTH_SECRET.encode(), state_data.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return "Invalid state signature", 400
        state = json.loads(state_data)
    except Exception:
        return "Invalid state", 400
    user_id = state["user_id"]
    server = state["server"]
    provider_id = state["provider"]
    account_id = state.get("account_id", "")
    # Look up OAuth provider config
    registry = _load_mcp_registry()
    providers = registry.get(server, {}).get("oauth_providers", [])
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        return "Unknown provider", 400
    client_id, client_secret = _resolve_oauth_creds(provider)
    domain = os.environ.get("DOMAIN_NAME")
    if domain:
        redirect_uri = f"https://{domain}/api/mcp/oauth/callback"
    else:
        redirect_uri = f"{request.scheme}://{request.host}/api/mcp/oauth/callback"
    # Exchange code for tokens
    import requests as _requests
    resp = _requests.post(provider["token_uri"], data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    if resp.status_code != 200:
        return f"<html><body><h3>Token exchange failed</h3><pre>{resp.text}</pre><script>setTimeout(()=>window.close(),5000)</script></body></html>"
    token_data = resp.json()
    # Extract the access token — support nested paths like "authed_user.access_token"
    token_path = provider.get("token_path", "access_token")
    access_token = token_data
    for key in token_path.split("."):
        access_token = access_token.get(key) if isinstance(access_token, dict) else None
    if not access_token:
        return f"<html><body><h3>No access token in response</h3><pre>{json.dumps(token_data, indent=2)}</pre></body></html>", 400
    # Check if this provider stores as a credential (e.g., Slack xoxp)
    store_as = provider.get("store_as")
    if store_as:
        # Store directly as a user credential token
        config = _db.get_config(user_id)
        tokens = config.get("tokens", {})
        tokens[store_as] = access_token
        _db.save_config(user_id, tokens=tokens)
    else:
        # Standard flow: build oauth_token and store on an account
        oauth_token = {
            "token": access_token,
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": provider["token_uri"],
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": provider.get("scope", "").split(),
            "expiry": (datetime.now(timezone.utc) +
                       timedelta(seconds=token_data.get("expires_in", 3600))).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        if account_id:
            accounts = _db.get_server_accounts(user_id, server)
            acct = next((a for a in accounts if a["id"] == account_id), None)
            if acct:
                cfg = dict(acct["config"])
                cfg["oauth_token"] = oauth_token
                _db.update_server_account(user_id, account_id, cfg)
        else:
            template = dict(provider.get("account_template", {}))
            # Try to get email for template substitution
            email = ""
            try:
                info_resp = _requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                           headers={"Authorization": f"Bearer {access_token}"})
                if info_resp.status_code == 200:
                    email = info_resp.json().get("email", "")
            except Exception:
                pass
            if email:
                for k, v in template.items():
                    if isinstance(v, str) and "{email}" in v:
                        template[k] = v.replace("{email}", email)
            template["oauth_token"] = oauth_token
            _db.add_server_account(user_id, server, template)
    # Reconnect MCP
    with _mcp_managers_lock:
        old = _mcp_managers.pop(user_id, None)
    if old:
        old.stop()
    return """<html><body style="font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><h3>Connected!</h3><p>You can close this window.</p></div>
<script>
if(window.opener){window.opener.postMessage({type:'oauth_complete'},'*')}
setTimeout(()=>window.close(),2000)
</script></body></html>"""


# ---------------------------------------------------------------------------
# Version history (DB-backed)
# ---------------------------------------------------------------------------


@app.route("/api/history")
def get_history():
    """Return recent version history for the current user."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"entries": []})
    entries = _db.get_history(user["id"])
    return jsonify({"entries": entries})


@app.route("/api/history/<int:history_id>/restore", methods=["POST"])
def restore_history(history_id):
    """Restore a todo from a history snapshot."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"error": "Not available"}), 400
    result = _db.restore_todo(user["id"], history_id)
    if not result:
        return jsonify({"error": "History entry not found"}), 404
    return jsonify(result)


# ---------------------------------------------------------------------------
# Git version control for todo files
# ---------------------------------------------------------------------------

def _todo_git_dir() -> str | None:
    """Return the git repo directory containing the todo file, or None."""
    todo_path = os.path.realpath(TODO_FILE)
    try:
        result = subprocess.run(
            ["git", "-C", os.path.dirname(todo_path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


@app.route("/api/git/log")
def git_log():
    """Return recent git log for the todo files."""
    git_dir = _todo_git_dir()
    if not git_dir:
        return jsonify({"error": "No git repo found for todo file"}), 404
    todo_real = os.path.realpath(TODO_FILE)
    completed_real = os.path.realpath(_completed_file_path(TODO_FILE))
    # Get paths relative to git root
    todo_rel = os.path.relpath(todo_real, git_dir)
    completed_rel = os.path.relpath(completed_real, git_dir)
    try:
        result = subprocess.run(
            ["git", "-C", git_dir, "log", "--oneline", "--format=%H|%ai|%s", "-30",
             "--", todo_rel, completed_rel],
            capture_output=True, text=True, timeout=10
        )
        commits = []
        for line in result.stdout.strip().splitlines():
            if "|" in line:
                parts = line.split("|", 2)
                commits.append({"hash": parts[0], "date": parts[1], "message": parts[2] if len(parts) > 2 else ""})
        return jsonify({"commits": commits, "git_dir": git_dir})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/git/commit", methods=["POST"])
def git_commit():
    """Commit current todo files."""
    git_dir = _todo_git_dir()
    if not git_dir:
        return jsonify({"error": "No git repo found"}), 404
    data = request.json or {}
    message = data.get("message", "").strip() or f"Manual save {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    todo_real = os.path.realpath(TODO_FILE)
    completed_real = os.path.realpath(_completed_file_path(TODO_FILE))
    todo_rel = os.path.relpath(todo_real, git_dir)
    completed_rel = os.path.relpath(completed_real, git_dir)
    try:
        subprocess.run(["git", "-C", git_dir, "add", todo_rel, completed_rel],
                       capture_output=True, text=True, timeout=10)
        result = subprocess.run(
            ["git", "-C", git_dir, "commit", "-m", message],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return jsonify({"ok": True, "message": message})
        elif "nothing to commit" in result.stdout:
            return jsonify({"ok": True, "message": "No changes to commit"})
        else:
            return jsonify({"error": result.stderr.strip() or result.stdout.strip()}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/git/rollback", methods=["POST"])
def git_rollback():
    """Rollback todo files to a specific commit."""
    git_dir = _todo_git_dir()
    if not git_dir:
        return jsonify({"error": "No git repo found"}), 404
    data = request.json or {}
    commit_hash = data.get("hash", "").strip()
    if not commit_hash:
        return jsonify({"error": "hash is required"}), 400
    todo_real = os.path.realpath(TODO_FILE)
    completed_real = os.path.realpath(_completed_file_path(TODO_FILE))
    todo_rel = os.path.relpath(todo_real, git_dir)
    completed_rel = os.path.relpath(completed_real, git_dir)
    try:
        # Checkout the files from that commit
        subprocess.run(
            ["git", "-C", git_dir, "checkout", commit_hash, "--", todo_rel, completed_rel],
            capture_output=True, text=True, timeout=10, check=True
        )
        # Commit the rollback
        subprocess.run(
            ["git", "-C", git_dir, "add", todo_rel, completed_rel],
            capture_output=True, text=True, timeout=10
        )
        subprocess.run(
            ["git", "-C", git_dir, "commit", "-m",
             f"Rollback to {commit_hash[:8]} — {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            capture_output=True, text=True, timeout=10
        )
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": exc.stderr.strip() if exc.stderr else str(exc)}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Interactive PTY terminal sessions
# ---------------------------------------------------------------------------

@app.route("/api/todos/<todo_id>/terminal", methods=["POST"])
def open_terminal(todo_id):
    """Create a tmux-backed terminal session for a todo. Returns existing if alive."""
    data = request.json or {}
    resume_id = data.get("resume_id")

    # Return existing alive session for this todo (unless resuming a specific conv)
    if not resume_id:
        for s in _pty_sessions.values():
            if s["todo_id"] == todo_id and s["alive"]:
                return jsonify({"session_id": s["id"], "title": s["title"], "existing": True})

    todos = _parse_todo_file(TODO_FILE)
    todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Todo not found"}), 404

    claude_bin, env = _resolve_claude_bin()
    if not claude_bin:
        return jsonify({"error": "claude binary not found"}), 500

    session_id = str(uuid.uuid4())[:8]
    tmux_name = f"t-{session_id}"
    todo_dir = os.path.dirname(os.path.abspath(TODO_FILE)) or os.getcwd()

    # Create a dedicated tmux session (one window, no switching possible)
    tmux = _tmux_bin()
    cmd_parts = [claude_bin, "--dangerously-skip-permissions"]
    if resume_id:
        cmd_parts.extend(["--resume", resume_id])
    inner_cmd = " ".join(cmd_parts)

    subprocess.run(
        [tmux, "new-session", "-d", "-s", tmux_name, "-x", "80", "-y", "24",
         "zsh", "-lic", f"unset CLAUDECODE; cd {todo_dir} && {inner_cmd}"],
        capture_output=True, check=True,
    )
    # Let the window resize to match the latest attached client
    subprocess.run([tmux, "set-option", "-t", tmux_name, "aggressive-resize", "on"],
                   capture_output=True)
    subprocess.run([tmux, "set-option", "-g", "window-size", "latest"],
                   capture_output=True)

    _pty_sessions[session_id] = {
        "id": session_id,
        "todo_id": todo_id,
        "title": todo.get("title", todo_id),
        "tmux_target": tmux_name,
        "alive": True,
        "needs_auto_send": not resume_id,
        "resume_id": resume_id,
        "created_at": time.time(),
    }

    # Auto-send /ea workon via tmux send-keys
    if not resume_id:
        def _auto_send():
            time.sleep(1.5)
            if _pty_sessions.get(session_id, {}).get("alive"):
                subprocess.run(
                    [tmux, "send-keys", "-t", tmux_name, f"/ea workon {todo_id}", "Enter"],
                    capture_output=True,
                )
        threading.Thread(target=_auto_send, daemon=True).start()

    return jsonify({"session_id": session_id, "title": todo.get("title", todo_id), "existing": False})


@app.route("/api/terminal/sessions")
def list_terminal_sessions():
    """List terminal sessions, syncing alive state with tmux."""
    live_sessions = set(_tmux_list_sessions())
    # Sync alive state with tmux reality
    for s in _pty_sessions.values():
        s["alive"] = f"t-{s['id']}" in live_sessions
    # Purge dead sessions older than 5 min
    cutoff = time.time() - 300
    stale = [sid for sid, s in _pty_sessions.items()
             if not s["alive"] and s["created_at"] < cutoff]
    for sid in stale:
        del _pty_sessions[sid]
    return jsonify([{
        "session_id": s["id"], "todo_id": s["todo_id"],
        "title": s["title"], "alive": s["alive"],
        "created_at": s["created_at"],
    } for s in _pty_sessions.values()])


@app.route("/api/terminal/<session_id>/kill", methods=["POST"])
def kill_terminal(session_id):
    """Kill a terminal session by destroying its tmux session."""
    session = _pty_sessions.get(session_id)
    if not session:
        return jsonify({"error": "not found"}), 404
    session["alive"] = False
    tmux_name = f"t-{session_id}"
    subprocess.run([_tmux_bin(), "kill-session", "-t", tmux_name], capture_output=True)
    return jsonify({"ok": True})


@sock.route("/api/terminal/<session_id>/ws")
def terminal_ws(ws, session_id):
    """WebSocket handler: attach to tmux window via PTY, bridge to browser."""
    session = _pty_sessions.get(session_id)
    if not session:
        ws.close()
        return

    tmux_name = f"t-{session_id}"

    # Verify tmux session still exists
    if not _tmux_session_exists(tmux_name):
        ws.send(json.dumps({"type": "error", "msg": "tmux session not found"}))
        ws.close()
        session["alive"] = False
        return

    # Attach to the dedicated tmux session (single window, no switching)
    tmux = _tmux_bin()
    attach_env = os.environ.copy()
    attach_env["TERM"] = "xterm-256color"

    master_fd, slave_fd = pty.openpty()
    _pty_set_winsize(master_fd, 24, 80)

    print(f"[terminal] Attaching to session {tmux_name}", flush=True)
    proc = subprocess.Popen(
        [tmux, "attach-session", "-t", tmux_name],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env=attach_env, close_fds=True,
    )
    os.close(slave_fd)

    # Bridge PTY ↔ WS (transient per connection — session persists in tmux)
    _terminal_io_loop(ws, master_fd, proc, tmux_target=tmux_name)


def _terminal_io_loop(ws, master_fd, proc, tmux_target=None):
    """Bridge PTY master fd ↔ WebSocket in a single-threaded poll loop."""
    # Make master_fd non-blocking so we can poll it alongside WS
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    print(f"[terminal] IO loop starting (pid={proc.pid})", flush=True)
    try:
        while proc.poll() is None:
            # 1. Read any available PTY output and forward to WS
            try:
                r, _, _ = _select.select([master_fd], [], [], 0)
                if r:
                    data = os.read(master_fd, 16384)
                    if data:
                        ws.send(data)
                    else:
                        break
            except OSError:
                break

            # 2. Check for WS input (short timeout to keep loop responsive)
            try:
                msg = ws.receive(timeout=0.05)
            except Exception:
                break
            if msg is None:
                continue  # timeout — no input yet, loop back to read PTY

            if isinstance(msg, bytes):
                try:
                    os.write(master_fd, msg)
                except OSError:
                    break
            elif isinstance(msg, str):
                try:
                    ctrl = json.loads(msg)
                    if ctrl.get("type") == "resize":
                        rows = int(ctrl.get("rows", 24))
                        cols = int(ctrl.get("cols", 80))
                        _pty_set_winsize(master_fd, rows, cols)
                        if tmux_target:
                            tmux = _tmux_bin()
                            # Force tmux to adopt the new size
                            subprocess.run(
                                [tmux, "resize-window", "-t", tmux_target,
                                 "-x", str(cols), "-y", str(rows)],
                                capture_output=True,
                            )
                            subprocess.run(
                                [tmux, "resize-pane", "-t", tmux_target,
                                 "-x", str(cols), "-y", str(rows)],
                                capture_output=True,
                            )
                    elif ctrl.get("type") == "close":
                        break
                    elif ctrl.get("type") == "input":
                        os.write(master_fd, ctrl["data"].encode())
                except (json.JSONDecodeError, ValueError, OSError):
                    pass
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.wait()
        print(f"[terminal] IO loop exited (rc={proc.returncode})", flush=True)


@app.route("/api/ea-update", methods=["POST"])
def ea_update():
    """Run /ea update via ChatAgent."""
    user = get_current_user()
    data = request.json or {}
    force = data.get("force", False)

    # Check for existing running job
    existing = next((j for j in _jobs.values()
                     if j["job_key"] == "ea-update" and j["status"] == "running"), None)
    if existing and not force:
        return jsonify({"status": "already_running", "job_id": existing["id"]})

    todo_dir = os.path.dirname(os.path.abspath(TODO_FILE)) or os.getcwd()
    job_id = _start_claude_chat_job("EA Update", "ea-update", "/ea update", todo_dir,
                                     user_id=user["id"] if user else None)
    return jsonify({"status": "started", "job_id": job_id})


@app.route("/api/ea-update-item", methods=["POST"])
def ea_update_item():
    """Run /ea checkon <item_id> via ChatAgent."""
    user = get_current_user()
    data = request.json
    item_id = (data.get("id") or "").strip()
    force = data.get("force", False)
    if not item_id:
        return jsonify({"error": "id required"}), 400

    job_key = f"ea-{item_id}"
    existing = next((j for j in _jobs.values()
                     if j["job_key"] == job_key and j["status"] == "running"), None)
    if existing and not force:
        return jsonify({"status": "already_running", "job_id": existing["id"]})

    todo_dir = os.path.dirname(os.path.abspath(TODO_FILE)) or os.getcwd()
    job_id = _start_claude_chat_job(f"Check: {item_id}", job_key, f"/ea checkon {item_id}", todo_dir,
                                     user_id=user["id"] if user else None)
    return jsonify({"status": "started", "job_id": job_id})


@app.route("/api/resume-conv", methods=["POST"])
def resume_conv():
    """Resume a Claude conversation in tmux."""
    data = request.json
    conv_id = data.get("conversation_id", "").strip()
    if not conv_id:
        return jsonify({"error": "No conversation ID provided"}), 400

    window_name = f"conv-{conv_id[:16]}"
    tmux_bin = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
    tmux_session = "0"
    try:
        result = subprocess.run(
            [tmux_bin, "has-session", "-t", tmux_session],
            capture_output=True,
        )
        if result.returncode != 0:
            subprocess.run(
                [tmux_bin, "new-session", "-d", "-s", tmux_session],
                check=True,
                capture_output=True,
            )

        subprocess.run(
            [tmux_bin, "new-window", "-t", f"{tmux_session}:", "-n", window_name],
            check=True,
            capture_output=True,
        )
        todo_dir = os.path.dirname(os.path.abspath(TODO_FILE)) or os.getcwd()
        subprocess.run(
            [
                tmux_bin, "send-keys",
                "-t", f"{tmux_session}:{window_name}",
                f"cd {todo_dir} && claude --dangerously-skip-permissions --resume {conv_id}",
                "Enter",
            ],
            check=True,
            capture_output=True,
        )

        return jsonify({"status": "resumed", "window": window_name})
    except FileNotFoundError:
        return jsonify({"error": "tmux is not installed"}), 500
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": f"tmux error: {exc.stderr.decode().strip()}"}), 500


@app.route("/api/jobs")
def list_jobs():
    """List all jobs, purging completed/killed entries older than 30 minutes."""
    cutoff = time.time() - 1800
    stale = [jid for jid, j in _jobs.items()
             if j["status"] in ("done", "error", "killed") and j["created_at"] < cutoff]
    for jid in stale:
        del _jobs[jid]
    return jsonify([{
        "id": j["id"], "label": j["label"], "job_key": j["job_key"],
        "status": j["status"], "line_count": len(j["output_lines"]), "created_at": j["created_at"],
        "conversation_id": j.get("conversation_id"),
    } for j in _jobs.values()])


@app.route("/api/jobs/<job_id>/stream")
def stream_job(job_id):
    """SSE stream of raw output lines for a job."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    def generate():
        sent = 0
        while True:
            while sent < len(job["output_lines"]):
                yield f"data: {json.dumps(job['output_lines'][sent])}\n\n"
                sent += 1
            if job["status"] in ("done", "error", "killed"):
                done_msg = {'__done__': True, 'status': job['status']}
                if job.get('conversation_id'):
                    done_msg['conversation_id'] = job['conversation_id']
                yield f"data: {json.dumps(done_msg)}\n\n"
                break
            time.sleep(0.05)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/jobs/<job_id>/kill", methods=["POST"])
def kill_job(job_id):
    """Cancel a running job (supports both local subprocess and API stream)."""
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    # Cancel API stream if present
    api_stream = job.get("_stream")
    if api_stream:
        try:
            api_stream.close()
        except Exception:
            pass
    # Kill local subprocess if present
    proc = job.get("proc")
    if proc and proc.poll() is None:
        _kill_process_tree(proc.pid)
    job["status"] = "killed"
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Embedded HTML UI
# ---------------------------------------------------------------------------

LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — Todo App</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f1117; color: #e2e8f0; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; }
  .card { background: #1a1d2e; border-radius: 12px; padding: 32px; width: 360px;
          box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
  h1 { font-size: 1.3rem; margin-bottom: 24px; text-align: center; }
  label { display: block; font-size: 0.82rem; font-weight: 600; margin: 12px 0 4px; }
  input { width: 100%; padding: 8px 12px; border: 1px solid #2d3348; border-radius: 6px;
          background: #0f1117; color: #e2e8f0; font-size: 0.9rem; font-family: inherit; }
  input:focus { outline: none; border-color: #4f6ef7; }
  button { width: 100%; padding: 10px; margin-top: 20px; border: none; border-radius: 6px;
           background: #4f6ef7; color: white; font-size: 0.9rem; font-weight: 600;
           cursor: pointer; font-family: inherit; }
  button:hover { background: #3a5bd9; }
  .error { color: #ef4444; font-size: 0.8rem; margin-top: 8px; display: none; }
  .toggle { text-align: center; margin-top: 16px; font-size: 0.8rem; }
  .toggle a { color: #4f6ef7; cursor: pointer; text-decoration: none; }
</style>
</head>
<body>
<div class="card">
  <h1 id="form-title">Sign In</h1>
  <form id="auth-form" onsubmit="return handleAuth(event)">
    <div id="name-field" style="display:none">
      <label for="name">Name</label>
      <input type="text" id="name" placeholder="Your name">
    </div>
    <label for="email">Email</label>
    <input type="email" id="email" required placeholder="you@example.com">
    <label for="password">Password</label>
    <input type="password" id="password" required placeholder="Password" minlength="6">
    <div class="error" id="error"></div>
    <button type="submit" id="submit-btn">Sign In</button>
  </form>
  <div class="toggle">
    <span id="toggle-text">Don't have an account?</span>
    <a onclick="toggleMode()"><span id="toggle-link">Register</span></a>
  </div>
</div>
<script>
let isRegister = false;
function toggleMode() {
  isRegister = !isRegister;
  document.getElementById('form-title').textContent = isRegister ? 'Register' : 'Sign In';
  document.getElementById('submit-btn').textContent = isRegister ? 'Create Account' : 'Sign In';
  document.getElementById('name-field').style.display = isRegister ? 'block' : 'none';
  document.getElementById('toggle-text').textContent = isRegister ? 'Already have an account?' : "Don't have an account?";
  document.getElementById('toggle-link').textContent = isRegister ? 'Sign In' : 'Register';
  document.getElementById('error').style.display = 'none';
}
async function handleAuth(e) {
  e.preventDefault();
  const errEl = document.getElementById('error');
  errEl.style.display = 'none';
  const body = { email: document.getElementById('email').value, password: document.getElementById('password').value };
  if (isRegister) body.name = document.getElementById('name').value;
  try {
    const res = await fetch('/api/auth/' + (isRegister ? 'register' : 'login'), {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { errEl.textContent = data.error || 'Failed'; errEl.style.display = 'block'; return; }
    window.location.reload();
  } catch { errEl.textContent = 'Network error'; errEl.style.display = 'block'; }
}
</script>
</body>
</html>"""


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Todo List</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script type="module" src="https://cdn.jsdelivr.net/npm/ldrs/dist/auto/mirage.js"></script>
<script type="module" src="https://cdn.jsdelivr.net/npm/ldrs/dist/auto/jellyTriangle.js"></script>
<script type="module" src="https://cdn.jsdelivr.net/npm/ldrs/dist/auto/bouncy.js"></script>
<script type="module" src="https://cdn.jsdelivr.net/npm/ldrs/dist/auto/ripples.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css">
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js"></script>
<script type="importmap">
{
  "imports": {
    "codemirror": "https://esm.sh/codemirror@6.0.1",
    "@codemirror/lang-markdown": "https://esm.sh/@codemirror/lang-markdown@6.3.2",
    "@codemirror/view": "https://esm.sh/@codemirror/view@6.36.5",
    "@codemirror/state": "https://esm.sh/@codemirror/state@6.5.2",
    "@codemirror/commands": "https://esm.sh/@codemirror/commands@6.8.0"
  }
}
</script>
<style>
  @font-face {
    font-family: 'Manrope';
    src: url('/static/fonts/Manrope-VariableFont_wght.ttf') format('truetype');
    font-weight: 200 800;
    font-display: swap;
  }
  :root {
    --bg: #f8f9fb; --card: #fff; --border: #e2e5ea; --text: #1a1d23;
    --muted: #6b7280; --subtle: #9ca3af;
    --accent: #4f6ef7; --accent-hover: #3b5de7; --accent-light: rgba(79,110,247,0.08);
    --completed-bg: #f0faf4; --completed-border: #6ee7a0; --completed-text: #166534;
    --danger: #ef4444; --danger-hover: #dc2626;
    --radius: 10px; --radius-lg: 14px;
    --shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.08);
    --shadow-lg: 0 10px 30px rgba(0,0,0,0.08), 0 2px 8px rgba(0,0,0,0.06);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: 'Manrope', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    max-width: 828px; margin: 0 auto; padding: 24px 20px;
    -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  }
  h1 { display: none; }
  h2 {
    font-size: 0.8rem; color: var(--subtle); margin: 28px 0 12px;
    text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600;
  }

  /* Add form */
  .add-form {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 18px; margin-bottom: 10px; box-shadow: var(--shadow-md);
    display: none; transition: box-shadow 0.2s;
  }
  .add-form.visible { display: block; }
  .add-form.kb-selected { box-shadow: 0 0 0 2px var(--accent), var(--shadow-md); }
  .active-header { display: grid; grid-template-columns: 1fr auto; align-items: start; gap: 4px 10px; position: sticky; top: 0; z-index: 101; background: var(--bg); padding: 4px 0; }
  .active-header-btns { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; grid-column: 1; row-gap: 6px; }
  .active-header h2 { margin: 0; }
  .active-header .btn-group { display: inline-flex; gap: 4px; white-space: nowrap; margin-left: 6px; }
  .ea-update-wrap { display: inline-flex; align-items: center; gap: 4px; position: relative; grid-column: 2; grid-row: 1; }
  .ea-update-bubble {
    display: none; position: absolute; top: 100%; right: 0; margin-top: 6px;
    width: 340px; max-height: 250px; overflow-y: auto;
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: var(--shadow-lg); padding: 8px 10px; z-index: 200;
    font-family: monospace; font-size: 0.82rem; line-height: 1.5;
    color: var(--muted); white-space: pre-wrap; word-break: break-all;
  }
  .ea-update-btn.running:hover + .ea-update-bubble.has-content { display: block; }
  /* Per-item checkon output bubble */
  .checkon-bubble {
    display: none; position: absolute; left: 18px; bottom: 100%; margin-bottom: 4px;
    width: 340px; max-height: 200px; overflow-y: auto;
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: var(--shadow-lg); padding: 8px 10px; z-index: 200;
    font-family: monospace; font-size: 0.82rem; line-height: 1.5;
    color: var(--muted); white-space: pre-wrap; word-break: break-all;
  }
  .todo-item:has(.job-spinner:not(.term-spinner):hover) .checkon-bubble:not(.done) { display: block; }
  .checkon-summary {
    display: none; font-family: monospace; font-size: 0.82rem; line-height: 1.5; color: var(--muted);
    background: rgba(0,0,0,0.025); border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 10px; margin-top: 8px; white-space: pre-wrap; word-break: break-word;
  }
  .checkon-summary .checkon-inline-spinner { display: inline-flex; vertical-align: baseline; margin-left: 6px; position: relative; top: 2px; }
  .checkon-header { font-size: 0.72rem; font-weight: 600; color: var(--subtle); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; padding-bottom: 3px; border-bottom: 1px solid var(--border); }
  .checkon-footer { font-size: 0.72rem; font-weight: 600; color: var(--subtle); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; padding-top: 3px; border-top: 1px solid var(--border); }
  .checkon-summary { display: none; }
  .todo-item.item-expanded .checkon-summary.has-content { display: block; }
  .todo-item:has(.job-spinner:not(.term-spinner):hover) { z-index: 200; overflow: visible; }
  .ea-update-btn {
    border: none; font-size: 0.72rem; font-weight: 600; letter-spacing: 0.02em;
    padding: 6px 14px; min-width: 72px; display: flex; align-items: center; justify-content: center; gap: 5px;
    background: var(--accent); color: #fff; border-radius: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12); cursor: pointer;
    transition: background 0.15s, box-shadow 0.15s, transform 0.1s;
  }
  .ea-update-btn:hover { background: #3a5bd9; box-shadow: 0 2px 8px rgba(79,110,247,0.35); transform: translateY(-1px); }
  .ea-update-btn:active { transform: translateY(0); box-shadow: 0 1px 2px rgba(0,0,0,0.1); }
  .ea-update-btn.running { background: transparent; border: 1px solid var(--accent); color: var(--accent); box-shadow: none; min-width: auto; }
  .ea-update-btn.running:hover { background: rgba(79,110,247,0.06); transform: none; box-shadow: none; }
  .ea-update-btn.running #ea-update-timer { display: none; }
  .ea-update-btn.running:hover #ea-update-timer { display: inline; }
  /* ml4-style scale transition for Update button */
  .ea-btn-wrap { position: relative; display: inline-flex; align-items: center; justify-content: center; height: 18px; min-width: 3.5em; }
  .ea-lbl, .ea-running { position: absolute; inset: 0; white-space: nowrap; display: flex; align-items: center; justify-content: center; gap: 5px; transform-origin: center; }
  @keyframes ea-btn-scale-in { from { opacity: 0; transform: scale(0.2); } to { opacity: 1; transform: scale(1); } }
  @keyframes ea-btn-scale-out { from { opacity: 1; transform: scale(1); } to { opacity: 0; transform: scale(3); } }
  .ea-lbl.anim-in, .ea-running.anim-in { animation: ea-btn-scale-in 350ms ease forwards; }
  .ea-lbl.anim-out, .ea-running.anim-out { animation: ea-btn-scale-out 250ms cubic-bezier(0.95,0.05,0.795,0.035) forwards; }
  .header-toggle {
    font-size: 0.7rem; padding: 3px 8px; border-radius: 12px;
    border: 1px solid var(--border); background: var(--card); color: var(--subtle);
    cursor: pointer; white-space: nowrap; transition: all 0.15s; font-weight: 500;
  }
  .header-toggle:hover { border-color: var(--accent); color: var(--accent); }
  .header-toggle.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .header-toggle.partial { background: var(--accent-light); color: var(--accent); border-color: var(--accent); }
  .fab-new {
    position: fixed; bottom: 24px; left: 24px; z-index: 900;
  }
  .fab-new .btn { font-size: 0.85rem; padding: 9px 22px; box-shadow: var(--shadow-lg); }
  .fab-help {
    position: fixed; bottom: 24px; right: 24px; z-index: 900;
  }
  .fab-help .btn { box-shadow: var(--shadow-lg); }
  .search-bar {
    margin-bottom: 10px;
    position: relative;
  }
  .search-clear-hint {
    display: none; position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
    font-size: 0.7rem; color: var(--subtle); background: var(--bg); padding: 2px 6px;
    border-radius: 4px; border: 1px solid var(--border); pointer-events: none;
  }
  .search-bar input.has-query ~ .search-clear-hint { display: block; }
  .search-bar input {
    width: 100%; padding: 9px 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.9rem; font-family: inherit; background: var(--card); color: var(--text);
    transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  }
  .search-bar input:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light);
  }
  .search-bar input.has-query {
    border-color: var(--accent); background: var(--accent-light);
    box-shadow: 0 0 0 2px var(--accent-light);
  }
  .search-bar input.has-query:focus {
    box-shadow: 0 0 0 3px var(--accent-light);
  }
  .search-bar input::placeholder { color: var(--subtle); }
  .sticky-header {
    position: sticky; top: 0; z-index: 102;
    background: var(--bg); padding-top: 10px; padding-bottom: 6px;
  }
  .active-header {
    top: var(--sticky-offset, 0px);
  }
  .section-header-row {
    top: var(--section-offset, 0px);
    margin-bottom: 10px;
  }
  .add-form input, .add-form textarea {
    width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 1rem; font-family: inherit; margin-bottom: 10px;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s; color: var(--text);
  }
  .add-form input:focus, .add-form textarea:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light);
  }
  .add-form textarea { resize: vertical; min-height: 56px; }
  .add-form .row { display: flex; gap: 8px; align-items: center; }
  .btn {
    padding: 9px 18px; border: none; border-radius: 8px; font-size: 0.9rem;
    font-weight: 600; cursor: pointer; transition: all 0.15s; letter-spacing: -0.01em;
  }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-hover); transform: translateY(-1px); box-shadow: var(--shadow-md); }
  .btn-primary:active { transform: translateY(0); }
  .btn-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); padding: 4px 10px; font-size: 0.75rem; }
  .btn-danger:hover { background: var(--danger); color: #fff; }
  .btn-sm { padding: 5px 12px; font-size: 0.75rem; }

  /* Todo items */
  .todo-item {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 14px 18px; margin-bottom: 6px; box-shadow: var(--shadow);
    display: flex; flex-direction: column; gap: 0;
    transition: all 0.2s ease;
    border-left: 3px solid transparent;
    position: relative; overflow: hidden;
  }
  .todo-header {
    display: flex; align-items: center; gap: 12px; width: 100%; position: relative;
  }
  .todo-item:hover { box-shadow: var(--shadow-md); transform: translateY(-1px); }
  .todo-item.status-completed {
    border-left-color: var(--completed-border); background: var(--completed-bg);
    opacity: 0.6;
  }
  .todo-item.status-completed:hover { opacity: 0.8; }
  .todo-item.dragging { opacity: 0.35; transform: scale(0.98); }
  .todo-item.drag-over-top { box-shadow: inset 0 3px 0 var(--accent); }
  .todo-item.drag-over-bottom { box-shadow: inset 0 -3px 0 var(--accent); }
  .section-header-row.drag-over-section { background: var(--accent-light); }
  .section-header-row.section-dragging { opacity: 0.35; }
  .section-header-row.section-drag-over-top { box-shadow: inset 0 3px 0 var(--accent); }
  .section-header-row.section-drag-over-bottom { box-shadow: inset 0 -3px 0 var(--accent); }
  .section-header-row[draggable="true"] { cursor: grab; }
  .section-header-row[draggable="true"]:active { cursor: grabbing; }

  .todo-checkbox {
    margin-top: 2px; width: 18px; height: 18px; cursor: pointer;
    accent-color: var(--accent); flex-shrink: 0;
    border-radius: 4px;
  }
  .todo-body { flex: 1; min-width: 0; }
  .todo-title { font-weight: 600; font-size: 1.02rem; word-break: break-word; letter-spacing: -0.01em; }
  .ea-update-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 100%;
    background-color: #f59e0b; margin-left: 6px; flex-shrink: 0;
    animation: sk-scaleout 1.0s infinite ease-in-out;
  }
  @keyframes sk-scaleout {
    0% { transform: scale(0); } 100% { transform: scale(1.0); opacity: 0; }
  }
  .todo-desc { color: var(--muted); font-size: 0.9rem; margin-top: 6px; word-break: break-word; line-height: 1.5; display: none; }
  .todo-item.item-expanded .todo-desc { display: block; }
  .todo-item.item-expanded .todo-meta { display: flex; }
  .todo-item.preview-expanded .todo-desc { display: block; }
  .todo-item.preview-expanded .todo-meta { display: flex; }
  .todo-desc p { margin: 0 0 0.4em; }
  .todo-desc p:last-child { margin-bottom: 0; }
  .todo-desc ul, .todo-desc ol { margin: 0.2em 0 0.4em 1.2em; padding: 0; }
  .todo-desc li { margin: 0.1em 0; }
  .todo-desc code { background: rgba(0,0,0,0.05); padding: 2px 5px; border-radius: 4px; font-size: 0.83em; }
  .todo-desc pre { background: rgba(0,0,0,0.03); padding: 10px; border-radius: 6px; overflow-x: auto; margin: 0.3em 0; }
  .todo-desc pre code { background: none; padding: 0; }
  .todo-desc a { color: var(--accent); text-decoration: none; }
  .todo-desc a.conv-link { font-family: monospace; font-size: 0.82rem; background: var(--accent-light); padding: 1px 6px; border-radius: 4px; }
  .todo-desc a:hover { text-decoration: underline; }
  .todo-desc h1, .todo-desc h2, .todo-desc h3 { font-size: 0.9em; margin: 0.4em 0 0.2em; }
  .todo-desc blockquote { border-left: 3px solid var(--border); margin: 0.3em 0; padding-left: 10px; color: var(--muted); }
  .todo-meta { display: none; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .priority-badge {
    font-size: 0.68rem; font-weight: 700; text-transform: uppercase; padding: 2px 0;
    border-radius: 12px; letter-spacing: 0.04em; flex-shrink: 0;
    width: 58px; text-align: center; display: inline-block;
  }
  .priority-high { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
  .priority-medium { background: #fffbeb; color: #a16207; border: 1px solid #fde68a; }
  .priority-low { background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }
  .priority-none { background: #f3f4f6; color: #9ca3af; border: 1px solid #e5e7eb; }
  .todo-item.priority-high-item { background: #fef8f8; }
  .todo-item.priority-none-item { background: #f3f4f6; opacity: 0.65; }
  .todo-item.priority-none-item:hover { opacity: 0.8; }

  .section-header-row {
    display: flex; align-items: center; gap: 8px;
    margin: 22px 0 10px; padding: 8px 0 6px;
    border-bottom: 2px solid var(--border);
    position: sticky; z-index: 100; background: var(--bg);
    transition: opacity 0.1s;
  }
  .section-header-row::after {
    content: ''; position: absolute; left: -4px; right: -4px; top: 100%;
    height: 24px; background: linear-gradient(to bottom, var(--bg), transparent);
    pointer-events: none;
  }
  .section-header-row h3 {
    font-size: 1rem; color: var(--muted); font-weight: 400; margin: 0;
    letter-spacing: -0.01em; cursor: pointer;
  }
  .section-header-row h3:hover { color: var(--accent); }
  .section-rename-input {
    font-size: 0.92rem; font-weight: 700; border: 1px solid var(--accent);
    border-radius: 6px; padding: 2px 8px; outline: none; font-family: inherit;
    box-shadow: 0 0 0 3px var(--accent-light); background: var(--card);
  }
  .section-count {
    font-size: 0.75rem; color: var(--subtle); font-weight: 500;
    background: rgba(0,0,0,0.04); padding: 1px 8px; border-radius: 12px;
  }
  .collapse-btn {
    font-size: 0.6rem; padding: 2px 6px; border-radius: 5px;
    border: 1px solid transparent; background: transparent; color: var(--subtle);
    cursor: pointer; transition: all 0.2s ease; line-height: 1;
  }
  .collapse-btn:hover { background: var(--accent-light); color: var(--accent); }
  .collapse-btn.collapsed { transform: rotate(-90deg); }
  .sort-priority-btn {
    font-size: 0.65rem; padding: 3px 10px; border-radius: 12px;
    border: 1px solid var(--border); background: var(--card); color: var(--subtle);
    cursor: pointer; white-space: nowrap; transition: all 0.15s; margin-left: auto;
    font-weight: 500;
  }
  .sort-priority-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }

  .todo-item { --item-bg: var(--card); }
  .todo-item.priority-high-item { --item-bg: #fef8f8; }
  .todo-item.priority-none-item { --item-bg: #f3f4f6; }
  .todo-item.status-completed { --item-bg: var(--completed-bg); }
  .todo-actions {
    display: flex; gap: 2px; align-items: center; justify-content: flex-end;
    position: absolute; right: 70px; top: 0; bottom: 0;
    padding-left: 24px; opacity: 0; transition: opacity 0.15s;
    background: linear-gradient(to right, transparent, var(--item-bg) 20px);
  }
  .todo-item:hover .todo-actions { opacity: 1; }
  .todo-actions select { font-size: 0.75rem; padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border); background: #fafafa; }

  .empty-state { text-align: center; color: var(--subtle); padding: 48px 0; font-size: 0.95rem; }

  .shortcuts-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4);
    z-index: 3000; justify-content: center; align-items: center;
  }
  .shortcuts-overlay.visible { display: flex; }
  .shortcuts-dialog {
    background: var(--card); border-radius: var(--radius-lg); padding: 24px 28px;
    box-shadow: var(--shadow-lg); max-width: 480px; width: 90%; max-height: 80vh;
    overflow-y: auto;
  }
  .shortcuts-dialog h2 { margin: 0 0 16px; font-size: 1.1rem; }
  .shortcuts-dialog table { width: 100%; border-collapse: collapse; }
  .shortcuts-dialog td { padding: 4px 0; font-size: 0.85rem; }
  .shortcuts-dialog td:first-child { font-weight: 600; white-space: nowrap; width: 120px; }
  .shortcuts-dialog td kbd {
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    padding: 1px 6px; font-size: 0.78rem; font-family: inherit;
  }
  .shortcuts-dialog .shortcut-section { color: var(--accent); font-weight: 700; font-size: 0.8rem; padding: 10px 0 4px; text-transform: uppercase; letter-spacing: 0.04em; }
  .settings-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4);
    z-index: 3000; justify-content: center; align-items: center;
  }
  .settings-overlay.visible { display: flex; }
  .settings-dialog {
    background: var(--card); border-radius: var(--radius-lg); padding: 24px 28px;
    box-shadow: var(--shadow-lg); max-width: 480px; width: 90%; max-height: 85vh; overflow-y: auto;
  }
  .mcp-server-row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 0.8rem; }
  .mcp-server-row:last-child { border-bottom: none; }
  .mcp-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .mcp-dot.connected { background: #22c55e; }
  .mcp-dot.disconnected { background: #ef4444; }
  .mcp-dot.disabled { background: #6b7280; }
  .mcp-server-toggle { width: 36px; height: 20px; border-radius: 10px; border: none; cursor: pointer; position: relative; transition: background 0.2s; flex-shrink: 0; }
  .mcp-server-toggle.on { background: #22c55e; }
  .mcp-server-toggle.off { background: #374151; }
  .mcp-server-toggle::after { content: ''; position: absolute; top: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: left 0.2s; }
  .mcp-server-toggle.on::after { left: 18px; }
  .mcp-server-toggle.off::after { left: 2px; }
  .mcp-tools-list { padding: 4px 0 4px 24px; font-size: 0.75rem; }
  .mcp-tool-row { display: flex; align-items: center; gap: 6px; padding: 2px 0; color: var(--muted); }
  .mcp-tool-row .tool-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mcp-tool-btn { font-size: 0.65rem; padding: 1px 6px; border-radius: 4px; border: 1px solid var(--border); background: none; color: var(--subtle); cursor: pointer; }
  .mcp-tool-btn:hover { border-color: var(--accent); color: var(--accent); }
  .mcp-tool-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .mcp-tool-btn.danger { border-color: #ef4444; color: #ef4444; }
  .mcp-tool-btn.danger:hover { background: #ef4444; color: #fff; }
  .mcp-tool-btn.danger.active { background: #ef4444; color: #fff; }
  .settings-dialog h2 { margin: 0 0 16px; font-size: 1.1rem; }
  .settings-dialog label { display: block; font-size: 0.82rem; font-weight: 600; margin: 12px 0 4px; color: var(--fg); }
  .settings-dialog label:first-of-type { margin-top: 0; }
  .settings-dialog input, .settings-dialog select {
    width: 100%; box-sizing: border-box; padding: 7px 10px; border: 1px solid var(--border);
    border-radius: var(--radius); font-size: 0.85rem; background: var(--bg); color: var(--fg);
    font-family: inherit;
  }
  .settings-dialog input:focus, .settings-dialog select:focus { outline: none; border-color: var(--accent); }
  .settings-dialog .settings-actions { display: flex; gap: 8px; margin-top: 18px; justify-content: flex-end; }
  .settings-dialog .settings-actions button { font-size: 0.82rem; padding: 6px 16px; }
  .settings-dialog .settings-hint { font-size: 0.75rem; color: var(--subtle); margin-top: 2px; }

  /* Edit mode */
  .edit-title {
    font-size: 1rem; width: 100%; padding: 10px 14px;
    border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s;
    font-family: inherit; color: var(--text);
  }
  .edit-title:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }
  .edit-desc {
    font-size: 0.9rem; width: 100%; padding: 10px 14px;
    border: 1px solid var(--border); border-radius: 8px; resize: vertical;
    min-height: 56px; font-family: inherit; overflow: hidden;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s;
  }
  .edit-desc:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }
  .edit-select {
    width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.85rem; font-family: inherit; margin-bottom: 10px;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s; color: var(--text);
  }
  .edit-select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }
  .edit-desc-cm { width: 100%; }
  .edit-desc-cm .cm-editor {
    font-size: 0.8rem; border: 1px solid var(--border); border-radius: 8px;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s;
  }
  .edit-desc-cm .cm-editor.cm-focused {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light);
  }
  .edit-desc-cm .cm-content { min-height: 56px; padding: 10px 14px; font-family: inherit; }
  .edit-desc-cm .cm-scroller { overflow: auto; }
  .edit-desc-cm .cm-line { line-height: 1.6; }
  .edit-select {
    width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.85rem; font-family: inherit; margin-bottom: 10px;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s; color: var(--text);
  }
  .edit-select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }
  .edit-actions { display: flex; gap: 8px; margin-top: 10px; align-items: center; }

  /* Section headers */
  .section-header {
    font-size: 0.95rem; color: var(--text); margin: 18px 0 8px; font-weight: 600;
    padding-bottom: 4px; border-bottom: 1px solid var(--border);
    display: none;
  }

  /* Keyboard-selected item */
  .todo-item.kb-selected {
    box-shadow: 0 0 0 2px var(--accent), var(--shadow);
    border-left-color: var(--accent);
  }
  .section-header-row.kb-selected {
    box-shadow: 0 0 0 2px var(--accent);
    border-radius: var(--radius);
    z-index: 101;
  }

  /* Context menu */
  .ctx-menu {
    display: none; position: fixed; z-index: 1000;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow-lg); min-width: 190px;
    padding: 5px 0; font-size: 0.9rem;
    backdrop-filter: blur(10px);
  }
  .ctx-menu.visible { display: block; }
  .ctx-menu-item {
    padding: 8px 14px; cursor: pointer; display: flex; align-items: center; gap: 8px;
    color: var(--text); user-select: none; position: relative;
    border-radius: 6px; margin: 1px 4px; transition: background 0.1s;
  }
  .ctx-menu-item:hover { background: var(--accent); color: #fff; }
  .ctx-menu-item.has-submenu::after {
    content: '\25B6'; font-size: 0.6rem; margin-left: auto; opacity: 0.5;
  }
  .ctx-menu-item:hover.has-submenu::after { opacity: 1; }
  .ctx-menu-sep { border-top: 1px solid var(--border); margin: 4px 8px; }
  .ctx-submenu {
    display: none; position: absolute; left: 100%; top: -5px;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow-lg); min-width: 160px;
    padding: 5px 0; backdrop-filter: blur(10px);
  }
  .ctx-menu-item:hover > .ctx-submenu { display: block; }
  .ctx-submenu .ctx-menu-item { padding: 7px 14px; font-size: 0.82rem; }
  .ctx-submenu .ctx-menu-item.active-section { font-weight: 700; opacity: 0.5; pointer-events: none; }

  /* Section picker dialog */
  .section-picker {
    display: none; position: fixed; z-index: 1100;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow-lg); min-width: 200px; max-width: 280px;
    padding: 6px 0; font-size: 0.9rem;
    backdrop-filter: blur(10px);
  }
  .section-picker.visible { display: block; }
  .section-picker-item {
    padding: 7px 14px; cursor: pointer; color: var(--text);
    border-radius: 6px; margin: 1px 4px; transition: background 0.1s;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .section-picker-item:hover { background: var(--accent-light); }
  .section-picker-item.sp-selected { background: var(--accent); color: #fff; }
  .section-picker-item.sp-current { opacity: 0.45; pointer-events: none; }
  .section-picker-input {
    width: calc(100% - 12px); margin: 4px 6px 4px; padding: 6px 10px;
    font-size: 0.88rem; font-family: inherit; border: 1px solid var(--border);
    border-radius: 6px; outline: none; background: var(--bg); color: var(--text);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .section-picker-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }

  /* Kbd styling */
  kbd {
    padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px;
    background: var(--card); font-size: 0.75rem; font-family: inherit;
    box-shadow: 0 1px 1px rgba(0,0,0,0.06);
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--subtle); }

  /* Swipe-to-complete (mobile) */
  .swipe-reveal {
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    display: flex; align-items: center; padding-left: 24px;
    background: #22c55e; color: #fff; font-size: 1.5rem; font-weight: 700;
    opacity: 0; pointer-events: none; border-radius: var(--radius);
    transition: opacity 0.15s;
  }
  .swipe-reveal-undo {
    background: #f59e0b;
  }
  .swipe-reveal-icon {
    transition: transform 0.15s;
  }
  .swipe-active .swipe-reveal { opacity: 1; }
  .swipe-threshold .swipe-reveal-icon { transform: scale(1.4); }
  .swipe-content {
    position: relative; background: inherit; z-index: 1;
    border-radius: inherit;
  }
  .swiping .swipe-content { transition: none; }
  .snap-back .swipe-content { transition: transform 0.25s cubic-bezier(0.2,0.8,0.4,1); transform: translateX(0) !important; }
  .snap-complete .swipe-content { transition: transform 0.2s ease-in; }

  /* Inline job spinner — SpinKit double bounce, morphs to ✕ on hover */
  .job-spinner {
    display: inline-flex; align-items: center; width: 13px; height: 13px; flex-shrink: 0;
    vertical-align: middle; margin-right: 5px; position: relative; cursor: pointer;
  }
  .job-spinner::after {
    content: '✕'; position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%); font-size: 9px;
    color: var(--danger); opacity: 0; transition: opacity 0.15s; pointer-events: none;
  }
  .job-spinner:hover l-jelly-triangle { opacity: 0; }
  .job-spinner:hover .sk-child { opacity: 0 !important; }
  .job-spinner:hover::after { opacity: 1; }
  /* Terminal session indicator */
  .term-spinner .sk-child {
    width: 100%; height: 100%; border-radius: 50%;
    background: #22c55e; opacity: 0.6;
    position: absolute; top: 0; left: 0;
    animation: sk-doubleBounce 2s infinite ease-in-out;
    transition: opacity 0.15s;
  }
  .term-spinner .sk-bounce2 { animation-delay: -1s; }
  @keyframes sk-doubleBounce { 0%, 100% { transform: scale(0); } 50% { transform: scale(1); } }
  .term-spinner::after { content: '↑'; color: #22c55e; font-size: 11px; font-weight: 700; }
  .chat-spinner::after { content: '↑'; color: #22c55e; font-size: 11px; font-weight: 700; }
  .chat-spinner:hover l-jelly-triangle { opacity: 1; }
  /* Terminal overlay */
  #terminal-overlay.visible { display: flex !important; }
  #terminal-container { width: 100%; }
  #terminal-container .xterm { width: 100% !important; height: 100% !important; }
  .xterm-viewport::-webkit-scrollbar { width: 6px; }
  .xterm-viewport::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 3px; }
  /* Chat overlay */
  #chat-overlay.visible { display: flex !important; }
  .chat-log { flex: 1; overflow-y: auto; padding: 16px 20px; font-family: 'Manrope', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 0.85rem; line-height: 1.55; color: #cbd5e1; word-break: break-word; }
  .chat-log::-webkit-scrollbar { width: 6px; }
  .chat-log::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); border-radius: 3px; }
  .chat-user-line { color: #93c5fd; font-weight: 500; padding: 6px 10px; background: rgba(79,110,247,0.08); border-radius: 6px; border-left: 3px solid var(--accent); }
  .chat-tool-line { color: rgba(255,255,255,0.4); font-size: 0.75rem; font-family: monospace; padding: 2px 10px; }
  .chat-cost-line { color: rgba(255,255,255,0.4); font-size: 0.75rem; font-family: monospace; padding: 2px 10px; }
  .chat-turn-sep { border: none; border-top: 1px solid rgba(255,255,255,0.06); margin: 12px 0; }
  .chat-input-bar { display: flex; gap: 8px; padding: 10px 16px; border-top: 1px solid rgba(255,255,255,0.1); flex-shrink: 0; background: rgba(0,0,0,0.15); }
  .chat-input { flex: 1; background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.1); color: #e2e8f0; padding: 10px 14px; border-radius: 10px; font-family: inherit; font-size: 0.85rem; outline: none; transition: border-color 0.15s; }
  .chat-input:focus { border-color: rgba(79,110,247,0.5); }
  .chat-input::placeholder { color: rgba(255,255,255,0.25); }
  .chat-send-btn { background: var(--accent); border: none; color: #fff; padding: 10px 18px; border-radius: 10px; cursor: pointer; font-size: 0.8rem; font-weight: 600; transition: background 0.15s, transform 0.1s; }
  .chat-send-btn:hover { background: #3a5bd9; transform: translateY(-1px); }
  .chat-send-btn:active { transform: translateY(0); }
  .chat-send-btn:disabled { opacity: 0.4; cursor: default; transform: none; }
  .chat-send-btn.chat-stop-mode { background: rgba(239,68,68,0.6); }
  .chat-send-btn.chat-stop-mode:hover { background: rgba(239,68,68,0.8); }
  .chat-unread-dot {
    display: inline-flex; align-items: center; margin-right: 2px; flex-shrink: 0;
  }
  .chat-assistant-block { margin: 4px 0; padding: 4px 10px; color: #e2e8f0; }
  .chat-assistant-block p { margin: 0; }
  .chat-assistant-block p + p { margin-top: 0.4em; }
  .chat-assistant-block pre { margin: 0.4em 0; padding: 8px 10px; background: rgba(0,0,0,0.3); border-radius: 6px; font-size: 0.8rem; overflow-x: auto; }
  .chat-assistant-block code { font-size: 0.8rem; background: rgba(0,0,0,0.25); padding: 1px 4px; border-radius: 3px; }
  .chat-assistant-block pre code { background: none; padding: 0; }
  .chat-assistant-block ul, .chat-assistant-block ol { margin: 0.2em 0; padding-left: 1.4em; }
  .chat-assistant-block h1, .chat-assistant-block h2, .chat-assistant-block h3, .chat-assistant-block h4 { margin: 0.5em 0 0.2em; color: #f1f5f9; }
  .chat-assistant-block blockquote { margin: 0.3em 0; padding-left: 0.8em; border-left: 3px solid rgba(79,110,247,0.4); color: #94a3b8; }
  .chat-assistant-block a { color: #60a5fa; text-decoration: none; }
  .chat-assistant-block a:hover { text-decoration: underline; }
  .chat-user-line { margin: 4px 0; }
  .chat-tool-line { margin: 2px 0; }
  .chat-cost-line { margin: 2px 0; }
  .chat-turn-sep { margin: 10px 0; }
  /* Inline job output — only visible when item is expanded */
  .item-job-output {
    display: none; font-family: monospace; font-size: 0.72rem; line-height: 1.5;
    color: var(--muted); white-space: pre-wrap; word-break: break-all;
    background: rgba(0,0,0,0.025); border-radius: 6px;
    padding: 6px 10px; margin-top: 8px; max-height: 200px; overflow-y: auto;
    border: 1px solid var(--border);
  }
  .todo-item.item-expanded .item-job-output { display: block; }
  .job-done-line { color: var(--accent); font-weight: 600; }
</style>
</head>
<body>

<div class="fab-new">
  <button class="btn btn-primary" id="add-toggle-btn" onclick="showAddForm()">+ New <span style="opacity:0.6;font-weight:400;font-size:0.8em">(n)</span></button>
</div>
<div class="fab-help" style="display:flex;gap:6px">
  <button class="btn btn-sm" onclick="showSettings()" style="border:1px solid var(--border);font-size:0.82rem;padding:5px 10px" title="Settings">&#9881;</button>
  <button class="btn btn-sm" onclick="showShortcuts()" style="border:1px solid var(--border);font-size:0.75rem;padding:5px 10px" title="Keyboard shortcuts (?)">?</button>
</div>

<div class="sticky-header">
  <div class="search-bar">
    <input type="text" id="search-input" placeholder="Search todos... (/)">
    <kbd class="search-clear-hint" id="search-clear-hint">Esc to clear</kbd>
  </div>
</div>

<div class="add-form" id="add-form">
  <input class="edit-title" type="text" id="new-title" placeholder="What needs to be done?">
  <textarea id="new-desc" placeholder="Description (optional)"></textarea>
  <select class="edit-select" id="new-section">
    <option value="">No section</option>
  </select>
  <input class="edit-title" type="text" id="new-section-custom" placeholder="New section name" style="display:none;">
  <div class="row">
    <select class="edit-select" id="new-priority" style="width:auto;margin-bottom:0">
      <option value="high">High</option>
      <option value="medium" selected>Medium</option>
      <option value="low">Low</option>
      <option value="none">None</option>
    </select>
    <button class="btn btn-primary btn-sm" onclick="addTodo()">Add Todo</button>
    <button class="btn btn-sm" onclick="hideAddForm()" style="border:1px solid var(--border)">Cancel <span style="opacity:0.6;font-weight:400">Esc</span></button>
  </div>
</div>

<div id="active-section"></div>
<div id="completed-section"></div>

<!-- Context menu -->
<div class="ctx-menu" id="ctx-menu"></div>
<!-- Section picker -->
<div class="section-picker" id="section-picker"></div>

<div class="shortcuts-overlay" id="shortcuts-overlay" onclick="if(event.target===this)hideShortcuts()">
  <div class="shortcuts-dialog">
    <h2>Keyboard Shortcuts</h2>
    <table>
      <tr><td colspan="2" class="shortcut-section">Navigation</td></tr>
      <tr><td><kbd>j</kbd> / <kbd>&#8595;</kbd></td><td>Move down</td></tr>
      <tr><td><kbd>k</kbd> / <kbd>&#8593;</kbd></td><td>Move up</td></tr>
      <tr><td><kbd>&#8984;&#8595;</kbd></td><td>Next section</td></tr>
      <tr><td><kbd>&#8984;&#8593;</kbd></td><td>Previous section</td></tr>
      <tr><td><kbd>&#8592;</kbd></td><td>Collapse section</td></tr>
      <tr><td><kbd>&#8594;</kbd></td><td>Expand section</td></tr>
      <tr><td><kbd>Alt+&#9166;</kbd></td><td>Collapse/expand all</td></tr>
      <tr><td colspan="2" class="shortcut-section">Actions</td></tr>
      <tr><td><kbd>n</kbd></td><td>New todo</td></tr>
      <tr><td><kbd>e</kbd></td><td>Edit selected</td></tr>
      <tr><td><kbd>Space</kbd></td><td>Toggle complete</td></tr>
      <tr><td><kbd>s</kbd></td><td>Open terminal</td></tr>
      <tr><td><kbd>c</kbd></td><td>Copy ID</td></tr>
      <tr><td><kbd>t</kbd></td><td>Bring to top</td></tr>
      <tr><td><kbd>&#8984;&#9003;</kbd></td><td>Delete</td></tr>
      <tr><td><kbd>&#8984;Z</kbd></td><td>Undo</td></tr>
      <tr><td><kbd>Enter</kbd></td><td>Toggle selected item description</td></tr>
      <tr><td><kbd>a</kbd></td><td>Toggle simple mode (hide descriptions)</td></tr>
      <tr><td colspan="2" class="shortcut-section">Priority</td></tr>
      <tr><td><kbd>1</kbd> <kbd>2</kbd> <kbd>3</kbd> <kbd>0</kbd></td><td>Set high / medium / low / none</td></tr>
      <tr><td><kbd>p</kbd></td><td>Sort section by priority</td></tr>
      <tr><td><kbd>Ctrl+1</kbd>...<kbd>0</kbd></td><td>Filter by priority</td></tr>
      <tr><td colspan="2" class="shortcut-section">Reorder</td></tr>
      <tr><td><kbd>Alt+j</kbd> / <kbd>Alt+k</kbd></td><td>Move item up/down</td></tr>
      <tr><td><kbd>Shift+J</kbd> / <kbd>K</kbd></td><td>Move to adjacent section</td></tr>
      <tr><td><kbd>Alt+&#8594;</kbd></td><td>Move to section picker</td></tr>
      <tr><td colspan="2" class="shortcut-section">Search</td></tr>
      <tr><td><kbd>/</kbd></td><td>Focus search</td></tr>
      <tr><td><kbd>Esc</kbd></td><td>Clear search / deselect</td></tr>
      <tr><td><kbd>?</kbd></td><td>Show this dialog</td></tr>
    </table>
  </div>
</div>

<div class="settings-overlay" id="settings-overlay" onclick="if(event.target===this)hideSettings()">
  <div class="settings-dialog">
    <h2>Settings</h2>
    <label for="settings-active-provider">Active Provider</label>
    <select id="settings-active-provider" onchange="_onActiveProviderChange()"></select>
    <div class="settings-hint">Which inference endpoint to use for chat.</div>
    <div id="provider-list"></div>
    <hr style="border:none;border-top:1px solid var(--border);margin:16px 0 12px">
    <button class="btn btn-sm" onclick="_addProvider()" style="border:1px solid var(--border);width:100%">+ Add Provider</button>
    <hr style="border:none;border-top:1px solid var(--border);margin:16px 0 12px">
    <div style="display:flex;align-items:center;gap:8px;margin:4px 0">
      <input type="checkbox" id="settings-subagents" style="margin:0;width:auto;flex-shrink:0">
      <span style="font-size:0.82rem;font-weight:600;cursor:pointer" onclick="document.getElementById('settings-subagents').click()">Enable subagents</span>
      <span style="font-size:0.75rem;color:var(--subtle)">max</span>
      <input type="number" id="settings-max-subagents" min="1" max="20" value="10" style="width:50px;padding:2px 6px;font-size:0.8rem;text-align:center">
    </div>
    <div class="settings-hint" style="margin-top:2px">Allow the model to spawn parallel subagents for tasks like /ea update and /ea triage.</div>
    <hr style="border:none;border-top:1px solid var(--border);margin:16px 0 12px">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span style="font-size:0.82rem;font-weight:600">MCP Servers</span>
      <button class="btn btn-sm" onclick="_refreshMcp()" style="border:1px solid var(--border);font-size:0.7rem;padding:2px 8px">Reconnect</button>
    </div>
    <div id="mcp-server-list" style="margin-bottom:8px;position:relative;min-height:30px">
      <div id="mcp-loading" style="display:none;position:absolute;inset:0;background:rgba(30,30,30,0.5);align-items:center;justify-content:center;border-radius:6px;z-index:10"><l-bouncy size="20" speed="1.75" color="var(--accent)"></l-bouncy></div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;margin:4px 0">
      <input type="checkbox" id="mcp-auto-approve-all" onchange="_toggleAutoApproveAll(this.checked)" style="margin:0;width:auto;flex-shrink:0">
      <span style="font-size:0.82rem;font-weight:600;cursor:pointer" onclick="document.getElementById('mcp-auto-approve-all').click()">Bypass tool approvals</span>
    </div>
    <div class="settings-hint" style="margin-top:2px">Skip approval prompts and allow the model to run all MCP tools automatically.</div>
    <hr style="border:none;border-top:1px solid var(--border);margin:16px 0 12px">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span style="font-size:0.82rem;font-weight:600">Version History</span>
      <button class="btn btn-sm" onclick="_gitCommit()" style="border:1px solid var(--border);font-size:0.7rem;padding:2px 8px">Save Snapshot</button>
    </div>
    <div id="git-log-list" style="max-height:200px;overflow-y:auto;margin-bottom:8px"></div>
    <div class="settings-actions">
      <button class="btn btn-sm" onclick="_logout()" style="border:1px solid var(--border);color:#ef4444;margin-right:auto">Logout</button>
      <button class="btn btn-sm" onclick="hideSettings()" style="border:1px solid var(--border)">Cancel</button>
      <button class="btn btn-sm btn-primary" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<script>
const API = '/api/todos';
let allTodos = [];
let editingId = null;
let cmEditor = null;
let cmModules = null; // lazily loaded CodeMirror modules
let lastMtime = 0;
let pollTimer = null;
let selectedIdx = -1; // -1 = nothing, 0 = add-form, 1+ = todo items
let insertBeforeId = null; // when adding, insert before this todo id
let searchQuery = ''; // fuzzy search filter
let showPriorities = new Set();  // colored: only show these
let hidePriorities = new Set();  // greyed: hide these
let ctxTargetId = null; // id of todo targeted by context menu
let visibleIds = []; // ordered list of todo ids as rendered
let sectionsOrder = []; // ordered list of section names as rendered
let addFormVisible = false;
let filterActiveSessions = false; // only show items with active terminal sessions
let filterUnread = false; // only show items with unread updates
let previewMode = false; // auto-expand selected item
let previewExpandedId = null; // item currently auto-expanded by preview
const expandedItems = new Set(); // items whose descriptions are expanded
const collapsedSections = new Set(['__completed__']); // collapsed section names
const _seenUpdates = new Set(); // todo IDs whose ⚡ update has been viewed
const SEL_ADD = 0; // index for the add-form position

async function loadTodos() {
  const res = await fetch(API);
  allTodos = await res.json();
  // Update our known mtime so polling doesn't re-trigger
  try {
    const mt = await fetch(API + '/mtime');
    const d = await mt.json();
    lastMtime = d.mtime;
  } catch(e) {}
  render();
}

// Poll for external file changes every 1.5s
async function pollForChanges() {
  try {
    const res = await fetch(API + '/mtime');
    const data = await res.json();
    if (data.mtime !== lastMtime) {
      lastMtime = data.mtime;
      // Don't reload if user is editing
      if (!editingId) {
        const res2 = await fetch(API);
        allTodos = await res2.json();
        render();
      }
    }
  } catch(e) {}
  // Also poll jobs and terminal sessions
  await pollJobs();
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollForChanges, 1500);
}

function render() {
  const active = allTodos.filter(t => t.status !== 'completed');
  const completed = allTodos.filter(t => t.status === 'completed');

  const activeEl = document.getElementById('active-section');
  const completedEl = document.getElementById('completed-section');

  // Rescue add-form before innerHTML overwrites it (it may be inside activeEl)
  const form = document.getElementById('add-form');
  const formWasVisible = addFormVisible;
  const formTitle = form.querySelector('#new-title')?.value || '';
  const formDesc = form.querySelector('#new-desc')?.value || '';
  const formPriority = form.querySelector('#new-priority')?.value || 'medium';
  const formSection = form.querySelector('#new-section')?.value || '';
  const formSectionCustom = form.querySelector('#new-section-custom')?.value || '';
  const searchBar = document.querySelector('.search-bar');
  if (searchBar) searchBar.after(form);

  // Apply search and priority filters
  const searching = searchQuery.trim().length > 0;

  const searchTokens = searching ? searchQuery.toLowerCase().trim().split(/\s+/) : [];
  const matchesSearch = t => {
    const text = ((t.title || '') + ' ' + (t.description || '') + ' ' + (t.section || '')).toLowerCase();
    return searchTokens.every(tok => text.includes(tok));
  };
  const afterSearch = f => searching ? f.filter(matchesSearch) : f;
  const afterPriority = f => {
    if (showPriorities.size === 0 && hidePriorities.size === 0) return f;
    return f.filter(t => {
      const p = t.priority || 'medium';
      if (showPriorities.size > 0) return showPriorities.has(p);
      return !hidePriorities.has(p);
    });
  };
  const afterSessions = f => {
    if (!filterActiveSessions) return f;
    // Sort by activity: active sessions/chats/unread first (most recent on top), rest below
    const activityScore = t => {
      const hasTerminal = _termSessions[t.id] && _termSessions[t.id].alive;
      const hasChatStream = _chatSessions[t.id] && _chatSessions[t.id].streamingJobId;
      const hasUnreadChat = _chatUnread.has(t.id);
      const hasUpdated = _parseTitle(t.title || '').hasUpdatedTag && !_seenUpdates.has(t.id);
      const isActive = hasTerminal || hasChatStream || hasUnreadChat || hasUpdated;
      if (!isActive) return 0;
      // Find most recent job for this todo
      let latest = 0;
      for (const j of Object.values(_jobsState)) {
        if (_todoIdForJob(j) === t.id && j.created_at > latest) latest = j.created_at;
      }
      return latest || 1; // 1 = active but no job timestamp
    };
    return [...f].sort((a, b) => activityScore(b) - activityScore(a));
  };
  const afterUnread = f => {
    if (!filterUnread) return f;
    return f.filter(t => (_parseTitle(t.title || '').hasUpdatedTag && !_seenUpdates.has(t.id)) || _chatUnread.has(t.id));
  };
  const filteredActive = afterUnread(afterSessions(afterPriority(afterSearch(active))));
  const filteredCompleted = afterUnread(afterSessions(afterPriority(afterSearch(completed))));


  if (filteredActive.length === 0 && filteredCompleted.length === 0 && !searching && allTodos.length === 0) {
    activeEl.innerHTML = '<div class="empty-state">No todos yet. Press <strong>n</strong> to add one!</div>';
    completedEl.innerHTML = '';
    visibleIds = [];
    if (formWasVisible) _restoreInlineForm(form, formTitle, formDesc, formPriority, formSection, formSectionCustom);
    applySelection();
    return;
  }

  // Group active by section preserving order of first appearance
  let activeHtml = '';
  const visibleActive = []; // track which active items are visible (not collapsed)
  const visibleActiveIds = []; // mix of todo IDs and '__section__:Name' markers for collapsed sections

  if (filterActiveSessions) {
    // Flat list, no section grouping — sorted by activity
    sectionsOrder = [];
    activeHtml = filteredActive.map(t => renderTodo(t)).join('');
    visibleActive.push(...filteredActive);
    visibleActiveIds.push(...filteredActive.map(t => t.id));
  } else {
    sectionsOrder = [];
    const seenSections = new Set();
    filteredActive.forEach(t => {
      const s = t.section || '';
      if (!seenSections.has(s)) { sectionsOrder.push(s); seenSections.add(s); }
    });

    sectionsOrder.forEach(section => {
      const items = filteredActive.filter(t => (t.section || '') === section);
      const isCollapsed = !searching && collapsedSections.has(section);
      const escSection = esc(section).replace(/'/g, "\\'");
      if (section) {
        activeHtml += `<div class="section-header-row" data-section="${esc(section)}" draggable="true">`
          + `<button class="collapse-btn${isCollapsed ? ' collapsed' : ''}" onclick="toggleSectionCollapse('${escSection}')" title="${isCollapsed ? 'Expand' : 'Collapse'}">&#9660;</button>`
          + `<h3 onclick="toggleSectionCollapse('${escSection}')" ondblclick="startSectionRename('${escSection}')">${esc(section)}</h3>`
          + `<span class="section-count">${items.length}</span>`
          + `<button class="sort-priority-btn" onclick="sortByPriority('${escSection}')" title="Sort by priority (high first)">&#9650; Priority</button>`
          + `</div>`;
      }
      if (isCollapsed) {
        if (section) visibleActiveIds.push('__section__:' + section);
      } else {
        activeHtml += items.map(t => renderTodo(t)).join('');
        visibleActive.push(...items);
        visibleActiveIds.push(...items.map(t => t.id));
      }
    });
  }

  // visibleIds includes todo IDs + section markers for collapsed sections
  visibleIds = [...visibleActiveIds, ...filteredCompleted.map(t => t.id)];

  const eaBtn = '<div class="ea-update-wrap"><button id="ea-update-btn" class="btn btn-sm ea-update-btn" onclick="eaUpdateToggle()" title="Run /ea update"><span class="ea-btn-wrap"><span id="ea-update-label" class="ea-lbl" style="opacity:1">Update</span><span id="ea-update-running" class="ea-running" style="opacity:0"><l-mirage size="28" speed="2.5" color="' + getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() + '"></l-mirage><span id="ea-update-timer">0:00</span></span></span></button><div id="ea-update-bubble" class="ea-update-bubble"></div></div>';
  const totalItems = filteredActive.length;
  const expandedCount = filteredActive.filter(t => expandedItems.has(t.id)).length;
  const simpleCls = expandedCount === 0 ? ' active' : (expandedCount < totalItems ? ' partial' : '');
  const simpleBtn = `<button class="header-toggle simple-toggle-btn${simpleCls}" onclick="toggleSimpleMode()" title="Toggle simple mode (a)">Simple</button>`;
  const pColors = {high:'#b91c1c',medium:'#a16207',low:'#15803d',none:'#9ca3af'};
  const pBg = {high:'#fef2f2',medium:'#fffbeb',low:'#f0fdf4',none:'#f3f4f6'};
  const pBorder = {high:'#fecaca',medium:'#fde68a',low:'#bbf7d0',none:'#e5e7eb'};
  const filterBtns = ['high','medium','low','none'].map(p => {
    const isShow = showPriorities.has(p);
    const isHide = hidePriorities.has(p);
    let style;
    if (isShow) style = `background:${pColors[p]};color:#fff;border-color:${pColors[p]}`;
    else if (isHide) style = `background:var(--border);color:var(--subtle);border-color:var(--border);text-decoration:line-through`;
    else style = `background:${pBg[p]};color:${pColors[p]};border-color:${pBorder[p]}`;
    const label = {high:'H',medium:'M',low:'L',none:'0'}[p];
    return `<button class="header-toggle" style="${style}" onclick="cycleFilter('${p}')" title="Filter ${p}">${label}</button>`;
  }).join('');
  const previewBtn = `<button class="header-toggle preview-toggle-btn${previewMode ? ' active' : ''}" onclick="togglePreviewMode()" title="Preview mode: auto-expand selected (v)">Preview</button>`;
  const sessionsBtn = `<button class="header-toggle${filterActiveSessions ? ' active' : ''}" onclick="toggleFilterSessions()" title="Filter by active sessions (Ctrl+S)" style="${filterActiveSessions ? '' : 'color:var(--subtle)'}">Sessions</button>`;
  const unreadBtn = `<button class="header-toggle${filterUnread ? ' active' : ''}" onclick="toggleFilterUnread()" title="Filter by unread updates" style="${filterUnread ? 'background:#f59e0b;color:#fff;border-color:#f59e0b' : 'color:#f59e0b;border-color:#f59e0b'}">Unread</button>`;
  const activeSections = sectionsOrder.filter(s => s);
  const collapsedCount = activeSections.filter(s => collapsedSections.has(s)).length;
  const allCollapsed = activeSections.length > 0 && collapsedCount === activeSections.length;
  const collapseAllBtn = activeSections.length === 0 ? '' : `<button class="collapse-btn${allCollapsed ? ' collapsed' : ''}" onclick="toggleCollapseAll()" title="Collapse/expand all sections">&#9660;</button>`;
  const modeGroup = `<span class="btn-group">${simpleBtn}${previewBtn}${sessionsBtn}${unreadBtn}</span>`;
  const headerBtns = '<div class="active-header-btns">' + collapseAllBtn + '<h2>Active' + (filteredActive.length ? ' (' + filteredActive.length + ')' : '') + '</h2>' + filterBtns + modeGroup + '</div>' + eaBtn;
  activeEl.innerHTML = filteredActive.length
    ? '<div class="active-header">' + headerBtns + '</div>' + activeHtml
    : '<div class="active-header">' + headerBtns + '</div><div class="empty-state">All done! &#127881;</div>';

  const isCompletedCollapsed = !searching && collapsedSections.has('__completed__');
  if (filteredCompleted.length) {
    completedEl.innerHTML = `<div class="section-header-row" data-section="__completed__">`
      + `<button class="collapse-btn${isCompletedCollapsed ? ' collapsed' : ''}" onclick="toggleSectionCollapse('__completed__')" title="${isCompletedCollapsed ? 'Expand' : 'Collapse'}">&#9660;</button>`
      + `<h2 style="margin:0;">Completed (${filteredCompleted.length})</h2>`
      + `</div>`
      + (isCompletedCollapsed ? '' : filteredCompleted.map(t => renderTodo(t)).join(''));
    if (isCompletedCollapsed) {
      visibleIds = [...visibleActiveIds, '__section__:__completed__'];
    }
  } else {
    completedEl.innerHTML = '';
  }

  // Update section dropdown options
  const allSections = [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
  const secSelect = document.getElementById('new-section');
  if (secSelect) {
    const curVal = secSelect.value;
    secSelect.innerHTML = '<option value="">No section</option>'
      + allSections.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')
      + '<option value="__custom__">Other...</option>';
    // Restore previous selection if still valid
    if ([...secSelect.options].some(o => o.value === curVal)) secSelect.value = curVal;
  }

  // Restore inline form if it was visible and we have an insertion target
  if (formWasVisible) _restoreInlineForm(form, formTitle, formDesc, formPriority, formSection, formSectionCustom);

  // Clamp selectedIdx if items disappeared (e.g. section collapsed)
  if (selectedIdx > visibleIds.length) selectedIdx = visibleIds.length > 0 ? visibleIds.length : -1;

  // Set sticky offsets for stacking: search bar → active header → section headers + hero spinner
  const stickyEl = document.querySelector('.sticky-header');
  const activeHeaderEl = document.querySelector('.active-header');
  const searchH = stickyEl ? stickyEl.offsetHeight : 0;
  document.documentElement.style.setProperty('--sticky-offset', searchH + 'px');
  const activeH = activeHeaderEl ? activeHeaderEl.offsetHeight : 0;
  document.documentElement.style.setProperty('--section-offset', (searchH + activeH) + 'px');

  applySelection();
  _restoreJobOutputs();
  _restoreEaUpdateBubble();
}


function _restoreInlineForm(form, title, desc, priority, section, sectionCustom) {
  if (insertBeforeId) {
    const targetEl = document.querySelector(`.todo-item[data-todo-id="${insertBeforeId}"]`);
    if (targetEl) targetEl.parentNode.insertBefore(form, targetEl);
  }
  form.classList.add('visible');
  form.querySelector('#new-title').value = title;
  form.querySelector('#new-desc').value = desc;
  form.querySelector('#new-priority').value = priority;
  // Restore section select — if the value exists in options, set it; otherwise set to custom
  const secSelect = form.querySelector('#new-section');
  const customInput = form.querySelector('#new-section-custom');
  if ([...secSelect.options].some(o => o.value === section)) {
    secSelect.value = section;
  } else if (section) {
    secSelect.value = '__custom__';
  }
  customInput.value = sectionCustom;
  customInput.style.display = secSelect.value === '__custom__' ? '' : 'none';
}

function renderTodo(t) {
  const checked = t.status === 'completed' ? 'checked' : '';
  const priorityClass = t.priority === 'none' ? 'priority-none-item' : (t.priority === 'high' ? 'priority-high-item' : '');
  const statusClass = t.status === 'completed' ? 'status-completed' : priorityClass;

  if (editingId === t.id) {
    return `<div class="todo-item ${statusClass}">
      <div class="todo-body">
        <input class="edit-title" id="edit-title-${t.id}" value="${esc(_parseTitle(t.title || '').displayTitle)}">
        <div class="edit-desc-cm" id="edit-desc-${t.id}"></div>
        <select id="edit-section-${t.id}" class="edit-select">
          <option value="">No section</option>
          ${allSectionsForEdit().map(s => `<option value="${esc(s)}" ${(t.section||'')===s?'selected':''}>${esc(s)}</option>`).join('')}
          <option value="__custom__">Other...</option>
        </select>
        <input class="edit-title" id="edit-section-custom-${t.id}" placeholder="New section name" style="display:none;">
        <div class="edit-actions">
          <select id="edit-priority-${t.id}" class="edit-select" style="width:auto;margin-bottom:0">
            ${['high','medium','low','none'].map(p =>
              `<option value="${p}" ${p===t.priority?'selected':''}>${p}</option>`
            ).join('')}
          </select>
          <button class="btn btn-primary btn-sm" onclick="saveEdit('${t.id}')">Save <span style="opacity:0.6;font-weight:400">&#8984;&#9166;</span></button>
          <button class="btn btn-sm" onclick="cancelEdit()" style="border:1px solid var(--border)">Cancel <span style="opacity:0.6;font-weight:400">Esc</span></button>
        </div>
      </div>
    </div>`;
  }

  const descHtml = t.description ? renderMd(t.description).replace(/conv:([a-zA-Z0-9_-]+)/g, `<a href="#" class="conv-link" onclick="event.preventDefault();event.stopPropagation();resumeConv('$1','${t.id}')" title="Resume conversation $1">conv:$1</a>`) : '';
  const desc = descHtml ? `<div class="todo-desc">${descHtml}</div>` : '';
  const priorityBadge = `<span class="priority-badge priority-${t.priority || 'medium'}">${t.priority || 'medium'}</span>`;

  const activeJob = _getActiveJobForTodo(t.id);
  const isRunning = activeJob && activeJob.status === 'running' && !(activeJob.job_key && activeJob.job_key.startsWith('chat-'));
  const spinner = isRunning ? `<span class="job-spinner" title="Stop job" onclick="event.stopPropagation();killJob('${activeJob.id}')"><l-jelly-triangle size="13" speed="1.75" color="var(--accent)"></l-jelly-triangle></span>` : '';
  const jobBubble = `<div class="checkon-bubble" id="checkon-bubble-${t.id}"></div>`;
  const jobSummary = `<div class="checkon-summary" id="checkon-summary-${t.id}"></div>`;

  const draggable = t.status !== 'completed' ? 'draggable="true"' : '';
  const itemToggled = expandedItems.has(t.id) ? ' item-expanded' : '';
  const isCompleted = t.status === 'completed';
  const swipeRevealClass = isCompleted ? 'swipe-reveal swipe-reveal-undo' : 'swipe-reveal';
  const swipeIcon = isCompleted ? '&#8634;' : '&#10003;';
  return `<div class="todo-item ${statusClass}${itemToggled}" data-todo-id="${t.id}" ${draggable} onclick="selectTodo('${t.id}')" ondblclick="startEdit('${t.id}')" oncontextmenu="showCtxMenu(event,'${t.id}')" style="cursor:pointer;">
    <div class="${swipeRevealClass}"><span class="swipe-reveal-icon">${swipeIcon}</span></div>
    <div class="swipe-content">
    <div class="todo-header">
      <div class="todo-title" style="flex:1;min-width:0;display:flex;align-items:center;gap:2px" onclick="event.stopPropagation();selectTodo('${t.id}');toggleItemDesc('${t.id}')">${spinner}${esc(_parseTitle(t.title || '').displayTitle)}</div>
      <div class="todo-actions">
        ${t.status !== 'completed' ? `<button onclick="event.stopPropagation();eaUpdateItem('${t.id}')" style="border:none;background:transparent;font-size:1rem;padding:4px 6px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Refresh via /ea checkon" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--subtle)'">&#8635;</button>` : ''}
        ${t.status !== 'completed' ? `<button onclick="event.stopPropagation();openChat('${t.id}')" style="border:none;background:transparent;font-size:1rem;padding:4px 6px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Chat (s)" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--subtle)'">&#9654;</button>` : ''}

      </div>
      ${priorityBadge}
    </div>
    ${desc}${jobSummary}${jobBubble}
    </div>
  </div>`;
}

function _parseTitle(raw) {
  // Extract bold text as display title: **title text**
  const boldMatch = raw.match(/\*\*(.+?)\*\*/);
  const stripped = raw.replace(/`(?:updated|read)[^`]*`/g, '').replace(/\*\*/g, '').trim();
  const displayTitle = boldMatch ? boldMatch[1] : stripped;
  const hasUpdatedTag = /`updated\s[^`]*`/.test(raw);
  return { displayTitle, hasUpdatedTag };
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function renderMd(s) {
  if (!s) return '';
  try {
    const renderer = new marked.Renderer();
    renderer.link = function(token) {
      const t = token.title ? ` title="${token.title}"` : '';
      return `<a href="${token.href}"${t} target="_blank" rel="noopener noreferrer">${token.text}</a>`;
    };
    return marked.parse(s, {breaks: true, renderer});
  } catch(e) {
    return esc(s);
  }
}

function selectTodo(id) {
  const idx = visibleIds.indexOf(id);
  if (idx >= 0) {
    selectedIdx = idx + 1;
    applySelection();
  }
}

// --- Context menu ---
function showCtxMenu(e, id) {
  e.preventDefault();
  e.stopPropagation();
  ctxTargetId = id;
  selectTodo(id);

  const todo = allTodos.find(t => t.id === id);
  if (!todo) return;

  const allSections = [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
  const curSection = todo.section || '';

  let sectionItems = allSections.map(s => {
    const isCur = s === curSection;
    return `<div class="ctx-menu-item${isCur ? ' active-section' : ''}" onclick="ctxMoveSection('${esc(s).replace(/'/g, "\\'")}')">` +
           `${esc(s)}${isCur ? ' &#10003;' : ''}</div>`;
  }).join('');
  sectionItems += `<div class="ctx-menu-sep"></div>`;
  sectionItems += `<div class="ctx-menu-item" onclick="ctxMoveSectionNew()">New section&hellip;</div>`;
  if (curSection) {
    sectionItems += `<div class="ctx-menu-item" onclick="ctxMoveSection('')">Remove from section</div>`;
  }

  const menu = document.getElementById('ctx-menu');

  const curPriority = todo.priority || 'medium';
  let priorityItems = ['high','medium','low','none'].map(p => {
    const isCur = p === curPriority;
    return `<div class="ctx-menu-item${isCur ? ' active-section' : ''}" onclick="ctxSetPriority('${p}')">${p}${isCur ? ' &#10003;' : ''}</div>`;
  }).join('');

  menu.innerHTML =
    `<div class="ctx-menu-item" onclick="ctxEdit()">&#9998; Edit</div>` +
    `<div class="ctx-menu-item has-submenu">&#128193; Move to section<div class="ctx-submenu">${sectionItems}</div></div>` +
    `<div class="ctx-menu-item has-submenu">&#9873; Set priority<div class="ctx-submenu">${priorityItems}</div></div>` +
    `<div class="ctx-menu-sep"></div>` +
    `<div class="ctx-menu-item" style="color:var(--danger)" onclick="ctxDelete()">&#128465; Delete</div>`;

  // Position: keep within viewport
  menu.classList.add('visible');
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let x = e.clientX, y = e.clientY;
  if (x + mw > window.innerWidth) x = window.innerWidth - mw - 8;
  if (y + mh > window.innerHeight) y = window.innerHeight - mh - 8;
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
}

function hideCtxMenu() {
  document.getElementById('ctx-menu').classList.remove('visible');
  ctxTargetId = null;
}

function ctxEdit() {
  const id = ctxTargetId;
  hideCtxMenu();
  if (id) startEdit(id);
}

function ctxDelete() {
  const id = ctxTargetId;
  hideCtxMenu();
  if (id) deleteTodo(id);
}

async function ctxMoveSection(section) {
  const id = ctxTargetId;
  const prevIdx = selectedIdx;
  hideCtxMenu();
  if (!id) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section})
  });
  await loadTodos();
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

function ctxMoveSectionNew() {
  hideCtxMenu();
  const name = prompt('New section name:');
  if (name !== null && name.trim()) {
    ctxTargetId && ctxMoveSection(name.trim());
  }
}

async function ctxSetPriority(priority) {
  const id = ctxTargetId;
  hideCtxMenu();
  if (!id) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({priority})
  });
  loadTodos();
}

async function sortByPriority(section) {
  await fetch(API + '/sort-priority', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section})
  });
  loadTodos();
}

// Close context menu on click outside or Escape
document.addEventListener('click', () => { hideCtxMenu(); if (sectionPickerOpen) hideSectionPicker(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('ctx-menu').classList.contains('visible')) {
    hideCtxMenu();
  }
});

function updateSimpleBtn() {
  const btn = document.querySelector('.simple-toggle-btn');
  if (!btn) return;
  const totalItems = allTodos.filter(t => t.status !== 'completed').length;
  const expandedCount = expandedItems.size;
  btn.classList.toggle('active', expandedCount === 0);
  btn.classList.toggle('partial', expandedCount > 0 && expandedCount < totalItems);
}

function toggleFilterSessions() {
  filterActiveSessions = !filterActiveSessions;
  render();
}

function toggleFilterUnread() {
  filterUnread = !filterUnread;
  render();
}

function toggleSimpleMode() {
  if (expandedItems.size > 0) {
    expandedItems.clear();
  } else {
    allTodos.filter(t => t.status !== 'completed').forEach(t => expandedItems.add(t.id));
  }
  render();
}

function togglePreviewMode() {
  previewMode = !previewMode;
  if (!previewMode && previewExpandedId) {
    const el = document.querySelector(`.todo-item[data-todo-id="${previewExpandedId}"]`);
    if (el) el.classList.remove('preview-expanded');
    previewExpandedId = null;
  }
  updatePreviewBtn();
  if (previewMode) applySelection();
}

function updatePreviewBtn() {
  const btn = document.querySelector('.preview-toggle-btn');
  if (btn) btn.classList.toggle('active', previewMode);
}

function cycleFilter(p) {
  if (showPriorities.has(p)) {
    showPriorities.delete(p);
    hidePriorities.add(p);
  } else if (hidePriorities.has(p)) {
    hidePriorities.delete(p);
  } else {
    showPriorities.add(p);
  }
  selectedIdx = -1;
  render();
}

function toggleItemDesc(id) {
  const el = document.querySelector(`.todo-item[data-todo-id="${id}"]`);
  if (!el) return;
  if (previewExpandedId === id) {
    el.classList.remove('preview-expanded');
    previewExpandedId = null;
  }
  if (expandedItems.has(id)) {
    expandedItems.delete(id);
    el.classList.remove('item-expanded');
  } else {
    expandedItems.add(id);
    el.classList.add('item-expanded');
  }
  updateSimpleBtn();
}

function toggleSectionCollapse(section) {
  if (collapsedSections.has(section)) collapsedSections.delete(section);
  else collapsedSections.add(section);
  render();
}

function collapseStep() {
  _flushPendingMarkRead();
  // First collapse all items, then collapse all sections
  if (expandedItems.size > 0) {
    expandedItems.clear();
    render();
    return;
  }
  const secs = sectionsOrder.filter(s => s);
  if (secs.length > 0 && !secs.every(s => collapsedSections.has(s))) {
    secs.forEach(s => collapsedSections.add(s));
    render();
  }
}

function expandStep() {
  // First expand all sections, then expand all items
  const secs = sectionsOrder.filter(s => s);
  if (secs.length > 0 && secs.some(s => collapsedSections.has(s))) {
    secs.forEach(s => collapsedSections.delete(s));
    render();
    return;
  }
  const activeTodos = allTodos.filter(t => t.status !== 'completed');
  if (activeTodos.some(t => !expandedItems.has(t.id))) {
    activeTodos.forEach(t => expandedItems.add(t.id));
    render();
  }
}

function toggleCollapseAll() {
  const secs = sectionsOrder.filter(s => s);
  const allCollapsed = secs.length > 0 && secs.every(s => collapsedSections.has(s));
  if (allCollapsed) {
    secs.forEach(s => collapsedSections.delete(s));
  } else {
    secs.forEach(s => collapsedSections.add(s));
  }
  render();
}

function getSectionOfSelected() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return null;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return null;
  return todo.status === 'completed' ? '__completed__' : (todo.section || '');
}

function getNextCollapsedSection() {
  // Find the nearest collapsed section relative to current selection
  // Strategy: look at all sections in order and find the first collapsed one
  // at or after the selected item's position, or the last one before it
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) {
    // Nothing selected — just expand the first collapsed section
    for (const s of sectionsOrder) {
      if (collapsedSections.has(s)) return s;
    }
    if (collapsedSections.has('__completed__')) return '__completed__';
    return null;
  }
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return collapsedSections.values().next().value || null;
  const curSection = todo.status === 'completed' ? '__completed__' : (todo.section || '');

  // Look for a collapsed section immediately following the current section
  const allSecs = [...sectionsOrder, '__completed__'];
  const curIdx = allSecs.indexOf(curSection);
  // Search forward first, then backward
  for (let i = curIdx + 1; i < allSecs.length; i++) {
    if (collapsedSections.has(allSecs[i])) return allSecs[i];
  }
  for (let i = curIdx - 1; i >= 0; i--) {
    if (collapsedSections.has(allSecs[i])) return allSecs[i];
  }
  return null;
}

function allSectionsForEdit() {
  return [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
}

function getSelectedSection() {
  const sel = document.getElementById('new-section');
  if (sel.value === '__custom__') return document.getElementById('new-section-custom').value.trim();
  return sel.value;
}

function onSectionChange() {
  const sel = document.getElementById('new-section');
  const customInput = document.getElementById('new-section-custom');
  if (sel.value === '__custom__') {
    customInput.style.display = '';
    customInput.focus();
  } else {
    customInput.style.display = 'none';
    customInput.value = '';
  }
}
document.getElementById('new-section').addEventListener('change', onSectionChange);

async function addTodo() {
  const title = document.getElementById('new-title').value.trim();
  if (!title) return;
  const desc = document.getElementById('new-desc').value.trim();
  const priority = document.getElementById('new-priority').value;
  const section = getSelectedSection();
  const payload = {title, description: desc, priority, section};
  if (insertBeforeId) payload.before_id = insertBeforeId;
  const res = await fetch(API, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const newTodo = await res.json();
  insertBeforeId = null;
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  document.getElementById('new-section-custom').value = '';
  document.getElementById('new-section-custom').style.display = 'none';
  hideAddForm();
  searchQuery = '';
  const searchEl = document.getElementById('search-input');
  searchEl.value = '';
  searchEl.classList.remove('has-query');
  await loadTodos();
  // Advance cursor to the newly created item
  if (newTodo && newTodo.id) {
    const idx = visibleIds.indexOf(newTodo.id);
    if (idx >= 0) {
      selectedIdx = idx + 1;
      applySelection();
    }
  }
}

async function toggleComplete(id, checked) {
  const todo = allTodos.find(t => t.id === id);
  const newStatus = checked ? 'completed' : 'open';
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: newStatus})
  });
  loadTodos();
}

async function changePriority(id, priority) {
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({priority})
  });
  loadTodos();
}

async function deleteTodo(id) {
  if (!confirm('Delete this todo?')) return;
  await fetch(API + '/' + id, {method: 'DELETE'});
  loadTodos();
}

function navigateSection(direction) {
  // Snap to first/last in current section, or jump to adjacent section if already at edge
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return;
  const curSection = todo.status === 'completed' ? '__completed__' : (todo.section || '');

  // Build index ranges for each section in visibleIds
  const sectionRanges = []; // [{section, start, end}] (1-based indices into visibleIds)
  let prev = null;
  for (let i = 0; i < visibleIds.length; i++) {
    const t = allTodos.find(x => x.id === visibleIds[i]);
    const sec = t && t.status === 'completed' ? '__completed__' : (t ? (t.section || '') : '');
    if (sec !== prev) {
      sectionRanges.push({section: sec, start: i + 1, end: i + 1});
      prev = sec;
    } else {
      sectionRanges[sectionRanges.length - 1].end = i + 1;
    }
  }

  const rangeIdx = sectionRanges.findIndex(r => r.section === curSection && selectedIdx >= r.start && selectedIdx <= r.end);
  if (rangeIdx < 0) return;
  const range = sectionRanges[rangeIdx];

  if (direction === 'down') {
    if (rangeIdx + 1 < sectionRanges.length) {
      selectedIdx = sectionRanges[rangeIdx + 1].start;
    }
  } else {
    if (rangeIdx - 1 >= 0) {
      selectedIdx = sectionRanges[rangeIdx - 1].start;
    }
  }
  applySelection();
}

async function moveToAdjacentSection(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const curSection = todo.section || '';
  const curIdx = sectionsOrder.indexOf(curSection);
  let newIdx = direction === 'down' ? curIdx + 1 : curIdx - 1;
  if (newIdx < 0 || newIdx >= sectionsOrder.length) return;
  const newSection = sectionsOrder[newIdx];
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section: newSection})
  });
  const prevIdx = selectedIdx;
  await loadTodos();
  // Stay at the original position (select the next item that took its place)
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

async function performUndo() {
  const res = await fetch('/api/undo', { method: 'POST' });
  if (res.ok) {
    await loadTodos();
    const toast = document.createElement('div');
    toast.innerHTML = 'Undone <span style="margin-left:8px;opacity:0.5;font-size:0.75rem">\u2318Z</span>';
    toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;z-index:2000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s';
    document.body.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; });
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 1200);
  }
}

function copyTodoId(id) {
  function showToast() {
    const toast = document.createElement('div');
    toast.textContent = 'Copied ID: ' + id;
    toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;font-family:monospace;z-index:2000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s';
    document.body.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; });
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(id).then(showToast).catch(() => { fallbackCopy(id); showToast(); });
  } else {
    fallbackCopy(id); showToast();
  }
}

async function startInTmux(id) {
  await openTerminal(id);
}

async function openTerminal(todoId, resumeId) {
  // If already have a live session with a terminal for this todo and not resuming, just switch to it
  if (!resumeId && _termSessions[todoId] && _termSessions[todoId].alive && _termSessions[todoId].term) {
    _showTerminalOverlay(todoId);
    return;
  }

  // Register session on server
  let data;
  try {
    const body = resumeId ? { resume_id: resumeId } : {};
    const res = await fetch(API + '/' + todoId + '/terminal', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) { showToast('Failed to open terminal', true); return; }
    data = await res.json();
  } catch (e) {
    showToast('Failed to open terminal', true);
    return;
  }

  const sessionId = data.session_id;

  // If server returned existing session and we already have a full client for it, switch
  if (data.existing && _termSessions[todoId] && _termSessions[todoId].sessionId === sessionId && _termSessions[todoId].term) {
    _showTerminalOverlay(todoId);
    return;
  }

  // Create new terminal instance
  const term = new Terminal({
    theme: { background: '#1a1b1e', foreground: '#e2e8f0', cursor: '#4f6ef7',
             selectionBackground: 'rgba(79,110,247,0.3)' },
    fontFamily: 'Menlo, Monaco, "Cascadia Code", monospace',
    fontSize: 11, lineHeight: 1.3, cursorBlink: true,
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);

  _termSessions[todoId] = { sessionId, term, fitAddon, ws: null, alive: true };

  _updateSpinnersInPlace();
  _showTerminalOverlay(todoId);

  // Connect WebSocket
  _connectTermWs(todoId, sessionId);
}

function _connectTermWs(todoId, sessionId) {
  const session = _termSessions[todoId];
  if (!session) return;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(proto + '://' + location.host + '/api/terminal/' + sessionId + '/ws');
  ws.binaryType = 'arraybuffer';
  session.ws = ws;

  ws.onopen = () => {
    const dims = session.fitAddon.proposeDimensions();
    if (dims) ws.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
  };

  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      session.term.write(new Uint8Array(e.data));
    } else if (typeof e.data === 'string') {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'error') session.term.writeln('\\r\\n\\x1b[31m' + msg.msg + '\\x1b[0m');
      } catch {}
    }
  };

  ws.onclose = () => {
    // Don't mark dead — PTY may still be alive for reconnection
  };

  ws.onerror = () => {};

  // Keystrokes → WS
  session.term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(new TextEncoder().encode(data));
    }
  });

  session.term.onBinary((data) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(Uint8Array.from(data, c => c.charCodeAt(0)));
    }
  });
}

function _showTerminalOverlay(todoId) {
  const session = _termSessions[todoId];
  if (!session || !session.term) return;

  _activeTermTodoId = todoId;
  const todo = allTodos.find(t => t.id === todoId);
  document.getElementById('terminal-title').textContent = todo ? todo.title : todoId;

  const overlay = document.getElementById('terminal-overlay');
  overlay.style.display = 'flex';
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => overlay.classList.add('visible'));

  // Mount this session's terminal into the container
  const container = document.getElementById('terminal-container');
  container.innerHTML = '';
  if (session.term.element) {
    // Already opened — just re-attach the existing DOM element
    container.appendChild(session.term.element);
  } else {
    session.term.open(container);
  }
  session.fitAddon.fit();
  session.term.focus();

  // Resize observer + window resize for horizontal/vertical reflow
  if (!_termResizeObserver) {
    let _fitTimer = null;
    const doFit = () => {
      if (_fitTimer) clearTimeout(_fitTimer);
      _fitTimer = setTimeout(() => {
        if (!_activeTermTodoId || !_termSessions[_activeTermTodoId]) return;
        const s = _termSessions[_activeTermTodoId];
        if (!s.fitAddon) return;
        s.fitAddon.fit();
        if (s.ws && s.ws.readyState === WebSocket.OPEN) {
          const dims = s.fitAddon.proposeDimensions();
          if (dims) {
            console.log('[terminal] resize:', dims.cols, 'x', dims.rows);
            s.ws.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
          }
        }
      }, 50);
    };
    _termResizeObserver = new ResizeObserver(doFit);
    window.addEventListener('resize', doFit);
  }
  _termResizeObserver.observe(container);
}

function _startTermResize(e) {
  e.preventDefault();
  const panel = document.getElementById('terminal-panel');
  const startY = e.clientY;
  const startH = panel.offsetHeight;
  let _dragFitTimer = null;
  const fitDuringDrag = () => {
    if (_dragFitTimer) return;
    _dragFitTimer = setTimeout(() => {
      _dragFitTimer = null;
      if (!_activeTermTodoId || !_termSessions[_activeTermTodoId]) return;
      const s = _termSessions[_activeTermTodoId];
      if (!s.fitAddon) return;
      s.fitAddon.fit();
      if (s.ws && s.ws.readyState === WebSocket.OPEN) {
        const dims = s.fitAddon.proposeDimensions();
        if (dims) s.ws.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
      }
    }, 50);
  };
  function onMove(e) {
    const h = Math.min(window.innerHeight * 0.9, Math.max(150, startH - (e.clientY - startY)));
    panel.style.height = h + 'px';
    fitDuringDrag();
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    if (_dragFitTimer) { clearTimeout(_dragFitTimer); _dragFitTimer = null; }
    fitDuringDrag();
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function termSendCommand(cmd) {
  if (!_activeTermTodoId) return;
  const session = _termSessions[_activeTermTodoId];
  if (!session || !session.ws || session.ws.readyState !== WebSocket.OPEN) return;
  session.ws.send(new TextEncoder().encode(cmd + '\r'));
  session.term.focus();
}

function copyTmuxAttach() {
  if (!_activeTermTodoId) return;
  const session = _termSessions[_activeTermTodoId];
  if (!session) return;
  const cmd = 'tmux attach -t t-' + session.sessionId;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cmd).then(() => showToast('Copied: ' + cmd)).catch(() => { _fallbackCopy(cmd); showToast('Copied: ' + cmd); });
  } else {
    _fallbackCopy(cmd); showToast('Copied: ' + cmd);
  }
}
function _fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta); ta.select();
  document.execCommand('copy'); ta.remove();
}

function minimizeTerminal() {
  const overlay = document.getElementById('terminal-overlay');
  overlay.classList.remove('visible');
  document.body.style.overflow = '';
  setTimeout(() => { overlay.style.display = 'none'; }, 100);
  if (_termResizeObserver) _termResizeObserver.disconnect();
  _activeTermTodoId = null;
}

async function killTerminal(todoId, skipMinimize) {
  if (!todoId) return;
  const session = _termSessions[todoId];
  if (!session) return;

  // Kill on server
  try {
    await fetch('/api/terminal/' + session.sessionId + '/kill', { method: 'POST' });
  } catch {}

  // Clean up client
  if (session.ws) { try { session.ws.close(); } catch {} }
  session.alive = false;

  if (!skipMinimize && _activeTermTodoId === todoId) {
    minimizeTerminal();
  }
  delete _termSessions[todoId];
  _updateSpinnersInPlace();
}

function showShortcuts() {
  document.getElementById('shortcuts-overlay').classList.add('visible');
}
function hideShortcuts() {
  document.getElementById('shortcuts-overlay').classList.remove('visible');
}

let _settingsProviders = {}; // name -> {type, api_key, model, base_url}

async function showSettings() {
  try {
    const res = await fetch('/api/config');
    const config = await res.json();
    // Build providers from config
    _settingsProviders = {};
    if (config.providers) {
      for (const [name, prov] of Object.entries(config.providers)) {
        _settingsProviders[name] = { ...prov };
      }
    }
    // Migration: if no providers dict but legacy keys exist, show them
    if (Object.keys(_settingsProviders).length === 0) {
      if (config.anthropic_api_key) {
        _settingsProviders['anthropic'] = { type: 'anthropic', api_key: config.anthropic_api_key, model: config.model || 'claude-sonnet-4-20250514' };
      }
      const oai = config.openai_compat || {};
      if (oai.base_url) {
        _settingsProviders['openai-compat'] = { type: 'openai_compat', base_url: oai.base_url, api_key: oai.api_key || '', model: oai.model || '' };
      }
    }
    _renderProviderList();
    // Set active provider dropdown
    const sel = document.getElementById('settings-active-provider');
    _rebuildActiveDropdown();
    sel.value = config.active_provider || config._active_provider_name || '';
    document.getElementById('settings-subagents').checked = config.subagents_enabled !== false;
    document.getElementById('settings-max-subagents').value = config.max_subagents || 10;
  } catch {}
  _loadMcpStatus();
  _loadGitLog();
  document.getElementById('settings-overlay').classList.add('visible');
}

let _mcpStatusData = null;

async function _loadMcpStatus() {
  const container = document.getElementById('mcp-server-list');
  if (!container) return;
  try {
    const res = await fetch('/api/mcp/status');
    const data = await res.json();
    _mcpStatusData = data;
    // Set global auto-approve checkbox
    const aaChk = document.getElementById('mcp-auto-approve-all');
    if (aaChk) aaChk.checked = !!data.auto_approve_all;
    if (!data.servers || data.servers.length === 0) {
      container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">No MCP servers configured.</div>';
      return;
    }
    container.innerHTML = data.servers.map(s => {
      const dot = s.enabled ? (s.connected ? 'connected' : 'disconnected') : 'disabled';
      const toggleCls = s.enabled ? 'on' : 'off';
      const info = !s.enabled ? 'disabled' : (s.connected ? s.tool_count + ' tools' : esc(s.error || 'disconnected'));
      const expandId = 'mcp-tools-' + s.name;
      let html = '<div class="mcp-server-row">'
        + '<button class="mcp-server-toggle ' + toggleCls + '" onclick="_toggleMcpServer(\'' + esc(s.name) + '\',' + !s.enabled + ')"></button>'
        + '<span class="mcp-dot ' + dot + '"></span>'
        + '<strong style="flex-shrink:0">' + esc(s.label || s.name) + '</strong>'
        + '<span style="color:var(--subtle);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(info) + '</span>';
      if (s.credential_fields && s.credential_fields.length > 0) {
        const allSet = s.credential_fields.every(f => f.has_value);
        html += '<button class="mcp-tool-btn" onclick="_toggleMcpPanel(\'' + esc(s.name) + '\',\'config\')" style="font-size:0.65rem;' + (allSet ? '' : 'color:#f59e0b;border-color:#f59e0b') + '">Config</button>';
      }
      if (s.account_fields && s.account_fields.length > 0) {
        const acctLabel = s.account_count > 0 ? 'Accounts (' + s.account_count + ')' : 'Accounts';
        html += '<button class="mcp-tool-btn" onclick="_toggleMcpPanel(\'' + esc(s.name) + '\',\'accounts\')" style="font-size:0.65rem;' + (s.account_count === 0 ? 'color:#f59e0b;border-color:#f59e0b' : '') + '">' + acctLabel + '</button>';
      }
      if (s.enabled && s.connected) {
        html += '<button class="mcp-tool-btn" onclick="_toggleMcpPanel(\'' + esc(s.name) + '\',\'tools\')" style="font-size:0.65rem">Tools</button>';
      }
      html += '</div>';
      // Config panel (credentials)
      html += '<div id="mcp-config-' + s.name + '" class="mcp-tools-list" style="display:none"></div>';
      // Accounts panel
      html += '<div id="mcp-accounts-' + s.name + '" class="mcp-tools-list" style="display:none"></div>';
      // Tools panel
      if (s.enabled && s.connected) {
        html += '<div id="' + expandId + '" class="mcp-tools-list" style="display:none">Loading...</div>';
      }
      return html;
    }).join('');
    // Load tool lists for connected servers
    data.servers.filter(s => s.enabled && s.connected).forEach(s => _renderMcpTools(s));
  } catch {
    container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">Failed to load MCP status.</div>';
  }
}

function _renderMcpTools(server) {
  const el = document.getElementById('mcp-tools-' + server.name);
  if (!el) return;
  const tools = server.tools || [];
  const disabled = new Set(server.disabled_tools || []);
  const autoApproved = new Set(server.auto_approved_tools || []);
  if (tools.length === 0) {
    el.innerHTML = '<div style="color:var(--subtle)">No tools available.</div>';
    return;
  }
  el.innerHTML = tools.map(tool => {
    const t = typeof tool === 'string' ? tool : tool.name;
    const isDis = disabled.has(t);
    const isAuto = autoApproved.has(t);
    const sn = esc(server.name);
    const tn = esc(t);
    return '<div class="mcp-tool-row" style="' + (isDis ? 'opacity:0.5' : '') + '">'
      + '<span class="tool-name">' + tn + '</span>'
      + '<button class="mcp-tool-btn' + (isAuto ? ' active' : '') + '" onclick="_setMcpToolInline(this,\'' + sn + '\',\'' + tn + '\',null,' + !isAuto + ')" title="Auto-approve this tool">Auto</button>'
      + '<button class="mcp-tool-btn danger' + (isDis ? ' active' : '') + '" onclick="_setMcpToolInline(this,\'' + sn + '\',\'' + tn + '\',' + !isDis + ',null)" title="Disable this tool">Off</button>'
      + '</div>';
  }).join('');
}

function _toggleMcpPanel(serverName, panel) {
  const panels = ['config', 'accounts', 'tools'];
  for (const p of panels) {
    const el = document.getElementById('mcp-' + p + '-' + serverName);
    if (!el) continue;
    if (p === panel) {
      const show = el.style.display === 'none';
      el.style.display = show ? 'block' : 'none';
      if (show) {
        if (p === 'config') _renderMcpConfig(serverName);
        if (p === 'accounts') _loadMcpAccounts(serverName);
      }
    } else {
      el.style.display = 'none';
    }
  }
}

function _renderMcpConfig(serverName) {
  const el = document.getElementById('mcp-config-' + serverName);
  if (!el || !_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const fields = server.credential_fields || [];
  if (fields.length === 0) {
    el.innerHTML = '<div style="color:var(--subtle)">No configuration needed.</div>';
    return;
  }
  let html = '';
  // OAuth connect buttons (for servers like Slack that use store_as)
  const oauthProviders = server.oauth_providers || [];
  if (oauthProviders.length > 0) {
    oauthProviders.forEach(p => {
      // Check if the credential this OAuth provides is already set
      const storeKey = server.credential_fields.find(f => f.has_value);
      const connected = !!storeKey;
      html += '<button onclick="_startOAuth(\'' + esc(serverName) + '\',\'' + esc(p.id) + '\',\'\')" style="background:#4285f4;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.78rem;margin-bottom:10px;width:100%">'
        + (connected ? 'Reconnect with ' : 'Connect with ') + esc(p.label) + '</button>';
    });
    if (fields.some(f => f.has_value)) {
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px"><span style="font-size:0.7rem;color:#22c55e">Connected</span>'
        + '<button class="mcp-tool-btn danger" onclick="_clearMcpCredential(\'' + esc(serverName) + '\')" style="font-size:0.65rem">Disconnect</button></div>';
    }
    html += '<details style="margin-bottom:8px"><summary style="font-size:0.72rem;color:var(--subtle);cursor:pointer">Or enter token manually</summary><div style="margin-top:6px">';
  }
  html += fields.map(f => {
    const fid = 'mcp-cred-' + serverName + '-' + f.key;
    return '<div style="margin-bottom:8px">'
      + '<label for="' + fid + '" style="font-size:0.72rem;color:var(--subtle);display:block;margin-bottom:2px">' + esc(f.label || f.key) + '</label>'
      + '<input id="' + fid + '" type="' + (f.type === 'password' ? 'password' : 'text') + '" '
      + 'placeholder="' + (f.has_value ? '(saved)' : 'Not set') + '" '
      + 'style="width:100%;padding:4px 8px;font-size:0.78rem;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);box-sizing:border-box">'
      + '</div>';
  }).join('')
    + '<button onclick="_saveMcpConfig(\'' + esc(serverName) + '\')" style="background:var(--accent);color:#fff;border:none;padding:4px 14px;border-radius:6px;cursor:pointer;font-size:0.75rem">Save</button>';
  if (oauthProviders.length > 0) {
    html += '</div></details>';
  }
  el.innerHTML = html;
}

async function _clearMcpCredential(serverName) {
  if (!confirm('Disconnect ' + serverName + '?')) return;
  if (!_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const tokens = {};
  for (const f of (server.credential_fields || [])) {
    tokens[f.key] = '';
  }
  _mcpAction(async () => {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tokens})
    });
    showToast(serverName + ' disconnected');
    await _refreshMcp();
  });
}

async function _saveMcpConfig(serverName) {
  if (!_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const tokens = {};
  let hasChange = false;
  for (const f of (server.credential_fields || [])) {
    const input = document.getElementById('mcp-cred-' + serverName + '-' + f.key);
    if (input && input.value.trim()) {
      tokens[f.key] = input.value.trim();
      hasChange = true;
    }
  }
  if (!hasChange) { showToast('No changes to save'); return; }
  _mcpAction(async () => {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tokens})
    });
    showToast('Credentials saved');
    const configEl = document.getElementById('mcp-config-' + serverName);
    if (configEl) configEl.style.display = 'none';
    await _refreshMcp();
  });
}

async function _loadMcpAccounts(serverName) {
  const el = document.getElementById('mcp-accounts-' + serverName);
  if (!el || !_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const fields = server.account_fields || [];
  try {
    const res = await fetch('/api/mcp/accounts/' + serverName);
    const data = await res.json();
    const accounts = data.accounts || [];
    let html = '';
    const oauthProviders = server.oauth_providers || [];
    // Existing accounts
    accounts.forEach(acct => {
      const isOAuth = acct.config.auth_type === 'oauth';
      const hasToken = !!acct.config.oauth_connected;
      html += '<div style="border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:8px">';
      html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">';
      html += '<strong style="font-size:0.78rem;flex:1">' + esc(acct.config.name || acct.config.user || acct.id.slice(0,8)) + '</strong>';
      if (isOAuth) {
        const statusColor = hasToken ? '#22c55e' : '#f59e0b';
        const statusText = hasToken ? 'Connected' : 'Not connected';
        html += '<span style="font-size:0.65rem;color:' + statusColor + '">' + statusText + '</span>';
        // Find the OAuth provider for reconnect
        const prov = oauthProviders[0];
        if (prov) {
          html += '<button class="mcp-tool-btn" onclick="_startOAuth(\'' + esc(serverName) + '\',\'' + esc(prov.id) + '\',\'' + esc(acct.id) + '\')" style="font-size:0.65rem">' + (hasToken ? 'Reconnect' : 'Connect') + '</button>';
        }
      }
      html += '<button class="mcp-tool-btn danger" onclick="_deleteMcpAccount(\'' + esc(serverName) + '\',\'' + esc(acct.id) + '\')">Delete</button>';
      html += '</div>';
      if (!isOAuth) {
        fields.forEach(f => {
          if (f.key === 'name') return;
          const val = acct.config[f.key];
          const display = f.type === 'password' ? (val ? '***' : 'Not set') : (val != null ? String(val) : '');
          html += '<div style="font-size:0.72rem;color:var(--subtle);padding:1px 0"><span style="color:var(--muted)">' + esc(f.label) + ':</span> ' + esc(display) + '</div>';
        });
      } else {
        if (acct.config.url) html += '<div style="font-size:0.72rem;color:var(--subtle);padding:1px 0">' + esc(acct.config.url) + '</div>';
      }
      html += '</div>';
    });
    // OAuth quick-connect buttons
    if (oauthProviders.length > 0) {
      oauthProviders.forEach(p => {
        html += '<button onclick="_startOAuth(\'' + esc(serverName) + '\',\'' + esc(p.id) + '\',\'\')" style="background:#4285f4;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.78rem;margin-bottom:8px;width:100%">Connect with ' + esc(p.label) + '</button>';
      });
    }
    // Manual add form (for non-OAuth)
    if (fields.length > 0) {
      html += '<details style="margin-top:4px"><summary style="font-size:0.72rem;color:var(--subtle);cursor:pointer">Add manually</summary>';
      html += '<div style="border:1px dashed var(--border);border-radius:6px;padding:8px;margin-top:4px">';
      fields.forEach(f => {
        const fid = 'mcp-acct-' + serverName + '-' + f.key;
        if (f.type === 'boolean') {
          html += '<label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;margin-bottom:4px;cursor:pointer">';
          html += '<input id="' + fid + '" type="checkbox"' + (f.default ? ' checked' : '') + '>';
          html += esc(f.label) + '</label>';
        } else if (f.type === 'select') {
          html += '<label style="font-size:0.72rem;color:var(--subtle);display:block;margin-bottom:2px">' + esc(f.label) + '</label>';
          html += '<select id="' + fid + '" style="width:100%;padding:4px 8px;font-size:0.78rem;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);margin-bottom:6px;box-sizing:border-box">';
          (f.options || []).forEach(o => { html += '<option' + (o === f.default ? ' selected' : '') + '>' + esc(o) + '</option>'; });
          html += '</select>';
        } else {
          html += '<label style="font-size:0.72rem;color:var(--subtle);display:block;margin-bottom:2px">' + esc(f.label) + '</label>';
          html += '<input id="' + fid + '" type="' + (f.type === 'password' ? 'password' : f.type === 'number' ? 'number' : 'text') + '"';
          if (f.placeholder) html += ' placeholder="' + esc(f.placeholder) + '"';
          if (f.default != null && f.type === 'number') html += ' value="' + f.default + '"';
          html += ' style="width:100%;padding:4px 8px;font-size:0.78rem;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);margin-bottom:6px;box-sizing:border-box">';
        }
      });
      html += '<button onclick="_addMcpAccount(\'' + esc(serverName) + '\')" style="background:var(--accent);color:#fff;border:none;padding:4px 14px;border-radius:6px;cursor:pointer;font-size:0.75rem">Add</button>';
      html += '</div></details>';
    }
    el.innerHTML = html;
  } catch { el.innerHTML = '<div style="color:var(--subtle)">Failed to load accounts.</div>'; }
}

async function _addMcpAccount(serverName) {
  if (!_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const config = {};
  for (const f of (server.account_fields || [])) {
    const el = document.getElementById('mcp-acct-' + serverName + '-' + f.key);
    if (!el) continue;
    if (f.type === 'boolean') config[f.key] = el.checked;
    else if (f.type === 'number') config[f.key] = parseInt(el.value) || f.default || 0;
    else config[f.key] = el.value.trim();
  }
  if (!config.name && !config.user) { showToast('Name is required', true); return; }
  _mcpAction(async () => {
    const res = await fetch('/api/mcp/accounts/' + serverName, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(config)
    });
    if (res.ok) {
      showToast('Account added');
      await _loadMcpStatus();
      _toggleMcpPanel(serverName, 'accounts');
    } else {
      const data = await res.json();
      showToast(data.error || 'Failed', true);
    }
  });
}

async function _startOAuth(serverName, providerId, accountId) {
  try {
    const params = new URLSearchParams({server: serverName, provider: providerId});
    if (accountId) params.set('account_id', accountId);
    const res = await fetch('/api/mcp/oauth/start?' + params);
    const data = await res.json();
    if (data.auth_url) {
      window.open(data.auth_url, 'oauth', 'width=600,height=700');
    } else {
      showToast(data.error || 'Failed to start OAuth', true);
    }
  } catch { showToast('Failed to start OAuth', true); }
}

// Listen for OAuth completion from popup
window.addEventListener('message', async (e) => {
  if (e.data && e.data.type === 'oauth_complete') {
    showToast('Account connected');
    _mcpLoading(true);
    await _loadMcpStatus();
    _mcpLoading(false);
    // Re-open panels that were open
    document.querySelectorAll('[id^="mcp-accounts-"]').forEach(el => {
      if (el.style.display !== 'none') {
        const name = el.id.replace('mcp-accounts-', '');
        _loadMcpAccounts(name);
      }
    });
  }
});

async function _deleteMcpAccount(serverName, accountId) {
  if (!confirm('Delete this account?')) return;
  _mcpAction(async () => {
    const res = await fetch('/api/mcp/accounts/' + serverName + '/' + accountId, { method: 'DELETE' });
    if (res.ok) {
      showToast('Account deleted');
      await _loadMcpStatus();
      _toggleMcpPanel(serverName, 'accounts');
    }
  });
}

function _mcpLoading(show) {
  const el = document.getElementById('mcp-loading');
  if (el) el.style.display = show ? 'flex' : 'none';
}

async function _mcpAction(fn) {
  _mcpLoading(true);
  try { await fn(); } finally { _mcpLoading(false); }
}

async function _toggleMcpServer(name, enabled) {
  _mcpAction(async () => {
    await fetch('/api/mcp/servers', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({server: name, enabled})
    });
    showToast(enabled ? name + ' enabled' : name + ' disabled');
    if (enabled) await new Promise(r => setTimeout(r, 3000));
    await _loadMcpStatus();
  });
}

async function _setMcpTool(server, tool, disabled, autoApproved) {
  const body = {server, tool};
  if (disabled !== null) body.disabled = disabled;
  if (autoApproved !== null) body.auto_approved = autoApproved;
  try {
    await fetch('/api/mcp/tools', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
  } catch { showToast('Failed to update tool', true); }
}

async function _setMcpToolInline(btn, server, tool, disabled, autoApproved) {
  // Toggle UI immediately without collapsing the panel
  btn.classList.toggle('active');
  if (disabled !== null) {
    const row = btn.closest('.mcp-tool-row');
    if (row) row.style.opacity = disabled ? '0.5' : '1';
  }
  await _setMcpTool(server, tool, disabled, autoApproved);
}

function _showToolApproval(data, todoId) {
  // Always store on session so it survives chat close/reopen
  const session = _chatSessions[todoId];
  if (session) {
    if (!session._activeApprovals) session._activeApprovals = {};
    session._activeApprovals[data.approval_id] = data;
  }
  // Show toast so user notices even if chat isn't open
  const streamEl = document.getElementById('chat-assistant-streaming');
  if (!streamEl) {
    showToast(data.server + '.' + data.tool_display + ' needs approval — open chat to respond');
    return;
  }
  _renderToolApprovalCard(data, streamEl);
}

function _renderToolApprovalCard(data, container) {
  const div = document.createElement('div');
  div.id = 'tool-approval-' + data.approval_id;
  div.style.cssText = 'background:rgba(79,110,247,0.08);border:1px solid var(--accent);border-radius:8px;padding:10px 12px;margin:6px 0;font-size:0.8rem';
  const argsStr = Object.entries(data.args || {}).map(([k,v]) => esc(k) + ': ' + esc(typeof v === 'string' ? v : JSON.stringify(v)).slice(0,80)).join('<br>');
  div.innerHTML = '<div style="font-weight:600;margin-bottom:6px">' + esc(data.server) + '.' + esc(data.tool_display) + ' wants to run</div>'
    + (argsStr ? '<div style="color:var(--subtle);font-size:0.75rem;margin-bottom:8px;font-family:monospace">' + argsStr + '</div>' : '')
    + '<div style="display:flex;gap:6px;align-items:center">'
    + '<button onclick="_respondToolApproval(\'' + esc(data.approval_id) + '\',true,false,this)" style="background:var(--accent);color:#fff;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:0.75rem">Approve</button>'
    + '<button onclick="_respondToolApproval(\'' + esc(data.approval_id) + '\',true,true,this)" style="background:#22c55e;color:#fff;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:0.75rem">Always Allow</button>'
    + '<button onclick="_respondToolApproval(\'' + esc(data.approval_id) + '\',false,false,this)" style="background:none;border:1px solid #ef4444;color:#ef4444;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:0.75rem">Deny</button>'
    + '</div>';
  container.appendChild(div);
  const log = document.getElementById('chat-log');
  if (log) log.scrollTop = log.scrollHeight;
}

async function _respondToolApproval(approvalId, approved, alwaysAllow, btn) {
  const card = document.getElementById('tool-approval-' + approvalId);
  // Disable all buttons in the card
  if (card) {
    card.querySelectorAll('button').forEach(b => { b.disabled = true; b.style.opacity = '0.4'; });
    card.style.borderColor = approved ? '#22c55e' : '#ef4444';
    card.style.background = approved ? 'rgba(34,197,94,0.06)' : 'rgba(239,68,68,0.06)';
    const statusDiv = document.createElement('div');
    statusDiv.style.cssText = 'font-size:0.7rem;margin-top:4px';
    statusDiv.style.color = approved ? '#22c55e' : '#ef4444';
    statusDiv.textContent = approved ? (alwaysAllow ? 'Always allowed' : 'Approved') : 'Denied';
    card.appendChild(statusDiv);
  }
  // Remove from active approvals so it doesn't re-render on chat reopen
  if (_activeChatTodoId) {
    const session = _chatSessions[_activeChatTodoId];
    if (session && session._activeApprovals) {
      delete session._activeApprovals[approvalId];
    }
  }
  try {
    await fetch('/api/mcp/approve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({approval_id: approvalId, approved, always_allow: alwaysAllow})
    });
  } catch { showToast('Failed to send approval', true); }
}

async function _toggleAutoApproveAll(checked) {
  try {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({auto_approve_all: checked})
    });
    showToast(checked ? 'Auto-approve enabled' : 'Auto-approve disabled');
  } catch { showToast('Failed to update', true); }
}

async function _logout() {
  if (!confirm('Log out?')) return;
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.reload();
}

async function _refreshMcp() {
  showToast('Reconnecting MCP servers...');
  _mcpLoading(true);
  try {
    await fetch('/api/mcp/reconnect', { method: 'POST' });
    await _loadMcpStatus();
    showToast('MCP reconnected');
  } catch {
    showToast('Failed to reconnect MCP', true);
  } finally {
    _mcpLoading(false);
  }
}

async function _loadGitLog() {
  const container = document.getElementById('git-log-list');
  if (!container) return;
  try {
    const res = await fetch('/api/git/log');
    const data = await res.json();
    if (data.error) {
      container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">' + esc(data.error) + '</div>';
      return;
    }
    if (!data.commits || data.commits.length === 0) {
      container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">No commits yet.</div>';
      return;
    }
    container.innerHTML = data.commits.map(c => {
      const date = c.date.replace(/\s\+.*/, '').replace('T', ' ').slice(0, 16);
      const hash = c.hash.slice(0, 8);
      return '<div style="display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid var(--border);font-size:0.75rem">'
        + '<code style="color:var(--subtle);flex-shrink:0">' + esc(hash) + '</code>'
        + '<span style="color:var(--subtle);flex-shrink:0;width:100px">' + esc(date) + '</span>'
        + '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(c.message) + '</span>'
        + '<button onclick="_gitRollback(\'' + esc(c.hash) + '\')" style="flex-shrink:0;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 6px;font-size:0.65rem;cursor:pointer;color:var(--subtle)" onmousedown="event.stopPropagation()">Restore</button>'
        + '</div>';
    }).join('');
  } catch {
    container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">Failed to load git log.</div>';
  }
}

async function _gitCommit() {
  const message = prompt('Commit message:', 'Manual save ' + new Date().toLocaleString());
  if (message === null) return;
  try {
    const res = await fetch('/api/git/commit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message }),
    });
    const data = await res.json();
    if (data.ok) {
      showToast(data.message || 'Saved');
      _loadGitLog();
    } else {
      showToast(data.error || 'Commit failed', true);
    }
  } catch {
    showToast('Failed to commit', true);
  }
}

async function _gitRollback(hash) {
  if (!confirm('Restore todos to commit ' + hash.slice(0, 8) + '? Current changes will be overwritten.')) return;
  try {
    const res = await fetch('/api/git/rollback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hash }),
    });
    const data = await res.json();
    if (data.ok) {
      showToast('Restored to ' + hash.slice(0, 8));
      _loadGitLog();
      loadTodos();
    } else {
      showToast(data.error || 'Rollback failed', true);
    }
  } catch {
    showToast('Failed to rollback', true);
  }
}

function _rebuildActiveDropdown() {
  const sel = document.getElementById('settings-active-provider');
  const cur = sel.value;
  sel.innerHTML = '<option value="local">Local CLI (fallback)</option>';
  for (const name of Object.keys(_settingsProviders)) {
    const prov = _settingsProviders[name];
    const label = name + ' (' + (prov.type === 'anthropic' ? 'Anthropic' : 'OpenAI-compat') + ')';
    sel.innerHTML += '<option value="' + esc(name) + '">' + esc(label) + '</option>';
  }
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
}

function _renderProviderList() {
  const container = document.getElementById('provider-list');
  let html = '';
  for (const [name, prov] of Object.entries(_settingsProviders)) {
    const isAnthro = prov.type === 'anthropic';
    html += '<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin:8px 0">';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><strong>' + esc(name) + '</strong>';
    html += '<span style="font-size:0.7rem;color:var(--subtle)">' + (isAnthro ? 'Anthropic' : 'OpenAI-compat') + '</span>';
    html += '<button onclick="_removeProvider(\'' + esc(name).replace(/'/g,"\\'") + '\')" style="margin-left:auto;background:none;border:none;color:var(--danger);cursor:pointer;font-size:0.75rem">Remove</button></div>';
    html += '<label style="font-size:0.75rem">API Key</label>';
    html += '<input type="password" data-prov="' + esc(name) + '" data-field="api_key" value="' + esc(prov.api_key || '') + '" placeholder="' + (isAnthro ? 'sk-ant-...' : 'API key') + '" style="width:100%;margin-bottom:4px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
    if (!isAnthro) {
      html += '<label style="font-size:0.75rem">Base URL</label>';
      html += '<input type="text" data-prov="' + esc(name) + '" data-field="base_url" value="' + esc(prov.base_url || '') + '" placeholder="https://..." style="width:100%;margin-bottom:4px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
    }
    html += '<label style="font-size:0.75rem">Model</label>';
    if (isAnthro) {
      html += '<select data-prov="' + esc(name) + '" data-field="model" style="width:100%;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
      for (const m of ['claude-sonnet-4-20250514','claude-haiku-4-5-20251001','claude-opus-4-20250514']) {
        html += '<option value="' + m + '"' + (prov.model === m ? ' selected' : '') + '>' + m.replace(/-20[0-9]+$/, '') + '</option>';
      }
      html += '</select>';
    } else {
      html += '<input type="text" data-prov="' + esc(name) + '" data-field="model" value="' + esc(prov.model || '') + '" placeholder="model-id" style="width:100%;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

function _addProvider() {
  const name = prompt('Provider name (e.g. "runpod", "ollama"):');
  if (!name || _settingsProviders[name]) return;
  const type = prompt('Type: "anthropic" or "openai_compat":', 'openai_compat');
  if (type !== 'anthropic' && type !== 'openai_compat') return;
  _settingsProviders[name] = { type, api_key: '', model: '', base_url: type === 'openai_compat' ? '' : undefined };
  _renderProviderList();
  _rebuildActiveDropdown();
}

function _removeProvider(name) {
  delete _settingsProviders[name];
  _renderProviderList();
  _rebuildActiveDropdown();
}

function _onActiveProviderChange() {}

function hideSettings() {
  document.getElementById('settings-overlay').classList.remove('visible');
}

async function saveSettings() {
  // Read values from DOM back into _settingsProviders
  document.querySelectorAll('#provider-list [data-prov]').forEach(el => {
    const name = el.dataset.prov;
    const field = el.dataset.field;
    if (_settingsProviders[name]) {
      const val = el.value.trim();
      if (field === 'api_key' && val.includes('...')) return; // Skip redacted
      _settingsProviders[name][field] = val;
    }
  });
  const activeProvider = document.getElementById('settings-active-provider').value;
  const body = {
    providers: _settingsProviders,
    active_provider: activeProvider,
    subagents_enabled: document.getElementById('settings-subagents').checked,
    max_subagents: parseInt(document.getElementById('settings-max-subagents').value) || 10,
  };
  try {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    hideSettings();
    showToast('Settings saved');
  } catch {
    showToast('Failed to save settings');
  }
}

async function resumeConv(convId, todoId) {
  // Open chat panel with this conversation
  if (todoId) {
    openChat(todoId, convId);
  }
}

// ---------------------------------------------------------------------------
// Chat UI
// ---------------------------------------------------------------------------

async function startChatBackground(todoId) {
  // Start /ea workon in background without opening the chat panel
  await _loadChatSession(todoId);
  let session = _chatSessions[todoId];
  const isNew = !session || session.messages.length === 0;
  if (!session) {
    _chatSessions[todoId] = { conversationId: null, messages: [], streamingText: '', streamingJobId: null };
    session = _chatSessions[todoId];
  }
  if (!isNew) {
    // Session exists — send checkon instead
    if (session.streamingJobId) { showToast('Chat is busy'); return; }
    const msg = '/ea checkon ' + todoId;
    session.messages.push({ role: 'user', content: msg });
    session.streamingJobId = 'pending';
    session.streamingText = '';
    _updateSpinnersInPlace();
    if (_activeChatTodoId === todoId) { _syncChatSendBtn(todoId); _renderChatLog(todoId); }
    showToast('Checking on...');
    try {
      const res = await fetch(API + '/' + todoId + '/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ message: msg }),
      });
      if (!res.ok) { _chatStreamDone(todoId, 'Failed to send'); return; }
      const data = await res.json();
      _streamChatResponse(todoId, data.job_id);
    } catch (e) {
      _chatStreamDone(todoId, 'Network error');
    }
    return;
  }
  const msg = '/ea workon ' + todoId;
  session.messages.push({ role: 'user', content: msg });
  session.streamingJobId = 'pending';
  session.streamingText = '';
  _updateSpinnersInPlace();
  try {
    const res = await fetch(API + '/' + todoId + '/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });
    if (!res.ok) { _chatStreamDone(todoId, 'Failed to send'); return; }
    const data = await res.json();
    _streamChatResponse(todoId, data.job_id);
  } catch (e) {
    _chatStreamDone(todoId, 'Network error');
  }
}

async function openChat(todoId, conversationId) {
  // Clear chat unread immediately (user is looking at it)
  if (_chatUnread.has(todoId)) {
    _chatUnread.delete(todoId);
    fetch('/api/chats/' + todoId + '/read', { method: 'POST' }).catch(() => {});
    _updateSpinnersInPlace();
  }
  // Defer title updated mark-read until navigating away
  _pendingMarkRead = todoId;
  // Load persisted chat from server
  await _loadChatSession(todoId);
  let session = _chatSessions[todoId];
  const isNew = !session || session.messages.length === 0;
  if (!session) {
    _chatSessions[todoId] = { conversationId: conversationId || null, messages: [], streamingText: '', streamingJobId: null };
    session = _chatSessions[todoId];
  } else if (conversationId) {
    session.conversationId = conversationId;
  }
  _showChatOverlay(todoId);
  // Auto-send /ea workon for brand-new chats (no conversationId = not resuming)
  if (isNew && !conversationId) {
    const msg = '/ea workon ' + todoId;
    session.messages.push({ role: 'user', content: msg });
    session.streamingJobId = 'pending';
    session.streamingText = '';
    _syncChatSendBtn(todoId);
    _renderChatLog(todoId);
    try {
      const res = await fetch(API + '/' + todoId + '/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ message: msg }),
      });
      if (!res.ok) { _chatStreamDone(todoId, 'Failed to send'); return; }
      const data = await res.json();
      _streamChatResponse(todoId, data.job_id);
    } catch (e) {
      _chatStreamDone(todoId, 'Network error');
    }
  }
}

function _showChatOverlay(todoId) {
  _activeChatTodoId = todoId;
  const todo = allTodos.find(t => t.id === todoId);
  const title = todo ? _parseTitle(todo.title || '').displayTitle : todoId;
  document.getElementById('chat-title').textContent = title;
  _updateProviderBadge();

  const overlay = document.getElementById('chat-overlay');
  overlay.style.display = 'flex';
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => overlay.classList.add('visible'));

  _renderChatLog(todoId);
  const input = document.getElementById('chat-input');
  input.value = '';
  input.focus();
  _syncChatSendBtn(todoId);
}

async function _updateProviderBadge() {
  const badge = document.getElementById('chat-provider-badge');
  if (!badge) return;
  try {
    const res = await fetch('/api/config');
    const config = await res.json();
    const name = config._active_provider_name || 'local';
    const type = config._active_provider_type || 'local';
    const providers = config.providers || {};
    const prov = providers[name] || {};
    let label = name;
    if (type === 'anthropic') label = (prov.model || 'claude').replace(/-20[0-9]+$/, '');
    else if (type === 'openai_compat') label = name + ': ' + (prov.model || 'default');
    badge.textContent = label;
  } catch {
    badge.textContent = '';
  }
}

function _syncChatSendBtn(todoId) {
  if (_activeChatTodoId !== todoId) return;
  const session = _chatSessions[todoId];
  const isStreaming = !!(session && session.streamingJobId);
  const btn = document.getElementById('chat-send-btn');
  if (isStreaming) {
    btn.textContent = 'Stop';
    btn.disabled = false;
    btn.classList.add('chat-stop-mode');
  } else {
    btn.textContent = 'Send';
    btn.disabled = false;
    btn.classList.remove('chat-stop-mode');
  }
}

function _renderChatLog(todoId) {
  const session = _chatSessions[todoId];
  if (!session) return;
  const log = document.getElementById('chat-log');
  let html = '';
  session.messages.forEach((msg, i) => {
    if (i > 0 && msg.role === 'user') html += '<hr class="chat-turn-sep">';
    if (msg.role === 'user') {
      html += '<div class="chat-user-line">&gt; ' + esc(msg.content) + '</div>';
    } else {
      html += '<div class="chat-assistant-block">' + renderMd(msg.content) + '</div>';
    }
  });
  // If currently streaming, re-attach the streaming area + spinner (no extra separator —
  // the user message that triggered this is already the last rendered item)
  if (session.streamingJobId) {
    html += '<div id="chat-assistant-streaming"></div>';
    html += '<div id="chat-spinner" style="margin-top:4px"><l-bouncy size="20" speed="1.75" color="var(--accent)"></l-bouncy></div>';
  }
  log.innerHTML = html;
  // If streaming, populate the streaming div with current partial text + pending approvals
  if (session.streamingJobId) {
    const streamEl = document.getElementById('chat-assistant-streaming');
    if (streamEl) {
      if (session.streamingText) {
        streamEl.innerHTML = '<div class="chat-assistant-block">' + renderMd(session.streamingText) + '</div>';
      }
      // Re-render any active (unanswered) approval cards
      if (session._activeApprovals) {
        for (const data of Object.values(session._activeApprovals)) {
          _renderToolApprovalCard(data, streamEl);
        }
      }
    }
  }
  log.scrollTop = log.scrollHeight;
}

async function restartChat() {
  const todoId = _activeChatTodoId;
  if (!todoId) return;
  // Stop any running job first
  const session = _chatSessions[todoId];
  if (session && session.streamingJobId && session.streamingJobId !== 'pending') {
    try { await fetch('/api/jobs/' + session.streamingJobId + '/kill', { method: 'POST' }); } catch {}
  }
  _chatSessions[todoId] = { conversationId: null, messages: [], streamingText: '', streamingJobId: null };
  _renderChatLog(todoId);
  _syncChatSendBtn(todoId);
  fetch('/api/chats/' + todoId, { method: 'DELETE' }).catch(() => {});
  // Auto-send /ea workon
  const msg = '/ea workon ' + todoId;
  const s = _chatSessions[todoId];
  s.messages.push({ role: 'user', content: msg });
  s.streamingJobId = 'pending';
  _syncChatSendBtn(todoId);
  _renderChatLog(todoId);
  try {
    const res = await fetch(API + '/' + todoId + '/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });
    if (!res.ok) { _chatStreamDone(todoId, 'Failed to send'); return; }
    const data = await res.json();
    _streamChatResponse(todoId, data.job_id);
  } catch (e) {
    _chatStreamDone(todoId, 'Network error');
  }
}

function minimizeChat() {
  _flushPendingMarkRead();
  const overlay = document.getElementById('chat-overlay');
  overlay.classList.remove('visible');
  document.body.style.overflow = '';
  setTimeout(() => { overlay.style.display = 'none'; }, 100);
  _activeChatTodoId = null;
}

let _stopConfirmTimer = null;
let _stopConfirmTodoId = null;

function chatSendOrStop(todoId) {
  const session = _chatSessions[todoId];
  if (session && session.streamingJobId) {
    // Require double-press to stop
    if (_stopConfirmTodoId === todoId && _stopConfirmTimer) {
      clearTimeout(_stopConfirmTimer);
      _stopConfirmTimer = null;
      _stopConfirmTodoId = null;
      stopChat(todoId);
      _syncChatSendBtn(todoId);
    } else {
      _stopConfirmTodoId = todoId;
      const btn = document.getElementById('chat-send-btn');
      if (btn) { btn.textContent = 'Confirm Stop'; }
      _stopConfirmTimer = setTimeout(() => {
        _stopConfirmTimer = null;
        _stopConfirmTodoId = null;
        _syncChatSendBtn(todoId);
      }, 2000);
    }
  } else {
    sendChatMessage(todoId);
  }
}

async function sendChatMessage(todoId) {
  const session = _chatSessions[todoId];
  if (session && session.streamingJobId) return;
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  await _sendChatDirect(todoId, message);
}

async function _sendChatDirect(todoId, message) {
  const session = _chatSessions[todoId];
  if (!session) return;

  // Add user message and mark as streaming (spinner will show via _renderChatLog)
  session.messages.push({ role: 'user', content: message });
  session.streamingJobId = 'pending';
  session.streamingText = '';

  if (_activeChatTodoId === todoId) {
    _syncChatSendBtn(todoId);
    _renderChatLog(todoId);
  }

  // POST to start job
  try {
    const res = await fetch(API + '/' + todoId + '/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message, conversation_id: session.conversationId }),
    });
    if (!res.ok) {
      _chatStreamDone(todoId, 'Failed to send message');
      return;
    }
    const data = await res.json();
    _streamChatResponse(todoId, data.job_id);
  } catch (e) {
    _chatStreamDone(todoId, 'Network error');
  }
}

function _streamChatResponse(todoId, jobId) {
  console.log('[chat] _streamChatResponse called', todoId, jobId);
  const session = _chatSessions[todoId];
  if (!session) { console.log('[chat] no session for', todoId); return; }

  session.streamingJobId = jobId;
  session.streamingText = '';
  _updateSpinnersInPlace();

  const es = new EventSource('/api/jobs/' + jobId + '/stream');
  let textDiv = null;
  let currentBlockText = ''; // text for the current block only (resets after tool calls)

  // Check if this stream is still the active one for this todo
  function isStale() {
    const cur = _chatSessions[todoId];
    return !cur || cur.streamingJobId !== jobId;
  }

  console.log('[chat] SSE connected for job', jobId, 'todo', todoId);
  es.onmessage = function(e) {
    if (isStale()) { console.log('[chat] stale, closing'); es.close(); return; }

    let raw;
    try { raw = JSON.parse(e.data); } catch { console.log('[chat] parse error', e.data); return; }
    console.log('[chat]', typeof raw === 'string' ? raw : JSON.stringify(raw));

    if (typeof raw === 'object' && raw.__done__) {
      es.close();
      if (isStale()) return;
      const cur = _chatSessions[todoId];
      if (raw.conversation_id && cur) {
        cur.conversationId = raw.conversation_id;
      }
      _chatStreamDone(todoId, null, cur ? cur.streamingText : '');
      return;
    }

    if (typeof raw === 'object' && raw.__tool_approval__) {
      _showToolApproval(raw, todoId);
      return;
    }

    if (typeof raw !== 'string') return;
    const line = raw;
    const cur = _chatSessions[todoId];

    // Look up the streaming element fresh each time (survives minimize/reopen)
    const streamEl = document.getElementById('chat-assistant-streaming');

    if (streamEl) {
      if (line.startsWith('error:') || line.startsWith('error ')) {
        const div = document.createElement('div');
        div.style.cssText = 'color:#ef4444;font-size:0.8rem;padding:6px 10px;background:rgba(239,68,68,0.08);border-radius:6px;border-left:3px solid #ef4444;margin:4px 0;white-space:pre-wrap;word-break:break-word';
        div.textContent = line;
        streamEl.appendChild(div);
        textDiv = null;
        currentBlockText = '';
      } else if (line.startsWith('\u25b6 ')) {
        const div = document.createElement('div');
        div.className = 'chat-tool-line';
        div.textContent = line;
        streamEl.appendChild(div);
        textDiv = null;
        currentBlockText = ''; // reset for next text block
      } else if (line.startsWith('\u2713 Done')) {
        const div = document.createElement('div');
        div.className = 'chat-cost-line';
        div.textContent = line;
        streamEl.appendChild(div);
        textDiv = null;
        currentBlockText = '';
      } else {
        cur.streamingText += (cur.streamingText ? '\n' : '') + line;
        currentBlockText += (currentBlockText ? '\n' : '') + line;
        if (!textDiv || !textDiv.parentNode) {
          textDiv = document.createElement('div');
          textDiv.className = 'chat-assistant-block';
          streamEl.appendChild(textDiv);
        }
        textDiv.innerHTML = renderMd(currentBlockText);
      }
    } else {
      // Panel is minimized — just accumulate text
      if (!line.startsWith('\u25b6 ') && !line.startsWith('\u2713 Done')) {
        cur.streamingText += (cur.streamingText ? '\n' : '') + line;
      }
    }

    const log = document.getElementById('chat-log');
    if (log) log.scrollTop = log.scrollHeight;
  };

  es.onerror = function() {
    es.close();
    if (isStale()) return;
    const cur = _chatSessions[todoId];
    _chatStreamDone(todoId, null, cur ? cur.streamingText : '');
  };
}

function _chatStreamDone(todoId, error, assistantText) {
  const session = _chatSessions[todoId];
  if (session) {
    session.streamingJobId = null;
    session.streamingText = '';
  }
  _updateSpinnersInPlace();

  if (_activeChatTodoId === todoId) {
    _syncChatSendBtn(todoId);
    const spinner = document.getElementById('chat-spinner');
    if (spinner) spinner.remove();
  }

  if (error) {
    if (_activeChatTodoId === todoId) {
      const log = document.getElementById('chat-log');
      if (log) log.insertAdjacentHTML('beforeend', '<div style="color:#ef4444">' + esc(error) + '</div>');
    }
  
    return;
  }

  if (session && assistantText) {
    session.messages.push({ role: 'assistant', content: assistantText });
  }

  // If chat panel is open for this item, mark as read; otherwise it stays unread
  if (_activeChatTodoId === todoId) {
    _chatUnread.delete(todoId);
    fetch('/api/chats/' + todoId + '/read', { method: 'POST' }).catch(() => {});
    _renderChatLog(todoId);
  }

  const input = document.getElementById('chat-input');
  if (input) input.focus();
}

async function stopChat(todoId) {
  const session = _chatSessions[todoId];
  if (!session || !session.streamingJobId || session.streamingJobId === 'pending') return;
  try {
    await fetch('/api/jobs/' + session.streamingJobId + '/kill', { method: 'POST' });
  } catch {}
}

async function _loadChatSession(todoId) {
  try {
    const res = await fetch('/api/chats/' + todoId);
    if (!res.ok) return;
    const data = await res.json();
    const existing = _chatSessions[todoId];
    if (existing) {
      existing.conversationId = data.conversationId || existing.conversationId;
      existing.messages = data.messages || existing.messages;
    } else {
      _chatSessions[todoId] = {
        conversationId: data.conversationId || null,
        messages: data.messages || [],
        streamingText: '',
        streamingJobId: null,
      };
    }
    // If server reports a running job, reconnect the SSE stream
    if (data.running_job_id) {
      const session = _chatSessions[todoId];
      if (!session.streamingJobId) {
        session.streamingJobId = data.running_job_id;
        session.streamingText = '';
        _streamChatResponse(todoId, data.running_job_id);
      }
    }
  } catch {}
}

function _startChatResize(e) {
  e.preventDefault();
  const panel = document.getElementById('chat-panel');
  const isTouch = e.type === 'touchstart';
  const startY = isTouch ? e.touches[0].clientY : e.clientY;
  const startH = panel.offsetHeight;
  function onMove(e) {
    const y = e.touches ? e.touches[0].clientY : e.clientY;
    const h = Math.min(window.innerHeight * 0.9, Math.max(150, startH - (y - startY)));
    panel.style.height = h + 'px';
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onUp);
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
  document.addEventListener('touchmove', onMove, { passive: false });
  document.addEventListener('touchend', onUp);
}

async function eaUpdateToggle() {
  const isRunning = Object.values(_jobsState).some(
    j => j.job_key === 'ea-update' && (j.status === 'running' || j.status === 'pending')
  );
  if (isRunning) await cancelEaUpdate(); else await eaUpdate();
}

async function eaUpdate(force) {
  try {
    const res = await fetch('/api/ea-update', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({force: !!force}) });
    const data = await res.json();
    if (res.ok && data.status === 'already_running') {
      showToast('Already running — click to restart', false, () => eaUpdate(true));
    } else if (res.ok) {
      showToast('EA update started', false);
      if (data.job_id) {
        // Animate immediately — don't wait for pollJobs round trip
        if (!_eaWasRunning) { _eaWasRunning = true; _transitionEaBtn(true); }
        const btn = document.getElementById('ea-update-btn');
        if (btn) btn.classList.add('running');
        _openEaUpdateStream(data.job_id);
        pollJobs();
      }
    } else {
      showToast(data.error || 'Failed to start EA update', true);
    }
  } catch (e) {
    showToast('Failed to start EA update', true);
  }
}

async function eaUpdateItem(id, force) {
  try {
    const res = await fetch('/api/ea-update-item', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, force: !!force})
    });
    const data = await res.json();
    if (res.ok && data.status === 'already_running') {
      if (data.job_id) await killJob(data.job_id);
      return;
    } else if (res.ok) {
      showToast(`Checking ${id}...`, false);
      if (data.job_id) {
        // Show spinner immediately — don't wait for pollJobs round trip
        _jobsState[data.job_id] = { id: data.job_id, job_key: 'ea-' + id, status: 'running', created_at: Date.now()/1000, _optimistic: true };
        _updateSpinnersInPlace();
        _openItemStream(id, data.job_id);
      }
    } else {
      showToast(data.error || 'Failed', true);
    }
  } catch (e) {
    // silent
  }
}

// ---- Shared toast helper ----
function showToast(msg, isError, onClick) {
  const toast = document.createElement('div');
  toast.textContent = msg;
  toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:' + (isError ? 'var(--danger)' : 'var(--text)') + ';color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;z-index:5000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s' + (onClick ? ';cursor:pointer' : '');
  if (onClick) toast.addEventListener('click', () => { toast.remove(); onClick(); });
  document.body.appendChild(toast);
  requestAnimationFrame(() => { toast.style.opacity = '1'; });
  setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 2000);
}

// ---- Inline job streaming ----
let _jobsPollTimer = null;
let _jobsState = {};          // jobId -> job metadata (from server)
let _clientJobLines = {};     // todoId -> string[] of parsed display lines
let _clientJobSummary = {};   // todoId -> string[] of message-only lines (no tool calls)
let _clientJobIds = {};       // todoId -> jobId that populated _clientJobLines
let _clientJobStartTime = {}; // todoId -> Date when checkon started
let _itemStreamSources = {};  // todoId -> EventSource
let _eaUpdateTimerInterval = null;
let _eaWasRunning = false;
let _eaUpdateBubbleLines = [];
let _eaUpdateBubbleStream = null; // EventSource for ea-update output

// Interactive terminal sessions
let _termSessions = {};       // todoId -> {sessionId, term, fitAddon, ws, alive}
let _activeTermTodoId = null;  // which session is visible in the overlay
let _termResizeObserver = null;

// Chat sessions
let _chatSessions = {};       // todoId -> { conversationId, messages: [{role, content}] }
let _activeChatTodoId = null;  // which chat is visible in the overlay
let _chatUnread = new Set();   // todoIds with unread chat responses
let _eaTransitionTimer = null;

function _todoIdForJob(job) {
  if (!job || !job.job_key) return null;
  const key = job.job_key;
  if (key.startsWith('workon-')) return key.slice(7);
  if (key.startsWith('chat-')) return key.slice(5);
  if (key.startsWith('ea-') && key !== 'ea-update') return key.slice(3);
  return null;
}

function _getActiveJobForTodo(todoId) {
  let fallback = null;
  for (const j of Object.values(_jobsState)) {
    if (_todoIdForJob(j) !== todoId || j.status === 'killed') continue;
    if (j.status === 'running' || j.status === 'pending') return j;
    if (!fallback) fallback = j;
  }
  return fallback;
}

function _openItemStream(todoId, jobId) {
  if (_itemStreamSources[todoId]) {
    _itemStreamSources[todoId].close();
    delete _itemStreamSources[todoId];
  }
  // New job for this todo — start fresh output
  if (_clientJobIds[todoId] !== jobId) {
    _clientJobLines[todoId] = [];
    _clientJobSummary[todoId] = [];
    _clientJobIds[todoId] = jobId;
    _clientJobStartTime[todoId] = new Date();
    // Clear summary div and add spinner
    const sumEl = document.getElementById('checkon-summary-' + todoId);
    if (sumEl) {
      const timeStr = _clientJobStartTime[todoId].toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      sumEl.innerHTML = '<div class="checkon-header">Status check — ' + timeStr + '</div><div class="checkon-body"></div><l-bouncy class="checkon-inline-spinner" size="20" speed="1.75" color="var(--muted)"></l-bouncy>';
      sumEl.classList.add('has-content');
    }
  }
  if (!_clientJobLines[todoId]) _clientJobLines[todoId] = [];
  if (!_clientJobSummary[todoId]) _clientJobSummary[todoId] = [];
  const existingCount = _clientJobLines[todoId].length;
  let parsedCount = 0;
  const src = new EventSource('/api/jobs/' + jobId + '/stream');
  _itemStreamSources[todoId] = src;
  src.onmessage = (e) => {
    let raw;
    try { raw = JSON.parse(e.data); } catch { return; }
    if (typeof raw === 'object' && raw.__done__) {
      src.close(); delete _itemStreamSources[todoId];
      // Mark bubble as done so hover no longer shows it
      const bubble = document.getElementById('checkon-bubble-' + todoId);
      if (bubble) bubble.classList.add('done');
      // Remove spinner and add completion footer
      const sumDone = document.getElementById('checkon-summary-' + todoId);
      if (sumDone) {
        const spinner = sumDone.querySelector('.checkon-inline-spinner');
        if (spinner) spinner.remove();
        const now = new Date();
        const timeStr = now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        const footer = document.createElement('div');
        footer.className = 'checkon-footer';
        footer.textContent = 'Complete — ' + timeStr;
        sumDone.appendChild(footer);
      }
      pollJobs();
      return;
    }
    const line = parseStreamLine(raw);
    if (!line) return;
    if (parsedCount < existingCount) { parsedCount++; return; }
    console.log(`[job:${jobId}]`, line);
    _clientJobLines[todoId].push(line);
    parsedCount++;
    // Bubble: all lines (live feedback while running)
    const outEl = document.getElementById('checkon-bubble-' + todoId);
    if (outEl) {
      outEl.classList.add('has-content');
      const div = document.createElement('div');
      if (line.startsWith('✓')) div.style.color = 'var(--accent)';
      div.textContent = line;
      outEl.appendChild(div);
      outEl.scrollTop = outEl.scrollHeight;
    }
    // Summary: only message lines (no tool calls)
    if (!line.startsWith('▶') && !line.startsWith('✓')) {
      _clientJobSummary[todoId].push('⏺ ' + line);
      const sumEl = document.getElementById('checkon-summary-' + todoId);
      if (sumEl) {
        sumEl.classList.add('has-content');
        const bodyEl = sumEl.querySelector('.checkon-body');
        if (bodyEl) bodyEl.textContent = _clientJobSummary[todoId].join('\n');
      }
    }
  };
  src.onerror = () => { src.close(); delete _itemStreamSources[todoId]; };
}

function _updateSpinnersInPlace() {
  document.querySelectorAll('.todo-item[data-todo-id]').forEach(el => {
    const todoId = el.dataset.todoId;
    const job = _getActiveJobForTodo(todoId);
    const termSession = _termSessions[todoId];
    const chatSession = _chatSessions[todoId];
    const titleEl = el.querySelector('.todo-title');
    if (!titleEl) return;
    const hasTerminal = termSession && termSession.alive;
    const hasChatStreaming = !!(chatSession && chatSession.streamingJobId);
    const hasJob = job && job.status === 'running' && !(job.job_key && job.job_key.startsWith('chat-'));

    // Terminal spinner (green, opens terminal on click)
    const existingTermSpinner = titleEl.querySelector('.term-spinner');
    if (hasTerminal) {
      if (!existingTermSpinner) {
        const s = document.createElement('span');
        s.className = 'job-spinner term-spinner'; s.title = 'Open terminal';
        s.innerHTML = '<span class="sk-child"></span><span class="sk-child sk-bounce2"></span>';
        s.onclick = e => { e.stopPropagation(); openTerminal(todoId); };
        titleEl.insertBefore(s, titleEl.firstChild);
      }
    } else {
      if (existingTermSpinner) existingTermSpinner.remove();
    }

    // Chat spinner (green jelly-triangle, opens chat on click)
    const existingChatSpinner = titleEl.querySelector('.chat-spinner');
    if (hasChatStreaming) {
      if (!existingChatSpinner) {
        const s = document.createElement('span');
        s.className = 'job-spinner chat-spinner'; s.title = 'Open chat';
        s.innerHTML = '<l-jelly-triangle size="13" speed="1.75" color="#22c55e"></l-jelly-triangle>';
        s.onclick = e => { e.stopPropagation(); openChat(todoId); };
        const after = titleEl.querySelector('.term-spinner');
        titleEl.insertBefore(s, after ? after.nextSibling : titleEl.firstChild);
      }
    } else {
      if (existingChatSpinner) existingChatSpinner.remove();
    }

    // Job spinner (accent color, kills job on click) — non-chat jobs only
    const existingJobSpinner = titleEl.querySelector('.job-spinner:not(.term-spinner):not(.chat-spinner)');
    if (hasJob) {
      if (!existingJobSpinner) {
        const s = document.createElement('span');
        s.className = 'job-spinner'; s.title = 'Stop job';
        s.innerHTML = '<l-jelly-triangle size="13" speed="1.75" color="var(--accent)"></l-jelly-triangle>';
        s.onclick = e => { e.stopPropagation(); killJob(job.id); };
        const insertBefore = titleEl.querySelector('.term-spinner') ? titleEl.querySelector('.term-spinner').nextSibling : titleEl.firstChild;
        titleEl.insertBefore(s, insertBefore);
      } else {
        existingJobSpinner.onclick = e => { e.stopPropagation(); killJob(job.id); };
      }
    } else {
      if (existingJobSpinner) existingJobSpinner.remove();
    }

    // Unread dot — chat unread OR updated tag unseen
    const todo = allTodos.find(x => x.id === todoId);
    const hasUpdatedTag = todo && _parseTitle(todo.title || '').hasUpdatedTag && !_seenUpdates.has(todoId);
    const hasUnread = _chatUnread.has(todoId) || hasUpdatedTag;
    const existingUnreadDot = titleEl.querySelector('.chat-unread-dot');
    if (hasUnread) {
      if (!existingUnreadDot) {
        const dot = document.createElement('span');
        dot.className = 'chat-unread-dot';
        dot.innerHTML = '<l-ripples size="13" speed="2" color="#f59e0b"></l-ripples>';
        dot.onclick = e => { e.stopPropagation(); openChat(todoId); };
        dot.style.cursor = 'pointer';
        titleEl.insertBefore(dot, titleEl.firstChild);
      }
    } else {
      if (existingUnreadDot) existingUnreadDot.remove();
    }
  });
}

function _restoreJobOutputs() {
  for (const [todoId, lines] of Object.entries(_clientJobLines)) {
    if (!lines.length) continue;
    const outEl = document.getElementById('checkon-bubble-' + todoId);
    if (outEl) {
      // If job is done (no active stream), mark bubble as done so hover won't show it
      const isDone = !_itemStreamSources[todoId];
      outEl.classList.add('has-content');
      if (isDone) outEl.classList.add('done');
      outEl.innerHTML = lines.map(l => {
        const style = l.startsWith('✓') ? ' style="color:var(--accent)"' : '';
        const d = document.createElement('div'); d.textContent = l;
        return `<div${style}>${d.innerHTML}</div>`;
      }).join('');
      outEl.scrollTop = outEl.scrollHeight;
    }
  }
  // Restore summaries
  for (const [todoId, lines] of Object.entries(_clientJobSummary)) {
    if (!lines.length) continue;
    const sumEl = document.getElementById('checkon-summary-' + todoId);
    if (sumEl) {
      sumEl.innerHTML = '';
      sumEl.classList.add('has-content');
      const startTime = _clientJobStartTime[todoId];
      const isStreaming = !!_itemStreamSources[todoId];
      if (startTime) {
        const timeStr = startTime.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        const header = document.createElement('div');
        header.className = 'checkon-header';
        header.textContent = 'Status check \u2014 ' + timeStr;
        sumEl.appendChild(header);
      }
      const body = document.createElement('div');
      body.className = 'checkon-body';
      body.textContent = lines.join('\n');
      sumEl.appendChild(body);
      if (isStreaming) {
        const spinner = document.createElement('l-bouncy');
        spinner.className = 'checkon-inline-spinner';
        spinner.setAttribute('size', '20');
        spinner.setAttribute('speed', '1.75');
        spinner.setAttribute('color', 'var(--muted)');
        sumEl.appendChild(spinner);
      }
    }
  }
}

function parseStreamLine(raw) {
  // Server pre-formats lines via the Claude Agent SDK; raw is already a display string.
  if (typeof raw !== 'string') return null;
  return raw.trim() || null;
}

async function killJob(jobId) {
  await fetch('/api/jobs/' + jobId + '/kill', { method: 'POST' });
  pollJobs();
}

function _transitionEaBtn(toRunning) {
  if (_eaTransitionTimer) { clearTimeout(_eaTransitionTimer); _eaTransitionTimer = null; }
  const lbl = document.getElementById('ea-update-label');
  const run = document.getElementById('ea-update-running');
  if (!lbl || !run) return;

  lbl.classList.remove('anim-in', 'anim-out');
  run.classList.remove('anim-in', 'anim-out');
  void lbl.offsetWidth; // force reflow

  const outEl = toRunning ? lbl : run;
  const inEl  = toRunning ? run : lbl;

  // Ensure outgoing starts at opacity:1, incoming at opacity:0
  outEl.style.opacity = '1';
  inEl.style.opacity = '0';
  void inEl.offsetWidth;

  outEl.classList.add('anim-out');
  inEl.classList.add('anim-in');

  _eaTransitionTimer = setTimeout(() => {
    outEl.classList.remove('anim-out'); outEl.style.opacity = '0';
    inEl.classList.remove('anim-in');   inEl.style.opacity = '1';
    _eaTransitionTimer = null;
  }, 400);
}

function _updateEaUpdateBtn() {
  const btn = document.getElementById('ea-update-btn');
  if (!btn) return;

  const job = Object.values(_jobsState).find(
    j => j.job_key === 'ea-update' && (j.status === 'running' || j.status === 'pending')
  );
  const isRunning = !!job;

  if (isRunning !== _eaWasRunning) {
    _eaWasRunning = isRunning;
    _transitionEaBtn(isRunning);
  }

  if (isRunning) {
    btn.classList.add('running');
    if (_eaUpdateTimerInterval) clearInterval(_eaUpdateTimerInterval);
    const tick = () => {
      const timer = document.getElementById('ea-update-timer');
      if (!timer) return;
      const elapsed = Math.floor(Date.now() / 1000 - job.created_at);
      const m = Math.floor(elapsed / 60);
      const s = String(elapsed % 60).padStart(2, '0');
      timer.textContent = m > 0 ? `${m}:${s}` : `0:${s}`;
    };
    tick();
    _eaUpdateTimerInterval = setInterval(tick, 1000);
  } else {
    btn.classList.remove('running');
    if (_eaUpdateTimerInterval) { clearInterval(_eaUpdateTimerInterval); _eaUpdateTimerInterval = null; }
  }
}

async function cancelEaUpdate() {
  const job = Object.values(_jobsState).find(
    j => j.job_key === 'ea-update' && (j.status === 'running' || j.status === 'pending')
  );
  if (job) await killJob(job.id);
}

function _restoreEaUpdateBubble() {
  const bubble = document.getElementById('ea-update-bubble');
  if (!bubble || !_eaUpdateBubbleLines.length) return;
  bubble.innerHTML = '';
  bubble.classList.add('has-content');
  for (const line of _eaUpdateBubbleLines) {
    const div = document.createElement('div');
    if (line.startsWith('✓')) div.style.color = 'var(--accent)';
    div.textContent = line;
    bubble.appendChild(div);
  }
  bubble.scrollTop = bubble.scrollHeight;
}

function _appendEaUpdateBubbleLine(line) {
  _eaUpdateBubbleLines.push(line);
  const bubble = document.getElementById('ea-update-bubble');
  if (!bubble) return;
  bubble.classList.add('has-content');
  const div = document.createElement('div');
  if (line.startsWith('✓')) div.style.color = 'var(--accent)';
  div.textContent = line;
  bubble.appendChild(div);
  bubble.scrollTop = bubble.scrollHeight;
}

function _openEaUpdateStream(jobId) {
  if (_eaUpdateBubbleStream) { _eaUpdateBubbleStream.close(); _eaUpdateBubbleStream = null; }
  _eaUpdateBubbleLines = [];
  const bubble = document.getElementById('ea-update-bubble');
  if (bubble) { bubble.textContent = ''; bubble.classList.remove('has-content'); }

  const src = new EventSource('/api/jobs/' + jobId + '/stream');
  _eaUpdateBubbleStream = src;
  src.onmessage = (e) => {
    let raw;
    try { raw = JSON.parse(e.data); } catch { return; }
    if (typeof raw === 'object' && raw.__done__) {
      src.close(); _eaUpdateBubbleStream = null;
      return;
    }
    const line = parseStreamLine(raw);
    if (!line) return;
    _appendEaUpdateBubbleLine(line);
  };
  src.onerror = () => { src.close(); _eaUpdateBubbleStream = null; };
}

async function pollJobs() {
  clearTimeout(_jobsPollTimer);
  try {
    const res = await fetch('/api/jobs');
    const jobs = await res.json();
    const serverIds = new Set(jobs.map(j => j.id));
    // Remove entries not on server (completed/purged), keep optimistic entries for jobs server hasn't seen yet
    for (const id of Object.keys(_jobsState)) {
      if (!serverIds.has(id) && _jobsState[id]._optimistic) {
        // Keep optimistic entry until server confirms
      } else if (!serverIds.has(id)) {
        delete _jobsState[id];
      }
    }
    // Update/add from server (server is authoritative)
    jobs.forEach(j => { _jobsState[j.id] = j; });
    // Open streams for running jobs that don't have one yet
    for (const j of jobs) {
      if (j.status !== 'running') continue;
      if (j.job_key === 'ea-update' && !_eaUpdateBubbleStream) {
        _openEaUpdateStream(j.id);
      }
      const todoId = _todoIdForJob(j);
      if (todoId && !_itemStreamSources[todoId] && !j.job_key.startsWith('chat-')) _openItemStream(todoId, j.id);
    }
    // Clear client lines for jobs that are gone
    for (const todoId of Object.keys(_clientJobLines)) {
      const stillActive = jobs.some(j => _todoIdForJob(j) === todoId && j.status !== 'killed');
      if (!stillActive) delete _clientJobLines[todoId];
    }
    _updateSpinnersInPlace();
    _restoreJobOutputs();
    _updateEaUpdateBtn();
    // Poll terminal sessions too
    try {
      const tRes = await fetch('/api/terminal/sessions');
      const sessions = await tRes.json();
      const aliveIds = new Set();
      const serverSessions = {};
      for (const s of sessions) {
        if (s.alive) { aliveIds.add(s.todo_id); serverSessions[s.todo_id] = s; }
      }
      // Create placeholder entries for server-known sessions missing on client
      for (const [todoId, s] of Object.entries(serverSessions)) {
        if (!_termSessions[todoId]) {
          _termSessions[todoId] = { sessionId: s.session_id, term: null, fitAddon: null, ws: null, alive: true };
        }
      }
      // Mark dead sessions on client
      for (const [todoId, ts] of Object.entries(_termSessions)) {
        if (!aliveIds.has(todoId) && ts.alive) {
          ts.alive = false;
          if (ts.term) ts.term.writeln('\\r\\n\\x1b[2m[session ended]\\x1b[0m');
        }
      }
      _updateSpinnersInPlace();
    } catch {}
    // Poll chat unread state
    try {
      const uRes = await fetch('/api/chats/unread');
      const unreadIds = await uRes.json();
      const newSet = new Set(unreadIds);
      if (_chatUnread.size !== newSet.size || [..._chatUnread].some(id => !newSet.has(id))) {
        _chatUnread = newSet;
        _updateSpinnersInPlace();
      }
    } catch {}
  } catch {}
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
}

async function bringToTop(id) {
  const res = await fetch(API + '/move-to-top', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id})
  });
  if (!res.ok) return;
  await loadTodos();
  const ni = visibleIds.indexOf(id);
  if (ni >= 0) selectedIdx = ni + 1;
  applySelection();
}

async function moveSelected(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const res = await fetch(API + '/reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, direction})
  });
  if (!res.ok) return;
  const rememberedId = id;
  await loadTodos();
  const newIdx = visibleIds.indexOf(rememberedId);
  if (newIdx >= 0) selectedIdx = newIdx + 1;
  applySelection();
}

async function moveSectionSelected(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const curId = visibleIds[selectedIdx - 1];
  if (!curId.startsWith('__section__:')) return;
  const section = curId.slice('__section__:'.length);
  if (section === '__completed__') return;

  const idx = sectionsOrder.indexOf(section);
  if (idx < 0) return;

  let beforeSection = null;
  if (direction === 'up') {
    // Move before the previous section; skip empty-string (unsectioned)
    let target = idx - 1;
    while (target >= 0 && sectionsOrder[target] === '') target--;
    if (target < 0) return;
    beforeSection = sectionsOrder[target];
  } else {
    // Move after the next section = move before the one two ahead
    let target = idx + 1;
    if (target >= sectionsOrder.length) return;
    // Place before the section that's two positions ahead, or null (end)
    beforeSection = (target + 1 < sectionsOrder.length) ? sectionsOrder[target + 1] : null;
  }

  const res = await fetch('/api/sections/reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section, before_section: beforeSection})
  });
  if (!res.ok) return;
  await loadTodos();
  const markerIdx = visibleIds.indexOf('__section__:' + section);
  if (markerIdx >= 0) selectedIdx = markerIdx + 1;
  applySelection();
}

async function startEdit(id) {
  editingId = id;
  render();
  // Lazily load CodeMirror modules
  if (!cmModules) {
    const [cmBundle, cmMd, cmView, cmState, cmCmd] = await Promise.all([
      import('codemirror'),
      import('@codemirror/lang-markdown'),
      import('@codemirror/view'),
      import('@codemirror/state'),
      import('@codemirror/commands')
    ]);
    cmModules = {
      basicSetup: cmBundle.basicSetup, markdown: cmMd.markdown,
      markdownKeymap: cmMd.markdownKeymap,
      EditorView: cmView.EditorView, keymap: cmView.keymap,
      EditorState: cmState.EditorState,
      moveLineUp: cmCmd.moveLineUp, moveLineDown: cmCmd.moveLineDown,
    };
  }
  const { basicSetup, markdown, EditorView, keymap, EditorState, markdownKeymap, moveLineUp, moveLineDown } = cmModules;
  const t = allTodos.find(t => t.id === id);
  const descEl = document.getElementById('edit-desc-' + id);
  if (descEl) {
    if (cmEditor) { cmEditor.destroy(); cmEditor = null; }
    const customKeymap = keymap.of([
      { key: 'Mod-Enter', run: () => { saveEdit(id); return true; } },
      { key: 'Escape', run: () => { cancelEdit(); return true; } },
      { key: 'Alt-ArrowUp', run: moveLineUp },
      { key: 'Alt-ArrowDown', run: moveLineDown },
      { key: 'Mod-x', run: (view) => {
        const sel = view.state.selection.main;
        if (!sel.empty) return false; // default cut if text selected
        const line = view.state.doc.lineAt(sel.head);
        const text = view.state.sliceDoc(line.from, Math.min(line.to + 1, view.state.doc.length));
        navigator.clipboard.writeText(text);
        view.dispatch({ changes: { from: line.from, to: Math.min(line.to + 1, view.state.doc.length) } });
        return true;
      }},
    ]);
    cmEditor = new EditorView({
      doc: t ? t.description : '',
      extensions: [
        customKeymap,
        basicSetup,
        keymap.of(markdownKeymap),
        markdown(),
        EditorView.lineWrapping,
        EditorView.theme({
          '&': { maxHeight: '300px' },
          '.cm-scroller': { overflow: 'auto' }
        })
      ],
      parent: descEl
    });
    // Move cursor to end and focus
    cmEditor.dispatch({ selection: { anchor: cmEditor.state.doc.length } });
    cmEditor.focus();
  }
  setTimeout(() => {
    const editEls = document.querySelectorAll(`#edit-title-${id}, #edit-desc-${id}, #edit-priority-${id}, #edit-section-${id}, #edit-section-custom-${id}`);
    // Section dropdown: show/hide custom input
    const secSelect = document.getElementById('edit-section-' + id);
    const secCustom = document.getElementById('edit-section-custom-' + id);
    if (secSelect && secCustom) {
      secSelect.addEventListener('change', () => {
        if (secSelect.value === '__custom__') {
          secCustom.style.display = '';
          secCustom.focus();
        } else {
          secCustom.style.display = 'none';
          secCustom.value = '';
        }
      });
    }
    // Keydown: Cmd+Enter to save, Escape to cancel (for non-CM fields)
    editEls.forEach(el => {
      el.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveEdit(id); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelEdit(); }
      });
    });
    // Cancel edit when clicking outside the todo item card
    const todoCard = descEl ? descEl.closest('.todo-item') : null;
    function onDocMousedown(e) {
      if (editingId !== id) { document.removeEventListener('mousedown', onDocMousedown, true); return; }
      if (todoCard && todoCard.contains(e.target)) return;
      e.preventDefault();
      cancelEdit();
    }
    document.addEventListener('mousedown', onDocMousedown, true);
  }, 0);
}

let _justCancelledEdit = false;
function cancelEdit() {
  const restoreId = editingId;
  if (cmEditor) { cmEditor.destroy(); cmEditor = null; }
  editingId = null;
  _justCancelledEdit = true;
  render();
  if (restoreId) selectTodo(restoreId);
}

async function saveEdit(id) {
  const editedTitle = document.getElementById('edit-title-' + id).value.trim();
  // Reconstruct full title preserving metadata tags from original
  const origTodo = allTodos.find(x => x.id === id);
  const origRaw = origTodo ? origTodo.title || '' : '';
  const metaMatch = origRaw.match(/`(?:updated|read)[^`]*`/);
  const meta = metaMatch ? ' ' + metaMatch[0] : '';
  const title = '**' + editedTitle + '**' + meta;
  const desc = cmEditor ? cmEditor.state.doc.toString().trim() : '';
  const priority = document.getElementById('edit-priority-' + id).value;
  const secSelect = document.getElementById('edit-section-' + id);
  const section = secSelect.value === '__custom__'
    ? document.getElementById('edit-section-custom-' + id).value.trim()
    : secSelect.value;
  if (!title) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, description: desc, priority, section})
  });
  if (cmEditor) { cmEditor.destroy(); cmEditor = null; }
  editingId = null;
  loadTodos();
}

// Enter key to add from title field
document.getElementById('new-title').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) { e.preventDefault(); addTodo(); }
});

let _preAddSelectedIdx = -1;
let _preAddSelectedId = null;

function showAddForm() {
  addFormVisible = true;
  // Remember selection before opening form
  _preAddSelectedIdx = selectedIdx;
  _preAddSelectedId = (selectedIdx >= 1 && selectedIdx <= visibleIds.length) ? visibleIds[selectedIdx - 1] : null;
  const form = document.getElementById('add-form');
  form.classList.add('visible');
  // If a todo is selected, pre-fill section, set insertion point, and move form inline
  if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
    const selId = visibleIds[selectedIdx - 1];
    const selTodo = allTodos.find(t => t.id === selId);
    if (selTodo && selTodo.status !== 'completed') {
      document.getElementById('new-section').value = selTodo.section || '';
      insertBeforeId = selId;
      // Move form to appear right before the selected item
      const targetEl = document.querySelector(`.todo-item[data-todo-id="${selId}"]`);
      if (targetEl) targetEl.parentNode.insertBefore(form, targetEl);
    } else {
      insertBeforeId = null;
    }
  } else {
    insertBeforeId = null;
  }
  selectedIdx = SEL_ADD;
  applySelection();
  document.getElementById('new-title').focus();
}

function hideAddForm() {
  addFormVisible = false;
  insertBeforeId = null;
  const form = document.getElementById('add-form');
  form.classList.remove('visible');
  // Move form back to its default position (after the search bar)
  const searchBar = document.querySelector('.search-bar');
  if (searchBar) searchBar.after(form);
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  document.getElementById('new-section-custom').value = '';
  document.getElementById('new-section-custom').style.display = 'none';
  // Restore previous selection
  if (_preAddSelectedId) {
    const idx = visibleIds.indexOf(_preAddSelectedId);
    selectedIdx = idx >= 0 ? idx + 1 : (visibleIds.length > 0 ? 1 : -1);
  } else {
    selectedIdx = visibleIds.length > 0 ? 1 : -1;
  }
  _preAddSelectedId = null;
  _preAddSelectedIdx = -1;
  applySelection();
}

// selectedIdx: -1=nothing, 0=add-form, 1..N=todo items (1-indexed into visibleIds)
// ---------------------------------------------------------------------------
// Drag and drop
// ---------------------------------------------------------------------------
let dragId = null;
let dragSectionName = null; // non-null when dragging a section header

function clearAllDragIndicators() {
  document.querySelectorAll('.drag-over-top,.drag-over-bottom').forEach(el => {
    el.classList.remove('drag-over-top', 'drag-over-bottom');
  });
  document.querySelectorAll('.drag-over-section,.section-drag-over-top,.section-drag-over-bottom').forEach(el => {
    el.classList.remove('drag-over-section', 'section-drag-over-top', 'section-drag-over-bottom');
  });
}

document.addEventListener('dragstart', e => {
  // Section header drag
  const sectionRow = e.target.closest('.section-header-row[draggable]');
  if (sectionRow && !e.target.closest('.todo-item')) {
    dragSectionName = sectionRow.dataset.section;
    dragId = null;
    sectionRow.classList.add('section-dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', 'section:' + dragSectionName);
    return;
  }
  // Todo item drag
  const item = e.target.closest('.todo-item[draggable]');
  if (!item) return;
  dragId = item.dataset.todoId;
  dragSectionName = null;
  item.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', dragId);
});

document.addEventListener('dragend', e => {
  dragId = null;
  dragSectionName = null;
  document.querySelectorAll('.dragging').forEach(el => el.classList.remove('dragging'));
  document.querySelectorAll('.section-dragging').forEach(el => el.classList.remove('section-dragging'));
  clearAllDragIndicators();
});

document.addEventListener('dragover', e => {
  // --- Section header being dragged ---
  if (dragSectionName !== null) {
    const targetHeader = e.target.closest('.section-header-row[data-section]');
    if (targetHeader && targetHeader.dataset.section !== dragSectionName) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      clearAllDragIndicators();
      const rect = targetHeader.getBoundingClientRect();
      const midY = rect.top + rect.height / 2;
      if (e.clientY < midY) {
        targetHeader.classList.add('section-drag-over-top');
      } else {
        targetHeader.classList.add('section-drag-over-bottom');
      }
    }
    return;
  }

  // --- Todo item being dragged ---
  if (!dragId) return;
  const item = e.target.closest('.todo-item[data-todo-id]');
  const sectionHeader = e.target.closest('.section-header-row');

  if (item && item.dataset.todoId !== dragId) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearAllDragIndicators();
    const rect = item.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
      item.classList.add('drag-over-top');
    } else {
      item.classList.add('drag-over-bottom');
    }
  } else if (sectionHeader) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearAllDragIndicators();
    sectionHeader.classList.add('drag-over-section');
  }
});

document.addEventListener('dragleave', e => {
  const item = e.target.closest('.todo-item');
  if (item) item.classList.remove('drag-over-top', 'drag-over-bottom');
  const sectionHeader = e.target.closest('.section-header-row');
  if (sectionHeader) sectionHeader.classList.remove('drag-over-section', 'section-drag-over-top', 'section-drag-over-bottom');
});

document.addEventListener('drop', async e => {
  // --- Section header drop ---
  if (dragSectionName !== null) {
    e.preventDefault();
    clearAllDragIndicators();
    const targetHeader = e.target.closest('.section-header-row[data-section]');
    if (!targetHeader || targetHeader.dataset.section === dragSectionName) {
      dragSectionName = null;
      return;
    }
    const targetSection = targetHeader.dataset.section;
    const rect = targetHeader.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;

    // Determine where to place the dragged section
    let beforeSection;
    if (e.clientY < midY) {
      // Drop above target
      beforeSection = targetSection;
    } else {
      // Drop below target — find the section after targetSection
      const targetIdx = sectionsOrder.indexOf(targetSection);
      beforeSection = (targetIdx + 1 < sectionsOrder.length) ? sectionsOrder[targetIdx + 1] : null;
    }
    // Don't move if it would end up in the same spot
    const curIdx = sectionsOrder.indexOf(dragSectionName);
    const beforeIdx = beforeSection !== null ? sectionsOrder.indexOf(beforeSection) : sectionsOrder.length;
    if (curIdx === beforeIdx || curIdx + 1 === beforeIdx) {
      dragSectionName = null;
      return;
    }

    const payload = {section: dragSectionName};
    if (beforeSection !== null) payload.before_section = beforeSection;
    await fetch('/api/sections/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    dragSectionName = null;
    await loadTodos();
    return;
  }

  // --- Todo item drop ---
  if (!dragId) return;
  e.preventDefault();
  const item = e.target.closest('.todo-item[data-todo-id]');
  const sectionHeader = e.target.closest('.section-header-row');

  let payload = {id: dragId};

  if (item && item.dataset.todoId !== dragId) {
    const targetId = item.dataset.todoId;
    const rect = item.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
      payload.before_id = targetId;
    } else {
      const nextItem = item.nextElementSibling?.closest?.('.todo-item[data-todo-id]')
        || item.nextElementSibling;
      if (nextItem && nextItem.classList.contains('todo-item') && nextItem.dataset.todoId) {
        payload.before_id = nextItem.dataset.todoId;
      } else {
        const targetTodo = allTodos.find(t => t.id === targetId);
        payload.section = targetTodo ? (targetTodo.section || '') : '';
      }
    }
  } else if (sectionHeader) {
    const h3 = sectionHeader.querySelector('h3');
    payload.section = h3 ? h3.textContent : '';
  } else {
    return;
  }

  clearAllDragIndicators();

  await fetch(API + '/drop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  await loadTodos();
  const ni = visibleIds.indexOf(dragId);
  if (ni >= 0) selectedIdx = ni + 1;
  applySelection();
  dragId = null;
});

/* ── Swipe-right to complete/uncomplete (mobile touch) ── */
(function() {
  if (!('ontouchstart' in window)) return;

  var THRESHOLD_RATIO = 0.30;
  var VELOCITY_THRESHOLD = 0.4; // px/ms

  var startX, startY, startTime, locked, item, content, itemW, isCompleted, todoId;

  function reset() {
    if (item) {
      item.classList.remove('swiping', 'swipe-active', 'swipe-threshold', 'snap-back', 'snap-complete');
      if (content) content.style.transform = '';
    }
    startX = startY = startTime = locked = item = content = itemW = isCompleted = todoId = null;
  }

  function findItem(el) {
    while (el && el !== document) {
      if (el.classList && el.classList.contains('todo-item')) return el;
      el = el.parentElement;
    }
    return null;
  }

  function shouldIgnore(target) {
    var tag = (target.tagName || '').toLowerCase();
    return tag === 'button' || tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'a';
  }

  document.addEventListener('touchstart', function(e) {
    if (e.touches.length !== 1) return;
    var t = e.touches[0];
    var target = e.target;
    if (shouldIgnore(target)) return;
    var el = findItem(target);
    if (!el) return;
    // Bail if item is in edit mode
    if (el.querySelector('.edit-form, .todo-edit')) return;

    item = el;
    content = el.querySelector('.swipe-content');
    if (!content) { item = null; return; }
    todoId = el.getAttribute('data-todo-id');
    itemW = el.offsetWidth;
    isCompleted = el.classList.contains('completed');
    startX = t.clientX;
    startY = t.clientY;
    startTime = Date.now();
    locked = null; // null = undecided, 'h' = horizontal, 'v' = vertical
  }, { passive: true });

  document.addEventListener('touchmove', function(e) {
    if (!item) return;
    var t = e.touches[0];
    var dx = t.clientX - startX;
    var dy = t.clientY - startY;

    if (locked === null) {
      var adx = Math.abs(dx), ady = Math.abs(dy);
      if (adx < 10 && ady < 10) return; // deadzone
      if (ady > adx) { locked = 'v'; reset(); return; }
      locked = 'h';
      item.classList.add('swiping', 'swipe-active');
    }

    if (locked !== 'h') return;
    e.preventDefault();

    // Only allow swiping right
    if (dx < 0) dx = 0;

    // Rubber-band past threshold
    var threshold = itemW * THRESHOLD_RATIO;
    var tx;
    if (dx <= threshold) {
      tx = dx;
    } else {
      tx = threshold + (dx - threshold) * 0.4;
    }
    content.style.transform = 'translateX(' + tx + 'px)';

    if (dx >= threshold) {
      item.classList.add('swipe-threshold');
    } else {
      item.classList.remove('swipe-threshold');
    }
  }, { passive: false });

  document.addEventListener('touchend', function(e) {
    if (!item || locked !== 'h') { reset(); return; }

    var t = e.changedTouches[0];
    var dx = t.clientX - startX;
    var elapsed = Date.now() - startTime;
    var velocity = dx / (elapsed || 1);
    var threshold = itemW * THRESHOLD_RATIO;
    var pastThreshold = dx >= threshold || (velocity > VELOCITY_THRESHOLD && dx > 30);

    if (pastThreshold) {
      // Animate off-screen, then toggle
      item.classList.remove('swiping');
      item.classList.add('snap-complete');
      content.style.transform = 'translateX(' + itemW + 'px)';

      var capturedId = todoId;
      var capturedCompleted = isCompleted;
      var capturedItem = item;

      var done = function() {
        capturedItem.removeEventListener('transitionend', done);
        toggleComplete(capturedId, !capturedCompleted);
      };
      // Listen on the content div for the transform transition
      content.addEventListener('transitionend', done, { once: true });
      // Safety fallback in case transitionend doesn't fire
      setTimeout(function() {
        done();
      }, 350);
    } else {
      // Snap back
      item.classList.remove('swiping', 'swipe-threshold');
      item.classList.add('snap-back');
      content.style.transform = '';
      var snapItem = item;
      setTimeout(function() {
        snapItem.classList.remove('snap-back', 'swipe-active');
      }, 300);
    }

    item = null; content = null; locked = null;
  }, { passive: true });

  document.addEventListener('touchcancel', function() {
    if (item) {
      item.classList.remove('swiping', 'swipe-threshold');
      item.classList.add('snap-back');
      if (content) content.style.transform = '';
      var snapItem = item;
      setTimeout(function() {
        snapItem.classList.remove('snap-back', 'swipe-active');
      }, 300);
    }
    item = null; content = null; locked = null;
  }, { passive: true });
})();

let _scrollAnim = null;
let _scrollTarget = null; // the element we're scrolling toward
function scrollIntoViewCentered(el) {
  const viewH = window.innerHeight;
  const pad = viewH * 0.3;
  const rect = el.getBoundingClientRect();
  if (rect.top >= pad && rect.bottom <= viewH - pad) {
    // Already in comfortable zone — cancel any animation and stop
    if (_scrollAnim) { cancelAnimationFrame(_scrollAnim); _scrollAnim = null; }
    _scrollTarget = null;
    return;
  }

  // If already animating toward this element, let it continue
  if (_scrollAnim && _scrollTarget === el) return;

  // Cancel previous animation
  if (_scrollAnim) cancelAnimationFrame(_scrollAnim);
  _scrollTarget = el;

  const duration = 180;
  const t0 = performance.now();

  function step(now) {
    const elapsed = now - t0;
    const progress = Math.min(elapsed / duration, 1);
    // Ease out cubic — fast start, gentle stop
    const eased = 1 - Math.pow(1 - progress, 3);

    // Recalculate target position every frame (tracks element through DOM changes)
    const r = _scrollTarget.getBoundingClientRect();
    const elCenter = r.top + r.height / 2;
    const targetY = window.innerHeight * 0.4;
    const remaining = elCenter - targetY;

    // Lerp: move a fraction of the remaining distance based on eased progress
    window.scrollBy(0, remaining * Math.min(eased * 0.5 + 0.15, 1));

    if (progress < 1 && Math.abs(remaining) > 1) {
      _scrollAnim = requestAnimationFrame(step);
    } else {
      _scrollAnim = null;
      _scrollTarget = null;
    }
  }
  _scrollAnim = requestAnimationFrame(step);
}

function selectedIsSection() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return false;
  return visibleIds[selectedIdx - 1].startsWith('__section__:');
}

let _pendingMarkRead = null; // todoId that was opened but not yet marked read

function _flushPendingMarkRead() {
  if (!_pendingMarkRead) return;
  const todoId = _pendingMarkRead;
  _pendingMarkRead = null;
  if (_seenUpdates.has(todoId)) return;
  const t = allTodos.find(x => x.id === todoId);
  if (t && t.title && _parseTitle(t.title).hasUpdatedTag) {
    _seenUpdates.add(todoId);
    _updateSpinnersInPlace();
    fetch(API + '/' + todoId + '/mark-read', { method: 'POST' }).catch(() => {});
  }
}

function applySelection() {
  _flushPendingMarkRead();
  // Collapse previous preview-expanded item
  if (previewExpandedId) {
    const prevEl = document.querySelector(`.todo-item[data-todo-id="${previewExpandedId}"]`);
    if (prevEl) prevEl.classList.remove('preview-expanded');
    previewExpandedId = null;
  }

  // Clear all highlights
  document.querySelectorAll('.todo-item.kb-selected').forEach(el => el.classList.remove('kb-selected'));
  document.querySelectorAll('.section-header-row.kb-selected').forEach(el => el.classList.remove('kb-selected'));
  const form = document.getElementById('add-form');
  form.classList.remove('kb-selected');

  if (selectedIdx === SEL_ADD && addFormVisible) {
    form.classList.add('kb-selected');
    scrollIntoViewCentered(form);
  } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
    const curId = visibleIds[selectedIdx - 1];
    if (curId.startsWith('__section__:')) {
      const sec = curId.slice('__section__:'.length);
      const el = document.querySelector(`.section-header-row[data-section="${sec}"]`);
      if (el) {
        el.classList.add('kb-selected');
        scrollIntoViewCentered(el);
      }
    } else {
      const el = document.querySelector(`.todo-item[data-todo-id="${curId}"]`);
      if (el) {
        el.classList.add('kb-selected');
        scrollIntoViewCentered(el);
        // Preview mode: auto-expand if currently collapsed
        if (previewMode) {
          const isExpanded = expandedItems.has(curId);
          if (!isExpanded) {
            el.classList.add('preview-expanded');
            previewExpandedId = curId;
          }
        }
      }
    }
  }
}

document.addEventListener('keydown', e => {
  // Settings dialog: Escape closes it, block all other keys while open
  const settingsOpen = document.getElementById('settings-overlay').classList.contains('visible');
  if (settingsOpen) {
    if (e.key === 'Escape') { e.preventDefault(); hideSettings(); }
    return;
  }
  // Shortcuts dialog: Escape closes it, block all other keys while open
  const shortcutsOpen = document.getElementById('shortcuts-overlay').classList.contains('visible');
  if (shortcutsOpen) {
    if (e.key === 'Escape') { e.preventDefault(); hideShortcuts(); }
    return;
  }

  // Section picker is fully handled by its own input's keydown — skip main handler
  if (sectionPickerOpen) return;

  // Ctrl+number: toggle priority filter (works from anywhere)
  // Option+number: show priority filter; Shift+Option+number: hide priority filter
  const optCodeMap = {'Digit1': 'high', 'Digit2': 'medium', 'Digit3': 'low', 'Digit0': 'none'};
  if (e.altKey && !e.metaKey && !e.ctrlKey && optCodeMap[e.code]) {
    e.preventDefault();
    const p = optCodeMap[e.code];
    if (e.shiftKey) {
      // Hide mode (greyed)
      showPriorities.delete(p);
      hidePriorities.has(p) ? hidePriorities.delete(p) : hidePriorities.add(p);
    } else {
      // Show mode (colored)
      hidePriorities.delete(p);
      showPriorities.has(p) ? showPriorities.delete(p) : showPriorities.add(p);
    }
    selectedIdx = -1;
    render();
    return;
  }

  if (e.altKey && e.key === 'Enter' && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
    e.preventDefault();
    toggleCollapseAll();
    return;
  }

  const tag = (e.target.tagName || '').toLowerCase();

  // When inside the add-form inputs, handle Escape to close form, Cmd+Enter to add
  if (addFormVisible && (tag === 'input' || tag === 'textarea' || tag === 'select')) {
    const inAddForm = e.target.closest('#add-form');
    if (inAddForm) {
      if (e.key === 'Escape') { e.preventDefault(); hideAddForm(); return; }
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); addTodo(); return; }
      return; // Let normal typing work
    }
  }

  // Search input: handle Escape, then let action keys fall through
  const inSearchInput = e.target.id === 'search-input';
  if (inSearchInput) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.target.value = '';
      searchQuery = '';
      e.target.classList.remove('has-query');
      e.target.blur();
      render();
      return;
    }
    // Only ArrowDown/ArrowUp blur and leave search to select items
    // Once an item is selected (selectedIdx >= 1), Enter and other action keys work on it
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      e.target.blur();
      // Fall through to main handler for navigation
    } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      // Item is selected — let Enter, Space, and other item actions through
      const itemActionKeys = new Set(['Enter', ' ']);
      if (itemActionKeys.has(e.key)) {
        e.preventDefault();
        e.target.blur();
        // Fall through to main handler
      } else {
        return; // Stay in search for typing
      }
    } else {
      return; // No item selected, stay in search
    }
  }

  // `/` focuses the search input from anywhere (before the input guard)
  if (e.key === '/' && tag !== 'input' && tag !== 'textarea' && tag !== 'select' && !editingId) {
    e.preventDefault();
    const si = document.getElementById('search-input');
    si.focus();
    si.select();
    return;
  }

  // Ignore when typing in other inputs or editing (but not search — handled above)
  if (!inSearchInput && (tag === 'input' || tag === 'textarea' || tag === 'select')) return;
  if (editingId) return;

  // Filter sessions: Ctrl+S
  if (e.ctrlKey && e.key === 's' && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    toggleFilterSessions();
    return;
  }

  if ((e.metaKey || e.ctrlKey) && e.key === 'u' && !e.shiftKey) {
    e.preventDefault();
    toggleFilterUnread();
    return;
  }

  // Undo: Cmd+Z / Ctrl+Z
  if ((e.metaKey || e.ctrlKey) && e.key === 'z' && !e.shiftKey) {
    e.preventDefault();
    performUndo();
    return;
  }

  const maxIdx = visibleIds.length; // 0=add-form, 1..N=todos
  const minIdx = addFormVisible ? SEL_ADD : 1;

  if (e.key === 'ArrowDown' && e.metaKey && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    navigateSection('down');
  } else if (e.key === 'ArrowUp' && e.metaKey && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    navigateSection('up');
  } else if ((e.key === 'ArrowDown' || e.key === 'j' || e.key === 'J') && e.shiftKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    moveToAdjacentSection('down');
  } else if ((e.key === 'ArrowUp' || e.key === 'k' || e.key === 'K') && e.shiftKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    moveToAdjacentSection('up');
  } else if (e.key === 'ArrowRight' && e.altKey && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    showSectionPicker();
  } else if ((e.key === 'ArrowDown' || e.key === 'j') && e.altKey) {
    e.preventDefault();
    if (selectedIsSection()) moveSectionSelected('down');
    else moveSelected('down');
  } else if ((e.key === 'ArrowUp' || e.key === 'k') && e.altKey) {
    e.preventDefault();
    if (selectedIsSection()) moveSectionSelected('up');
    else moveSelected('up');
  } else if (e.key === 'ArrowDown' || e.key === 'j') {
    e.preventDefault();
    if (visibleIds.length === 0 && !addFormVisible) return;
    if (selectedIdx < 0) {
      selectedIdx = minIdx;
    } else {
      selectedIdx = Math.min(selectedIdx + 1, maxIdx);
    }
    applySelection();
  } else if (e.key === 'ArrowUp' || e.key === 'k') {
    e.preventDefault();
    if (visibleIds.length === 0 && !addFormVisible) return;
    selectedIdx = Math.max(selectedIdx - 1, minIdx);
    applySelection();
  } else if (e.key === ' ') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      const id = visibleIds[selectedIdx - 1];
      const todo = allTodos.find(t => t.id === id);
      if (todo) toggleComplete(id, todo.status !== 'completed');
    }
  } else if (e.key === 'Enter') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      const curId = visibleIds[selectedIdx - 1];
      if (curId.startsWith('__section__:')) {
        const sec = curId.slice('__section__:'.length);
        collapsedSections.delete(sec);
        render();
      } else {
        openChat(curId);
      }
    }
  } else if (e.key === 'e') {
    e.preventDefault();
    if (selectedIdx === SEL_ADD && addFormVisible) {
      document.getElementById('new-title').focus();
    } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && selectedIsSection()) {
      const sec = visibleIds[selectedIdx - 1].slice('__section__:'.length);
      startSectionRename(sec);
    } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      startEdit(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'c') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      copyTodoId(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 's') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      openChat(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === '.') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      startChatBackground(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'x') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      stopChat(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'r' && !e.metaKey && !e.ctrlKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      eaUpdateItem(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === '0' || e.key === '1' || e.key === '2' || e.key === '3') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      const pMap = {'1': 'high', '2': 'medium', '3': 'low', '0': 'none'};
      changePriority(visibleIds[selectedIdx - 1], pMap[e.key]);
    }
  } else if (e.key === 'p') {
    const sec = getSectionOfSelected();
    if (sec !== null && sec !== '__completed__') {
      e.preventDefault();
      sortByPriority(sec);
    }
  } else if (e.key === 't') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      bringToTop(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'Backspace' && e.metaKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      deleteTodo(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'ArrowLeft' && !e.metaKey && !e.altKey && !e.shiftKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      const curId = visibleIds[selectedIdx - 1];
      if (!curId.startsWith('__section__:')) {
        const isExpanded = expandedItems.has(curId);
        if (isExpanded) {
          // Collapse item description
          e.preventDefault();
          toggleItemDesc(curId);
        } else {
          // Collapse the section, select the section marker
          const sec = getSectionOfSelected();
          if (sec !== null && sec !== '' && !collapsedSections.has(sec)) {
            e.preventDefault();
            collapsedSections.add(sec);
            render();
            const markerIdx = visibleIds.indexOf('__section__:' + sec);
            if (markerIdx >= 0) {
              selectedIdx = markerIdx + 1;
              applySelection();
            }
          }
        }
      }
    }
  } else if (e.key === 'ArrowRight' && !e.metaKey && !e.altKey && !e.shiftKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      const curId = visibleIds[selectedIdx - 1];
      if (curId.startsWith('__section__:')) {
        // Expand collapsed section
        e.preventDefault();
        const sec = curId.slice('__section__:'.length);
        collapsedSections.delete(sec);
        render();
      } else {
        // Expand item description
        const isExpanded = expandedItems.has(curId);
        if (!isExpanded) {
          e.preventDefault();
          toggleItemDesc(curId);
        }
      }
    }
  } else if (e.key === '-') {
    e.preventDefault();
    collapseStep();
  } else if (e.key === '+' || e.key === '=') {
    e.preventDefault();
    expandStep();
  } else if (e.key === 'v') {
    e.preventDefault();
    togglePreviewMode();
  } else if (e.key === '?') {
    e.preventDefault();
    showShortcuts();
  } else if (e.key === 'n') {
    e.preventDefault();
    showAddForm();
  } else if (e.key === 'Escape') {
    if (_justCancelledEdit) { _justCancelledEdit = false; return; }
    e.preventDefault();
    if (document.getElementById('terminal-overlay').classList.contains('visible')) {
      minimizeTerminal(); return;
    }
    if (addFormVisible) { hideAddForm(); }
    else if (searchQuery.trim().length > 0 || showPriorities.size > 0 || hidePriorities.size > 0) {
      searchQuery = '';
      showPriorities.clear();
      hidePriorities.clear();
      const searchEl = document.getElementById('search-input');
      searchEl.value = '';
      searchEl.classList.remove('has-query');
      render();
    }
    else { selectedIdx = -1; applySelection(); }
  }
});

// --- Search input ---
document.getElementById('search-input').addEventListener('input', e => {
  searchQuery = e.target.value;
  e.target.classList.toggle('has-query', searchQuery.trim().length > 0);
  selectedIdx = -1;
  render();
});

// --- Section picker (Opt+Right) ---
let sectionPickerOpen = false;
let sectionPickerIdx = 0;
let spAllItems = [];      // full unfiltered list [{name, label, isCurrent}]
let spFilteredItems = [];  // after fuzzy filter
let spLastQuery = '';      // persists across open/close
let sectionPickerTodoId = null;

function spFuzzyMatch(query, text) {
  // Simple fuzzy: every char of query appears in order in text (case-insensitive)
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length;
}

function spFuzzyScore(query, text) {
  // Lower = better. Prioritize: exact prefix > substring > fuzzy
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (t.startsWith(q)) return 0;
  if (t.includes(q)) return 1;
  return 2;
}

function showSectionPicker() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const curSection = todo.section || '';

  const allSections = [];
  const seen = new Set();
  allTodos.forEach(t => {
    const s = t.section || '';
    if (s && !seen.has(s)) { allSections.push(s); seen.add(s); }
  });
  spAllItems = [{name: '', label: '(No section)', isCurrent: curSection === ''}];
  allSections.forEach(s => {
    spAllItems.push({name: s, label: s, isCurrent: s === curSection});
  });

  sectionPickerTodoId = id;
  sectionPickerOpen = true;

  // Restore last query and filter accordingly
  if (spLastQuery) {
    spFilteredItems = spAllItems
      .filter(x => !x.isCurrent && spFuzzyMatch(spLastQuery, x.label))
      .sort((a, b) => spFuzzyScore(spLastQuery, a.label) - spFuzzyScore(spLastQuery, b.label));
  } else {
    spFilteredItems = spAllItems.filter(x => !x.isCurrent);
  }
  sectionPickerIdx = 0;

  renderSectionPicker();

  // Position near the selected todo item
  const el = document.querySelector(`.todo-item[data-todo-id="${id}"]`);
  const picker = document.getElementById('section-picker');
  if (el) {
    const rect = el.getBoundingClientRect();
    let x = rect.right - 240;
    let y = rect.top + rect.height + 4;
    if (x < 8) x = 8;
    if (y + 220 > window.innerHeight) y = rect.top - picker.offsetHeight - 4;
    picker.style.left = x + 'px';
    picker.style.top = y + 'px';
  }

  // Focus input after render, select all text so user can type to replace
  setTimeout(() => {
    const inp = document.getElementById('sp-input');
    if (inp) {
      inp.focus();
      if (spLastQuery) inp.select();
    }
  }, 0);
}

function renderSectionPicker() {
  const picker = document.getElementById('section-picker');
  const existingInput = document.getElementById('sp-input');
  const inputVal = existingInput ? existingInput.value : spLastQuery;

  let itemsHtml = '';
  if (spFilteredItems.length === 0 && inputVal.trim()) {
    itemsHtml = `<div class="section-picker-item sp-selected" onmousedown="spCommitNew()">Create &ldquo;${esc(inputVal.trim())}&rdquo;</div>`;
  } else {
    itemsHtml = spFilteredItems.map((item, i) => {
      const cls = ['section-picker-item'];
      if (i === sectionPickerIdx) cls.push('sp-selected');
      return `<div class="${cls.join(' ')}" data-sp-idx="${i}" onmousedown="spCommitIdx(${i})">${esc(item.label)}</div>`;
    }).join('');
  }

  picker.innerHTML =
    `<input type="text" class="section-picker-input" id="sp-input" placeholder="Search or create section..." autocomplete="off" value="${esc(inputVal)}">`
    + itemsHtml;
  picker.classList.add('visible');

  // Restore cursor position and set up events
  const input = document.getElementById('sp-input');
  input.setSelectionRange(inputVal.length, inputVal.length);
  input.addEventListener('input', spOnInput);
  input.addEventListener('keydown', spOnKeydown);
}

function spOnInput(e) {
  const query = e.target.value.trim();
  spLastQuery = e.target.value;
  if (!query) {
    spFilteredItems = spAllItems.filter(x => !x.isCurrent);
  } else {
    spFilteredItems = spAllItems
      .filter(x => !x.isCurrent && spFuzzyMatch(query, x.label))
      .sort((a, b) => spFuzzyScore(query, a.label) - spFuzzyScore(query, b.label));
  }
  sectionPickerIdx = 0;
  spUpdateItems();
}

function spUpdateItems() {
  // Re-render just the items, not the input (preserves focus/cursor)
  const picker = document.getElementById('section-picker');
  const input = document.getElementById('sp-input');
  const inputVal = input?.value || '';

  // Remove old items (everything after the input)
  while (picker.lastChild && picker.lastChild !== input) {
    picker.removeChild(picker.lastChild);
  }

  if (spFilteredItems.length === 0 && inputVal.trim()) {
    const div = document.createElement('div');
    div.className = 'section-picker-item sp-selected';
    div.innerHTML = `Create &ldquo;${esc(inputVal.trim())}&rdquo;`;
    div.onmousedown = () => spCommitNew();
    picker.appendChild(div);
  } else {
    spFilteredItems.forEach((item, i) => {
      const div = document.createElement('div');
      div.className = 'section-picker-item' + (i === sectionPickerIdx ? ' sp-selected' : '');
      div.textContent = item.label;
      div.onmousedown = () => spCommitIdx(i);
      picker.appendChild(div);
    });
  }
}

function spOnKeydown(e) {
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (spFilteredItems.length > 0) {
      sectionPickerIdx = Math.min(sectionPickerIdx + 1, spFilteredItems.length - 1);
      spUpdateItems();
    }
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (spFilteredItems.length > 0) {
      sectionPickerIdx = Math.max(sectionPickerIdx - 1, 0);
      spUpdateItems();
    }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const inputVal = e.target.value.trim();
    if (spFilteredItems.length > 0) {
      spCommitIdx(sectionPickerIdx);
    } else if (inputVal) {
      spCommitNew();
    } else {
      hideSectionPicker();
    }
  } else if (e.key === 'Escape') {
    e.preventDefault();
    hideSectionPicker();
  } else if (e.key === 'ArrowLeft') {
    if (!e.target.value) {
      e.preventDefault();
      hideSectionPicker();
    }
  }
  e.stopPropagation();
}

function hideSectionPicker() {
  sectionPickerOpen = false;
  sectionPickerTodoId = null;
  document.getElementById('section-picker').classList.remove('visible');
}

async function spMoveTo(sectionName) {
  const id = sectionPickerTodoId;
  const prevIdx = selectedIdx;
  hideSectionPicker();
  if (!id) return;

  await fetch(API + '/drop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, section: sectionName})
  });
  await fetch(API + '/move-to-top', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id})
  });

  await loadTodos();
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

function spCommitIdx(idx) {
  const item = spFilteredItems[idx];
  if (!item) { hideSectionPicker(); return; }
  spMoveTo(item.name);
}

function spCommitNew() {
  const input = document.getElementById('sp-input');
  const name = input?.value.trim();
  if (name) spMoveTo(name);
  else hideSectionPicker();
}

// --- Section rename ---
let renamingSection = null;

function startSectionRename(sectionName) {
  renamingSection = sectionName;
  const row = document.querySelector(`.section-header-row[data-section="${CSS.escape(sectionName)}"]`);
  if (!row) return;
  const h3 = row.querySelector('h3');
  if (!h3) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'section-rename-input';
  input.value = sectionName;
  h3.replaceWith(input);
  input.focus();
  input.select();

  function commit() {
    const newName = input.value.trim();
    if (newName && newName !== sectionName) {
      saveSectionRename(sectionName, newName);
    } else {
      renamingSection = null;
      render();
    }
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.preventDefault(); renamingSection = null; render(); }
  });
  input.addEventListener('blur', () => {
    setTimeout(() => { if (renamingSection === sectionName) commit(); }, 100);
  });
}

async function saveSectionRename(oldName, newName) {
  renamingSection = null;
  await fetch('/api/sections/rename', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_name: oldName, new_name: newName})
  });
  // Update collapsed sections set if the renamed section was collapsed
  if (collapsedSections.has(oldName)) {
    collapsedSections.delete(oldName);
    collapsedSections.add(newName);
  }
  loadTodos();
}

loadTodos();
startPolling();
pollJobs();

// Fade section headers and items behind them as they get covered by the next sticky header
window.addEventListener('scroll', () => {
  const offset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--section-offset')) || 0;
  const headers = [...document.querySelectorAll('.section-header-row')];
  for (let i = 0; i < headers.length; i++) {
    const h = headers[i];
    const rect = h.getBoundingClientRect();
    const isStuck = rect.top <= offset + 1;
    if (!isStuck) { h.style.opacity = ''; continue; }
    const next = headers[i + 1];
    if (!next) { h.style.opacity = ''; continue; }
    const nextTop = next.getBoundingClientRect().top;
    const dist = nextTop - offset;
    const hh = h.offsetHeight;
    const fadeZone = hh * 1.5;
    let fade;
    if (dist <= 0) fade = '0';
    else if (dist < fadeZone) fade = (dist / fadeZone).toFixed(2);
    else fade = '';
    h.style.opacity = fade;
  }
}, { passive: true });
</script>



<!-- Terminal overlay -->
<div id="terminal-overlay" onclick="if(event.target===this)minimizeTerminal()" style="display:none;position:fixed;inset:0;z-index:4000;background:rgba(0,0,0,0.5);flex-direction:column;justify-content:flex-end">
  <div id="terminal-panel" style="background:#1a1b1e;border-radius:14px 14px 0 0;height:55vh;min-height:150px;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 -8px 32px rgba(0,0,0,0.4)">
    <div id="terminal-resize-handle" style="height:12px;cursor:ns-resize;flex-shrink:0;display:flex;justify-content:center;align-items:center" onmousedown="_startTermResize(event)"><span style="width:40px;height:4px;border-radius:2px;background:rgba(255,255,255,0.25)"></span></div>
    <div id="terminal-titlebar" style="display:flex;align-items:center;padding:6px 14px 10px;gap:10px;border-bottom:1px solid rgba(255,255,255,0.1);flex-shrink:0;cursor:ns-resize" onmousedown="_startTermResize(event)">
      <span id="terminal-title" style="color:#e2e8f0;font-size:0.85rem;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
      <button onclick="termSendCommand('/ea sync')" style="background:rgba(255,255,255,0.1);border:none;color:#e2e8f0;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:0.75rem">Log Status</button>
      <button onclick="copyTmuxAttach()" style="background:rgba(255,255,255,0.1);border:none;color:#e2e8f0;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:0.75rem">Attach</button>
      <button onclick="killTerminal(_activeTermTodoId)" style="background:rgba(239,68,68,0.2);border:1px solid rgba(239,68,68,0.4);color:#fca5a5;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:0.75rem">Kill</button>
    </div>
    <div id="terminal-container" style="flex:1;overflow:hidden;padding:4px"></div>
  </div>
</div>

<!-- Chat overlay -->
<div id="chat-overlay" onclick="if(event.target===this)minimizeChat()" style="display:none;position:fixed;inset:0;z-index:4000;background:rgba(0,0,0,0.5);flex-direction:column;justify-content:flex-end;align-items:center">
  <div id="chat-panel" style="background:#1a1b1e;border-radius:14px 14px 0 0;height:77vh;min-height:150px;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 -8px 32px rgba(0,0,0,0.4);width:100%;max-width:994px">
    <div style="height:12px;cursor:ns-resize;flex-shrink:0;display:flex;justify-content:center;align-items:center;touch-action:none" onmousedown="_startChatResize(event)" ontouchstart="_startChatResize(event)"><span style="width:40px;height:4px;border-radius:2px;background:rgba(255,255,255,0.25)"></span></div>
    <div style="display:flex;align-items:center;padding:6px 14px 10px;gap:10px;border-bottom:1px solid rgba(255,255,255,0.1);flex-shrink:0;cursor:ns-resize;touch-action:none" onmousedown="_startChatResize(event)" ontouchstart="_startChatResize(event)">
      <span id="chat-title" style="color:#e2e8f0;font-size:0.85rem;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
      <span id="chat-provider-badge" style="font-size:0.65rem;color:rgba(255,255,255,0.45);background:rgba(255,255,255,0.08);padding:2px 7px;border-radius:4px;white-space:nowrap;flex-shrink:0;opacity:0;transition:opacity 0.15s" onmouseenter="this.style.opacity='1'" onmouseleave="this.style.opacity='0'"></span>
      <button onclick="if(_activeChatTodoId){document.getElementById('chat-input').value='/ea checkon '+_activeChatTodoId;sendChatMessage(_activeChatTodoId)}" style="background:rgba(255,255,255,0.1);border:none;color:#e2e8f0;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:0.75rem" onmousedown="event.stopPropagation()">Check On</button>
      <button onclick="if(_activeChatTodoId){document.getElementById('chat-input').value='/compact';sendChatMessage(_activeChatTodoId)}" style="background:rgba(255,255,255,0.1);border:none;color:#e2e8f0;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:0.75rem" onmousedown="event.stopPropagation()">Compact</button>
      <button onclick="restartChat()" style="background:rgba(255,255,255,0.1);border:none;color:#e2e8f0;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:0.75rem" onmousedown="event.stopPropagation()">Restart</button>
    </div>
    <div id="chat-log" class="chat-log"></div>
    <div class="chat-input-bar">
      <input id="chat-input" class="chat-input" placeholder="Send a message..." autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();chatSendOrStop(_activeChatTodoId)}else if(event.key==='Escape'){minimizeChat()}">
      <button id="chat-send-btn" class="chat-send-btn" onclick="chatSendOrStop(_activeChatTodoId)">Send</button>
    </div>
  </div>
</div>

</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Simple Todo App")
    parser.add_argument("todo_file", nargs="?", default="todos.md", help="Path to the todo markdown file")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5111, help="Port to listen on")
    args = parser.parse_args()

    TODO_FILE = args.todo_file

    # Initialize database if DATABASE_URL is set
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        _db.init(database_url)
        _USE_DB = True
        print(f"Database connected: {database_url.split('@')[-1] if '@' in database_url else database_url}")
    else:
        # File mode — create file if it doesn't exist
        if not os.path.exists(TODO_FILE):
            _write_todo_file(TODO_FILE, [])
            print(f"Created new todo file: {TODO_FILE}")

    # Ensure config directory exists (migrates flat file if needed, seeds context)
    if not _USE_DB:
        _ensure_config_dir()

    # MCP servers are initialized lazily per-user on first access.
    # Register cleanup handler for all managers.
    import atexit
    def _shutdown_all_mcp():
        with _mcp_managers_lock:
            for mgr in _mcp_managers.values():
                mgr.stop()
            _mcp_managers.clear()
    atexit.register(_shutdown_all_mcp)

    # Recover any existing tmux sessions from a previous server run
    _tmux_recover_sessions()

    print(f"Serving todo UI for: {os.path.abspath(TODO_FILE)}")
    print(f"Open http://{args.host}:{args.port} in your browser")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=True)
