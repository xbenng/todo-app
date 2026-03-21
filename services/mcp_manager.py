"""MCP (Model Context Protocol) server manager.

Manages MCP server connections in a background asyncio event loop.
"""

import asyncio
import json
import os
import shutil
import threading

try:
    from mcp import ClientSessionGroup, StdioServerParameters
    from mcp.client.session_group import SseServerParameters, StreamableHttpParameters
except ImportError:
    ClientSessionGroup = None

from services.shell_utils import _get_user_shell_env


class MCPManager:
    """Manages MCP server connections in a background asyncio event loop."""

    def __init__(self):
        self._loop: object = None  # asyncio event loop
        self._thread: threading.Thread | None = None
        self._group: object = None  # ClientSessionGroup
        self._tool_defs: list[dict] = []  # cached Anthropic API format
        self._mcp_tool_names: set[str] = set()
        self._started = threading.Event()
        self._server_status: dict[str, dict] = {}  # name -> {connected, tool_count}

    def start(self, server_configs: dict, tokens: dict | None = None):
        """Start background event loop thread and connect to all configured MCP servers."""
        if not ClientSessionGroup:
            return
        self._tokens = tokens or {}
        self._server_configs = server_configs
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)
        # Connect to servers (blocking wait)
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._connect_all(server_configs), self._loop
            )
            future.result(timeout=60)
        except Exception as exc:
            print(f"MCP startup error: {exc}")

    def stop(self):
        """Shutdown all MCP connections and stop the event loop."""
        if self._loop and self._loop.is_running():
            async def _shutdown():
                if self._group:
                    await self._group.__aexit__(None, None, None)
            try:
                future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
                future.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)

    def get_tool_definitions(self) -> list[dict]:
        """Return MCP tools in Anthropic API tool definition format."""
        return list(self._tool_defs)

    def is_mcp_tool(self, name: str) -> bool:
        """Check if a tool name belongs to an MCP server."""
        return name in self._mcp_tool_names

    def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> str:
        """Execute an MCP tool call synchronously from a Flask thread."""
        if not self._loop or not self._group:
            return json.dumps({"error": "MCP not initialized"})
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_call_tool(name, arguments), self._loop
            )
            return future.result(timeout=timeout)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def get_status(self) -> dict:
        """Return status of all MCP servers."""
        return dict(self._server_status)

    # -- internal --

    def _run_loop(self):
        """Background thread target: run an asyncio event loop forever."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    async def _connect_all(self, server_configs: dict):
        """Connect to all configured MCP servers."""
        # Track which server_info.name maps to which config key.
        # We populate this before connecting so the name_hook can use it.
        self._name_map = {}  # server_impl_name -> config_key
        self._pending_config_key = None  # set before each connect

        def name_hook(name, server_info):
            # Register mapping on first tool encountered
            if server_info.name not in self._name_map and self._pending_config_key:
                self._name_map[server_info.name] = self._pending_config_key
            config_key = self._name_map.get(server_info.name, server_info.name)
            return f"mcp__{config_key}__{name}"

        self._group = ClientSessionGroup(component_name_hook=name_hook)
        await self._group.__aenter__()

        for server_name, config in server_configs.items():
            try:
                self._pending_config_key = server_name
                await asyncio.wait_for(
                    self._connect_server(server_name, config),
                    timeout=30
                )
                tool_count = sum(1 for t in self._group.tools
                                 if t.startswith(f"mcp__{server_name}__"))
                self._server_status[server_name] = {
                    "connected": True, "tool_count": tool_count
                }
                print(f"MCP connected: {server_name} ({tool_count} tools)", flush=True)
            except Exception as exc:
                self._server_status[server_name] = {
                    "connected": False, "error": str(exc)
                }
                print(f"MCP failed: {server_name}: {exc}", flush=True)
        self._pending_config_key = None

        # Rebuild cached tool definitions
        self._rebuild_tool_cache()

    async def _connect_server(self, server_name: str, config: dict):
        """Connect to a single MCP server. Returns the ClientSession."""
        server_type = config.get("type", "stdio")

        if server_type == "stdio":
            # Start with user's full shell environment so npx/node/python are on PATH
            shell_env = _get_user_shell_env()
            # Merge server-specific env vars on top
            server_env = self._resolve_env(config.get("env", {}))
            full_env = dict(shell_env)
            full_env.update(server_env)
            # Resolve command via shell PATH if not absolute
            command = config["command"]
            if not os.path.isabs(command):
                resolved = shutil.which(command, path=full_env.get("PATH", os.defpath))
                if resolved:
                    command = resolved
            params = StdioServerParameters(
                command=command,
                args=config.get("args", []),
                env=full_env,
            )
        elif server_type == "streamable-http":
            params = StreamableHttpParameters(
                url=config["url"],
                headers=config.get("headers"),
            )
        elif server_type in ("sse", "http"):
            params = SseServerParameters(
                url=config["url"],
                headers=config.get("headers"),
            )
        else:
            raise ValueError(f"Unknown MCP server type: {server_type}")

        return await self._group.connect_to_server(params)

    def _resolve_env(self, env: dict) -> dict:
        """Replace ${tokens.X} placeholders with actual values from config tokens."""
        resolved = {}
        for key, value in env.items():
            if isinstance(value, str) and value.startswith("${tokens.") and value.endswith("}"):
                token_key = value[9:-1]
                resolved[key] = self._tokens.get(token_key, value)
            else:
                resolved[key] = value
        return resolved

    def _rebuild_tool_cache(self):
        """Convert MCP Tool objects to Anthropic API tool definition dicts."""
        self._tool_defs = []
        self._mcp_tool_names = set()
        if not self._group:
            return
        for name, tool in self._group.tools.items():
            self._tool_defs.append({
                "name": name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            })
            self._mcp_tool_names.add(name)

    async def _async_call_tool(self, name: str, arguments: dict) -> str:
        """Execute an MCP tool and return the result as a JSON string."""
        result = await self._group.call_tool(name, arguments)
        parts = []
        for block in (result.content or []):
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(f"[binary data: {getattr(block, 'mimeType', 'unknown')}]")
        if result.isError:
            return json.dumps({"error": "\n".join(parts) if parts else "Tool call failed"})
        return "\n".join(parts) if parts else json.dumps({"result": "ok"})
