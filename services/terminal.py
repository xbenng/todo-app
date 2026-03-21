"""Terminal/PTY session management — tmux helpers and WebSocket I/O bridge."""

import fcntl
import json
import os
import select as _select
import shutil
import struct
import subprocess
import termios
import time

import state


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
        if session_id in state._pty_sessions:
            continue
        state._pty_sessions[session_id] = {
            "id": session_id,
            "todo_id": None,
            "title": f"Recovered: {session_id}",
            "tmux_target": sname,
            "alive": True,
            "needs_auto_send": False,
            "resume_id": None,
            "created_at": time.time(),
        }
    print(f"[terminal] Recovered {len(state._pty_sessions)} tmux sessions", flush=True)


def _terminal_io_loop(ws, master_fd, proc, tmux_target=None):
    """Bridge PTY master fd <-> WebSocket in a single-threaded poll loop."""
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
