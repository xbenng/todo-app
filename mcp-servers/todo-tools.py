#!/usr/bin/env python3
"""MCP stdio server that proxies todo tools through the unified execute-tool endpoint.

All calls route through _execute_tool on the server with as_agent=True,
so writes correctly mark items as unread.
"""
import json
import os
import urllib.request
import urllib.error
import urllib.parse

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("todo-tools")

API_BASE = os.environ.get("TODO_API_BASE", "http://localhost:5222")
AUTH_TOKEN = os.environ.get("TODO_AUTH_TOKEN", "")


def _call_tool(tool_name: str, tool_input: dict) -> str:
    """Call a tool via the unified execute-tool endpoint."""
    url = f"{API_BASE}/api/execute-tool"
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    body = json.dumps({"tool": tool_name, "input": tool_input, "as_agent": True}).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        return json.dumps({"error": f"HTTP {e.code}: {e.read().decode()[:200]}"})


@mcp.tool()
def read_todos(status_filter: str = "open", detail: bool = False) -> str:
    """Read todo items. Returns summaries by default. Use detail=true for full descriptions, or get_todo for a single item.

    Args:
        status_filter: Filter by status - "all", "open", or "completed". Default: open
        detail: Include full descriptions. Default: false
    """
    return _call_tool("read_todos", {"status_filter": status_filter, "detail": detail})


@mcp.tool()
def get_todo(todo_id: str) -> str:
    """Get a single todo item by ID with full details including description.

    Args:
        todo_id: The todo item ID
    """
    return _call_tool("get_todo", {"todo_id": todo_id})


@mcp.tool()
def update_todo(todo_id: str, title: str = "", description: str = "",
                status: str = "", priority: str = "", section: str = "") -> str:
    """Update a todo item's fields.

    Args:
        todo_id: The todo item ID
        title: New title (leave empty to keep current)
        description: New description in markdown (leave empty to keep current)
        status: "open" or "completed" (leave empty to keep current)
        priority: "high", "medium", "low", or "none" (leave empty to keep current)
        section: Section/category name (leave empty to keep current)
    """
    inp = {"todo_id": todo_id}
    if title:
        inp["title"] = title
    if description:
        inp["description"] = description
    if status:
        inp["status"] = status
    if priority:
        inp["priority"] = priority
    if section:
        inp["section"] = section
    return _call_tool("update_todo", inp)


@mcp.tool()
def create_todo(title: str, description: str = "", priority: str = "none",
                section: str = "") -> str:
    """Create a new todo item.

    Args:
        title: Todo title
        description: Todo description in markdown
        priority: "high", "medium", "low", or "none"
        section: Section/category name
    """
    inp = {"title": title}
    if description:
        inp["description"] = description
    if priority:
        inp["priority"] = priority
    if section:
        inp["section"] = section
    return _call_tool("create_todo", inp)


@mcp.tool()
def search_todos(query: str) -> str:
    """Search todos by text query across titles and descriptions.

    Args:
        query: Search query text
    """
    return _call_tool("search_todos", {"query": query})


if __name__ == "__main__":
    mcp.run(transport="stdio")
