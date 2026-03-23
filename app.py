#!/usr/bin/env python3
"""
Dossie — AI-powered executive assistant and todo manager.

Requires PostgreSQL. Set DATABASE_URL in .env or environment.

Usage:
    python app.py [--host HOST] [--port PORT]
"""

import sys
import os
import logging
import subprocess

# macOS 26 SIGSEGV workaround: Network.framework's pthread_atfork handler
# crashes when multi-threaded Python processes call fork(). We override
# _execute_child to use posix_spawn (no fork) whenever possible.
# Key insight: Python's _posix_spawn fails with close_fds=True because
# os.POSIX_SPAWN_CLOSEFROM is missing on this build. We pass close_fds=False
# to _posix_spawn (minor fd leak, but avoids the fatal crash).
import shutil as _shutil
_orig_execute_child = subprocess.Popen._execute_child
def _no_fork_execute_child(self, args, executable, preexec_fn, close_fds,
                           pass_fds, cwd, env,
                           startupinfo, creationflags, shell,
                           p2cread, p2cwrite,
                           c2pread, c2pwrite,
                           errread, errwrite,
                           restore_signals,
                           gid, gids, uid, umask,
                           start_new_session, process_group):
    if preexec_fn is None:
        _exec = executable
        if _exec is None and args:
            _exec = _shutil.which(args[0]) if isinstance(args, (list, tuple)) else _shutil.which(args)
        if _exec is not None:
            try:
                # Pass close_fds=False to avoid needing POSIX_SPAWN_CLOSEFROM
                self._posix_spawn(args, _exec, env, restore_signals,
                                  False, p2cread, p2cwrite, c2pread,
                                  c2pwrite, errread, errwrite)
                return
            except Exception:
                pass  # fall through to fork-based path
    return _orig_execute_child(
        self, args, executable, preexec_fn, close_fds,
        pass_fds, cwd, env, startupinfo, creationflags, shell,
        p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
        restore_signals, gid, gids, uid, umask,
        start_new_session, process_group)
subprocess.Popen._execute_child = _no_fork_execute_child

# Load .env file if present (before any other env lookups)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from flask import Flask, jsonify, request
from flask_sock import Sock

import state
import db as _db
from routes import register_blueprints

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dossie")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
sock = Sock(app)

# Store in shared state so services can access them
state.app = app
state.sock = sock

# Register all route blueprints
register_blueprints(app)

# Terminal/tmux WebSocket routes deprecated — removed to avoid macOS fork crashes

# CSRF protection — require Content-Type: application/json on state-changing requests.
# Browsers enforce that HTML forms cannot set this header, so cross-origin form
# submissions are blocked. The JS frontend already sends this header on all fetches.
@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "DELETE"):
        # Skip for OAuth callback (browser redirect, not JS fetch)
        if request.path == "/api/mcp/oauth/callback":
            return
        # Skip for WebSocket upgrade requests
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return
        content_type = request.content_type or ""
        if "application/json" not in content_type:
            return jsonify({"error": "Content-Type must be application/json"}), 415

# Global error handler — never expose tracebacks to clients
@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    log.exception("Unhandled exception")
    return jsonify({"error": "Internal server error"}), 500

@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(405)
def handle_method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import atexit

    parser = argparse.ArgumentParser(description="Dossie — AI-powered Todo App")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5111, help="Port to listen on")
    args = parser.parse_args()

    # Initialize database (required)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL environment variable is required.")
        log.error("Set it in .env or export it: export DATABASE_URL=postgresql://localhost/todos")
        sys.exit(1)

    _db.init(database_url)
    log.info("Database connected: %s", database_url.split('@')[-1] if '@' in database_url else database_url)

    # Register cleanup handler for MCP managers
    def _shutdown_all_mcp():
        with state._mcp_managers_lock:
            for mgr in state._mcp_managers.values():
                mgr.stop()
            state._mcp_managers.clear()
    atexit.register(_shutdown_all_mcp)

    state.port = args.port
    log.info("Open http://%s:%s in your browser", args.host, args.port)
    app.run(host=args.host, port=args.port, debug=True)
