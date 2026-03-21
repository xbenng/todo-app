#!/usr/bin/env python3
"""
Dossie — AI-powered executive assistant and todo manager.

Requires PostgreSQL. Set DATABASE_URL in .env or environment.

Usage:
    python app.py [--host HOST] [--port PORT]
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

from flask import Flask
from flask_sock import Sock

import state
import db as _db
from routes import register_blueprints
from routes.terminal import register_websocket

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

# Register WebSocket routes (terminal)
register_websocket(sock)

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
        print("ERROR: DATABASE_URL environment variable is required.")
        print("Set it in .env or export it: export DATABASE_URL=postgresql://localhost/todos")
        sys.exit(1)

    _db.init(database_url)
    print(f"Database connected: {database_url.split('@')[-1] if '@' in database_url else database_url}")

    # Register cleanup handler for MCP managers
    def _shutdown_all_mcp():
        with state._mcp_managers_lock:
            for mgr in state._mcp_managers.values():
                mgr.stop()
            state._mcp_managers.clear()
    atexit.register(_shutdown_all_mcp)

    # Recover any existing tmux sessions from a previous server run
    from services.terminal import _tmux_recover_sessions
    _tmux_recover_sessions()

    print(f"Open http://{args.host}:{args.port} in your browser")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=True)
