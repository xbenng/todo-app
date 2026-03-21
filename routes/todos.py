"""Todo CRUD, sections, and utility routes."""
import json
from flask import Blueprint, request, jsonify, render_template
from routes.auth import get_current_user
from schemas import (CreateTodoRequest, UpdateTodoRequest, ReorderRequest,
                     MoveToTopRequest, SortPriorityRequest, DropRequest,
                     RenameSectionRequest, ReorderSectionRequest,
                     UpdateSectionRequest, ExecuteToolRequest,
                     validate_request)
from services.file_io import (
    VALID_PRIORITIES, DEFAULT_PRIORITY, PRIORITY_ORDER,
)
from services.tools import _execute_tool
import state
import db as _db

bp = Blueprint('todos', __name__)


@bp.route("/")
def index():
    user = get_current_user()
    if not user:
        return render_template("login.html")
    return render_template("app.html")


@bp.route("/api/todos", methods=["GET"])
def get_todos():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    return jsonify(_db.get_todos(user["id"]))


@bp.route("/api/todos", methods=["POST"])
@validate_request(CreateTodoRequest)
def add_todo(data: CreateTodoRequest):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    todo = _db.create_todo(
        user["id"], data.title,
        description=data.description,
        priority=data.priority,
        section=data.section,
    )
    return jsonify(todo), 201


@bp.route("/api/todos/<todo_id>", methods=["GET"])
def get_single_todo(todo_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    todo = _db.get_todo(user["id"], todo_id)
    if not todo:
        return jsonify({"error": "Not found"}), 404
    return jsonify(todo)


@bp.route("/api/todos/<todo_id>", methods=["PUT"])
@validate_request(UpdateTodoRequest)
def update_todo_route(data: UpdateTodoRequest, todo_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    fields = {k: v for k, v in data.model_dump(exclude_none=True).items() if k != "mark_unread"}
    result = _db.update_todo(user["id"], todo_id, **fields)
    if not result:
        return jsonify({"error": "Not found"}), 404
    if data.mark_unread:
        _db.mark_chat_unread(todo_id, user["id"])
    return jsonify(result)


@bp.route("/api/todos/search", methods=["GET"])
def search_todos_route():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    query = (request.args.get("q") or "").lower()
    if not query:
        return jsonify([])
    todos = _db.get_todos(user["id"])
    results = [t for t in todos
               if query in t.get("title", "").lower()
               or query in t.get("description", "").lower()]
    return jsonify(results)


@bp.route("/api/todos/<todo_id>", methods=["DELETE"])
def delete_todo_route(todo_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    if not _db.delete_todo(user["id"], todo_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@bp.route("/api/todos/reorder", methods=["POST"])
@validate_request(ReorderRequest)
def reorder_todo(data: ReorderRequest):
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    todo_id = data.id
    direction = data.direction

    all_todos = _db.get_todos(user["id"])
    active = [t for t in all_todos if t["status"] != "completed"]

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

    for i, t in enumerate(active):
        t["position"] = i
    _db.bulk_update_todos(user["id"], active)
    return jsonify({"ok": True, "moved": True})


@bp.route("/api/todos/move-to-top", methods=["POST"])
@validate_request(MoveToTopRequest)
def move_to_top(data: MoveToTopRequest):
    """Move a todo to the top of its section."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    todo_id = data.id

    all_todos = _db.get_todos(user["id"])
    active = [t for t in all_todos if t["status"] != "completed"]

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

    for i, t in enumerate(active):
        t["position"] = i
    _db.bulk_update_todos(user["id"], active)
    return jsonify({"ok": True, "moved": True})


@bp.route("/api/todos/sort-priority", methods=["POST"])
@validate_request(SortPriorityRequest)
def sort_by_priority(data: SortPriorityRequest):
    """Sort todos by priority within a given section."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    section = data.section

    all_todos = _db.get_todos(user["id"])
    active = [t for t in all_todos if t["status"] != "completed"]

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

    for i, t in enumerate(rebuilt):
        t["position"] = i
    _db.bulk_update_todos(user["id"], rebuilt)
    return jsonify({"ok": True})


@bp.route("/api/todos/drop", methods=["POST"])
@validate_request(DropRequest)
def drop_todo(data: DropRequest):
    """Move a todo to a specific position: before another item, or to the end of a section."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    todo_id = data.id
    before_id = data.before_id
    target_section = data.section

    all_todos = _db.get_todos(user["id"])
    active = [t for t in all_todos if t["status"] != "completed"]

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

    for i, t in enumerate(active):
        t["position"] = i
    _db.bulk_update_todos(user["id"], active)
    return jsonify({"ok": True})


@bp.route("/api/sections/rename", methods=["POST"])
@validate_request(RenameSectionRequest)
def rename_section(data: RenameSectionRequest):
    """Rename a section header across all todos."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    old_name = data.old_name
    new_name = data.new_name
    if old_name == new_name:
        return jsonify({"ok": True})

    todos = _db.get_todos(user["id"])
    changed = False
    for t in todos:
        if t.get("section", "") == old_name:
            t["section"] = new_name
            changed = True
    if not changed:
        return jsonify({"error": "Section not found"}), 404
    _db.bulk_update_todos(user["id"], [t for t in todos if t.get("section") == new_name])
    return jsonify({"ok": True})


@bp.route("/api/sections/reorder", methods=["POST"])
@validate_request(ReorderSectionRequest)
def reorder_section(data: ReorderSectionRequest):
    """Move a section (and all its todos) before another section."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    section = data.section
    before_section = data.before_section

    # Get current section order from DB
    sections = _db.get_sections(user["id"])
    sections_order = [s["name"] for s in sections]
    # Add section if not in DB yet
    if section not in sections_order:
        sections_order.append(section)
    sections_order.remove(section)
    if before_section is not None:
        if before_section in sections_order:
            idx = sections_order.index(before_section)
            sections_order.insert(idx, section)
        else:
            sections_order.append(section)
    else:
        sections_order.append(section)
    _db.reorder_sections(user["id"], sections_order)
    return jsonify({"ok": True})


@bp.route("/api/sections", methods=["GET"])
def get_sections():
    """Return sections for the current user, ordered by position."""
    user = get_current_user()
    if not user:
        return jsonify([])
    return jsonify(_db.get_sections(user["id"]))


@bp.route("/api/sections", methods=["PUT"])
@validate_request(UpdateSectionRequest)
def update_section(data: UpdateSectionRequest):
    """Update a section's directives."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not available"}), 400
    _db.upsert_section(user["id"], data.name, directives=data.directives)
    return jsonify({"ok": True})


@bp.route("/api/execute-tool", methods=["POST"])
@validate_request(ExecuteToolRequest)
def execute_tool_endpoint(data: ExecuteToolRequest):
    """Unified tool execution endpoint. Routes through _execute_tool with agent context."""
    from flask import current_app
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    tool_name = data.tool
    tool_input = data.input
    as_agent = data.as_agent
    agent_ctx = {"job_id": "__api__", "provider": {}, "depth": 0} if as_agent else None
    # Ensure __api__ pseudo-job exists with user_id so _execute_tool can resolve it
    if as_agent:
        state._jobs["__api__"] = {"user_id": user["id"]}
    try:
        result = _execute_tool(tool_name, tool_input, todo_id=None, agent_context=agent_ctx)
        return current_app.response_class(result, mimetype="application/json")
    finally:
        state._jobs.pop("__api__", None)


@bp.route("/api/todos/mtime", methods=["GET"])
def get_mtime():
    """Return the max modification time for change detection."""
    user = get_current_user()
    if user:
        mtime = _db.get_todos_mtime(user["id"])
        return jsonify({"mtime": mtime})
    return jsonify({"mtime": 0})
