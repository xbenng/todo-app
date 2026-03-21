"""Todo CRUD, sections, and utility routes."""
import os, json, uuid
import re as _re
from datetime import datetime as _dt
from flask import Blueprint, request, jsonify, render_template
from routes.auth import get_current_user
from state import _USE_DB, TODO_FILE, _undo_stack, _undo_stacks, _jobs
from services.file_io import (
    _parse_todo_file, _write_todo_file, _snapshot_and_write,
    _completed_file_path,
    VALID_PRIORITIES, DEFAULT_PRIORITY, PRIORITY_ORDER,
)
from services.tools import _execute_tool
import db as _db

bp = Blueprint('todos', __name__)


@bp.route("/")
def index():
    user = get_current_user()
    if _USE_DB and not user:
        return render_template("login.html")
    return render_template("app.html")


@bp.route("/api/todos", methods=["GET"])
def get_todos():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    if _USE_DB:
        return jsonify(_db.get_todos(user["id"]))
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    return jsonify(active + completed)


@bp.route("/api/todos", methods=["POST"])
def add_todo():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    if _USE_DB:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "Title is required"}), 400
        todo = _db.create_todo(
            user["id"], title,
            description=(data.get("description") or "").strip(),
            priority=data.get("priority", DEFAULT_PRIORITY),
            section=(data.get("section") or "").strip(),
        )
        return jsonify(todo), 201

    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    new_todo = {
        "id": str(uuid.uuid4())[:8],
        "title": data.get("title", "").strip(),
        "description": data.get("description", "").strip(),
        "status": "open",
        "priority": data.get("priority", DEFAULT_PRIORITY),
        "section": data.get("section", "").strip(),
    }
    if not new_todo["title"]:
        return jsonify({"error": "Title is required"}), 400

    before_id = data.get("before_id")
    if before_id:
        idx = next((i for i, t in enumerate(active) if t["id"] == before_id), None)
        if idx is not None:
            active.insert(idx, new_todo)
        else:
            active.append(new_todo)
        todos = active + completed
    else:
        todos.append(new_todo)
    _snapshot_and_write(TODO_FILE, todos)
    return jsonify(new_todo), 201


@bp.route("/api/todos/<todo_id>", methods=["GET"])
def get_single_todo(todo_id):
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    if _USE_DB:
        todo = _db.get_todo(user["id"], todo_id)
    else:
        todos = _parse_todo_file(TODO_FILE) + _parse_todo_file(_completed_file_path(TODO_FILE))
        todo = next((t for t in todos if t["id"] == todo_id), None)
    if not todo:
        return jsonify({"error": "Not found"}), 404
    return jsonify(todo)


@bp.route("/api/todos/<todo_id>", methods=["PUT"])
def update_todo_route(todo_id):
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    if _USE_DB:
        fields = {}
        for k in ("title", "description", "status", "priority", "section"):
            if k in data:
                val = data[k]
                if k == "status" and val not in ("open", "completed"):
                    continue
                if k == "priority" and val not in VALID_PRIORITIES:
                    continue
                fields[k] = val.strip() if isinstance(val, str) else val
        result = _db.update_todo(user["id"], todo_id, **fields)
        if not result:
            return jsonify({"error": "Not found"}), 404
        if data.get("mark_unread"):
            _db.mark_chat_unread(todo_id, user["id"])
        return jsonify(result)

    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    for t in todos:
        if t["id"] == todo_id:
            if "title" in data:
                t["title"] = data["title"].strip()
            if "description" in data:
                t["description"] = data["description"].strip()
            if "status" in data and data["status"] in ("open", "completed"):
                t["status"] = data["status"]
            if "priority" in data and data["priority"] in VALID_PRIORITIES:
                t["priority"] = data["priority"]
            if "section" in data:
                t["section"] = data["section"].strip()
            _snapshot_and_write(TODO_FILE, todos)
            return jsonify(t)
    return jsonify({"error": "Not found"}), 404


@bp.route("/api/todos/search", methods=["GET"])
def search_todos_route():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    query = (request.args.get("q") or "").lower()
    if not query:
        return jsonify([])
    if _USE_DB:
        todos = _db.get_todos(user["id"])
    else:
        todos = _parse_todo_file(TODO_FILE) + _parse_todo_file(_completed_file_path(TODO_FILE))
    results = [t for t in todos
               if query in t.get("title", "").lower()
               or query in t.get("description", "").lower()]
    return jsonify(results)


@bp.route("/api/todos/<todo_id>/mark-read", methods=["POST"])
def mark_read(todo_id):
    """Replace `updated ...` tag with `read ...` and current timestamp."""
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    for t in todos:
        if t["id"] == todo_id:
            now = _dt.now().strftime("%Y-%m-%d %H:%M")
            t["title"] = _re.sub(r'`updated[^`]*`', f'`read {now}`', t["title"])
            _snapshot_and_write(TODO_FILE, todos)
            return jsonify(t)
    return jsonify({"error": "Not found"}), 404


@bp.route("/api/todos/<todo_id>", methods=["DELETE"])
def delete_todo_route(todo_id):
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    if _USE_DB:
        if not _db.delete_todo(user["id"], todo_id):
            return jsonify({"error": "Not found"}), 404
        return jsonify({"ok": True})
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    new_todos = [t for t in todos if t["id"] != todo_id]
    if len(new_todos) == len(todos):
        return jsonify({"error": "Not found"}), 404
    _snapshot_and_write(TODO_FILE, new_todos)
    return jsonify({"ok": True})


@bp.route("/api/todos/reorder", methods=["POST"])
def reorder_todo():
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    todo_id = data.get("id")
    direction = data.get("direction")  # "up" or "down"
    if not todo_id or direction not in ("up", "down"):
        return jsonify({"error": "id and direction (up/down) required"}), 400

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    # Find the item in active list
    idx = next((i for i, t in enumerate(active) if t["id"] == todo_id), None)
    if idx is None:
        return jsonify({"error": "Not found or not an active item"}), 404

    item = active[idx]
    item_section = item.get("section", "")

    # Build ordered list of sections (preserving first-appearance order)
    sections_order: list[str] = []
    seen: set[str] = set()
    for t in active:
        s = t.get("section", "")
        if s not in seen:
            sections_order.append(s)
            seen.add(s)

    # Get items in the same section
    section_items = [t for t in active if t.get("section", "") == item_section]
    pos_in_section = next(i for i, t in enumerate(section_items) if t["id"] == todo_id)

    if direction == "down":
        if pos_in_section < len(section_items) - 1:
            # Swap within section: find next same-section item in the flat list
            cur_flat = idx
            nxt_flat = cur_flat + 1
            while nxt_flat < len(active) and active[nxt_flat].get("section", "") != item_section:
                nxt_flat += 1
            if nxt_flat < len(active):
                active[cur_flat], active[nxt_flat] = active[nxt_flat], active[cur_flat]
            else:
                return jsonify({"ok": True, "moved": False})
        else:
            # At bottom of section — move to adjacent section below
            sec_idx = sections_order.index(item_section)
            if sec_idx + 1 >= len(sections_order):
                return jsonify({"ok": True, "moved": False})
            new_section = sections_order[sec_idx + 1]
            item["section"] = new_section
            # Move item to the top of the new section
            active.pop(idx)
            first_in_new = next((i for i, t in enumerate(active) if t.get("section", "") == new_section), len(active))
            active.insert(first_in_new, item)
    else:  # direction == "up"
        if pos_in_section > 0:
            # Swap within section: find previous same-section item in the flat list
            cur_flat = idx
            prev_flat = cur_flat - 1
            while prev_flat >= 0 and active[prev_flat].get("section", "") != item_section:
                prev_flat -= 1
            if prev_flat >= 0:
                active[cur_flat], active[prev_flat] = active[prev_flat], active[cur_flat]
            else:
                return jsonify({"ok": True, "moved": False})
        else:
            # At top of section — move to adjacent section above
            sec_idx = sections_order.index(item_section)
            if sec_idx - 1 < 0:
                return jsonify({"ok": True, "moved": False})
            new_section = sections_order[sec_idx - 1]
            item["section"] = new_section
            # Move item to the bottom of the new section
            active.pop(idx)
            # Find last item in the new section
            last_in_new = -1
            for i, t in enumerate(active):
                if t.get("section", "") == new_section:
                    last_in_new = i
            active.insert(last_in_new + 1, item)

    if _USE_DB:
        for i, t in enumerate(active):
            t["position"] = i
        _db.bulk_update_todos(user["id"], active)
    else:
        _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True, "moved": True})


@bp.route("/api/todos/move-to-top", methods=["POST"])
def move_to_top():
    """Move a todo to the top of its section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    todo_id = data.get("id")
    if not todo_id:
        return jsonify({"error": "id required"}), 400

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    idx = next((i for i, t in enumerate(active) if t["id"] == todo_id), None)
    if idx is None:
        return jsonify({"error": "Not found or not an active item"}), 404

    item = active[idx]
    section = item.get("section", "")

    # Find the first item in the same section
    first_idx = next(i for i, t in enumerate(active) if t.get("section", "") == section)
    if idx == first_idx:
        return jsonify({"ok": True, "moved": False})

    # Remove from current position, insert at the top of the section
    active.pop(idx)
    active.insert(first_idx, item)

    if _USE_DB:
        for i, t in enumerate(active):
            t["position"] = i
        _db.bulk_update_todos(user["id"], active)
    else:
        _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True, "moved": True})


@bp.route("/api/todos/sort-priority", methods=["POST"])
def sort_by_priority():
    """Sort todos by priority within a given section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    section = data.get("section", "")

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    # Separate items in the target section from others, preserving order
    section_items = []
    other_items = []
    for t in active:
        if t.get("section", "") == section:
            section_items.append(t)
        else:
            other_items.append(t)

    # Sort the section items by priority
    section_items.sort(key=lambda t: PRIORITY_ORDER.get(t.get("priority", DEFAULT_PRIORITY), 1))

    # Rebuild active list: insert sorted section items back in position
    rebuilt = []
    inserted = False
    for t in active:
        if t.get("section", "") == section:
            if not inserted:
                rebuilt.extend(section_items)
                inserted = True
        else:
            rebuilt.append(t)
    if not inserted:
        rebuilt.extend(section_items)

    if _USE_DB:
        for i, t in enumerate(rebuilt):
            t["position"] = i
        _db.bulk_update_todos(user["id"], rebuilt)
    else:
        _snapshot_and_write(TODO_FILE, rebuilt + completed)
    return jsonify({"ok": True})


@bp.route("/api/todos/drop", methods=["POST"])
def drop_todo():
    """Move a todo to a specific position: before another item, or to the end of a section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    todo_id = data.get("id")
    before_id = data.get("before_id")  # insert before this item (None = end of section)
    target_section = data.get("section")  # required if before_id is None

    if not todo_id:
        return jsonify({"error": "id required"}), 400

    if _USE_DB:
        all_todos = _db.get_todos(user["id"])
        active = [t for t in all_todos if t["status"] != "completed"]
        completed = [t for t in all_todos if t["status"] == "completed"]
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    idx = next((i for i, t in enumerate(active) if t["id"] == todo_id), None)
    if idx is None:
        return jsonify({"error": "Not found or not an active item"}), 404
    item = active.pop(idx)

    if before_id:
        target_idx = next((i for i, t in enumerate(active) if t["id"] == before_id), None)
        if target_idx is not None:
            item["section"] = active[target_idx].get("section", "")
            active.insert(target_idx, item)
        else:
            active.append(item)
    elif target_section is not None:
        item["section"] = target_section
        last_in_section = -1
        for i, t in enumerate(active):
            if t.get("section", "") == target_section:
                last_in_section = i
        active.insert(last_in_section + 1, item)
    else:
        active.append(item)

    if _USE_DB:
        for i, t in enumerate(active):
            t["position"] = i
        _db.bulk_update_todos(user["id"], active)
    else:
        _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True})


@bp.route("/api/sections/rename", methods=["POST"])
def rename_section():
    """Rename a section header across all todos."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "old_name and new_name required"}), 400
    if old_name == new_name:
        return jsonify({"ok": True})

    if _USE_DB:
        todos = _db.get_todos(user["id"])
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))
        todos = active + completed
    changed = False
    for t in todos:
        if t.get("section", "") == old_name:
            t["section"] = new_name
            changed = True
    if not changed:
        return jsonify({"error": "Section not found"}), 404
    if _USE_DB:
        _db.bulk_update_todos(user["id"], [t for t in todos if t.get("section") == new_name])
    else:
        _snapshot_and_write(TODO_FILE, todos)
    return jsonify({"ok": True})


@bp.route("/api/sections/reorder", methods=["POST"])
def reorder_section():
    """Move a section (and all its todos) before another section."""
    user = get_current_user()
    if _USE_DB and not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json
    section = (data.get("section") or "").strip()
    before_section = data.get("before_section")  # None = move to end

    if not section:
        return jsonify({"error": "section required"}), 400

    if _USE_DB:
        # Get current section order from DB
        sections = _db.get_sections(user["id"])
        sections_order = [s["name"] for s in sections]
        # Add section if not in DB yet
        if section not in sections_order:
            sections_order.append(section)
        sections_order.remove(section)
        if before_section is not None:
            before_section = before_section.strip()
            if before_section in sections_order:
                idx = sections_order.index(before_section)
                sections_order.insert(idx, section)
            else:
                sections_order.append(section)
        else:
            sections_order.append(section)
        _db.reorder_sections(user["id"], sections_order)
    else:
        active = _parse_todo_file(TODO_FILE)
        completed = _parse_todo_file(_completed_file_path(TODO_FILE))
        sections_order = []
        seen = set()
        for t in active:
            s = t.get("section", "")
            if s not in seen:
                sections_order.append(s)
                seen.add(s)
        if section not in sections_order:
            return jsonify({"error": "Section not found"}), 404
        sections_order.remove(section)
        if before_section is not None:
            before_section = before_section.strip()
            if before_section in sections_order:
                idx = sections_order.index(before_section)
                sections_order.insert(idx, section)
            else:
                sections_order.append(section)
        else:
            sections_order.append(section)
        section_groups = {}
        for t in active:
            section_groups.setdefault(t.get("section", ""), []).append(t)
        rebuilt = []
        for s in sections_order:
            rebuilt.extend(section_groups.get(s, []))
        _snapshot_and_write(TODO_FILE, rebuilt + completed)
    return jsonify({"ok": True})


@bp.route("/api/sections", methods=["GET"])
def get_sections():
    """Return sections for the current user, ordered by position."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify([])
    return jsonify(_db.get_sections(user["id"]))


@bp.route("/api/sections", methods=["PUT"])
def update_section():
    """Update a section's directives."""
    user = get_current_user()
    if not _USE_DB or not user:
        return jsonify({"error": "Not available"}), 400
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    _db.upsert_section(user["id"], name, directives=data.get("directives"))
    return jsonify({"ok": True})


@bp.route("/api/execute-tool", methods=["POST"])
def execute_tool_endpoint():
    """Unified tool execution endpoint. Routes through _execute_tool with agent context."""
    from flask import current_app
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.json or {}
    tool_name = data.get("tool")
    tool_input = data.get("input", {})
    as_agent = data.get("as_agent", False)
    if not tool_name:
        return jsonify({"error": "tool required"}), 400
    agent_ctx = {"job_id": "__api__", "provider": {}, "depth": 0} if as_agent else None
    # Ensure __api__ pseudo-job exists with user_id so _execute_tool can resolve it
    if as_agent:
        _jobs["__api__"] = {"user_id": user["id"]}
    try:
        result = _execute_tool(tool_name, tool_input, todo_id=None, agent_context=agent_ctx)
        return current_app.response_class(result, mimetype="application/json")
    finally:
        _jobs.pop("__api__", None)


@bp.route("/api/todos/mtime", methods=["GET"])
def get_mtime():
    """Return the max modification time for change detection."""
    if _USE_DB:
        user = get_current_user()
        if user:
            mtime = _db.get_todos_mtime(user["id"])
            return jsonify({"mtime": mtime})
        return jsonify({"mtime": 0})
    mtime = 0
    for p in (TODO_FILE, _completed_file_path(TODO_FILE)):
        try:
            mtime = max(mtime, os.path.getmtime(p))
        except OSError:
            pass
    return jsonify({"mtime": mtime})


@bp.route("/api/undo", methods=["POST"])
def undo():
    """Restore the previous file state from the undo stack."""
    if not _undo_stack:
        return jsonify({"error": "Nothing to undo"}), 400
    old_active, old_completed = _undo_stack.pop()
    _write_todo_file(TODO_FILE, old_active + old_completed)
    return jsonify({"ok": True})
