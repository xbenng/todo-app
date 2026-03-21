"""System prompt construction for the chat agent.

Assembles the system prompt from config, context files, and runtime state.
Context files are filtered by command — each EA command only gets the
context it needs (rules + its specific step).
"""

import os
from datetime import datetime

import state
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

# Map command prefixes to the context files they need.
# ea-rules.md is always included for any EA command.
# Non-EA commands (regular chat) get all context files.
_COMMAND_CONTEXT_MAP = {
    "/ea update": ["ea-rules.md", "ea-update.md"],
    "/ea workon": ["ea-rules.md", "ea-workon.md"],
    "/ea checkon": ["ea-rules.md", "ea-checkon.md"],
    "/ea consolidate": ["ea-rules.md", "ea-checkon.md"],
    "/ea sync": ["ea-rules.md"],
    "/ea triage": ["ea-rules.md"],
}


def _filter_context_files(ctx: dict[str, str], message: str | None) -> dict[str, str]:
    """Filter context files based on the command in the message.

    For EA commands, only include the relevant step file + shared rules.
    For regular chat, include everything.
    """
    if not message:
        return ctx

    # Check if message matches any EA command prefix
    for prefix, allowed_files in _COMMAND_CONTEXT_MAP.items():
        if message.startswith(prefix):
            return {name: content for name, content in ctx.items()
                    if name in allowed_files or not name.startswith("ea-")}

    # Regular chat — include everything
    return ctx


def _build_system_prompt(todo_id: str | None, user_id: str | None = None,
                         message: str | None = None) -> str:
    """Build a system prompt from per-user DB config, filtered by command."""
    parts = []

    if user_id:
        config = _db.get_config(user_id)
        # 1. Base system prompt from DB
        sp = config.get("system_prompt")
        if sp and sp.strip():
            parts.append(sp.strip())
        else:
            parts.append(_DEFAULT_SYSTEM_PROMPT.strip())
        # 2. Context files from user_context_files table (filtered by command)
        ctx = _db.get_context_files(user_id)
        ctx = _filter_context_files(ctx, message)
        for name in sorted(ctx.keys()):
            content = ctx[name]
            if content and content.strip():
                parts.append(f"# {name}\n{content.strip()}")
    else:
        parts.append(_DEFAULT_SYSTEM_PROMPT.strip())

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

    # 5. Todo context
    if todo_id:
        parts.append(f"Current conversation is for todo item ID: {todo_id}")

    return "\n\n---\n\n".join(parts)
