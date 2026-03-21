"""System prompt construction for the chat agent.

Assembles the system prompt from config, context files, and runtime state.
"""

import os
from datetime import datetime

from state import _USE_DB, TODO_FILE
from services.mcp_utils import _get_mcp_tools
import db as _db


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


def _build_system_prompt(todo_id: str | None, user_id: str | None = None) -> str:
    """Build a system prompt from per-user DB config or file-based fallback."""
    parts = []

    if _USE_DB and user_id:
        config = _db.get_config(user_id)
        # 1. Base system prompt from DB
        sp = config.get("system_prompt")
        if sp and sp.strip():
            parts.append(sp.strip())
        else:
            parts.append(_DEFAULT_SYSTEM_PROMPT.strip())
        # 2. Context files from user_context_files table
        ctx = _db.get_context_files(user_id)
        for name in sorted(ctx.keys()):
            content = ctx[name]
            if content and content.strip():
                parts.append(f"# {name}\n{content.strip()}")
    else:
        # File-based fallback
        from services.file_io import _config_dir_path
        config_dir = _config_dir_path()
        # 1. Base system prompt
        prompt_path = os.path.join(config_dir, "system-prompt.md")
        if os.path.exists(prompt_path):
            try:
                with open(prompt_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append(content)
            except OSError:
                pass
        if not parts:
            parts.append(_DEFAULT_SYSTEM_PROMPT.strip())
        # 2. Context files from disk
        context_dir = os.path.join(config_dir, "context")
        if os.path.isdir(context_dir):
            for name in sorted(os.listdir(context_dir)):
                if name.endswith(".md"):
                    fp = os.path.join(context_dir, name)
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                        if content:
                            parts.append(f"# {name}\n{content}")
                    except OSError:
                        pass

    # 3. Global context files (shared across all users)
    global_ctx_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "context")
    if os.path.isdir(global_ctx_dir):
        for name in sorted(os.listdir(global_ctx_dir)):
            if name.endswith(".md"):
                try:
                    with open(os.path.join(global_ctx_dir, name), "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    if content:
                        parts.append(f"# {name}\n{content}")
                except OSError:
                    pass

    # 4. Current date
    parts.append(f"Today's date is {datetime.now().strftime('%Y-%m-%d')}.")

    # 4. Todo context
    if todo_id:
        parts.append(f"Current conversation is for todo item ID: {todo_id}")

    return "\n\n---\n\n".join(parts)
