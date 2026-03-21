"""Chat routes: send messages, get history, manage conversations."""
import os, json
from flask import Blueprint, request, jsonify
from routes.auth import get_current_user
import state
from services.chat_runner import _start_claude_chat_job
import db as _db

bp = Blueprint('chat', __name__)


@bp.route("/api/todos/<todo_id>/start", methods=["POST"])
def start_in_tmux(todo_id):
    """Launch a headless Claude Code session working on the given todo."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    todo = _db.get_todo(user["id"], todo_id)
    if not todo:
        return jsonify({"error": "Todo not found"}), 404

    todo_dir = os.getcwd()
    from services.chat_runner import _start_claude_job
    job_id = _start_claude_job(todo.get("title", todo_id), f"workon-{todo_id}",
                               f"/ea workon {todo_id}", todo_dir)
    return jsonify({"status": "started", "job_id": job_id})


@bp.route("/api/todos/<todo_id>/chat", methods=["POST"])
def chat_with_todo(todo_id):
    """Send a chat message for a todo item, optionally resuming a conversation."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    todo = _db.get_todo(user["id"], todo_id)
    if not todo:
        return jsonify({"error": "Todo not found"}), 404

    data = request.json or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    resume_conv = data.get("resume_conv")
    if resume_conv is not None:
        _db.resume_conversation(todo_id, int(resume_conv))
    _db.add_message(todo_id, user["id"], "user", message)
    meta = _db.get_chat_meta(todo_id)
    conversation_id = meta["conversation_id"] if meta else None

    todo_dir = os.getcwd()

    job_id = _start_claude_chat_job(
        label=f"chat: {todo.get('title', todo_id)[:40]}",
        job_key=f"chat-{todo_id}",
        message=message,
        cwd=todo_dir,
        conversation_id=conversation_id,
        todo_id=todo_id,
        user_id=user["id"],
    )
    return jsonify({"job_id": job_id, "conversation_id": conversation_id})


@bp.route("/api/chats/<todo_id>")
def get_chat(todo_id):
    """Return the persisted chat session for a todo item, plus any running job."""
    user = get_current_user()
    include_tool = request.args.get("include_tool", "false").lower() == "true"
    messages = _db.get_messages(todo_id, include_tool=include_tool) if user else []
    # Filter out structured JSON content (tool use blocks) from assistant messages for display
    if not include_tool:
        clean = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str) and content.startswith(("[", "{")):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, list) and any(
                        isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
                        for b in parsed
                    ):
                        # Extract only text blocks
                        text_parts = [b["text"] for b in parsed
                                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
                        if text_parts:
                            m = dict(m)
                            m["content"] = "\n".join(text_parts)
                        else:
                            continue  # skip pure tool-use messages
                except (json.JSONDecodeError, ValueError):
                    pass
            clean.append(m)
        messages = clean
    meta = _db.get_chat_meta(todo_id)
    chat = {
        "conversationId": meta["conversation_id"] if meta else None,
        "messages": messages,
    }
    # Check for the most recent running chat job for this todo
    job_key = f"chat-{todo_id}"
    latest_job = None
    for j in state._jobs.values():
        if j["job_key"] == job_key and j["status"] in ("pending", "running"):
            if not latest_job or j["created_at"] > latest_job["created_at"]:
                latest_job = j
    if latest_job:
        chat["running_job_id"] = latest_job["id"]
    return jsonify(chat)


@bp.route("/api/chats/<todo_id>", methods=["DELETE"])
def delete_chat(todo_id):
    """Restart chat — starts a new conversation, preserving old messages."""
    _db.restart_conversation(todo_id)
    return jsonify({"ok": True})


@bp.route("/api/chats/<todo_id>/conversations")
def get_conversations(todo_id):
    """List all conversations for a todo."""
    current_num, convs = _db.get_conversations(todo_id)
    return jsonify({"conversations": convs, "current": current_num})


@bp.route("/api/chats/<todo_id>/conversations/<int:conv_num>")
def get_conversation(todo_id, conv_num):
    """Get messages from a specific past conversation."""
    messages = _db.get_conversation_messages(todo_id, conv_num)
    return jsonify({"messages": messages})


@bp.route("/api/chats/unread")
def get_unread_chats():
    """Return list of todo_ids with unread chat responses."""
    user = get_current_user()
    if user:
        return jsonify(list(_db.get_unread_todo_ids(user["id"])))
    return jsonify([])


@bp.route("/api/chats/<todo_id>/read", methods=["POST"])
def mark_chat_read(todo_id):
    """Mark a chat as read."""
    _db.mark_chat_read(todo_id)
    return jsonify({"ok": True})
