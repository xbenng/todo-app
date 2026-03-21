"""Shared global state for the todo app.

All mutable state that needs to be accessed across modules lives here.
Import what you need: `from state import _jobs, _USE_DB, ...`
"""
import threading
from collections import deque

# Flask app and WebSocket — set by app.py at startup
app = None
sock = None

# Path to markdown todo file (set from CLI args)
TODO_FILE = "todos.md"

# Database mode flag — True when DATABASE_URL is configured
_USE_DB = False

# Undo stacks
_undo_stack = deque(maxlen=30)
_undo_stacks = {}  # user_id -> deque

# Jobs: job_id -> {id, label, job_key, status, output_lines, proc, created_at, user_id, ...}
_jobs = {}

# PTY sessions: session_id -> {id, todo_id, title, tmux_target, alive, ...}
_pty_sessions = {}

# MCP managers: user_id -> MCPManager
_mcp_managers = {}
_mcp_managers_lock = threading.Lock()

# Pending tool approvals: approval_id -> {event, approved, ...}
_pending_approvals = {}
_approvals_lock = threading.Lock()

# Per-user temp dirs for MCP config
_user_temp_dirs = {}
