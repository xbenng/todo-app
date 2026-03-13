#!/usr/bin/env python3
"""
Simple Todo App — A lightweight web UI for managing a markdown-based todo list.

Usage:
    python app.py [path/to/todos.md]

If no file is specified, defaults to 'todos.md' in the current directory.
The file will be created if it doesn't exist.
"""

import sys
import os
import re
import json
import uuid
import copy
from collections import deque
from datetime import datetime
from flask import Flask, request, jsonify, Response

app = Flask(__name__)
TODO_FILE = "todos.md"

# Undo stack: each entry is (active_todos_list, completed_todos_list)
_undo_stack: deque[tuple[list[dict], list[dict]]] = deque(maxlen=30)


def _completed_file_path(path: str) -> str:
    """Derive the completed-todos file path from the main todo file path.
    e.g. todos.md -> todos-completed.md
    """
    base, ext = os.path.splitext(path)
    return f"{base}-completed{ext}"


# ---------------------------------------------------------------------------
# File format parser / writer
# ---------------------------------------------------------------------------

VALID_PRIORITIES = {"low", "medium", "high"}
DEFAULT_PRIORITY = "medium"
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _parse_todo_file(path: str) -> list[dict]:
    """Parse a todos.md file into a list of todo dicts.

    Tracks ## headers as section names and assigns them to subsequent todos.
    """
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        file_lines = f.readlines()

    todos: list[dict] = []
    current_section = ""
    i = 0
    while i < len(file_lines):
        line = file_lines[i].rstrip("\n")

        # Track section headers (## level only)
        if line.startswith("## "):
            current_section = line[3:].strip()
            i += 1
            continue

        # Skip h1 headers and blank lines
        if line.startswith("# ") or not line.strip():
            i += 1
            continue

        # Match todo item
        m = re.match(r"^- \[([ xX])\] (.+)", line)
        if m:
            checked = m.group(1).lower() == "x"
            first_line = m.group(2).strip()

            # Collect continuation lines (indented or blank)
            desc_raw_lines: list[str] = []
            i += 1
            while i < len(file_lines):
                cl = file_lines[i].rstrip("\n")
                if cl.startswith("  ") or cl.strip() == "":
                    desc_raw_lines.append(cl)
                    i += 1
                else:
                    break

            # Strip trailing blank lines from description
            while desc_raw_lines and not desc_raw_lines[-1].strip():
                desc_raw_lines.pop()

            # Extract id if present: <!-- id:xxxx -->
            id_match = re.search(r"<!-- id:(\S+?) -->", first_line)
            todo_id = id_match.group(1) if id_match else str(uuid.uuid4())[:8]
            if id_match:
                first_line = first_line.replace(id_match.group(0), "").strip()

            # Extract status tag: [critical], [in-progress], etc.
            status = "completed" if checked else "open"
            # Strip legacy status tags from title
            status_match = re.match(r"^\[(\S+?)\]\s*", first_line)
            if status_match:
                first_line = first_line[status_match.end() :].strip()

            # Extract priority tag: {high}, {medium}, {low}
            priority = DEFAULT_PRIORITY
            priority_match = re.search(r"\{(high|medium|low)\}", first_line, re.IGNORECASE)
            if priority_match:
                priority = priority_match.group(1).lower()
                first_line = first_line.replace(priority_match.group(0), "").strip()

            # Title is the remainder of first line (strip bold markers)
            title = first_line.strip("*").strip()

            # Description is remaining lines, de-indented
            desc_lines = []
            for dl in desc_raw_lines:
                stripped = dl.strip()
                if stripped:
                    desc_lines.append(re.sub(r"^  ", "", dl.rstrip()))
                else:
                    desc_lines.append("")
            description = "\n".join(desc_lines).strip()

            todos.append(
                {
                    "id": todo_id,
                    "title": title,
                    "description": description,
                    "status": status,
                    "priority": priority,
                    "section": current_section,
                }
            )
        else:
            i += 1

    return todos


def _write_todo_file(path: str, todos: list[dict]) -> None:
    """Write todos across two files: active items in the main file, completed in a -completed file."""
    active = [t for t in todos if t["status"] != "completed"]
    completed = [t for t in todos if t["status"] == "completed"]

    # Write active file — group by section, preserving order of first appearance
    lines = ["# Todo List", ""]
    sections_order: list[str] = []
    seen_sections: set[str] = set()
    for t in active:
        s = t.get("section", "")
        if s not in seen_sections:
            sections_order.append(s)
            seen_sections.add(s)

    for section in sections_order:
        if section:
            lines.append(f"## {section}")
            lines.append("")
        items = [t for t in active if t.get("section", "") == section]
        for t in items:
            lines.extend(_format_todo(t, checked=False))
            lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # Write completed file
    comp_path = _completed_file_path(path)
    comp_lines = ["# Completed Todos", ""]
    for t in completed:
        comp_lines.extend(_format_todo(t, checked=True))
        comp_lines.append("")

    with open(comp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(comp_lines))


def _format_todo(t: dict, checked: bool) -> list[str]:
    """Format a single todo as markdown lines."""
    checkbox = "[x]" if checked else "[ ]"
    priority = t.get("priority", DEFAULT_PRIORITY)
    priority_tag = f" {{{priority}}}" if priority != DEFAULT_PRIORITY else ""
    id_tag = f" <!-- id:{t['id']} -->"

    title_line = f"- {checkbox} **{t['title']}**{priority_tag}{id_tag}"
    result = [title_line]

    if t.get("description"):
        for dline in t["description"].split("\n"):
            result.append(f"  {dline}")

    return result


def _snapshot_and_write(path: str, todos: list[dict]) -> None:
    """Snapshot current state for undo, then write new state."""
    old_active = _parse_todo_file(path)
    old_completed = _parse_todo_file(_completed_file_path(path))
    _undo_stack.append((copy.deepcopy(old_active), copy.deepcopy(old_completed)))
    _write_todo_file(path, todos)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


@app.route("/api/todos", methods=["GET"])
def get_todos():
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    return jsonify(active + completed)


@app.route("/api/todos", methods=["POST"])
def add_todo():
    data = request.json
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


@app.route("/api/todos/<todo_id>", methods=["PUT"])
def update_todo(todo_id):
    data = request.json
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


@app.route("/api/todos/<todo_id>", methods=["DELETE"])
def delete_todo(todo_id):
    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))
    todos = active + completed
    new_todos = [t for t in todos if t["id"] != todo_id]
    if len(new_todos) == len(todos):
        return jsonify({"error": "Not found"}), 404
    _snapshot_and_write(TODO_FILE, new_todos)
    return jsonify({"ok": True})


@app.route("/api/todos/reorder", methods=["POST"])
def reorder_todo():
    data = request.json
    todo_id = data.get("id")
    direction = data.get("direction")  # "up" or "down"
    if not todo_id or direction not in ("up", "down"):
        return jsonify({"error": "id and direction (up/down) required"}), 400

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

    _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True, "moved": True})


@app.route("/api/todos/move-to-top", methods=["POST"])
def move_to_top():
    """Move a todo to the top of its section."""
    data = request.json
    todo_id = data.get("id")
    if not todo_id:
        return jsonify({"error": "id required"}), 400

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

    _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True, "moved": True})


@app.route("/api/todos/sort-priority", methods=["POST"])
def sort_by_priority():
    """Sort todos by priority within a given section."""
    data = request.json
    section = data.get("section", "")

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

    _snapshot_and_write(TODO_FILE, rebuilt + completed)
    return jsonify({"ok": True})


@app.route("/api/todos/drop", methods=["POST"])
def drop_todo():
    """Move a todo to a specific position: before another item, or to the end of a section."""
    data = request.json
    todo_id = data.get("id")
    before_id = data.get("before_id")  # insert before this item (None = end of section)
    target_section = data.get("section")  # required if before_id is None

    if not todo_id:
        return jsonify({"error": "id required"}), 400

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

    _snapshot_and_write(TODO_FILE, active + completed)
    return jsonify({"ok": True})


@app.route("/api/sections/rename", methods=["POST"])
def rename_section():
    """Rename a section header across all todos."""
    data = request.json
    old_name = (data.get("old_name") or "").strip()
    new_name = (data.get("new_name") or "").strip()
    if not old_name or not new_name:
        return jsonify({"error": "old_name and new_name required"}), 400
    if old_name == new_name:
        return jsonify({"ok": True})

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
    _snapshot_and_write(TODO_FILE, todos)
    return jsonify({"ok": True})


@app.route("/api/sections/reorder", methods=["POST"])
def reorder_section():
    """Move a section (and all its todos) before another section."""
    data = request.json
    section = (data.get("section") or "").strip()
    before_section = data.get("before_section")  # None = move to end

    if not section:
        return jsonify({"error": "section required"}), 400

    active = _parse_todo_file(TODO_FILE)
    completed = _parse_todo_file(_completed_file_path(TODO_FILE))

    # Build current section order
    sections_order = []
    seen = set()
    for t in active:
        s = t.get("section", "")
        if s not in seen:
            sections_order.append(s)
            seen.add(s)

    if section not in sections_order:
        return jsonify({"error": "Section not found"}), 404

    # Remove the section from its current position
    sections_order.remove(section)

    # Insert before the target section, or at the end
    if before_section is not None:
        before_section = before_section.strip()
        if before_section in sections_order:
            idx = sections_order.index(before_section)
            sections_order.insert(idx, section)
        else:
            sections_order.append(section)
    else:
        sections_order.append(section)

    # Rebuild the active list in the new section order
    section_groups = {}
    for t in active:
        s = t.get("section", "")
        section_groups.setdefault(s, []).append(t)

    rebuilt = []
    for s in sections_order:
        rebuilt.extend(section_groups.get(s, []))

    _snapshot_and_write(TODO_FILE, rebuilt + completed)
    return jsonify({"ok": True})


@app.route("/api/todos/mtime", methods=["GET"])
def get_mtime():
    """Return the max modification time across both files for change detection."""
    mtime = 0
    for p in (TODO_FILE, _completed_file_path(TODO_FILE)):
        try:
            mtime = max(mtime, os.path.getmtime(p))
        except OSError:
            pass
    return jsonify({"mtime": mtime})


@app.route("/api/undo", methods=["POST"])
def undo():
    """Restore the previous file state from the undo stack."""
    if not _undo_stack:
        return jsonify({"error": "Nothing to undo"}), 400
    old_active, old_completed = _undo_stack.pop()
    _write_todo_file(TODO_FILE, old_active + old_completed)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Embedded HTML UI
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Todo List</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  :root {
    --bg: #f8f9fb; --card: #fff; --border: #e2e5ea; --text: #1a1d23;
    --muted: #6b7280; --subtle: #9ca3af;
    --accent: #4f6ef7; --accent-hover: #3b5de7; --accent-light: rgba(79,110,247,0.08);
    --completed-bg: #f0faf4; --completed-border: #6ee7a0; --completed-text: #166534;
    --danger: #ef4444; --danger-hover: #dc2626;
    --radius: 10px; --radius-lg: 14px;
    --shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06);
    --shadow-md: 0 4px 12px rgba(0,0,0,0.06), 0 1px 3px rgba(0,0,0,0.08);
    --shadow-lg: 0 10px 30px rgba(0,0,0,0.08), 0 2px 8px rgba(0,0,0,0.06);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
    max-width: 720px; margin: 0 auto; padding: 24px 20px;
    -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  }
  h1 { display: none; }
  h2 {
    font-size: 0.8rem; color: var(--subtle); margin: 28px 0 12px;
    text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600;
  }

  /* Add form */
  .add-form {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 18px; margin-bottom: 10px; box-shadow: var(--shadow-md);
    display: none; transition: box-shadow 0.2s;
  }
  .add-form.visible { display: block; }
  .add-form.kb-selected { box-shadow: 0 0 0 2px var(--accent), var(--shadow-md); }
  .add-toggle {
    position: fixed; top: 20px; left: 20px; z-index: 900;
    display: flex; flex-direction: column; align-items: flex-start; gap: 8px;
  }
  .add-toggle h1 {
    display: block; font-size: 1.6rem; margin: 0; font-weight: 700;
    letter-spacing: -0.02em; color: var(--text);
  }
  .add-toggle .btn-row { display: flex; align-items: center; gap: 10px; }
  .add-toggle .btn { font-size: 0.85rem; padding: 9px 22px; box-shadow: var(--shadow-lg); }
  .search-bar {
    margin-bottom: 10px;
    position: relative;
  }
  .search-clear-hint {
    display: none; position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
    font-size: 0.7rem; color: var(--subtle); background: var(--bg); padding: 2px 6px;
    border-radius: 4px; border: 1px solid var(--border); pointer-events: none;
  }
  .search-bar input.has-query ~ .search-clear-hint { display: block; }
  .search-bar input {
    width: 100%; padding: 9px 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.9rem; font-family: inherit; background: var(--card); color: var(--text);
    transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  }
  .search-bar input:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light);
  }
  .search-bar input.has-query {
    border-color: var(--accent); background: var(--accent-light);
    box-shadow: 0 0 0 2px var(--accent-light);
  }
  .search-bar input.has-query:focus {
    box-shadow: 0 0 0 3px var(--accent-light);
  }
  .search-bar input::placeholder { color: var(--subtle); }
  .add-form input, .add-form textarea, .add-form select {
    width: 100%; padding: 10px 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 1rem; font-family: inherit; margin-bottom: 10px;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s; color: var(--text);
  }
  .add-form input:focus, .add-form textarea:focus, .add-form select:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light);
  }
  .add-form textarea { resize: vertical; min-height: 56px; }
  .add-form .row { display: flex; gap: 8px; align-items: center; }
  .add-form .row select { width: auto; margin-bottom: 0; }
  .btn {
    padding: 9px 18px; border: none; border-radius: 8px; font-size: 0.9rem;
    font-weight: 600; cursor: pointer; transition: all 0.15s; letter-spacing: -0.01em;
  }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-hover); transform: translateY(-1px); box-shadow: var(--shadow-md); }
  .btn-primary:active { transform: translateY(0); }
  .btn-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); padding: 4px 10px; font-size: 0.75rem; }
  .btn-danger:hover { background: var(--danger); color: #fff; }
  .btn-sm { padding: 5px 12px; font-size: 0.75rem; }

  /* Todo items */
  .todo-item {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 14px 18px; margin-bottom: 6px; box-shadow: var(--shadow);
    display: flex; align-items: flex-start; gap: 12px;
    transition: all 0.2s ease;
    border-left: 3px solid transparent;
  }
  .todo-item:hover { box-shadow: var(--shadow-md); transform: translateY(-1px); }
  .todo-item.status-completed {
    border-left-color: var(--completed-border); background: var(--completed-bg);
    opacity: 0.6;
  }
  .todo-item.status-completed:hover { opacity: 0.8; }
  .todo-item.dragging { opacity: 0.35; transform: scale(0.98); }
  .todo-item.drag-over-top { box-shadow: inset 0 3px 0 var(--accent); }
  .todo-item.drag-over-bottom { box-shadow: inset 0 -3px 0 var(--accent); }
  .section-header-row.drag-over-section { background: var(--accent-light); }
  .section-header-row.section-dragging { opacity: 0.35; }
  .section-header-row.section-drag-over-top { box-shadow: inset 0 3px 0 var(--accent); }
  .section-header-row.section-drag-over-bottom { box-shadow: inset 0 -3px 0 var(--accent); }
  .section-header-row[draggable="true"] { cursor: grab; }
  .section-header-row[draggable="true"]:active { cursor: grabbing; }

  .todo-checkbox {
    margin-top: 2px; width: 18px; height: 18px; cursor: pointer;
    accent-color: var(--accent); flex-shrink: 0;
    border-radius: 4px;
  }
  .todo-body { flex: 1; min-width: 0; }
  .todo-title { font-weight: 600; font-size: 1.02rem; word-break: break-word; letter-spacing: -0.01em; }
  .todo-desc { color: var(--muted); font-size: 0.9rem; margin-top: 4px; word-break: break-word; line-height: 1.5; }
  .todo-desc p { margin: 0 0 0.4em; }
  .todo-desc p:last-child { margin-bottom: 0; }
  .todo-desc ul, .todo-desc ol { margin: 0.2em 0 0.4em 1.2em; padding: 0; }
  .todo-desc li { margin: 0.1em 0; }
  .todo-desc code { background: rgba(0,0,0,0.05); padding: 2px 5px; border-radius: 4px; font-size: 0.83em; }
  .todo-desc pre { background: rgba(0,0,0,0.03); padding: 10px; border-radius: 6px; overflow-x: auto; margin: 0.3em 0; }
  .todo-desc pre code { background: none; padding: 0; }
  .todo-desc a { color: var(--accent); text-decoration: none; }
  .todo-desc a:hover { text-decoration: underline; }
  .todo-desc h1, .todo-desc h2, .todo-desc h3 { font-size: 0.9em; margin: 0.4em 0 0.2em; }
  .todo-desc blockquote { border-left: 3px solid var(--border); margin: 0.3em 0; padding-left: 10px; color: var(--muted); }
  .todo-meta { display: flex; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .priority-badge {
    font-size: 0.68rem; font-weight: 700; text-transform: uppercase; padding: 2px 7px;
    border-radius: 12px; letter-spacing: 0.04em; flex-shrink: 0; align-self: flex-start; margin-top: 3px;
  }
  .priority-high { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }
  .priority-medium { background: #fffbeb; color: #a16207; border: 1px solid #fde68a; }
  .priority-low { background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }

  .section-header-row {
    display: flex; align-items: center; gap: 8px;
    margin: 22px 0 10px; padding: 8px 0 6px;
    border-bottom: 2px solid var(--border);
    position: sticky; top: 0; z-index: 100; background: var(--bg);
  }
  .section-header-row h3 {
    font-size: 0.92rem; color: var(--text); font-weight: 700; margin: 0;
    letter-spacing: -0.01em; cursor: pointer;
  }
  .section-header-row h3:hover { color: var(--accent); }
  .section-rename-input {
    font-size: 0.92rem; font-weight: 700; border: 1px solid var(--accent);
    border-radius: 6px; padding: 2px 8px; outline: none; font-family: inherit;
    box-shadow: 0 0 0 3px var(--accent-light); background: var(--card);
  }
  .section-count {
    font-size: 0.75rem; color: var(--subtle); font-weight: 500;
    background: rgba(0,0,0,0.04); padding: 1px 8px; border-radius: 12px;
  }
  .collapse-btn {
    font-size: 0.6rem; padding: 2px 6px; border-radius: 5px;
    border: 1px solid transparent; background: transparent; color: var(--subtle);
    cursor: pointer; transition: all 0.2s ease; line-height: 1;
  }
  .collapse-btn:hover { background: var(--accent-light); color: var(--accent); }
  .collapse-btn.collapsed { transform: rotate(-90deg); }
  .sort-priority-btn {
    font-size: 0.65rem; padding: 3px 10px; border-radius: 12px;
    border: 1px solid var(--border); background: var(--card); color: var(--subtle);
    cursor: pointer; white-space: nowrap; transition: all 0.15s; margin-left: auto;
    font-weight: 500;
  }
  .sort-priority-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }

  .todo-actions {
    display: flex; gap: 2px; flex-shrink: 0; align-items: flex-start;
    opacity: 0; transition: opacity 0.15s;
  }
  .todo-item:hover .todo-actions { opacity: 1; }
  .todo-actions select { font-size: 0.75rem; padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border); background: #fafafa; }

  .empty-state { text-align: center; color: var(--subtle); padding: 48px 0; font-size: 0.95rem; }

  /* Edit mode */
  .edit-title {
    font-size: 1.02rem; font-weight: 600; width: 100%; padding: 8px 12px;
    border: 1px solid var(--border); border-radius: 8px; margin-bottom: 6px;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s;
  }
  .edit-title:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }
  .edit-desc {
    font-size: 0.9rem; width: 100%; padding: 8px 12px;
    border: 1px solid var(--border); border-radius: 8px; resize: vertical;
    min-height: 44px; font-family: inherit;
    background: var(--bg); transition: border-color 0.15s, box-shadow 0.15s;
  }
  .edit-desc:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }
  .edit-actions { display: flex; gap: 6px; margin-top: 8px; }

  /* Section headers */
  .section-header {
    font-size: 0.95rem; color: var(--text); margin: 18px 0 8px; font-weight: 600;
    padding-bottom: 4px; border-bottom: 1px solid var(--border);
    display: none;
  }

  /* Keyboard-selected item */
  .todo-item.kb-selected {
    box-shadow: 0 0 0 2px var(--accent), var(--shadow);
    border-left-color: var(--accent);
  }

  /* Context menu */
  .ctx-menu {
    display: none; position: fixed; z-index: 1000;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow-lg); min-width: 190px;
    padding: 5px 0; font-size: 0.9rem;
    backdrop-filter: blur(10px);
  }
  .ctx-menu.visible { display: block; }
  .ctx-menu-item {
    padding: 8px 14px; cursor: pointer; display: flex; align-items: center; gap: 8px;
    color: var(--text); user-select: none; position: relative;
    border-radius: 6px; margin: 1px 4px; transition: background 0.1s;
  }
  .ctx-menu-item:hover { background: var(--accent); color: #fff; }
  .ctx-menu-item.has-submenu::after {
    content: '\25B6'; font-size: 0.6rem; margin-left: auto; opacity: 0.5;
  }
  .ctx-menu-item:hover.has-submenu::after { opacity: 1; }
  .ctx-menu-sep { border-top: 1px solid var(--border); margin: 4px 8px; }
  .ctx-submenu {
    display: none; position: absolute; left: 100%; top: -5px;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow-lg); min-width: 160px;
    padding: 5px 0; backdrop-filter: blur(10px);
  }
  .ctx-menu-item:hover > .ctx-submenu { display: block; }
  .ctx-submenu .ctx-menu-item { padding: 7px 14px; font-size: 0.82rem; }
  .ctx-submenu .ctx-menu-item.active-section { font-weight: 700; opacity: 0.5; pointer-events: none; }

  /* Section picker dialog */
  .section-picker {
    display: none; position: fixed; z-index: 1100;
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow-lg); min-width: 200px; max-width: 280px;
    padding: 6px 0; font-size: 0.9rem;
    backdrop-filter: blur(10px);
  }
  .section-picker.visible { display: block; }
  .section-picker-item {
    padding: 7px 14px; cursor: pointer; color: var(--text);
    border-radius: 6px; margin: 1px 4px; transition: background 0.1s;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .section-picker-item:hover { background: var(--accent-light); }
  .section-picker-item.sp-selected { background: var(--accent); color: #fff; }
  .section-picker-item.sp-current { opacity: 0.45; pointer-events: none; }
  .section-picker-input {
    width: calc(100% - 12px); margin: 4px 6px 4px; padding: 6px 10px;
    font-size: 0.88rem; font-family: inherit; border: 1px solid var(--border);
    border-radius: 6px; outline: none; background: var(--bg); color: var(--text);
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .section-picker-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-light); }

  /* Kbd styling */
  kbd {
    padding: 2px 6px; border: 1px solid var(--border); border-radius: 4px;
    background: var(--card); font-size: 0.75rem; font-family: inherit;
    box-shadow: 0 1px 1px rgba(0,0,0,0.06);
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--subtle); }
</style>
</head>
<body>

<div class="add-toggle">
  <h1>&#9744; To Do List</h1>
  <div class="btn-row">
    <button class="btn btn-primary" id="add-toggle-btn" onclick="showAddForm()">+ New Todo</button>
    <span style="color:var(--subtle);font-size:0.78rem">or press <kbd>n</kbd></span>
  </div>
</div>

<div class="search-bar">
  <input type="text" id="search-input" placeholder="Search todos... (/)">
  <kbd class="search-clear-hint" id="search-clear-hint">Esc to clear</kbd>
</div>

<div class="add-form" id="add-form">
  <input type="text" id="new-title" placeholder="What needs to be done?">
  <textarea id="new-desc" placeholder="Description (optional)"></textarea>
  <select id="new-section" style="font-size:0.85rem; width:100%; margin-bottom:8px;">
    <option value="">No section</option>
  </select>
  <input type="text" id="new-section-custom" placeholder="New section name" style="font-size:0.85rem; display:none;">
  <div class="row">
    <select id="new-priority">
      <option value="high">High</option>
      <option value="medium" selected>Medium</option>
      <option value="low">Low</option>
    </select>
    <button class="btn btn-primary" onclick="addTodo()">Add Todo</button>
    <button class="btn btn-sm" onclick="hideAddForm()" style="border:1px solid var(--border)">Cancel <span style="opacity:0.6;font-weight:400">Esc</span></button>
  </div>
</div>

<div id="active-section"></div>
<div id="completed-section"></div>

<!-- Context menu -->
<div class="ctx-menu" id="ctx-menu"></div>
<!-- Section picker -->
<div class="section-picker" id="section-picker"></div>

<script>
const API = '/api/todos';
let allTodos = [];
let editingId = null;
let lastMtime = 0;
let pollTimer = null;
let selectedIdx = -1; // -1 = nothing, 0 = add-form, 1+ = todo items
let insertBeforeId = null; // when adding, insert before this todo id
let searchQuery = ''; // fuzzy search filter
let ctxTargetId = null; // id of todo targeted by context menu
let visibleIds = []; // ordered list of todo ids as rendered
let sectionsOrder = []; // ordered list of section names as rendered
let addFormVisible = false;
const collapsedSections = new Set(); // collapsed section names
const SEL_ADD = 0; // index for the add-form position

async function loadTodos() {
  const res = await fetch(API);
  allTodos = await res.json();
  // Update our known mtime so polling doesn't re-trigger
  try {
    const mt = await fetch(API + '/mtime');
    const d = await mt.json();
    lastMtime = d.mtime;
  } catch(e) {}
  render();
}

// Poll for external file changes every 1.5s
async function pollForChanges() {
  try {
    const res = await fetch(API + '/mtime');
    const data = await res.json();
    if (data.mtime !== lastMtime) {
      lastMtime = data.mtime;
      // Don't reload if user is editing
      if (!editingId) {
        const res2 = await fetch(API);
        allTodos = await res2.json();
        render();
      }
    }
  } catch(e) {}
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollForChanges, 1500);
}

function render() {
  const active = allTodos.filter(t => t.status !== 'completed');
  const completed = allTodos.filter(t => t.status === 'completed');

  const activeEl = document.getElementById('active-section');
  const completedEl = document.getElementById('completed-section');

  // Rescue add-form before innerHTML overwrites it (it may be inside activeEl)
  const form = document.getElementById('add-form');
  const formWasVisible = addFormVisible;
  const formTitle = form.querySelector('#new-title')?.value || '';
  const formDesc = form.querySelector('#new-desc')?.value || '';
  const formPriority = form.querySelector('#new-priority')?.value || 'medium';
  const formSection = form.querySelector('#new-section')?.value || '';
  const formSectionCustom = form.querySelector('#new-section-custom')?.value || '';
  const searchBar = document.querySelector('.search-bar');
  if (searchBar) searchBar.after(form);

  // Apply search filter — each space-separated word must appear as a substring
  const searching = searchQuery.trim().length > 0;
  const searchTokens = searching ? searchQuery.toLowerCase().trim().split(/\s+/) : [];
  const matchesSearch = t => {
    const text = ((t.title || '') + ' ' + (t.description || '') + ' ' + (t.section || '')).toLowerCase();
    return searchTokens.every(tok => text.includes(tok));
  };
  const filteredActive = searching ? active.filter(matchesSearch) : active;
  const filteredCompleted = searching ? completed.filter(matchesSearch) : completed;

  if (filteredActive.length === 0 && filteredCompleted.length === 0) {
    if (searching) {
      activeEl.innerHTML = '<div class="empty-state">No matching todos</div>';
    } else {
      activeEl.innerHTML = '<div class="empty-state">No todos yet. Press <strong>n</strong> to add one!</div>';
    }
    completedEl.innerHTML = '';
    visibleIds = [];
    // Restore form state if it was visible
    if (formWasVisible) _restoreInlineForm(form, formTitle, formDesc, formPriority, formSection, formSectionCustom);
    applySelection();
    return;
  }

  // Group active by section preserving order of first appearance
  sectionsOrder = [];
  const seenSections = new Set();
  filteredActive.forEach(t => {
    const s = t.section || '';
    if (!seenSections.has(s)) { sectionsOrder.push(s); seenSections.add(s); }
  });

  let activeHtml = '';
  const visibleActive = []; // track which active items are visible (not collapsed)
  sectionsOrder.forEach(section => {
    const items = filteredActive.filter(t => (t.section || '') === section);
    const isCollapsed = !searching && collapsedSections.has(section);
    const escSection = esc(section).replace(/'/g, "\\'");
    if (section) {
      activeHtml += `<div class="section-header-row" data-section="${esc(section)}" draggable="true">`
        + `<button class="collapse-btn${isCollapsed ? ' collapsed' : ''}" onclick="toggleSectionCollapse('${escSection}')" title="${isCollapsed ? 'Expand' : 'Collapse'}">&#9660;</button>`
        + `<h3 ondblclick="startSectionRename('${escSection}')">${esc(section)}</h3>`
        + `<span class="section-count">${items.length}</span>`
        + `<button class="sort-priority-btn" onclick="sortByPriority('${escSection}')" title="Sort by priority (high first)">&#9650; Priority</button>`
        + `</div>`;
    }
    if (!isCollapsed) {
      activeHtml += items.map(t => renderTodo(t)).join('');
      visibleActive.push(...items);
    }
  });

  // visibleIds only includes non-collapsed active items + completed
  visibleIds = [...visibleActive, ...filteredCompleted].map(t => t.id);

  activeEl.innerHTML = filteredActive.length
    ? '<h2>Active (' + filteredActive.length + ')</h2>' + activeHtml
    : '<h2>Active</h2><div class="empty-state">All done! &#127881;</div>';

  const isCompletedCollapsed = !searching && collapsedSections.has('__completed__');
  if (filteredCompleted.length) {
    completedEl.innerHTML = `<div class="section-header-row">`
      + `<button class="collapse-btn${isCompletedCollapsed ? ' collapsed' : ''}" onclick="toggleSectionCollapse('__completed__')" title="${isCompletedCollapsed ? 'Expand' : 'Collapse'}">&#9660;</button>`
      + `<h2 style="margin:0;">Completed (${filteredCompleted.length})</h2>`
      + `</div>`
      + (isCompletedCollapsed ? '' : filteredCompleted.map(t => renderTodo(t)).join(''));
    if (isCompletedCollapsed) {
      visibleIds = visibleActive.map(t => t.id);
    }
  } else {
    completedEl.innerHTML = '';
  }

  // Update section dropdown options
  const allSections = [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
  const secSelect = document.getElementById('new-section');
  if (secSelect) {
    const curVal = secSelect.value;
    secSelect.innerHTML = '<option value="">No section</option>'
      + allSections.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')
      + '<option value="__custom__">Other...</option>';
    // Restore previous selection if still valid
    if ([...secSelect.options].some(o => o.value === curVal)) secSelect.value = curVal;
  }

  // Restore inline form if it was visible and we have an insertion target
  if (formWasVisible) _restoreInlineForm(form, formTitle, formDesc, formPriority, formSection, formSectionCustom);

  // Clamp selectedIdx if items disappeared (e.g. section collapsed)
  if (selectedIdx > visibleIds.length) selectedIdx = visibleIds.length > 0 ? visibleIds.length : -1;

  applySelection();
}

function _restoreInlineForm(form, title, desc, priority, section, sectionCustom) {
  if (insertBeforeId) {
    const targetEl = document.querySelector(`.todo-item[data-todo-id="${insertBeforeId}"]`);
    if (targetEl) targetEl.parentNode.insertBefore(form, targetEl);
  }
  form.classList.add('visible');
  form.querySelector('#new-title').value = title;
  form.querySelector('#new-desc').value = desc;
  form.querySelector('#new-priority').value = priority;
  // Restore section select — if the value exists in options, set it; otherwise set to custom
  const secSelect = form.querySelector('#new-section');
  const customInput = form.querySelector('#new-section-custom');
  if ([...secSelect.options].some(o => o.value === section)) {
    secSelect.value = section;
  } else if (section) {
    secSelect.value = '__custom__';
  }
  customInput.value = sectionCustom;
  customInput.style.display = secSelect.value === '__custom__' ? '' : 'none';
}

function renderTodo(t) {
  const checked = t.status === 'completed' ? 'checked' : '';
  const statusClass = t.status === 'completed' ? 'status-completed' : '';

  if (editingId === t.id) {
    return `<div class="todo-item ${statusClass}">
      <div class="todo-body">
        <input class="edit-title" id="edit-title-${t.id}" value="${esc(t.title)}">
        <textarea class="edit-desc" id="edit-desc-${t.id}">${esc(t.description)}</textarea>
        <select id="edit-section-${t.id}" style="font-size:0.85rem; font-weight:400; margin-bottom:4px; width:100%; padding:4px 8px; border:1px solid var(--border); border-radius:4px;">
          <option value="">No section</option>
          ${allSectionsForEdit().map(s => `<option value="${esc(s)}" ${(t.section||'')===s?'selected':''}>${esc(s)}</option>`).join('')}
          <option value="__custom__">Other...</option>
        </select>
        <input class="edit-title" id="edit-section-custom-${t.id}" placeholder="New section name" style="font-size:0.85rem; font-weight:400; margin-bottom:4px; display:none;">
        <div class="edit-actions">
          <select id="edit-priority-${t.id}">
            ${['high','medium','low'].map(p =>
              `<option value="${p}" ${p===t.priority?'selected':''}>${p}</option>`
            ).join('')}
          </select>
          <button class="btn btn-primary btn-sm" onclick="saveEdit('${t.id}')">Save <span style="opacity:0.6;font-weight:400">&#8984;&#9166;</span></button>
          <button class="btn btn-sm" onclick="cancelEdit()" style="border:1px solid var(--border)">Cancel <span style="opacity:0.6;font-weight:400">Esc</span></button>
        </div>
      </div>
    </div>`;
  }

  const desc = t.description ? `<div class="todo-desc">${renderMd(t.description)}</div>` : '';
  const priorityBadge = `<span class="priority-badge priority-${t.priority || 'medium'}">${t.priority || 'medium'}</span>`;

  const draggable = t.status !== 'completed' ? 'draggable="true"' : '';
  return `<div class="todo-item ${statusClass}" data-todo-id="${t.id}" ${draggable} onclick="selectTodo('${t.id}')" oncontextmenu="showCtxMenu(event,'${t.id}')" style="cursor:pointer;">
    <input type="checkbox" class="todo-checkbox" ${checked} onchange="toggleComplete('${t.id}', this.checked)" onclick="event.stopPropagation()">
    <div class="todo-body" ondblclick="startEdit('${t.id}')">
      <div class="todo-title">${esc(t.title)}</div>
      ${desc}
    </div>
    ${priorityBadge}
    <div class="todo-actions">
      ${t.status !== 'completed' ? `<button onclick="event.stopPropagation();copyWorkon('${t.id}')" style="border:none;background:transparent;font-size:0.8rem;padding:2px 4px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Copy /ea workon command" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--subtle)'">&#9654;</button>` : ''}
      ${t.status !== 'completed' ? `<button onclick="event.stopPropagation();bringToTop('${t.id}')" style="border:none;background:transparent;font-size:1rem;padding:2px 4px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Bring to top" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--subtle)'">&#x2912;</button>` : ''}
      <button onclick="event.stopPropagation();deleteTodo('${t.id}')" style="border:none;background:transparent;font-size:0.8rem;padding:2px 6px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Delete" onmouseover="this.style.color='var(--danger)'" onmouseout="this.style.color='var(--subtle)'">&#10005;</button>
    </div>
  </div>`;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function renderMd(s) {
  if (!s) return '';
  try {
    const renderer = new marked.Renderer();
    renderer.link = function(token) {
      const t = token.title ? ` title="${token.title}"` : '';
      return `<a href="${token.href}"${t} target="_blank" rel="noopener noreferrer">${token.text}</a>`;
    };
    return marked.parse(s, {breaks: true, renderer});
  } catch(e) {
    return esc(s);
  }
}

function selectTodo(id) {
  const idx = visibleIds.indexOf(id);
  if (idx >= 0) {
    selectedIdx = idx + 1;
    applySelection();
  }
}

// --- Context menu ---
function showCtxMenu(e, id) {
  e.preventDefault();
  e.stopPropagation();
  ctxTargetId = id;
  selectTodo(id);

  const todo = allTodos.find(t => t.id === id);
  if (!todo) return;

  const allSections = [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
  const curSection = todo.section || '';

  let sectionItems = allSections.map(s => {
    const isCur = s === curSection;
    return `<div class="ctx-menu-item${isCur ? ' active-section' : ''}" onclick="ctxMoveSection('${esc(s).replace(/'/g, "\\'")}')">` +
           `${esc(s)}${isCur ? ' &#10003;' : ''}</div>`;
  }).join('');
  sectionItems += `<div class="ctx-menu-sep"></div>`;
  sectionItems += `<div class="ctx-menu-item" onclick="ctxMoveSectionNew()">New section&hellip;</div>`;
  if (curSection) {
    sectionItems += `<div class="ctx-menu-item" onclick="ctxMoveSection('')">Remove from section</div>`;
  }

  const menu = document.getElementById('ctx-menu');

  const curPriority = todo.priority || 'medium';
  let priorityItems = ['high','medium','low'].map(p => {
    const isCur = p === curPriority;
    return `<div class="ctx-menu-item${isCur ? ' active-section' : ''}" onclick="ctxSetPriority('${p}')">${p}${isCur ? ' &#10003;' : ''}</div>`;
  }).join('');

  menu.innerHTML =
    `<div class="ctx-menu-item" onclick="ctxEdit()">&#9998; Edit</div>` +
    `<div class="ctx-menu-item has-submenu">&#128193; Move to section<div class="ctx-submenu">${sectionItems}</div></div>` +
    `<div class="ctx-menu-item has-submenu">&#9873; Set priority<div class="ctx-submenu">${priorityItems}</div></div>` +
    `<div class="ctx-menu-sep"></div>` +
    `<div class="ctx-menu-item" style="color:var(--danger)" onclick="ctxDelete()">&#128465; Delete</div>`;

  // Position: keep within viewport
  menu.classList.add('visible');
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let x = e.clientX, y = e.clientY;
  if (x + mw > window.innerWidth) x = window.innerWidth - mw - 8;
  if (y + mh > window.innerHeight) y = window.innerHeight - mh - 8;
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
}

function hideCtxMenu() {
  document.getElementById('ctx-menu').classList.remove('visible');
  ctxTargetId = null;
}

function ctxEdit() {
  const id = ctxTargetId;
  hideCtxMenu();
  if (id) startEdit(id);
}

function ctxDelete() {
  const id = ctxTargetId;
  hideCtxMenu();
  if (id) deleteTodo(id);
}

async function ctxMoveSection(section) {
  const id = ctxTargetId;
  const prevIdx = selectedIdx;
  hideCtxMenu();
  if (!id) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section})
  });
  await loadTodos();
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

function ctxMoveSectionNew() {
  hideCtxMenu();
  const name = prompt('New section name:');
  if (name !== null && name.trim()) {
    ctxTargetId && ctxMoveSection(name.trim());
  }
}

async function ctxSetPriority(priority) {
  const id = ctxTargetId;
  hideCtxMenu();
  if (!id) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({priority})
  });
  loadTodos();
}

async function sortByPriority(section) {
  await fetch(API + '/sort-priority', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section})
  });
  loadTodos();
}

// Close context menu on click outside or Escape
document.addEventListener('click', () => { hideCtxMenu(); if (sectionPickerOpen) hideSectionPicker(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('ctx-menu').classList.contains('visible')) {
    hideCtxMenu();
  }
});

function toggleSectionCollapse(section) {
  if (collapsedSections.has(section)) collapsedSections.delete(section);
  else collapsedSections.add(section);
  render();
}

function getSectionOfSelected() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return null;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return null;
  return todo.status === 'completed' ? '__completed__' : (todo.section || '');
}

function getNextCollapsedSection() {
  // Find the nearest collapsed section relative to current selection
  // Strategy: look at all sections in order and find the first collapsed one
  // at or after the selected item's position, or the last one before it
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) {
    // Nothing selected — just expand the first collapsed section
    for (const s of sectionsOrder) {
      if (collapsedSections.has(s)) return s;
    }
    if (collapsedSections.has('__completed__')) return '__completed__';
    return null;
  }
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return collapsedSections.values().next().value || null;
  const curSection = todo.status === 'completed' ? '__completed__' : (todo.section || '');

  // Look for a collapsed section immediately following the current section
  const allSecs = [...sectionsOrder, '__completed__'];
  const curIdx = allSecs.indexOf(curSection);
  // Search forward first, then backward
  for (let i = curIdx + 1; i < allSecs.length; i++) {
    if (collapsedSections.has(allSecs[i])) return allSecs[i];
  }
  for (let i = curIdx - 1; i >= 0; i--) {
    if (collapsedSections.has(allSecs[i])) return allSecs[i];
  }
  return null;
}

function allSectionsForEdit() {
  return [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
}

function getSelectedSection() {
  const sel = document.getElementById('new-section');
  if (sel.value === '__custom__') return document.getElementById('new-section-custom').value.trim();
  return sel.value;
}

function onSectionChange() {
  const sel = document.getElementById('new-section');
  const customInput = document.getElementById('new-section-custom');
  if (sel.value === '__custom__') {
    customInput.style.display = '';
    customInput.focus();
  } else {
    customInput.style.display = 'none';
    customInput.value = '';
  }
}
document.getElementById('new-section').addEventListener('change', onSectionChange);

async function addTodo() {
  const title = document.getElementById('new-title').value.trim();
  if (!title) return;
  const desc = document.getElementById('new-desc').value.trim();
  const priority = document.getElementById('new-priority').value;
  const section = getSelectedSection();
  const payload = {title, description: desc, priority, section};
  if (insertBeforeId) payload.before_id = insertBeforeId;
  await fetch(API, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  insertBeforeId = null;
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  document.getElementById('new-section-custom').value = '';
  document.getElementById('new-section-custom').style.display = 'none';
  hideAddForm();
  searchQuery = '';
  const searchEl = document.getElementById('search-input');
  searchEl.value = '';
  searchEl.classList.remove('has-query');
  loadTodos();
}

async function toggleComplete(id, checked) {
  const todo = allTodos.find(t => t.id === id);
  const newStatus = checked ? 'completed' : 'open';
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: newStatus})
  });
  loadTodos();
}

async function changePriority(id, priority) {
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({priority})
  });
  loadTodos();
}

async function deleteTodo(id) {
  if (!confirm('Delete this todo?')) return;
  await fetch(API + '/' + id, {method: 'DELETE'});
  loadTodos();
}

function navigateSection(direction) {
  // Snap to first/last in current section, or jump to adjacent section if already at edge
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return;
  const curSection = todo.status === 'completed' ? '__completed__' : (todo.section || '');

  // Build index ranges for each section in visibleIds
  const sectionRanges = []; // [{section, start, end}] (1-based indices into visibleIds)
  let prev = null;
  for (let i = 0; i < visibleIds.length; i++) {
    const t = allTodos.find(x => x.id === visibleIds[i]);
    const sec = t && t.status === 'completed' ? '__completed__' : (t ? (t.section || '') : '');
    if (sec !== prev) {
      sectionRanges.push({section: sec, start: i + 1, end: i + 1});
      prev = sec;
    } else {
      sectionRanges[sectionRanges.length - 1].end = i + 1;
    }
  }

  const rangeIdx = sectionRanges.findIndex(r => r.section === curSection && selectedIdx >= r.start && selectedIdx <= r.end);
  if (rangeIdx < 0) return;
  const range = sectionRanges[rangeIdx];

  if (direction === 'down') {
    if (selectedIdx < range.end) {
      // Snap to last in section
      selectedIdx = range.end;
    } else if (rangeIdx + 1 < sectionRanges.length) {
      // Jump to first of next section
      selectedIdx = sectionRanges[rangeIdx + 1].start;
    }
  } else {
    if (selectedIdx > range.start) {
      // Snap to first in section
      selectedIdx = range.start;
    } else if (rangeIdx - 1 >= 0) {
      // Jump to last of previous section
      selectedIdx = sectionRanges[rangeIdx - 1].end;
    }
  }
  applySelection();
}

async function moveToAdjacentSection(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const curSection = todo.section || '';
  const curIdx = sectionsOrder.indexOf(curSection);
  let newIdx = direction === 'down' ? curIdx + 1 : curIdx - 1;
  if (newIdx < 0 || newIdx >= sectionsOrder.length) return;
  const newSection = sectionsOrder[newIdx];
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section: newSection})
  });
  const prevIdx = selectedIdx;
  await loadTodos();
  // Stay at the original position (select the next item that took its place)
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

async function performUndo() {
  const res = await fetch('/api/undo', { method: 'POST' });
  if (res.ok) {
    await loadTodos();
    const toast = document.createElement('div');
    toast.innerHTML = 'Undone <span style="margin-left:8px;opacity:0.5;font-size:0.75rem">\u2318Z</span>';
    toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;z-index:2000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s';
    document.body.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; });
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 1200);
  }
}

function copyWorkon(id) {
  const text = '/ea workon ' + id;
  function showToast() {
    const el = document.querySelector(`.todo-item[data-todo-id="${id}"]`);
    if (!el) return;
    const toast = document.createElement('div');
    toast.textContent = 'Copied: ' + text;
    toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;font-family:monospace;z-index:2000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s';
    document.body.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; });
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(showToast).catch(() => { fallbackCopy(text); showToast(); });
  } else {
    fallbackCopy(text); showToast();
  }
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
}

async function bringToTop(id) {
  const res = await fetch(API + '/move-to-top', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id})
  });
  if (!res.ok) return;
  await loadTodos();
  const ni = visibleIds.indexOf(id);
  if (ni >= 0) selectedIdx = ni + 1;
  applySelection();
}

async function moveSelected(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const res = await fetch(API + '/reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, direction})
  });
  if (!res.ok) return;
  const rememberedId = id;
  await loadTodos();
  const newIdx = visibleIds.indexOf(rememberedId);
  if (newIdx >= 0) selectedIdx = newIdx + 1;
  applySelection();
}

function startEdit(id) {
  editingId = id;
  render();
  document.getElementById('edit-title-' + id)?.focus();
  setTimeout(() => {
    const editEls = document.querySelectorAll(`#edit-title-${id}, #edit-desc-${id}, #edit-priority-${id}, #edit-section-${id}, #edit-section-custom-${id}`);
    // Section dropdown: show/hide custom input
    const secSelect = document.getElementById('edit-section-' + id);
    const secCustom = document.getElementById('edit-section-custom-' + id);
    if (secSelect && secCustom) {
      secSelect.addEventListener('change', () => {
        if (secSelect.value === '__custom__') {
          secCustom.style.display = '';
          secCustom.focus();
        } else {
          secCustom.style.display = 'none';
          secCustom.value = '';
        }
      });
    }
    // Keydown: Cmd+Enter to save, Escape to cancel
    editEls.forEach(el => {
      el.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveEdit(id); }
        if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
      });
    });
    // Cancel edit when focus leaves all edit fields
    editEls.forEach(el => {
      el.addEventListener('blur', () => {
        setTimeout(() => {
          if (editingId !== id) return;
          const active = document.activeElement;
          const stillInEdit = Array.from(editEls).some(e => e === active || e.contains(active));
          const clickedBtn = active && active.closest('.edit-actions');
          if (!stillInEdit && !clickedBtn) cancelEdit();
        }, 100);
      });
    });
  }, 0);
}

function cancelEdit() {
  editingId = null;
  render();
}

async function saveEdit(id) {
  const title = document.getElementById('edit-title-' + id).value.trim();
  const desc = document.getElementById('edit-desc-' + id).value.trim();
  const priority = document.getElementById('edit-priority-' + id).value;
  const secSelect = document.getElementById('edit-section-' + id);
  const section = secSelect.value === '__custom__'
    ? document.getElementById('edit-section-custom-' + id).value.trim()
    : secSelect.value;
  if (!title) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, description: desc, priority, section})
  });
  editingId = null;
  loadTodos();
}

// Enter key to add from title field
document.getElementById('new-title').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) { e.preventDefault(); addTodo(); }
});

function showAddForm() {
  addFormVisible = true;
  const form = document.getElementById('add-form');
  form.classList.add('visible');
  // If a todo is selected, pre-fill section, set insertion point, and move form inline
  if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
    const selId = visibleIds[selectedIdx - 1];
    const selTodo = allTodos.find(t => t.id === selId);
    if (selTodo && selTodo.status !== 'completed') {
      document.getElementById('new-section').value = selTodo.section || '';
      insertBeforeId = selId;
      // Move form to appear right before the selected item
      const targetEl = document.querySelector(`.todo-item[data-todo-id="${selId}"]`);
      if (targetEl) targetEl.parentNode.insertBefore(form, targetEl);
    } else {
      insertBeforeId = null;
    }
  } else {
    insertBeforeId = null;
  }
  selectedIdx = SEL_ADD;
  applySelection();
  document.getElementById('new-title').focus();
}

function hideAddForm() {
  addFormVisible = false;
  insertBeforeId = null;
  const form = document.getElementById('add-form');
  form.classList.remove('visible');
  // Move form back to its default position (after the search bar)
  const searchBar = document.querySelector('.search-bar');
  if (searchBar) searchBar.after(form);
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  document.getElementById('new-section-custom').value = '';
  document.getElementById('new-section-custom').style.display = 'none';
  // Move selection to first todo if any
  selectedIdx = visibleIds.length > 0 ? 1 : -1;
  applySelection();
}

// selectedIdx: -1=nothing, 0=add-form, 1..N=todo items (1-indexed into visibleIds)
// ---------------------------------------------------------------------------
// Drag and drop
// ---------------------------------------------------------------------------
let dragId = null;
let dragSectionName = null; // non-null when dragging a section header

function clearAllDragIndicators() {
  document.querySelectorAll('.drag-over-top,.drag-over-bottom').forEach(el => {
    el.classList.remove('drag-over-top', 'drag-over-bottom');
  });
  document.querySelectorAll('.drag-over-section,.section-drag-over-top,.section-drag-over-bottom').forEach(el => {
    el.classList.remove('drag-over-section', 'section-drag-over-top', 'section-drag-over-bottom');
  });
}

document.addEventListener('dragstart', e => {
  // Section header drag
  const sectionRow = e.target.closest('.section-header-row[draggable]');
  if (sectionRow && !e.target.closest('.todo-item')) {
    dragSectionName = sectionRow.dataset.section;
    dragId = null;
    sectionRow.classList.add('section-dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', 'section:' + dragSectionName);
    return;
  }
  // Todo item drag
  const item = e.target.closest('.todo-item[draggable]');
  if (!item) return;
  dragId = item.dataset.todoId;
  dragSectionName = null;
  item.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', dragId);
});

document.addEventListener('dragend', e => {
  dragId = null;
  dragSectionName = null;
  document.querySelectorAll('.dragging').forEach(el => el.classList.remove('dragging'));
  document.querySelectorAll('.section-dragging').forEach(el => el.classList.remove('section-dragging'));
  clearAllDragIndicators();
});

document.addEventListener('dragover', e => {
  // --- Section header being dragged ---
  if (dragSectionName !== null) {
    const targetHeader = e.target.closest('.section-header-row[data-section]');
    if (targetHeader && targetHeader.dataset.section !== dragSectionName) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      clearAllDragIndicators();
      const rect = targetHeader.getBoundingClientRect();
      const midY = rect.top + rect.height / 2;
      if (e.clientY < midY) {
        targetHeader.classList.add('section-drag-over-top');
      } else {
        targetHeader.classList.add('section-drag-over-bottom');
      }
    }
    return;
  }

  // --- Todo item being dragged ---
  if (!dragId) return;
  const item = e.target.closest('.todo-item[data-todo-id]');
  const sectionHeader = e.target.closest('.section-header-row');

  if (item && item.dataset.todoId !== dragId) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearAllDragIndicators();
    const rect = item.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
      item.classList.add('drag-over-top');
    } else {
      item.classList.add('drag-over-bottom');
    }
  } else if (sectionHeader) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearAllDragIndicators();
    sectionHeader.classList.add('drag-over-section');
  }
});

document.addEventListener('dragleave', e => {
  const item = e.target.closest('.todo-item');
  if (item) item.classList.remove('drag-over-top', 'drag-over-bottom');
  const sectionHeader = e.target.closest('.section-header-row');
  if (sectionHeader) sectionHeader.classList.remove('drag-over-section', 'section-drag-over-top', 'section-drag-over-bottom');
});

document.addEventListener('drop', async e => {
  // --- Section header drop ---
  if (dragSectionName !== null) {
    e.preventDefault();
    clearAllDragIndicators();
    const targetHeader = e.target.closest('.section-header-row[data-section]');
    if (!targetHeader || targetHeader.dataset.section === dragSectionName) {
      dragSectionName = null;
      return;
    }
    const targetSection = targetHeader.dataset.section;
    const rect = targetHeader.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;

    // Determine where to place the dragged section
    let beforeSection;
    if (e.clientY < midY) {
      // Drop above target
      beforeSection = targetSection;
    } else {
      // Drop below target — find the section after targetSection
      const targetIdx = sectionsOrder.indexOf(targetSection);
      beforeSection = (targetIdx + 1 < sectionsOrder.length) ? sectionsOrder[targetIdx + 1] : null;
    }
    // Don't move if it would end up in the same spot
    const curIdx = sectionsOrder.indexOf(dragSectionName);
    const beforeIdx = beforeSection !== null ? sectionsOrder.indexOf(beforeSection) : sectionsOrder.length;
    if (curIdx === beforeIdx || curIdx + 1 === beforeIdx) {
      dragSectionName = null;
      return;
    }

    const payload = {section: dragSectionName};
    if (beforeSection !== null) payload.before_section = beforeSection;
    await fetch('/api/sections/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    dragSectionName = null;
    await loadTodos();
    return;
  }

  // --- Todo item drop ---
  if (!dragId) return;
  e.preventDefault();
  const item = e.target.closest('.todo-item[data-todo-id]');
  const sectionHeader = e.target.closest('.section-header-row');

  let payload = {id: dragId};

  if (item && item.dataset.todoId !== dragId) {
    const targetId = item.dataset.todoId;
    const rect = item.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
      payload.before_id = targetId;
    } else {
      const nextItem = item.nextElementSibling?.closest?.('.todo-item[data-todo-id]')
        || item.nextElementSibling;
      if (nextItem && nextItem.classList.contains('todo-item') && nextItem.dataset.todoId) {
        payload.before_id = nextItem.dataset.todoId;
      } else {
        const targetTodo = allTodos.find(t => t.id === targetId);
        payload.section = targetTodo ? (targetTodo.section || '') : '';
      }
    }
  } else if (sectionHeader) {
    const h3 = sectionHeader.querySelector('h3');
    payload.section = h3 ? h3.textContent : '';
  } else {
    return;
  }

  clearAllDragIndicators();

  await fetch(API + '/drop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  await loadTodos();
  const ni = visibleIds.indexOf(dragId);
  if (ni >= 0) selectedIdx = ni + 1;
  applySelection();
  dragId = null;
});

let _scrollAnim = null;
let _scrollTarget = null; // the element we're scrolling toward
function scrollIntoViewCentered(el) {
  const viewH = window.innerHeight;
  const pad = viewH * 0.3;
  const rect = el.getBoundingClientRect();
  if (rect.top >= pad && rect.bottom <= viewH - pad) {
    // Already in comfortable zone — cancel any animation and stop
    if (_scrollAnim) { cancelAnimationFrame(_scrollAnim); _scrollAnim = null; }
    _scrollTarget = null;
    return;
  }

  // If already animating toward this element, let it continue
  if (_scrollAnim && _scrollTarget === el) return;

  // Cancel previous animation
  if (_scrollAnim) cancelAnimationFrame(_scrollAnim);
  _scrollTarget = el;

  const duration = 180;
  const t0 = performance.now();

  function step(now) {
    const elapsed = now - t0;
    const progress = Math.min(elapsed / duration, 1);
    // Ease out cubic — fast start, gentle stop
    const eased = 1 - Math.pow(1 - progress, 3);

    // Recalculate target position every frame (tracks element through DOM changes)
    const r = _scrollTarget.getBoundingClientRect();
    const elCenter = r.top + r.height / 2;
    const targetY = window.innerHeight * 0.4;
    const remaining = elCenter - targetY;

    // Lerp: move a fraction of the remaining distance based on eased progress
    window.scrollBy(0, remaining * Math.min(eased * 0.5 + 0.15, 1));

    if (progress < 1 && Math.abs(remaining) > 1) {
      _scrollAnim = requestAnimationFrame(step);
    } else {
      _scrollAnim = null;
      _scrollTarget = null;
    }
  }
  _scrollAnim = requestAnimationFrame(step);
}

function applySelection() {
  // Clear all highlights
  document.querySelectorAll('.todo-item.kb-selected').forEach(el => el.classList.remove('kb-selected'));
  const form = document.getElementById('add-form');
  form.classList.remove('kb-selected');

  if (selectedIdx === SEL_ADD && addFormVisible) {
    form.classList.add('kb-selected');
    scrollIntoViewCentered(form);
  } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
    const todoIdx = selectedIdx - 1;
    const el = document.querySelector(`.todo-item[data-todo-id="${visibleIds[todoIdx]}"]`);
    if (el) {
      el.classList.add('kb-selected');
      scrollIntoViewCentered(el);
    }
  }
}

document.addEventListener('keydown', e => {
  // Section picker is fully handled by its own input's keydown — skip main handler
  if (sectionPickerOpen) return;

  const tag = (e.target.tagName || '').toLowerCase();

  // When inside the add-form inputs, handle Escape to close form, Cmd+Enter to add
  if (addFormVisible && (tag === 'input' || tag === 'textarea' || tag === 'select')) {
    const inAddForm = e.target.closest('#add-form');
    if (inAddForm) {
      if (e.key === 'Escape') { e.preventDefault(); hideAddForm(); return; }
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); addTodo(); return; }
      return; // Let normal typing work
    }
  }

  // Search input: handle Escape, ArrowDown/j to navigate into results
  const inSearchInput = e.target.id === 'search-input';
  if (inSearchInput) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.target.value = '';
      searchQuery = '';
      e.target.classList.remove('has-query');
      e.target.blur();
      render();
    } else if (e.key === 'ArrowDown' || (e.key === 'j' && e.ctrlKey)) {
      e.preventDefault();
      e.target.blur();
      if (visibleIds.length > 0) {
        selectedIdx = 1;
        applySelection();
      }
    }
    return; // Let normal typing work in search input
  }

  // `/` focuses the search input from anywhere (before the input guard)
  if (e.key === '/' && tag !== 'input' && tag !== 'textarea' && tag !== 'select' && !editingId) {
    e.preventDefault();
    document.getElementById('search-input').focus();
    return;
  }

  // Ignore when typing in other inputs or editing
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (editingId) return;

  // Undo: Cmd+Z / Ctrl+Z
  if ((e.metaKey || e.ctrlKey) && e.key === 'z' && !e.shiftKey) {
    e.preventDefault();
    performUndo();
    return;
  }

  const maxIdx = visibleIds.length; // 0=add-form, 1..N=todos
  const minIdx = addFormVisible ? SEL_ADD : 1;

  if (e.key === 'ArrowDown' && e.metaKey && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    navigateSection('down');
  } else if (e.key === 'ArrowUp' && e.metaKey && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    navigateSection('up');
  } else if ((e.key === 'ArrowDown' || e.key === 'j' || e.key === 'J') && e.shiftKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    moveToAdjacentSection('down');
  } else if ((e.key === 'ArrowUp' || e.key === 'k' || e.key === 'K') && e.shiftKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    moveToAdjacentSection('up');
  } else if (e.key === 'ArrowRight' && e.altKey && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    showSectionPicker();
  } else if ((e.key === 'ArrowDown' || e.key === 'j') && e.altKey) {
    e.preventDefault();
    moveSelected('down');
  } else if ((e.key === 'ArrowUp' || e.key === 'k') && e.altKey) {
    e.preventDefault();
    moveSelected('up');
  } else if (e.key === 'ArrowDown' || e.key === 'j') {
    e.preventDefault();
    if (visibleIds.length === 0 && !addFormVisible) return;
    if (selectedIdx < 0) {
      selectedIdx = minIdx;
    } else {
      selectedIdx = Math.min(selectedIdx + 1, maxIdx);
    }
    applySelection();
  } else if (e.key === 'ArrowUp' || e.key === 'k') {
    e.preventDefault();
    if (visibleIds.length === 0 && !addFormVisible) return;
    selectedIdx = Math.max(selectedIdx - 1, minIdx);
    applySelection();
  } else if (e.key === ' ') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      const id = visibleIds[selectedIdx - 1];
      const todo = allTodos.find(t => t.id === id);
      if (todo) toggleComplete(id, todo.status !== 'completed');
    }
  } else if (e.key === 'e') {
    e.preventDefault();
    if (selectedIdx === SEL_ADD && addFormVisible) {
      document.getElementById('new-title').focus();
    } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      startEdit(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'c') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      copyWorkon(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === '1' || e.key === '2' || e.key === '3') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      const pMap = {'1': 'high', '2': 'medium', '3': 'low'};
      changePriority(visibleIds[selectedIdx - 1], pMap[e.key]);
    }
  } else if (e.key === 't') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      bringToTop(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'Backspace' && e.metaKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      deleteTodo(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'ArrowLeft' && !e.metaKey && !e.altKey && !e.shiftKey) {
    // Collapse section of selected item
    const sec = getSectionOfSelected();
    if (sec !== null && sec !== '' && !collapsedSections.has(sec)) {
      e.preventDefault();
      collapsedSections.add(sec);
      render();
    }
  } else if (e.key === 'ArrowRight' && !e.metaKey && !e.altKey && !e.shiftKey) {
    // Expand nearest collapsed section above/at current position
    if (collapsedSections.size > 0) {
      e.preventDefault();
      // Find which collapsed section the selection is adjacent to
      const sec = getNextCollapsedSection();
      if (sec) {
        collapsedSections.delete(sec);
        render();
      }
    }
  } else if (e.key === 'n') {
    e.preventDefault();
    showAddForm();
  } else if (e.key === 'Escape') {
    e.preventDefault();
    if (addFormVisible) { hideAddForm(); }
    else if (searchQuery.trim().length > 0) {
      searchQuery = '';
      const searchEl = document.getElementById('search-input');
      searchEl.value = '';
      searchEl.classList.remove('has-query');
      render();
    }
    else { selectedIdx = -1; applySelection(); }
  }
});

// --- Search input ---
document.getElementById('search-input').addEventListener('input', e => {
  searchQuery = e.target.value;
  e.target.classList.toggle('has-query', searchQuery.trim().length > 0);
  selectedIdx = -1;
  render();
});

// --- Section picker (Opt+Right) ---
let sectionPickerOpen = false;
let sectionPickerIdx = 0;
let spAllItems = [];      // full unfiltered list [{name, label, isCurrent}]
let spFilteredItems = [];  // after fuzzy filter
let sectionPickerTodoId = null;

function spFuzzyMatch(query, text) {
  // Simple fuzzy: every char of query appears in order in text (case-insensitive)
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length;
}

function spFuzzyScore(query, text) {
  // Lower = better. Prioritize: exact prefix > substring > fuzzy
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (t.startsWith(q)) return 0;
  if (t.includes(q)) return 1;
  return 2;
}

function showSectionPicker() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const curSection = todo.section || '';

  const allSections = [];
  const seen = new Set();
  allTodos.forEach(t => {
    const s = t.section || '';
    if (s && !seen.has(s)) { allSections.push(s); seen.add(s); }
  });
  spAllItems = [{name: '', label: '(No section)', isCurrent: curSection === ''}];
  allSections.forEach(s => {
    spAllItems.push({name: s, label: s, isCurrent: s === curSection});
  });

  sectionPickerTodoId = id;
  sectionPickerOpen = true;
  spFilteredItems = spAllItems.filter(x => !x.isCurrent);
  sectionPickerIdx = 0;

  renderSectionPicker();

  // Position near the selected todo item
  const el = document.querySelector(`.todo-item[data-todo-id="${id}"]`);
  const picker = document.getElementById('section-picker');
  if (el) {
    const rect = el.getBoundingClientRect();
    let x = rect.right - 240;
    let y = rect.top + rect.height + 4;
    if (x < 8) x = 8;
    if (y + 220 > window.innerHeight) y = rect.top - picker.offsetHeight - 4;
    picker.style.left = x + 'px';
    picker.style.top = y + 'px';
  }

  // Focus input after render
  setTimeout(() => {
    const inp = document.getElementById('sp-input');
    if (inp) inp.focus();
  }, 0);
}

function renderSectionPicker() {
  const picker = document.getElementById('section-picker');
  const inputVal = document.getElementById('sp-input')?.value || '';

  let itemsHtml = '';
  if (spFilteredItems.length === 0 && inputVal.trim()) {
    itemsHtml = `<div class="section-picker-item sp-selected" onmousedown="spCommitNew()">Create &ldquo;${esc(inputVal.trim())}&rdquo;</div>`;
  } else {
    itemsHtml = spFilteredItems.map((item, i) => {
      const cls = ['section-picker-item'];
      if (i === sectionPickerIdx) cls.push('sp-selected');
      return `<div class="${cls.join(' ')}" data-sp-idx="${i}" onmousedown="spCommitIdx(${i})">${esc(item.label)}</div>`;
    }).join('');
  }

  picker.innerHTML =
    `<input type="text" class="section-picker-input" id="sp-input" placeholder="Search or create section..." autocomplete="off" value="${esc(inputVal)}">`
    + itemsHtml;
  picker.classList.add('visible');

  // Restore cursor position and set up events
  const input = document.getElementById('sp-input');
  input.setSelectionRange(inputVal.length, inputVal.length);
  input.addEventListener('input', spOnInput);
  input.addEventListener('keydown', spOnKeydown);
}

function spOnInput(e) {
  const query = e.target.value.trim();
  if (!query) {
    spFilteredItems = spAllItems.filter(x => !x.isCurrent);
  } else {
    spFilteredItems = spAllItems
      .filter(x => !x.isCurrent && spFuzzyMatch(query, x.label))
      .sort((a, b) => spFuzzyScore(query, a.label) - spFuzzyScore(query, b.label));
  }
  sectionPickerIdx = 0;
  spUpdateItems();
}

function spUpdateItems() {
  // Re-render just the items, not the input (preserves focus/cursor)
  const picker = document.getElementById('section-picker');
  const input = document.getElementById('sp-input');
  const inputVal = input?.value || '';

  // Remove old items (everything after the input)
  while (picker.lastChild && picker.lastChild !== input) {
    picker.removeChild(picker.lastChild);
  }

  if (spFilteredItems.length === 0 && inputVal.trim()) {
    const div = document.createElement('div');
    div.className = 'section-picker-item sp-selected';
    div.innerHTML = `Create &ldquo;${esc(inputVal.trim())}&rdquo;`;
    div.onmousedown = () => spCommitNew();
    picker.appendChild(div);
  } else {
    spFilteredItems.forEach((item, i) => {
      const div = document.createElement('div');
      div.className = 'section-picker-item' + (i === sectionPickerIdx ? ' sp-selected' : '');
      div.textContent = item.label;
      div.onmousedown = () => spCommitIdx(i);
      picker.appendChild(div);
    });
  }
}

function spOnKeydown(e) {
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (spFilteredItems.length > 0) {
      sectionPickerIdx = Math.min(sectionPickerIdx + 1, spFilteredItems.length - 1);
      spUpdateItems();
    }
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (spFilteredItems.length > 0) {
      sectionPickerIdx = Math.max(sectionPickerIdx - 1, 0);
      spUpdateItems();
    }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const inputVal = e.target.value.trim();
    if (spFilteredItems.length > 0) {
      spCommitIdx(sectionPickerIdx);
    } else if (inputVal) {
      spCommitNew();
    } else {
      hideSectionPicker();
    }
  } else if (e.key === 'Escape') {
    e.preventDefault();
    hideSectionPicker();
  }
  e.stopPropagation();
}

function hideSectionPicker() {
  sectionPickerOpen = false;
  sectionPickerTodoId = null;
  document.getElementById('section-picker').classList.remove('visible');
}

async function spMoveTo(sectionName) {
  const id = sectionPickerTodoId;
  const prevIdx = selectedIdx;
  hideSectionPicker();
  if (!id) return;

  await fetch(API + '/drop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, section: sectionName})
  });
  await fetch(API + '/move-to-top', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id})
  });

  await loadTodos();
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

function spCommitIdx(idx) {
  const item = spFilteredItems[idx];
  if (!item) { hideSectionPicker(); return; }
  spMoveTo(item.name);
}

function spCommitNew() {
  const input = document.getElementById('sp-input');
  const name = input?.value.trim();
  if (name) spMoveTo(name);
  else hideSectionPicker();
}

// --- Section rename ---
let renamingSection = null;

function startSectionRename(sectionName) {
  renamingSection = sectionName;
  const row = document.querySelector(`.section-header-row[data-section="${CSS.escape(sectionName)}"]`);
  if (!row) return;
  const h3 = row.querySelector('h3');
  if (!h3) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'section-rename-input';
  input.value = sectionName;
  h3.replaceWith(input);
  input.focus();
  input.select();

  function commit() {
    const newName = input.value.trim();
    if (newName && newName !== sectionName) {
      saveSectionRename(sectionName, newName);
    } else {
      renamingSection = null;
      render();
    }
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.preventDefault(); renamingSection = null; render(); }
  });
  input.addEventListener('blur', () => {
    setTimeout(() => { if (renamingSection === sectionName) commit(); }, 100);
  });
}

async function saveSectionRename(oldName, newName) {
  renamingSection = null;
  await fetch('/api/sections/rename', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_name: oldName, new_name: newName})
  });
  // Update collapsed sections set if the renamed section was collapsed
  if (collapsedSections.has(oldName)) {
    collapsedSections.delete(oldName);
    collapsedSections.add(newName);
  }
  loadTodos();
}

loadTodos();
startPolling();
</script>

</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Simple Todo App")
    parser.add_argument("todo_file", nargs="?", default="todos.md", help="Path to the todo markdown file")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5111, help="Port to listen on")
    args = parser.parse_args()

    TODO_FILE = args.todo_file

    # Create file if it doesn't exist
    if not os.path.exists(TODO_FILE):
        _write_todo_file(TODO_FILE, [])
        print(f"Created new todo file: {TODO_FILE}")

    print(f"Serving todo UI for: {os.path.abspath(TODO_FILE)}")
    print(f"Open http://{args.host}:{args.port} in your browser")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=True)
