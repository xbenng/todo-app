"""History and DB version control routes."""
from flask import Blueprint, jsonify
from routes.auth import get_current_user
import db as _db

bp = Blueprint('history', __name__)


@bp.route("/api/history")
def get_history():
    """Return recent version history for the current user."""
    user = get_current_user()
    if not user:
        return jsonify({"entries": []})
    entries = _db.get_history(user["id"])
    return jsonify({"entries": entries})


@bp.route("/api/todos/<todo_id>/history")
def get_todo_history(todo_id):
    """Return version history for a specific todo item."""
    user = get_current_user()
    if not user:
        return jsonify({"entries": []})
    entries = _db.get_todo_history(user["id"], todo_id)
    return jsonify({"entries": entries})


@bp.route("/api/history/<int:history_id>/restore", methods=["POST"])
def restore_history(history_id):
    """Restore a todo from a history snapshot."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not available"}), 400
    result = _db.restore_todo(user["id"], history_id)
    if not result:
        return jsonify({"error": "History entry not found"}), 404
    return jsonify(result)
