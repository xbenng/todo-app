import json, re
from concurrent.futures import ThreadPoolExecutor, as_completed
import state
from services.tools import _execute_tool, _get_tool_definitions
from services.mcp_utils import _get_mcp_tools
from services.system_prompt import _build_system_prompt
import db as _db

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import openai as openai_mod
except ImportError:
    openai_mod = None


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from model output."""
    return re.sub(r'<think>[\s\S]*?</think>\s*', '', text).strip()


def _openai_tool_defs(depth: int = 0, user_id: str | None = None) -> list[dict]:
    """Convert Anthropic-format tool definitions to OpenAI function-calling format."""
    tools = _get_tool_definitions(depth, user_id)
    tools = tools + _get_mcp_tools(user_id)
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        }
    } for t in tools]


class ChatAgent:
    """Unified agentic loop for both Anthropic and OpenAI-compatible providers.

    Handles: message history, system prompt, streaming, tool execution,
    output emission, persistence. Provider-specific logic is in _call_anthropic
    and _call_openai.
    """

    def __init__(self, job_id: str, todo_id: str | None, provider: dict, depth: int = 0):
        self.job_id = job_id
        self.todo_id = todo_id
        self.provider = provider
        self.ptype = provider.get("type", "local")
        self.model = provider.get("model", "claude-sonnet-4-20250514")
        self.depth = depth
        self.user_id = state._jobs.get(job_id, {}).get("user_id")
        self.assistant_text_lines: list[str] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    @property
    def job(self):
        return state._jobs[self.job_id]

    def emit(self, line: str, is_text: bool = False) -> None:
        if line.strip():
            self.job["output_lines"].append(line)
            if is_text:
                self.assistant_text_lines.append(line)

    def is_killed(self) -> bool:
        return self.job["status"] == "killed"

    def _build_history(self, message: str) -> list[dict]:
        """Build messages array from persisted history + new message.

        If auto_compact is enabled in user config, automatically
        summarizes older messages to stay within context limits.
        """
        if self.todo_id:
            raw_messages = _db.get_messages(self.todo_id)
        else:
            raw_messages = []
        messages = []
        for m in raw_messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        # Auto-compact if enabled
        if self.user_id:
            config = _db.get_config(self.user_id)
        else:
            config = {}
        if config.get("auto_compact", False) and len(messages) > 8:
            threshold = config.get("compact_threshold", 100000)
            keep_recent = config.get("compact_keep_recent", 8)
            messages = self._maybe_compact(messages, threshold, keep_recent)
        return messages

    @staticmethod
    def _msg_size(m: dict) -> int:
        c = m.get("content", "")
        return len(json.dumps(c) if isinstance(c, (list, dict)) else c)

    def _maybe_compact(self, messages: list[dict], threshold: int = 80000,
                       keep_recent: int = 4) -> list[dict]:
        """If conversation history is too large, summarize older messages."""
        total_chars = sum(self._msg_size(m) for m in messages)
        if total_chars < threshold:
            return messages
        if len(messages) <= keep_recent + 1:
            return messages  # Not enough to compact
        old_messages = messages[:-keep_recent]
        recent_messages = messages[-keep_recent:]
        self.emit("⟳ Compacting conversation history...")
        summary = self._summarize_messages(old_messages)
        if not summary:
            return messages  # Summarization failed, use original
        # Replace old messages with a single summary message pair
        compacted = [
            {"role": "user", "content": "[Earlier conversation summary]"},
            {"role": "assistant", "content": summary},
        ] + recent_messages
        old_chars = sum(len(m["content"]) for m in old_messages)
        new_chars = len(summary)
        print(f"[compact] Compacted {len(old_messages)} messages ({old_chars} chars) → summary ({new_chars} chars)", flush=True)
        return compacted

    def _summarize_messages(self, messages: list[dict]) -> str | None:
        """Use the LLM to summarize a list of messages into a concise summary."""
        def _fmt(m):
            c = m.get("content", "")
            if isinstance(c, (list, dict)):
                c = json.dumps(c, ensure_ascii=False)[:2000]
            return f"**{m['role'].upper()}:** {c}"
        conversation_text = "\n\n".join(_fmt(m) for m in messages)
        summary_prompt = (
            "Summarize the following conversation concisely. Preserve:\n"
            "- Key decisions made\n"
            "- Action items and their status\n"
            "- Important facts and context\n"
            "- Tool calls and their results (briefly)\n"
            "Drop: greetings, filler, repeated information.\n"
            "Output a concise summary in bullet points.\n\n"
            f"CONVERSATION:\n{conversation_text}"
        )
        try:
            if self.ptype == "anthropic" and anthropic:
                client = anthropic.Anthropic(api_key=self.provider.get("api_key"))
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": summary_prompt}],
                )
                return resp.content[0].text if resp.content else None
            elif self.ptype == "openai_compat" and openai_mod:
                client = openai_mod.OpenAI(
                    base_url=self.provider.get("base_url"),
                    api_key=self.provider.get("api_key", "none"),
                )
                resp = client.chat.completions.create(
                    model=self.model,
                    max_tokens=2048,
                    messages=[
                        {"role": "system", "content": "You are a concise summarizer."},
                        {"role": "user", "content": summary_prompt},
                    ],
                )
                return resp.choices[0].message.content if resp.choices else None
        except Exception as exc:
            print(f"[compact] Summarization failed: {exc}", flush=True)
        return None

    def _get_tools_anthropic(self) -> list[dict]:
        tools = _get_tool_definitions(self.depth, self.user_id)
        tools = tools + _get_mcp_tools(self.user_id)
        return tools

    def _get_tools_openai(self) -> list[dict]:
        return _openai_tool_defs(self.depth, self.user_id)

    def _persist_response(self) -> None:
        """Persist assistant text response to chats/DB. Tool calls are not persisted."""
        if not self.todo_id or not self.assistant_text_lines:
            return
        try:
            content = "\n".join(self.assistant_text_lines)
            user_id = self.job.get("user_id")
            _db.add_message(self.todo_id, user_id, "assistant", content)
        except Exception as exc:
            print(f"[persist] ERROR saving chat for {self.todo_id}: {exc}")

    # ------------------------------------------------------------------
    # Anthropic provider
    # ------------------------------------------------------------------

    def _run_anthropic(self, message: str) -> None:
        api_key = self.provider.get("api_key")
        if not api_key or not anthropic:
            self.emit("error: Anthropic API not configured")
            self.job["status"] = "error"
            return

        client = anthropic.Anthropic(api_key=api_key)
        messages = self._build_history(message)
        tools = self._get_tools_anthropic()
        system_prompt = _build_system_prompt(self.todo_id, self.user_id)

        while not self.is_killed():
            text_buf = ""
            with client.messages.stream(
                model=self.model,
                system=system_prompt,
                messages=messages,
                max_tokens=8192,
                tools=tools,
            ) as stream:
                self.job["_stream"] = stream
                for event in stream:
                    if self.is_killed():
                        stream.close()
                        return
                    if event.type == "content_block_start":
                        if hasattr(event, "content_block"):
                            block = event.content_block
                            if block.type == "tool_use":
                                if text_buf.strip():
                                    for ln in text_buf.strip().splitlines():
                                        self.emit(ln, is_text=True)
                                    text_buf = ""
                                self.emit(f"▶ {block.name}...")
                            elif block.type == "text" and text_buf.strip():
                                for ln in text_buf.strip().splitlines():
                                    self.emit(ln, is_text=True)
                                text_buf = ""
                    elif event.type == "content_block_delta":
                        if hasattr(event, "delta") and event.delta.type == "text_delta":
                            text_buf += event.delta.text
                            while "\n" in text_buf:
                                line, text_buf = text_buf.split("\n", 1)
                                self.emit(line, is_text=True)
                    elif event.type == "content_block_stop":
                        if text_buf.strip():
                            for ln in text_buf.strip().splitlines():
                                self.emit(ln, is_text=True)
                            text_buf = ""
                if text_buf.strip():
                    for ln in text_buf.strip().splitlines():
                        self.emit(ln, is_text=True)

            self.job.pop("_stream", None)
            response = stream.get_final_message()

            if response.usage:
                self.total_input_tokens += response.usage.input_tokens
                self.total_output_tokens += response.usage.output_tokens

            if response.stop_reason == "tool_use":
                assistant_content = []
                tool_blocks = []
                for block in response.content:
                    if block.type == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        assistant_content.append({
                            "type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input
                        })
                        tool_blocks.append(block)
                # Execute tool calls in parallel
                agent_ctx = {"job_id": self.job_id, "provider": self.provider, "depth": self.depth}
                if len(tool_blocks) > 1:
                    with ThreadPoolExecutor(max_workers=len(tool_blocks)) as ex:
                        futures = {
                            ex.submit(_execute_tool, b.name, b.input, self.todo_id, agent_ctx): b
                            for b in tool_blocks
                        }
                        result_map = {}
                        for f in as_completed(futures):
                            b = futures[f]
                            try:
                                result_map[b.id] = f.result(timeout=60)
                            except Exception as exc:
                                result_map[b.id] = json.dumps({"error": str(exc)[:200]})
                    tool_results = [{"type": "tool_result", "tool_use_id": b.id, "content": result_map[b.id]} for b in tool_blocks]
                else:
                    tool_results = [{
                        "type": "tool_result",
                        "tool_use_id": tool_blocks[0].id,
                        "content": _execute_tool(tool_blocks[0].name, tool_blocks[0].input, self.todo_id, agent_ctx)
                    }] if tool_blocks else []
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": tool_results})
                continue
            break  # end_turn or max_tokens

    # ------------------------------------------------------------------
    # OpenAI-compatible provider
    # ------------------------------------------------------------------

    def _run_openai(self, message: str) -> None:
        base_url = self.provider.get("base_url")
        api_key = self.provider.get("api_key", "none")
        if not base_url or not openai_mod:
            self.emit("error: OpenAI-compatible endpoint not configured")
            self.job["status"] = "error"
            return

        client = openai_mod.OpenAI(base_url=base_url, api_key=api_key)
        system_prompt = _build_system_prompt(self.todo_id, self.user_id)
        messages = [{"role": "system", "content": system_prompt}] + self._build_history(message)
        tools = self._get_tools_openai()
        max_tokens = min(self.provider.get("max_tokens", 4096), 4096)

        for _ in range(10):  # max iterations
            if self.is_killed():
                return
            kwargs = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
            if tools and self.provider.get("tool_use", True):
                kwargs["tools"] = tools

            response = client.chat.completions.create(**kwargs)

            if response.usage:
                self.total_input_tokens += response.usage.prompt_tokens or 0
                self.total_output_tokens += response.usage.completion_tokens or 0

            choice = response.choices[0]
            msg = choice.message

            if msg.content:
                cleaned = _strip_think_tags(msg.content)
                if cleaned:
                    for line in cleaned.splitlines():
                        self.emit(line, is_text=True)

            if msg.tool_calls:
                assistant_msg = {"role": "assistant", "content": msg.content or ""}
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
                messages.append(assistant_msg)
                for tc in msg.tool_calls:
                    self.emit(f"▶ {tc.function.name}...")
                # Parse args for all tool calls
                parsed_calls = []
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    parsed_calls.append((tc, args))
                # Execute in parallel if multiple
                agent_ctx = {"job_id": self.job_id, "provider": self.provider, "depth": self.depth}
                if len(parsed_calls) > 1:
                    with ThreadPoolExecutor(max_workers=len(parsed_calls)) as ex:
                        futures = {
                            ex.submit(_execute_tool, tc.function.name, args, self.todo_id, agent_ctx): tc
                            for tc, args in parsed_calls
                        }
                        result_map = {}
                        for f in as_completed(futures):
                            tc = futures[f]
                            try:
                                result_map[tc.id] = f.result(timeout=60)
                            except Exception as exc:
                                result_map[tc.id] = json.dumps({"error": str(exc)[:200]})
                    for tc, _ in parsed_calls:
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_map[tc.id]})
                else:
                    tc, args = parsed_calls[0]
                    result = _execute_tool(tc.function.name, args, self.todo_id, agent_ctx)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue
            break  # No tool calls — done

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, message: str) -> None:
        """Run the agentic loop. Called from a thread."""
        self.job["status"] = "running"
        try:
            if self.ptype == "anthropic":
                self._run_anthropic(message)
            elif self.ptype == "openai_compat":
                self._run_openai(message)
            else:
                self.emit(f"error: unknown provider type '{self.ptype}'")
                self.job["status"] = "error"
                return

            if self.is_killed():
                return

            # Cost/token summary
            if self.ptype == "anthropic":
                cost = (self.total_input_tokens * 3.0 + self.total_output_tokens * 15.0) / 1_000_000
                self.emit(f"✓ Done — ${cost:.4f}" if cost > 0 else "✓ Done")
            else:
                self.emit(f"✓ Done (tokens: {self.total_input_tokens}+{self.total_output_tokens})")

            self.job["status"] = "done"
            self._persist_response()

        except Exception as exc:
            self.job.pop("_stream", None)
            if not self.is_killed():
                exc_str = str(exc)
                # Provide actionable detail for common errors
                if "max_tokens" in exc_str or "context_length" in exc_str or "too long" in exc_str.lower() or "maximum" in exc_str.lower():
                    hist_count = len(self.assistant_text_lines)
                    self.emit(f"error: Context length exceeded ({self.total_input_tokens} input tokens, {hist_count} lines). "
                              f"Try Restart to reduce context.")
                elif "401" in exc_str or "auth" in exc_str.lower() or "api_key" in exc_str.lower():
                    self.emit(f"error: Authentication failed. Check your API key in Settings. Raw: {exc_str[:200]}")
                elif "429" in exc_str or "rate" in exc_str.lower():
                    self.emit(f"error: Rate limited. Too many requests — wait a moment and try again. Raw: {exc_str[:200]}")
                elif "connection" in exc_str.lower() or "timeout" in exc_str.lower() or "refused" in exc_str.lower():
                    self.emit(f"error: Connection failed. Is the endpoint reachable? Provider: {self.ptype}, model: {self.model}. Raw: {exc_str[:200]}")
                else:
                    self.emit(f"error: {self.ptype}/{self.model} — {exc_str[:300]}")
                self.job["status"] = "error"
