"""File I/O helpers — config, chat persistence, and todo file parsing/writing."""

import copy
import json
import os
import re
import shutil
import uuid

import state


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _completed_file_path(path: str) -> str:
    """Derive the completed-todos file path from the main todo file path.
    e.g. todos.md -> todos-completed.md
    """
    base, ext = os.path.splitext(path)
    return f"{base}-completed{ext}"


def _chats_file_path(path: str) -> str:
    """Derive the chats file path from the main todo file path.
    e.g. todos.md -> todos-chats.json
    """
    base, _ = os.path.splitext(path)
    return f"{base}-chats.json"


# ---------------------------------------------------------------------------
# Chat persistence
# ---------------------------------------------------------------------------

def _load_chats() -> dict:
    """Load chat sessions from disk."""
    path = _chats_file_path(state.TODO_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_chats(data: dict) -> None:
    """Save chat sessions to disk."""
    path = _chats_file_path(state.TODO_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Config directory and config loading
# ---------------------------------------------------------------------------

_DEFAULT_SYSTEM_PROMPT = """\
You are a helpful assistant managing a todo list and connected services.

## Built-in Tools
- **read_todos** — read all todos (with optional status filter: all/open/completed)
- **update_todo** — update a todo's title, description, status, priority, or section
- **create_todo** — create a new todo item
- **search_todos** — search todos by text query
- **read_chat_history** — read chat history for a specific todo
- **spawn_agents** — launch multiple subagents in parallel for independent tasks

## MCP Tools
You also have access to MCP (Model Context Protocol) tools for connected services like Slack, Email, Calendar, Smartsheet, and others. These are prefixed with `mcp__{server}__` (e.g., `mcp__slack__channels_list`). Use them when the user asks about their communications, calendar, or other connected data.

## Guidelines
- Use tools proactively — don't ask the user to check things they asked you about.
- Be concise. Use bullets, not paragraphs.
- When referencing information from external sources, include links where possible.
"""

# Source files to seed into the config context/ directory on first run.
_CONTEXT_SEED_FILES = [
    ("claude.md", "~/.claude/CLAUDE.md"),
    ("communication-style.md", "~/.claude/communication-style.md"),
    ("org.md", "~/.claude/org.md"),
]
_MEMORY_INDEX_PATH = ""  # Disabled — memory context is not seeded


def _config_dir_path() -> str:
    """Return the config directory path derived from the todo file."""
    base, _ = os.path.splitext(state.TODO_FILE)
    return f"{base}-config"


def _ensure_config_dir() -> str:
    """Create config dir structure. Migrate from flat file if needed. Return dir path."""
    config_dir = _config_dir_path()
    context_dir = os.path.join(config_dir, "context")
    os.makedirs(context_dir, exist_ok=True)
    os.makedirs(os.path.join(config_dir, "users"), exist_ok=True)

    # Migrate from old flat config file
    old_flat = f"{os.path.splitext(state.TODO_FILE)[0]}-config.json"
    new_json = os.path.join(config_dir, "config.json")
    if os.path.exists(old_flat) and not os.path.exists(new_json):
        shutil.move(old_flat, new_json)

    # Seed system-prompt.md if missing
    prompt_path = os.path.join(config_dir, "system-prompt.md")
    if not os.path.exists(prompt_path):
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_SYSTEM_PROMPT)

    # Seed context files from ~/.claude/ if context dir is empty
    if not any(f.endswith(".md") for f in os.listdir(context_dir)):
        for dest_name, src_path in _CONTEXT_SEED_FILES:
            src = os.path.expanduser(src_path)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(context_dir, dest_name))
                except OSError:
                    pass
        # Assemble memory from MEMORY.md + referenced files
        _seed_memory_context(context_dir)

    return config_dir


def _seed_memory_context(context_dir: str) -> None:
    """Read MEMORY.md index, resolve relative .md links, assemble into context/memory.md."""
    index_path = os.path.expanduser(_MEMORY_INDEX_PATH)
    if not os.path.exists(index_path):
        return
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_content = f.read()
        parts = [index_content.strip()]
        # Resolve relative .md links: [name](file.md)
        memory_dir = os.path.dirname(index_path)
        for match in re.finditer(r'\[.*?\]\(([^)]+\.md)\)', index_content):
            ref_path = os.path.join(memory_dir, match.group(1))
            if os.path.exists(ref_path):
                with open(ref_path, "r", encoding="utf-8") as rf:
                    parts.append(rf.read().strip())
        with open(os.path.join(context_dir, "memory.md"), "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(parts))
    except OSError:
        pass


def _load_config() -> dict:
    """Load server config from disk."""
    path = os.path.join(_config_dir_path(), "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_mcp_registry() -> dict:
    """Load MCP server registry from mcp-servers/registry.json."""
    registry_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "mcp-servers", "registry.json")
    if not os.path.exists(registry_path):
        return {}
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_config(data: dict) -> None:
    """Save server config to disk."""
    _ensure_config_dir()
    path = os.path.join(_config_dir_path(), "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# File format parser / writer
# ---------------------------------------------------------------------------

VALID_PRIORITIES = {"low", "medium", "high", "none"}
DEFAULT_PRIORITY = "medium"
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3}


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
            priority_match = re.search(r"\{(high|medium|low|none)\}", first_line, re.IGNORECASE)
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
    state._undo_stack.append((copy.deepcopy(old_active), copy.deepcopy(old_completed)))
    _write_todo_file(path, todos)
