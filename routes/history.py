"""History and git version control routes."""
import os, subprocess
from datetime import datetime
from flask import Blueprint, request, jsonify
from routes.auth import get_current_user
from state import _USE_DB, TODO_FILE
from services.file_io import _completed_file_path
import db as _db

bp = Blueprint('history', __name__)


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


@bp.route("/api/history")
def get_history():
    """Return recent version history for the current user."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"entries": []})
    entries = _db.get_history(user["id"])
    return jsonify({"entries": entries})


@bp.route("/api/todos/<todo_id>/history")
def get_todo_history(todo_id):
    """Return version history for a specific todo item."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"entries": []})
    entries = _db.get_todo_history(user["id"], todo_id)
    return jsonify({"entries": entries})


@bp.route("/api/history/<int:history_id>/restore", methods=["POST"])
def restore_history(history_id):
    """Restore a todo from a history snapshot."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"error": "Not available"}), 400
    result = _db.restore_todo(user["id"], history_id)
    if not result:
        return jsonify({"error": "History entry not found"}), 404
    return jsonify(result)


@bp.route("/api/git/log")
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


@bp.route("/api/git/commit", methods=["POST"])
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


@bp.route("/api/git/rollback", methods=["POST"])
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
