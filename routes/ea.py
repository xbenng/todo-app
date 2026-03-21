"""EA update routes."""
import os, json, shutil, subprocess
from flask import Blueprint, request, jsonify
from routes.auth import get_current_user
import state
from services.chat_runner import _start_claude_chat_job
import db as _db
from schemas import EaUpdateItemRequest, ResumeConvRequest, validate_request

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
@validate_request(ResumeConvRequest)
def resume_conv(data: ResumeConvRequest):
    """Resume a Claude conversation in tmux."""
    window_name = f"conv-{data.conversation_id[:16]}"
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
        todo_dir = os.getcwd()
        subprocess.run(
            [
                tmux_bin, "send-keys",
                "-t", f"{tmux_session}:{window_name}",
                f"cd {todo_dir} && claude --dangerously-skip-permissions --resume {data.conversation_id}",
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
