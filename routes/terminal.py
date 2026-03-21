"""Terminal/PTY routes and WebSocket handler."""
import os, json, subprocess, threading, time, uuid, pty, fcntl, termios, struct
import select as _select
from flask import Blueprint, request, jsonify
from routes.auth import get_current_user
import state
from services.terminal import _terminal_io_loop, _pty_set_winsize, _tmux_bin, _tmux_session_exists, _tmux_list_sessions
from services.shell_utils import _kill_process_tree, _resolve_claude_bin
import db as _db

bp = Blueprint('terminal', __name__)


@bp.route("/api/todos/<todo_id>/terminal", methods=["POST"])
def open_terminal(todo_id):
    """Create a tmux-backed terminal session for a todo. Returns existing if alive."""
    data = request.json or {}
    resume_id = data.get("resume_id")

    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    # Return existing alive session for this todo (unless resuming a specific conv)
    if not resume_id:
        for s in state._pty_sessions.values():
            if s["todo_id"] == todo_id and s["alive"]:
                return jsonify({"session_id": s["id"], "title": s["title"], "existing": True})

    todo = _db.get_todo(user["id"], todo_id)
    if not todo:
        return jsonify({"error": "Todo not found"}), 404

    claude_bin, env = _resolve_claude_bin()
    if not claude_bin:
        return jsonify({"error": "claude binary not found"}), 500

    session_id = str(uuid.uuid4())[:8]
    tmux_name = f"t-{session_id}"
    todo_dir = os.getcwd()

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

    state._pty_sessions[session_id] = {
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
            if state._pty_sessions.get(session_id, {}).get("alive"):
                subprocess.run(
                    [tmux, "send-keys", "-t", tmux_name, f"/ea workon {todo_id}", "Enter"],
                    capture_output=True,
                )
        threading.Thread(target=_auto_send, daemon=True).start()

    return jsonify({"session_id": session_id, "title": todo.get("title", todo_id), "existing": False})


@bp.route("/api/terminal/sessions")
def list_terminal_sessions():
    """List terminal sessions, syncing alive state with tmux."""
    live_sessions = set(_tmux_list_sessions())
    # Sync alive state with tmux reality
    for s in state._pty_sessions.values():
        s["alive"] = f"t-{s['id']}" in live_sessions
    # Purge dead sessions older than 5 min
    cutoff = time.time() - 300
    stale = [sid for sid, s in state._pty_sessions.items()
             if not s["alive"] and s["created_at"] < cutoff]
    for sid in stale:
        del state._pty_sessions[sid]
    return jsonify([{
        "session_id": s["id"], "todo_id": s["todo_id"],
        "title": s["title"], "alive": s["alive"],
        "created_at": s["created_at"],
    } for s in state._pty_sessions.values()])


@bp.route("/api/terminal/<session_id>/kill", methods=["POST"])
def kill_terminal(session_id):
    """Kill a terminal session by destroying its tmux session."""
    session = state._pty_sessions.get(session_id)
    if not session:
        return jsonify({"error": "not found"}), 404
    session["alive"] = False
    tmux_name = f"t-{session_id}"
    subprocess.run([_tmux_bin(), "kill-session", "-t", tmux_name], capture_output=True)
    return jsonify({"ok": True})


def register_websocket(sock):
    @sock.route("/api/terminal/<session_id>/ws")
    def terminal_ws(ws, session_id):
        """WebSocket handler: attach to tmux window via PTY, bridge to browser."""
        session = state._pty_sessions.get(session_id)
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

        # Bridge PTY <-> WS (transient per connection — session persists in tmux)
        _terminal_io_loop(ws, master_fd, proc, tmux_target=tmux_name)
