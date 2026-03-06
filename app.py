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
from datetime import datetime
from flask import Flask, request, jsonify, Response

app = Flask(__name__)
TODO_FILE = "todos.md"


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

    todos.append(new_todo)
    _write_todo_file(TODO_FILE, todos)
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
            _write_todo_file(TODO_FILE, todos)
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
    _write_todo_file(TODO_FILE, new_todos)
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

    _write_todo_file(TODO_FILE, active + completed)
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

    _write_todo_file(TODO_FILE, active + completed)
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

    _write_todo_file(TODO_FILE, rebuilt + completed)
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
  :root {
    --bg: #f5f5f5; --card: #fff; --border: #ddd; --text: #222;
    --muted: #666; --accent: #2563eb; --accent-hover: #1d4ed8;
    --completed-bg: #f0fdf4; --completed-border: #86efac; --completed-text: #166534;
    --danger: #dc2626; --danger-hover: #b91c1c;
    --radius: 8px; --shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
    max-width: 700px; margin: 0 auto; padding: 24px 16px;
  }
  h1 { font-size: 1.5rem; margin-bottom: 20px; }
  h2 { font-size: 1.1rem; color: var(--muted); margin: 24px 0 12px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }

  /* Add form */
  .add-form {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 16px; margin-bottom: 24px; box-shadow: var(--shadow);
    display: none;
  }
  .add-form.visible { display: block; }
  .add-form.kb-selected { outline: 2px solid var(--accent); outline-offset: -2px; }
  .add-toggle {
    display: flex; align-items: center; gap: 8px; margin-bottom: 16px;
  }
  .add-toggle .btn { font-size: 0.9rem; padding: 8px 20px; }
  .add-form input, .add-form textarea, .add-form select {
    width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px;
    font-size: 0.9rem; font-family: inherit; margin-bottom: 8px; background: #fafafa;
  }
  .add-form textarea { resize: vertical; min-height: 50px; }
  .add-form .row { display: flex; gap: 8px; align-items: center; }
  .add-form select { width: auto; margin-bottom: 0; }
  .btn {
    padding: 8px 16px; border: none; border-radius: 6px; font-size: 0.85rem;
    font-weight: 600; cursor: pointer; transition: background 0.15s;
  }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); padding: 4px 10px; font-size: 0.75rem; }
  .btn-danger:hover { background: var(--danger); color: #fff; }
  .btn-sm { padding: 4px 10px; font-size: 0.75rem; }

  /* Todo items */
  .todo-item {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 12px 16px; margin-bottom: 8px; box-shadow: var(--shadow);
    display: flex; align-items: flex-start; gap: 12px; transition: opacity 0.2s;
    border-left: 4px solid var(--border);
  }
  .todo-item.status-completed { border-left-color: var(--completed-border); background: var(--completed-bg); opacity: 0.7; }

  .todo-checkbox { margin-top: 3px; width: 18px; height: 18px; cursor: pointer; accent-color: var(--accent); flex-shrink: 0; }
  .todo-body { flex: 1; min-width: 0; }
  .todo-title { font-weight: 600; font-size: 0.95rem; word-break: break-word; }
  .todo-desc { color: var(--muted); font-size: 0.85rem; margin-top: 4px; word-break: break-word; }
  .todo-desc p { margin: 0 0 0.4em; }
  .todo-desc p:last-child { margin-bottom: 0; }
  .todo-desc ul, .todo-desc ol { margin: 0.2em 0 0.4em 1.2em; padding: 0; }
  .todo-desc li { margin: 0.1em 0; }
  .todo-desc code { background: rgba(0,0,0,0.06); padding: 1px 4px; border-radius: 3px; font-size: 0.85em; }
  .todo-desc pre { background: rgba(0,0,0,0.04); padding: 8px; border-radius: 4px; overflow-x: auto; margin: 0.3em 0; }
  .todo-desc pre code { background: none; padding: 0; }
  .todo-desc a { color: var(--accent); }
  .todo-desc h1, .todo-desc h2, .todo-desc h3 { font-size: 0.9em; margin: 0.4em 0 0.2em; }
  .todo-desc blockquote { border-left: 3px solid var(--border); margin: 0.3em 0; padding-left: 8px; color: var(--muted); }
  .todo-meta { display: flex; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .priority-badge {
    font-size: 0.65rem; font-weight: 700; text-transform: uppercase; padding: 1px 6px;
    border-radius: 10px; letter-spacing: 0.04em; flex-shrink: 0; align-self: flex-start; margin-top: 3px;
  }
  .priority-high { background: #fef2f2; color: #991b1b; border: 1px solid #fca5a5; }
  .priority-medium { background: #fffbeb; color: #92400e; border: 1px solid #fcd34d; }
  .priority-low { background: #f0fdf4; color: #166534; border: 1px solid #86efac; }

  .section-header-row {
    display: flex; align-items: center; gap: 8px;
    margin: 18px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border);
  }
  .section-header-row h3 { font-size: 0.95rem; color: var(--text); font-weight: 600; margin: 0; }
  .sort-priority-btn {
    font-size: 0.7rem; padding: 2px 8px; border-radius: 10px;
    border: 1px solid var(--border); background: #fafafa; color: var(--muted);
    cursor: pointer; white-space: nowrap; transition: background 0.15s;
  }
  .sort-priority-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }

  .todo-actions { display: flex; gap: 4px; flex-shrink: 0; align-items: flex-start; }
  .todo-actions select { font-size: 0.75rem; padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border); background: #fafafa; }

  .empty-state { text-align: center; color: var(--muted); padding: 40px 0; font-size: 0.95rem; }

  /* Edit mode */
  .edit-title { font-size: 0.95rem; font-weight: 600; width: 100%; padding: 4px 8px; border: 1px solid var(--accent); border-radius: 4px; margin-bottom: 4px; }
  .edit-desc { font-size: 0.85rem; width: 100%; padding: 4px 8px; border: 1px solid var(--accent); border-radius: 4px; resize: vertical; min-height: 40px; font-family: inherit; }
  .edit-actions { display: flex; gap: 4px; margin-top: 6px; }

  /* Section headers */
  .section-header {
    font-size: 0.95rem; color: var(--text); margin: 18px 0 8px; font-weight: 600;
    padding-bottom: 4px; border-bottom: 1px solid var(--border);
    display: none;
  }

  /* Keyboard-selected item */
  .todo-item.kb-selected { outline: 2px solid var(--accent); outline-offset: -2px; }

  /* Context menu */
  .ctx-menu {
    display: none; position: fixed; z-index: 1000;
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.18); min-width: 180px;
    padding: 4px 0; font-size: 0.85rem;
  }
  .ctx-menu.visible { display: block; }
  .ctx-menu-item {
    padding: 7px 14px; cursor: pointer; display: flex; align-items: center; gap: 8px;
    color: var(--text); user-select: none; position: relative;
  }
  .ctx-menu-item:hover { background: var(--accent); color: #fff; }
  .ctx-menu-item.has-submenu::after {
    content: '\25B6'; font-size: 0.65rem; margin-left: auto; opacity: 0.6;
  }
  .ctx-menu-item:hover.has-submenu::after { opacity: 1; }
  .ctx-menu-sep { border-top: 1px solid var(--border); margin: 3px 0; }
  .ctx-submenu {
    display: none; position: absolute; left: 100%; top: -4px;
    background: var(--card); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.18); min-width: 160px;
    padding: 4px 0;
  }
  .ctx-menu-item:hover > .ctx-submenu { display: block; }
  .ctx-submenu .ctx-menu-item { padding: 6px 14px; font-size: 0.83rem; }
  .ctx-submenu .ctx-menu-item.active-section { font-weight: 700; opacity: 0.5; pointer-events: none; }
</style>
</head>
<body>

<h1>&#9744; Todo List</h1>

<div class="add-toggle">
  <button class="btn btn-primary" id="add-toggle-btn" onclick="showAddForm()">+ New Todo</button>
  <span style="color:var(--muted);font-size:0.8rem">or press <kbd style="padding:1px 5px;border:1px solid var(--border);border-radius:3px;background:#fff;font-size:0.8rem">n</kbd></span>
</div>

<div class="add-form" id="add-form">
  <input type="text" id="new-title" placeholder="What needs to be done?">
  <textarea id="new-desc" placeholder="Description (optional)"></textarea>
  <input type="text" id="new-section" placeholder="Section (optional)" list="section-suggestions" style="font-size:0.85rem;">
  <datalist id="section-suggestions"></datalist>
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

<script>
const API = '/api/todos';
let allTodos = [];
let editingId = null;
let lastMtime = 0;
let pollTimer = null;
let selectedIdx = -1; // -1 = nothing, 0 = add-form, 1+ = todo items
let ctxTargetId = null; // id of todo targeted by context menu
let visibleIds = []; // ordered list of todo ids as rendered
let sectionsOrder = []; // ordered list of section names as rendered
let addFormVisible = false;
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

  if (active.length === 0 && completed.length === 0) {
    activeEl.innerHTML = '<div class="empty-state">No todos yet. Press <strong>n</strong> to add one!</div>';
    completedEl.innerHTML = '';
    applySelection();
    return;
  }

  visibleIds = [...active, ...completed].map(t => t.id);

  // Group active by section preserving order of first appearance
  sectionsOrder = [];
  const seenSections = new Set();
  active.forEach(t => {
    const s = t.section || '';
    if (!seenSections.has(s)) { sectionsOrder.push(s); seenSections.add(s); }
  });

  let activeHtml = '';
  sectionsOrder.forEach(section => {
    if (section) {
      activeHtml += `<div class="section-header-row"><h3>${esc(section)}</h3><button class="sort-priority-btn" onclick="sortByPriority('${esc(section).replace(/'/g, "\\\'")}')" title="Sort by priority (high first)">&#9650; Priority</button></div>`;
    }
    const items = active.filter(t => (t.section || '') === section);
    activeHtml += items.map(t => renderTodo(t)).join('');
  });

  activeEl.innerHTML = active.length
    ? '<h2>Active (' + active.length + ')</h2>' + activeHtml
    : '<h2>Active</h2><div class="empty-state">All done! &#127881;</div>';

  completedEl.innerHTML = completed.length
    ? '<h2>Completed (' + completed.length + ')</h2>' + completed.map(t => renderTodo(t)).join('')
    : '';

  // Update section suggestions datalist
  const allSections = [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
  const dl = document.getElementById('section-suggestions');
  if (dl) dl.innerHTML = allSections.map(s => `<option value="${esc(s)}">`).join('');

  applySelection();
}

function renderTodo(t) {
  const checked = t.status === 'completed' ? 'checked' : '';
  const statusClass = t.status === 'completed' ? 'status-completed' : '';

  if (editingId === t.id) {
    return `<div class="todo-item ${statusClass}">
      <div class="todo-body">
        <input class="edit-title" id="edit-title-${t.id}" value="${esc(t.title)}">
        <textarea class="edit-desc" id="edit-desc-${t.id}">${esc(t.description)}</textarea>
        <input class="edit-title" id="edit-section-${t.id}" value="${esc(t.section || '')}" placeholder="Section" list="section-suggestions" style="font-size:0.85rem; font-weight:400; margin-bottom:4px;">
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

  return `<div class="todo-item ${statusClass}" data-todo-id="${t.id}" onclick="selectTodo('${t.id}')" oncontextmenu="showCtxMenu(event,'${t.id}')" style="cursor:pointer;">
    <input type="checkbox" class="todo-checkbox" ${checked} onchange="toggleComplete('${t.id}', this.checked)" onclick="event.stopPropagation()">
    <div class="todo-body" ondblclick="startEdit('${t.id}')">
      <div class="todo-title">${esc(t.title)}</div>
      ${desc}
    </div>
    ${priorityBadge}
    <div class="todo-actions">
      ${t.status !== 'completed' ? `<button class="btn btn-sm" onclick="event.stopPropagation();bringToTop('${t.id}')" style="border:none;background:transparent;font-size:1rem;padding:2px 4px;cursor:pointer;opacity:0.4;line-height:1" title="Bring to top of section" onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.4'">&#x2912;</button>` : ''}
      <button onclick="event.stopPropagation();deleteTodo('${t.id}')" style="border:none;background:transparent;font-size:0.85rem;padding:2px 6px;cursor:pointer;opacity:0.35;line-height:1;color:var(--muted);font-weight:600" title="Delete" onmouseover="this.style.opacity='1';this.style.color='var(--danger)'" onmouseout="this.style.opacity='0.35';this.style.color='var(--muted)'">&#10005;</button>
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
    return marked.parse(s, {breaks: true});
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
  hideCtxMenu();
  if (!id) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section})
  });
  loadTodos();
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
document.addEventListener('click', () => hideCtxMenu());
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('ctx-menu').classList.contains('visible')) {
    hideCtxMenu();
  }
});

async function addTodo() {
  const title = document.getElementById('new-title').value.trim();
  if (!title) return;
  const desc = document.getElementById('new-desc').value.trim();
  const priority = document.getElementById('new-priority').value;
  const section = document.getElementById('new-section').value.trim();
  await fetch(API, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, description: desc, priority, section})
  });
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  hideAddForm();
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
  const rememberedId = id;
  await loadTodos();
  const ni = visibleIds.indexOf(rememberedId);
  if (ni >= 0) selectedIdx = ni + 1;
  applySelection();
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
    const editEls = document.querySelectorAll(`#edit-title-${id}, #edit-desc-${id}, #edit-priority-${id}, #edit-section-${id}`);
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
  const section = document.getElementById('edit-section-' + id).value.trim();
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
  selectedIdx = SEL_ADD;
  applySelection();
  document.getElementById('new-title').focus();
}

function hideAddForm() {
  addFormVisible = false;
  const form = document.getElementById('add-form');
  form.classList.remove('visible');
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  // Move selection to first todo if any
  selectedIdx = visibleIds.length > 0 ? 1 : -1;
  applySelection();
}

// selectedIdx: -1=nothing, 0=add-form, 1..N=todo items (1-indexed into visibleIds)
function applySelection() {
  // Clear all highlights
  document.querySelectorAll('.todo-item.kb-selected').forEach(el => el.classList.remove('kb-selected'));
  const form = document.getElementById('add-form');
  form.classList.remove('kb-selected');

  if (selectedIdx === SEL_ADD && addFormVisible) {
    form.classList.add('kb-selected');
    form.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
    const todoIdx = selectedIdx - 1;
    const el = document.querySelector(`.todo-item[data-todo-id="${visibleIds[todoIdx]}"]`);
    if (el) {
      el.classList.add('kb-selected');
      el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
  }
}

document.addEventListener('keydown', e => {
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

  // Ignore when typing in other inputs or editing
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (editingId) return;

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
  } else if (e.key === 'n') {
    e.preventDefault();
    showAddForm();
  } else if (e.key === 'Escape') {
    e.preventDefault();
    if (addFormVisible) { hideAddForm(); }
    else { selectedIdx = -1; applySelection(); }
  }
});

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
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (use 0.0.0.0 for network access)")
    parser.add_argument("--port", type=int, default=5111, help="Port to listen on")
    args = parser.parse_args()

    TODO_FILE = args.todo_file

    # Create file if it doesn't exist
    if not os.path.exists(TODO_FILE):
        _write_todo_file(TODO_FILE, [])
        print(f"Created new todo file: {TODO_FILE}")

    print(f"Serving todo UI for: {os.path.abspath(TODO_FILE)}")
    print(f"Open http://{args.host}:{args.port} in your browser")
    app.run(host=args.host, port=args.port, debug=True)
