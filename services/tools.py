"""Tool definitions and execution for the chat agent.

Contains built-in tool schemas, the main tool dispatcher, and subagent spawning.
"""

import json
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import state
from services.mcp_utils import (
    _get_mcp_tools, _parse_mcp_tool_name, _check_tool_permission, _get_mcp_manager,
)
from services.file_io import VALID_PRIORITIES, DEFAULT_PRIORITY
import db as _db

log = logging.getLogger("services.tools")


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from model output."""
    return re.sub(r'<think>[\s\S]*?</think>\s*', '', text).strip()


def _get_tool_definitions(depth: int = 0, user_id: str | None = None) -> list[dict]:
    """Return Claude API tool definitions for server-side tools."""
    tools = [
        {
            "name": "read_todos",
            "description": "Read todo items. Returns summaries (id, title, status, priority, section) by default. Use detail=true for full descriptions. Use get_todo for a single item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "enum": ["all", "open", "completed"],
                        "description": "Filter by status. Default: open"
                    },
                    "detail": {
                        "type": "boolean",
                        "description": "Include full descriptions. Default: false (summaries only)"
                    }
                },
                "required": []
            }
        },
        {
            "name": "get_todo",
            "description": "Get a single todo item by ID with full details including description.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The todo item ID"}
                },
                "required": ["todo_id"]
            }
        },
        {
            "name": "update_todo",
            "description": "Update a todo item's fields (title, description, status, priority, section).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The todo item ID"},
                    "title": {"type": "string", "description": "New title"},
                    "description": {"type": "string", "description": "New description (markdown)"},
                    "status": {"type": "string", "enum": ["open", "completed"]},
                    "priority": {"type": "string", "enum": ["high", "medium", "low", "none"]},
                    "section": {"type": "string", "description": "Section/category name"}
                },
                "required": ["todo_id"]
            }
        },
        {
            "name": "create_todo",
            "description": "Create a new todo item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Todo title"},
                    "description": {"type": "string", "description": "Todo description (markdown)"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low", "none"]},
                    "section": {"type": "string", "description": "Section/category name"}
                },
                "required": ["title"]
            }
        },
        {
            "name": "search_todos",
            "description": "Search todos by text query across titles and descriptions.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        },
        {
            "name": "read_chat_history",
            "description": "Read the chat history for a specific todo item.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "string", "description": "The todo item ID"}
                },
                "required": ["todo_id"]
            }
        },
    ]
    if user_id and user_id != "local":
        config = _db.get_config(user_id)
    else:
        config = {}
    if depth < 2 and config.get("subagents_enabled", True):
        tools.append({
            "name": "spawn_agents",
            "description": "Launch multiple subagents in parallel in a SINGLE call. Pass ALL agents in the 'agents' array — "
                           "they run concurrently via a thread pool. Do NOT call this tool multiple times sequentially; "
                           "instead, batch all independent tasks into one call. Each subagent gets its own prompt, "
                           "full tool access (including MCP), and returns results. "
                           "This is the 'Agent tool' referenced in the EA skill instructions.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string", "description": "Complete instructions for this subagent. Include all context it needs — it has no access to the parent conversation."},
                                "label": {"type": "string", "description": "Short label for progress output (e.g., 'Slack sweep')."},
                                "model": {"type": "string", "description": "Optional model override. Defaults to the parent's model."}
                            },
                            "required": ["prompt"]
                        },
                        "description": "Array of agent specs to launch in parallel."
                    }
                },
                "required": ["agents"]
            }
        })
    return tools


def _execute_tool(name: str, input_data: dict, todo_id: str | None,
                  agent_context: dict | None = None) -> str:
    """Execute a server-side tool and return the result as a string.

    agent_context: {job_id, provider, depth} — passed when called from ChatAgent.
    """
    depth = agent_context.get("depth", 0) if agent_context else 0
    prefix = f"[tool d={depth}]"
    log.info("%s %s(%s)", prefix, name, json.dumps(input_data)[:200])

    # Resolve user_id from agent context for DB-aware operations
    user_id = None
    if agent_context and agent_context.get("job_id"):
        user_id = state._jobs.get(agent_context["job_id"], {}).get("user_id")

    # Require user_id — never fall through without authentication
    if not user_id and name not in ("spawn_agents",):
        return json.dumps({"error": "Not authenticated"})

    try:
        if name == "read_todos":
            todos = _db.get_todos(user_id)
            status_filter = input_data.get("status_filter", "open")
            if status_filter == "open":
                todos = [t for t in todos if t["status"] != "completed"]
            elif status_filter == "completed":
                todos = [t for t in todos if t["status"] == "completed"]
            # Strip descriptions unless detail=true
            if not input_data.get("detail"):
                todos = [{k: v for k, v in t.items() if k != "description"} for t in todos]
            return json.dumps(todos, ensure_ascii=False)

        elif name == "get_todo":
            tid = input_data.get("todo_id", "")
            todo = _db.get_todo(user_id, tid)
            if not todo:
                return json.dumps({"error": f"Todo {tid} not found"})
            return json.dumps(todo, ensure_ascii=False)

        elif name == "update_todo":
            tid = input_data["todo_id"]
            fields = {}
            for k in ("title", "description", "status", "priority", "section"):
                if k in input_data:
                    val = input_data[k]
                    if k == "status" and val not in ("open", "completed"):
                        continue
                    if k == "priority" and val not in VALID_PRIORITIES:
                        continue
                    fields[k] = val.strip() if isinstance(val, str) else val
            result = _db.update_todo(user_id, tid, **fields)
            if not result:
                return json.dumps({"error": f"Todo {tid} not found"})
            if agent_context:
                _db.mark_chat_unread(tid, user_id)
            return json.dumps(result, ensure_ascii=False)

        elif name == "create_todo":
            title = (input_data.get("title") or "").strip()
            if not title:
                return json.dumps({"error": "Title is required"})
            new_todo = _db.create_todo(
                user_id, title,
                description=(input_data.get("description") or "").strip(),
                priority=input_data.get("priority", DEFAULT_PRIORITY),
                section=(input_data.get("section") or "").strip(),
            )
            if agent_context:
                _db.mark_chat_unread(new_todo["id"], user_id)
            return json.dumps(new_todo, ensure_ascii=False)

        elif name == "search_todos":
            query = input_data.get("query", "").lower()
            results = _db.search_todos(user_id, query)
            # Return summaries — use get_todo for full detail
            results = [{k: v for k, v in t.items() if k != "description"} for t in results]
            return json.dumps(results, ensure_ascii=False)

        elif name == "read_chat_history":
            tid = input_data.get("todo_id", todo_id)
            messages = _db.get_messages(tid)
            return json.dumps(messages[-20:], ensure_ascii=False)

        elif name == "spawn_agents":
            if not agent_context:
                return json.dumps({"error": "spawn_agents requires agent context"})
            agents = input_data.get("agents", [])
            log.info("spawn_agents: received %d agent(s)", len(agents))
            if not agents:
                return json.dumps({"error": "No agents specified"})
            if user_id:
                config = _db.get_config(user_id)
            else:
                config = {}
            max_subagents = config.get("max_subagents", 10)
            if len(agents) > max_subagents:
                return json.dumps({"error": f"Maximum {max_subagents} parallel agents"})
            depth = agent_context.get("depth", 0)
            if depth >= 2:
                return json.dumps({"error": "Maximum agent nesting depth reached"})
            return _execute_spawn_agents(agents, agent_context, todo_id)

        else:
            # Delegate to MCP if it's an MCP tool
            if not user_id and agent_context and agent_context.get("job_id"):
                user_id = state._jobs.get(agent_context["job_id"], {}).get("user_id")
            mgr = _get_mcp_manager(user_id)
            if mgr and mgr.is_mcp_tool(name):
                # Check runtime permission before executing
                if user_id and user_id != "local":
                    permission = _check_tool_permission(user_id, name, input_data, agent_context)
                    if permission == "denied":
                        return json.dumps({"error": f"Tool '{name}' was denied by the user"})
                return mgr.call_tool(name, input_data)
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as exc:
        log.error(f"{prefix} {name} ERROR: {exc}")
        return json.dumps({"error": str(exc)})


def _execute_spawn_agents(agents: list[dict], agent_context: dict, todo_id: str | None) -> str:
    """Launch subagents in parallel and return their results."""
    job_id = agent_context["job_id"]
    provider = agent_context["provider"]
    depth = agent_context.get("depth", 0)

    state._jobs[job_id]["output_lines"].append(f"\u26a1 Launching {len(agents)} subagent(s)...")

    results = []
    user_id = state._jobs[job_id].get("user_id")
    if user_id:
        config = _db.get_config(user_id)
    else:
        config = {}
    max_workers = config.get("max_subagents", 10)
    with ThreadPoolExecutor(max_workers=min(len(agents), max_workers)) as executor:
        futures = {}
        for i, spec in enumerate(agents):
            label = spec.get("label", f"agent-{i+1}")
            future = executor.submit(
                _run_subagent,
                job_id=job_id, todo_id=todo_id, provider=provider,
                prompt=spec["prompt"], label=label, depth=depth + 1,
            )
            futures[future] = label

        subagent_timeout = config.get("subagent_timeout", 120)
        for future in as_completed(futures, timeout=subagent_timeout + 30):
            try:
                result = future.result(timeout=subagent_timeout)
                results.append(result)
            except Exception as exc:
                results.append({
                    "label": futures[future], "result": "",
                    "error": f"Timed out or failed: {str(exc)[:200]}",
                    "input_tokens": 0, "output_tokens": 0,
                })

    total_in = sum(r.get("input_tokens", 0) for r in results)
    total_out = sum(r.get("output_tokens", 0) for r in results)
    state._jobs[job_id]["output_lines"].append(
        f"\u2713 All {len(results)} subagent(s) complete (tokens: {total_in}+{total_out})"
    )
    return json.dumps({"agents": results}, ensure_ascii=False)


def _run_subagent(job_id: str, todo_id: str | None, provider: dict,
                  prompt: str, label: str, depth: int) -> dict:
    """Run a single subagent using ChatAgent. Returns {label, result, error, tokens}."""
    import time
    import uuid
    from services.chat_agent import ChatAgent

    job = state._jobs[job_id]

    def emit(line: str):
        if line.strip():
            job["output_lines"].append(f"[{label}] {line}")

    emit(f"Starting ({provider.get('model', '?')})...")

    # Create ephemeral sub-job so ChatAgent has its own output context
    sub_job_id = str(uuid.uuid4())[:8]
    state._jobs[sub_job_id] = {
        "id": sub_job_id, "label": label, "job_key": f"subagent-{sub_job_id}",
        "status": "running", "output_lines": [],
        "proc": None, "created_at": time.time(),
        "user_id": job.get("user_id"), "todo_id": todo_id,
    }

    try:
        agent = ChatAgent(sub_job_id, todo_id, provider, depth=depth, persist=False)
        agent.run(prompt)

        # Forward output to parent job
        for line in state._jobs.get(sub_job_id, {}).get("output_lines", []):
            if isinstance(line, str) and line.strip():
                job["output_lines"].append(f"[{label}] {line}")

        return {
            "label": label, "result": agent.assistant_text, "error": None,
            "input_tokens": agent.total_input_tokens,
            "output_tokens": agent.total_output_tokens,
        }
    except Exception as exc:
        emit(f"Error: {str(exc)[:200]}")
        return {"label": label, "result": "", "error": str(exc),
                "input_tokens": 0, "output_tokens": 0}
    finally:
        state._jobs.pop(sub_job_id, None)
