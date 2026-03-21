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
from flask import Flask, request, jsonify, Response, render_template
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
    from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
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

    def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> str:
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
                await asyncio.wait_for(
                    self._connect_server(server_name, config),
                    timeout=30
                )
                tool_count = sum(1 for t in self._group.tools
                                 if t.startswith(f"mcp__{server_name}__"))
                self._server_status[server_name] = {
                    "connected": True, "tool_count": tool_count
                }
                print(f"MCP connected: {server_name} ({tool_count} tools)", flush=True)
            except Exception as exc:
                self._server_status[server_name] = {
                    "connected": False, "error": str(exc)
                }
                print(f"MCP failed: {server_name}: {exc}", flush=True)
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
        elif server_type == "streamable-http":
            params = StreamableHttpParameters(
                url=config["url"],
                headers=config.get("headers"),
            )
        elif server_type in ("sse", "http"):
            params = SseServerParameters(
                url=config["url"],
                headers=config.get("headers"),
            )
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
You are a helpful assistant managing a todo list and connected services.

## Built-in Tools
- **read_todos** — read all todos (with optional status filter: all/open/completed)
- **update_todo** — update a todo's title, description, status, priority, or section
- **create_todo** — create a new todo item
- **search_todos** — search todos by text query
- **read_chat_history** — read chat history for a specific todo
- **spawn_agents** — launch multiple subagents in parallel for independent tasks

## MCP Tools
You also have access to MCP (Model Context Protocol) tools for connected services like Slack, Email, Calendar, Smartsheet, and others. These are prefixed with `mcp__{server}__` (e.g., `mcp__slack__channels_list`). Use them when the user asks about their communications, calendar, or other connected data.

## Guidelines
- Use tools proactively — don't ask the user to check things they asked you about.
- Be concise. Use bullets, not paragraphs.
- When referencing information from external sources, include links where possible.
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
                            if _USE_DB and user_id and user_id != "local":
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
    if _USE_DB and user_id and user_id != "local":
        config = _db.get_config(user_id)
        prefs = _db.get_mcp_preferences(user_id)
        enabled = {name for name, p in prefs.items() if p["enabled"]}
    else:
        config = _load_config()
        enabled = None
    tokens = config.get("tokens", {})
    app_dir = os.path.dirname(os.path.abspath(__file__))
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
                resolved = shutil.which(command)
                if resolved:
                    command = resolved
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
        if _USE_DB and user_id and user_id != "local":
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
        return render_template("login.html")
    return render_template("app.html")


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


@app.route("/api/todos/<todo_id>", methods=["GET"])
def get_single_todo(todo_id):
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    if _USE_DB:
        todo = _db.get_todo(user["id"], todo_id)
    else:
        todos = _parse_todo_file(TODO_FILE) + _parse_todo_file(_completed_file_path(TODO_FILE))
        todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404
    return jsonify(todo)


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
        if data.get("mark_unread"):
            _db.mark_chat_unread(todo_id, user["id"])
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


@app.route("/api/todos/search", methods=["GET"])
def search_todos_route():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    query = (request.args.get("q") or "").lower()
    if not query:
        return jsonify([])
    if _USE_DB:
        todos = _db.get_todos(user["id"])
    else:
        todos = _parse_todo_file(TODO_FILE) + _parse_todo_file(_completed_file_path(TODO_FILE))
    results = [t for t in todos
               if query in t.get("title", "").lower()
               or query in t.get("description", "").lower()]
    return jsonify(results)


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
        # Get current section order from DB
        sections = _db.get_sections(user["id"])
        sections_order = [s["name"] for s in sections]
        # Add section if not in DB yet
        if section not in sections_order:
            sections_order.append(section)
        sections_order.remove(section)
        if before_section is not None:
            before_section = before_section.strip()
            if before_section in sections_order:
                idx = sections_order.index(before_section)
                sections_order.insert(idx, section)
            else:
                sections_order.append(section)
        else:
            sections_order.append(section)
        _db.reorder_sections(user["id"], sections_order)
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))
        sections_order = []
        seen = set()
        for t in active:
            s = t.get("section", "")
            if s not in seen:
                sections_order.append(s)
                seen.add(s)
        if section not in sections_order:
            return jsonify({"error": "Section not found"}), 404
        sections_order.remove(section)
        if before_section is not None:
            before_section = before_section.strip()
            if before_section in sections_order:
                idx = sections_order.index(before_section)
                sections_order.insert(idx, section)
            else:
                sections_order.append(section)
        else:
            sections_order.append(section)
        section_groups = {}
        for t in active:
            section_groups.setdefault(t.get("section", ""), []).append(t)
        rebuilt = []
        for s in sections_order:
            rebuilt.extend(section_groups.get(s, []))
        _snapshot_and_write(TODO_FILE, rebuilt + completed)
    return jsonify({"ok": True})


@app.route("/api/sections", methods=["GET"])
def get_sections():
    """Return sections for the current user, ordered by position."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify([])
    return jsonify(_db.get_sections(user["id"]))


@app.route("/api/sections", methods=["PUT"])
def update_section():
    """Update a section's directives."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"error": "Not available"}), 400
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    _db.upsert_section(user["id"], name, directives=data.get("directives"))
    return jsonify({"ok": True})


@app.route("/api/execute-tool", methods=["POST"])
def execute_tool_endpoint():
    """Unified tool execution endpoint. Routes through _execute_tool with agent context."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    tool_name = data.get("tool")
    tool_input = data.get("input", {})
    as_agent = data.get("as_agent", False)
    if not tool_name:
        return jsonify({"error": "tool required"}), 400
    agent_ctx = {"job_id": "__api__", "provider": {}, "depth": 0} if as_agent else None
    # Ensure __api__ pseudo-job exists with user_id so _execute_tool can resolve it
    if as_agent:
        _jobs["__api__"] = {"user_id": user["id"]}
    try:
        result = _execute_tool(tool_name, tool_input, todo_id=None, agent_context=agent_ctx)
        return app.response_class(result, mimetype="application/json")
    finally:
        _jobs.pop("__api__", None)


@app.route("/api/todos/mtime", methods=["GET"])
def get_mtime():
    """Return the max modification time for change detection."""
    if _USE_DB:
        user = get_current_user()
        if user:
            mtime = _db.get_todos_mtime(user["id"])
            return jsonify({"mtime": mtime})
        return jsonify({"mtime": 0})
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

    # Append DB context files to the CLI's system prompt
    user_id = _jobs.get(job_id, {}).get("user_id")
    todo_id = _jobs.get(job_id, {}).get("todo_id")
    append_prompt = ""
    if _USE_DB and user_id and user_id != "local":
        ctx = _db.get_context_files(user_id)
        if ctx:
            append_prompt = "\n\n".join(f"# {name}\n{content.strip()}"
                                        for name, content in sorted(ctx.items())
                                        if content and content.strip())
    cmd = [claude_bin, "-p", prompt, "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose",
           "--effort", "low", "--include-partial-messages"]
    if append_prompt:
        cmd.extend(["--system-prompt", append_prompt])
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
            "description": "Read todo items. Returns summaries (id, title, status, priority, section) by default. Use detail=true for full descriptions. Use get_todo for a single item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "enum": ["all", "open", "completed"],
                        "description": "Filter by status. Default: open"
                    },
                    "detail": {
                        "type": "boolean",
                        "description": "Include full descriptions. Default: false (summaries only)"
                    }
                },
                "required": []
            }
        },
        {
            "name": "get_todo",
            "description": "Get a single todo item by ID with full details including description.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The todo item ID"}
                },
                "required": ["todo_id"]
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
            status_filter = input_data.get("status_filter", "open")
            if status_filter == "open":
                todos = [t for t in todos if t["status"] != "completed"]
            elif status_filter == "completed":
                todos = [t for t in todos if t["status"] == "completed"]
            # Strip descriptions unless detail=true
            if not input_data.get("detail"):
                todos = [{k: v for k, v in t.items() if k != "description"} for t in todos]
            return json.dumps(todos, ensure_ascii=False)

        elif name == "get_todo":
            tid = input_data.get("todo_id", "")
            if _USE_DB and user_id:
                todo = _db.get_todo(user_id, tid)
            else:
                active = _parse_todo_file(TODO_FILE)
                completed = _parse_todo_file(_completed_file_path(TODO_FILE))
                todo = next((t for t in active + completed if t["id"] == tid), None)
            if not todo:
                return json.dumps({"error": f"Todo {tid} not found"})
            return json.dumps(todo, ensure_ascii=False)

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
                if agent_context:
                    _db.mark_chat_unread(tid, user_id)
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
                if agent_context:
                    _db.mark_chat_unread(new_todo["id"], user_id)
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
            # Return summaries — use get_todo for full detail
            results = [{k: v for k, v in t.items() if k != "description"} for t in results]
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

        subagent_timeout = config.get("subagent_timeout", 120)
        for future in as_completed(futures, timeout=subagent_timeout + 30):
            try:
                result = future.result(timeout=subagent_timeout)
                results.append(result)
            except Exception as exc:
                results.append({
                    "label": futures[future], "result": "",
                    "error": f"Timed out or failed: {str(exc)[:200]}",
                    "input_tokens": 0, "output_tokens": 0,
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

    # 3. Global context files (shared across all users)
    global_ctx_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context")
    if os.path.isdir(global_ctx_dir):
        for name in sorted(os.listdir(global_ctx_dir)):
            if name.endswith(".md"):
                try:
                    with open(os.path.join(global_ctx_dir, name), "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        parts.append(f"# {name}\n{content}")
                except OSError:
                    pass

    # 4. Current date
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
        """Build messages array from persisted history + new message.

        If auto_compact is enabled in user config, automatically
        summarizes older messages to stay within context limits.
        """
        if _USE_DB and self.todo_id:
            raw_messages = _db.get_messages(self.todo_id)
        elif self.todo_id:
            chats = _load_chats()
            chat = chats.get(self.todo_id, {"messages": []})
            raw_messages = chat.get("messages", [])
        else:
            raw_messages = []
        messages = []
        for m in raw_messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        # Auto-compact if enabled
        if _USE_DB and self.user_id:
            config = _db.get_config(self.user_id)
        else:
            config = _load_config()
        if config.get("auto_compact", False) and len(messages) > 8:
            threshold = config.get("compact_threshold", 100000)
            keep_recent = config.get("compact_keep_recent", 8)
            messages = self._maybe_compact(messages, threshold, keep_recent)
        return messages

    @staticmethod
    def _msg_size(m: dict) -> int:
        c = m.get("content", "")
        return len(json.dumps(c) if isinstance(c, (list, dict)) else c)

    def _maybe_compact(self, messages: list[dict], threshold: int = 80000,
                       keep_recent: int = 4) -> list[dict]:
        """If conversation history is too large, summarize older messages."""
        total_chars = sum(self._msg_size(m) for m in messages)
        if total_chars < threshold:
            return messages
        if len(messages) <= keep_recent + 1:
            return messages  # Not enough to compact
        old_messages = messages[:-keep_recent]
        recent_messages = messages[-keep_recent:]
        self.emit("⟳ Compacting conversation history...")
        summary = self._summarize_messages(old_messages)
        if not summary:
            return messages  # Summarization failed, use original
        # Replace old messages with a single summary message pair
        compacted = [
            {"role": "user", "content": "[Earlier conversation summary]"},
            {"role": "assistant", "content": summary},
        ] + recent_messages
        old_chars = sum(len(m["content"]) for m in old_messages)
        new_chars = len(summary)
        print(f"[compact] Compacted {len(old_messages)} messages ({old_chars} chars) → summary ({new_chars} chars)", flush=True)
        return compacted

    def _summarize_messages(self, messages: list[dict]) -> str | None:
        """Use the LLM to summarize a list of messages into a concise summary."""
        def _fmt(m):
            c = m.get("content", "")
            if isinstance(c, (list, dict)):
                c = json.dumps(c, ensure_ascii=False)[:2000]
            return f"**{m['role'].upper()}:** {c}"
        conversation_text = "\n\n".join(_fmt(m) for m in messages)
        summary_prompt = (
            "Summarize the following conversation concisely. Preserve:\n"
            "- Key decisions made\n"
            "- Action items and their status\n"
            "- Important facts and context\n"
            "- Tool calls and their results (briefly)\n"
            "Drop: greetings, filler, repeated information.\n"
            "Output a concise summary in bullet points.\n\n"
            f"CONVERSATION:\n{conversation_text}"
        )
        try:
            if self.ptype == "anthropic" and anthropic:
                client = anthropic.Anthropic(api_key=self.provider.get("api_key"))
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": summary_prompt}],
                )
                return resp.content[0].text if resp.content else None
            elif self.ptype == "openai_compat" and openai_mod:
                client = openai_mod.OpenAI(
                    base_url=self.provider.get("base_url"),
                    api_key=self.provider.get("api_key", "none"),
                )
                resp = client.chat.completions.create(
                    model=self.model,
                    max_tokens=2048,
                    messages=[
                        {"role": "system", "content": "You are a concise summarizer."},
                        {"role": "user", "content": summary_prompt},
                    ],
                )
                return resp.choices[0].message.content if resp.choices else None
        except Exception as exc:
            print(f"[compact] Summarization failed: {exc}", flush=True)
        return None

    def _get_tools_anthropic(self) -> list[dict]:
        tools = _get_tool_definitions(self.depth, self.user_id)
        tools = tools + _get_mcp_tools(self.user_id)
        return tools

    def _get_tools_openai(self) -> list[dict]:
        return _openai_tool_defs(self.depth, self.user_id)

    def _persist_response(self) -> None:
        """Persist assistant text response to chats/DB. Tool calls are not persisted."""
        if not self.todo_id or not self.assistant_text_lines:
            return
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
                assistant_content = []
                tool_blocks = []
                for block in response.content:
                    if block.type == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        assistant_content.append({
                            "type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input
                        })
                        tool_blocks.append(block)
                # Execute tool calls in parallel
                agent_ctx = {"job_id": self.job_id, "provider": self.provider, "depth": self.depth}
                if len(tool_blocks) > 1:
                    with ThreadPoolExecutor(max_workers=len(tool_blocks)) as ex:
                        futures = {
                            ex.submit(_execute_tool, b.name, b.input, self.todo_id, agent_ctx): b
                            for b in tool_blocks
                        }
                        result_map = {}
                        for f in as_completed(futures):
                            b = futures[f]
                            try:
                                result_map[b.id] = f.result(timeout=60)
                            except Exception as exc:
                                result_map[b.id] = json.dumps({"error": str(exc)[:200]})
                    tool_results = [{"type": "tool_result", "tool_use_id": b.id, "content": result_map[b.id]} for b in tool_blocks]
                else:
                    tool_results = [{
                        "type": "tool_result",
                        "tool_use_id": tool_blocks[0].id,
                        "content": _execute_tool(tool_blocks[0].name, tool_blocks[0].input, self.todo_id, agent_ctx)
                    }] if tool_blocks else []
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
                # Parse args for all tool calls
                parsed_calls = []
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    parsed_calls.append((tc, args))
                # Execute in parallel if multiple
                agent_ctx = {"job_id": self.job_id, "provider": self.provider, "depth": self.depth}
                if len(parsed_calls) > 1:
                    with ThreadPoolExecutor(max_workers=len(parsed_calls)) as ex:
                        futures = {
                            ex.submit(_execute_tool, tc.function.name, args, self.todo_id, agent_ctx): tc
                            for tc, args in parsed_calls
                        }
                        result_map = {}
                        for f in as_completed(futures):
                            tc = futures[f]
                            try:
                                result_map[tc.id] = f.result(timeout=60)
                            except Exception as exc:
                                result_map[tc.id] = json.dumps({"error": str(exc)[:200]})
                    for tc, _ in parsed_calls:
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_map[tc.id]})
                else:
                    tc, args = parsed_calls[0]
                    result = _execute_tool(tc.function.name, args, self.todo_id, agent_ctx)
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
                    self.emit(f"error: Context length exceeded ({self.total_input_tokens} input tokens, {hist_count} lines). "
                              f"Try Restart to reduce context.")
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

    # Build system prompt and MCP config from DB
    user_id = _jobs.get(job_id, {}).get("user_id")
    system_prompt = _build_system_prompt(todo_id, user_id)
    cmd = [claude_bin, "-p", message, "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose"]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    # Build per-user MCP config from registry + credentials
    mcp_config = _build_cli_mcp_config(user_id)
    if mcp_config:
        cmd.extend(["--mcp-config", json.dumps(mcp_config)])
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
        resume_conv = data.get("resume_conv")
        if resume_conv is not None:
            _db.resume_conversation(todo_id, int(resume_conv))
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
        include_tool = request.args.get("include_tool", "false").lower() == "true"
        messages = _db.get_messages(todo_id, include_tool=include_tool) if user else []
        # Filter out structured JSON content (tool use blocks) from assistant messages for display
        if not include_tool:
            clean = []
            for m in messages:
                content = m.get("content", "")
                if isinstance(content, str) and content.startswith(("[", "{")):
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, list) and any(
                            isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                            for b in parsed
                        ):
                            # Extract only text blocks
                            text_parts = [b["text"] for b in parsed
                                         if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
                            if text_parts:
                                m = dict(m)
                                m["content"] = "\n".join(text_parts)
                            else:
                                continue  # skip pure tool-use messages
                    except (json.JSONDecodeError, ValueError):
                        pass
                clean.append(m)
            messages = clean
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
    """Restart chat — starts a new conversation, preserving old messages."""
    if _USE_DB:
        _db.restart_conversation(todo_id)
    else:
        chats = _load_chats()
        chats.pop(todo_id, None)
        _save_chats(chats)
    return jsonify({"ok": True})


@app.route("/api/chats/<todo_id>/conversations")
def get_conversations(todo_id):
    """List all conversations for a todo."""
    if not _USE_DB:
        return jsonify({"conversations": []})
    current_num, convs = _db.get_conversations(todo_id)
    return jsonify({"conversations": convs, "current": current_num})


@app.route("/api/chats/<todo_id>/conversations/<int:conv_num>")
def get_conversation(todo_id, conv_num):
    """Get messages from a specific past conversation."""
    if not _USE_DB:
        return jsonify({"messages": []})
    messages = _db.get_conversation_messages(todo_id, conv_num)
    return jsonify({"messages": messages})


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
            "bearer_token_key": reg_entry.get("bearer_token", ""),
            "bearer_connected": bool(
                reg_entry.get("bearer_token") and
                user_tokens.get(reg_entry.get("bearer_token"))
            ),
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
    # PKCE support
    code_verifier = ""
    if provider.get("pkce"):
        import secrets as _secrets
        code_verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
    # Build state (signed) — include code_verifier for PKCE
    state_payload = {"user_id": user["id"], "server": server,
                     "provider": provider_id, "account_id": account_id}
    if code_verifier:
        state_payload["code_verifier"] = code_verifier
    state_data = json.dumps(state_payload)
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
    if code_verifier:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if provider.get("scope"):
        params["scope"] = provider["scope"]
    if provider.get("user_scope"):
        params["user_scope"] = provider["user_scope"]
    if not provider.get("user_scope") and not provider.get("pkce"):
        # Google-style: request offline access for refresh tokens
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    # Provider-specific extra params (e.g., Atlassian audience)
    extra = provider.get("extra_auth_params", {})
    params.update(extra)
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
    token_payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    # Include PKCE code_verifier if present in state
    code_verifier = state.get("code_verifier")
    if code_verifier:
        token_payload["code_verifier"] = code_verifier
    resp = _requests.post(provider["token_uri"], data=token_payload)
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
    store_as_oauth = provider.get("store_as_oauth")
    if store_as:
        # Store directly as a user credential token (raw access token)
        config = _db.get_config(user_id)
        tokens = config.get("tokens", {})
        tokens[store_as] = access_token
        _db.save_config(user_id, tokens=tokens)
    elif store_as_oauth:
        # Store full oauth_token object as a credential (supports refresh)
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
        config = _db.get_config(user_id)
        tokens = config.get("tokens", {})
        tokens[store_as_oauth] = oauth_token
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
    return render_template("oauth_complete.html")


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


@app.route("/api/todos/<todo_id>/history")
def get_todo_history(todo_id):
    """Return version history for a specific todo item."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"entries": []})
    entries = _db.get_todo_history(user["id"], todo_id)
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
    message = data.get("message") or f"/ea checkon {item_id}"
    label = "Consolidate" if "consolidate" in message else f"Check: {item_id}"
    job_id = _start_claude_chat_job(label, job_key, message, todo_dir,
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
