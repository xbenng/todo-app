"""EA update routes."""
import os, json
from flask import Blueprint, request, jsonify
from routes.auth import get_current_user
import state
from services.chat_runner import _start_claude_chat_job
import db as _db
from schemas import EaUpdateItemRequest, validate_request

bp = Blueprint('ea', __name__)


@bp.route("/api/ea-update", methods=["POST"])
def ea_update():
    """Run /ea update via ChatAgent."""
    user = get_current_user()
    data = request.json or {}
    force = data.get("force", False)

    # Check for existing running job
    existing = next((j for j in state._jobs.values()
                     if j["job_key"] == "ea-update" and j["status"] == "running"), None)
    if existing and not force:
        return jsonify({"status": "already_running", "job_id": existing["id"]})

    todo_dir = os.getcwd()
    job_id = _start_claude_chat_job("EA Update", "ea-update", "/ea update", todo_dir,
                                     user_id=user["id"] if user else None)
    return jsonify({"status": "started", "job_id": job_id})


@bp.route("/api/ea-update-item", methods=["POST"])
@validate_request(EaUpdateItemRequest)
def ea_update_item(data: EaUpdateItemRequest):
    """Run /ea checkon <item_id> via ChatAgent."""
    user = get_current_user()

    job_key = f"ea-{data.id}"
    existing = next((j for j in state._jobs.values()
                     if j["job_key"] == job_key and j["status"] == "running"), None)
    if existing and not data.force:
        return jsonify({"status": "already_running", "job_id": existing["id"]})

    todo_dir = os.getcwd()
    message = data.message or f"/ea checkon {data.id}"
    label = "Consolidate" if "consolidate" in message else f"Check: {data.id}"
    job_id = _start_claude_chat_job(label, job_key, message, todo_dir,
                                     user_id=user["id"] if user else None)
    return jsonify({"status": "started", "job_id": job_id})


@bp.route("/api/resume-conv", methods=["POST"])
def resume_conv():
    """Deprecated — tmux terminal sessions removed."""
    return jsonify({"error": "Terminal sessions have been deprecated"}), 410
