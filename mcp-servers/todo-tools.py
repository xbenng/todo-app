#!/usr/bin/env python3
"""MCP stdio server that proxies todo tools back to the todo-app HTTP API.

Launched by Claude CLI as a stdio subprocess. Receives the API base URL
and auth token via environment variables.
"""
import json
import os
import urllib.request
import urllib.error

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("todo-tools")

API_BASE = os.environ.get("TODO_API_BASE", "http://localhost:5222")
AUTH_TOKEN = os.environ.get("TODO_AUTH_TOKEN", "")


def _call_api(endpoint: str, method: str = "GET", data: dict | None = None) -> dict:
    """Call the todo-app HTTP API."""
    url = f"{API_BASE}{endpoint}"
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}


@mcp.tool()
def read_todos(status_filter: str = "open", detail: bool = False) -> str:
    """Read todo items. Returns summaries by default. Use detail=true for full descriptions, or get_todo for a single item.

    Args:
        status_filter: Filter by status - "all", "open", or "completed". Default: open
        detail: Include full descriptions. Default: false
    """
    params = f"status={status_filter}"
    if detail:
        params += "&detail=true"
    result = _call_api(f"/api/todos?{params}")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_todo(todo_id: str) -> str:
    """Get a single todo item by ID with full details including description.

    Args:
        todo_id: The todo item ID
    """
    result = _call_api(f"/api/todos/{urllib.parse.quote(todo_id)}")
    return json.dumps(result, ensure_ascii=False)


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
    data = {"todo_id": todo_id}
    if title:
        data["title"] = title
    if description:
        data["description"] = description
    if status:
        data["status"] = status
    if priority:
        data["priority"] = priority
    if section:
        data["section"] = section
    result = _call_api(f"/api/todos/{todo_id}", method="PUT", data=data)
    return json.dumps(result, ensure_ascii=False)


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
    data = {"title": title}
    if description:
        data["description"] = description
    if priority:
        data["priority"] = priority
    if section:
        data["section"] = section
    result = _call_api("/api/todos", method="POST", data=data)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def search_todos(query: str) -> str:
    """Search todos by text query across titles and descriptions.

    Args:
        query: Search query text
    """
    result = _call_api(f"/api/todos/search?q={urllib.parse.quote(query)}")
    return json.dumps(result, ensure_ascii=False)


import urllib.parse

if __name__ == "__main__":
    mcp.run(transport="stdio")
