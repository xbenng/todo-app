"""Shell utility functions — resolving binaries, env, and process management."""

import os
import shutil
import signal
import subprocess


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
