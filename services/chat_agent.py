"""ChatAgent: unified agentic loop with pluggable LLM backends.

Architecture:
    LLMBackend (ABC)            — provider-specific API calls
      ├── AnthropicBackend      — streaming via client.messages.stream()
      └── OpenAIBackend         — non-streaming via completions.create()
    ChatAgent                   — orchestration: history, tools, emit, persist
"""

import json
import re
import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

import state
from services.tools import _execute_tool, _get_tool_definitions
from services.mcp_utils import _get_mcp_tools
from services.system_prompt import _build_system_prompt
import db as _db

log = logging.getLogger("services.chat_agent")

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


# ---------------------------------------------------------------------------
# Normalized types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single tool call extracted from an LLM response."""
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    done: bool = True
    _raw_content: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class LLMBackend(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    def call(self, messages: list[dict], system_prompt: str,
             tools: list[dict],
             on_text: Callable[[str], None] | None = None,
             stream_handle_sink: dict | None = None,
             ) -> LLMResponse:
        """Make one LLM API call. Returns normalized response."""

    @abstractmethod
    def format_tool_results(self, response: LLMResponse,
                           results: dict[str, str]) -> list[dict]:
        """Format assistant message + tool results to append to messages."""

    @abstractmethod
    def initial_messages(self, history: list[dict], system_prompt: str) -> list[dict]:
        """Format the initial messages array for this provider."""

    def convert_tools(self, tools: list[dict]) -> list[dict]:
        """Convert Anthropic-format tool defs to this provider's format."""
        return tools


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------

class AnthropicBackend(LLMBackend):
    """Anthropic API backend with streaming support."""

    def __init__(self, api_key: str, model: str):
        if not anthropic:
            raise RuntimeError("Anthropic SDK not installed")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def call(self, messages, system_prompt, tools, on_text=None, stream_handle_sink=None):
        if on_text:
            return self._call_streaming(messages, system_prompt, tools, on_text, stream_handle_sink)
        return self._call_sync(messages, system_prompt, tools)

    def _call_sync(self, messages, system_prompt, tools):
        response = self.client.messages.create(
            model=self.model, system=system_prompt,
            messages=messages, max_tokens=8192, tools=tools or None,
        )
        text_parts = []
        raw_content = []
        for block in response.content:
            if block.type == "text":
                raw_content.append({"type": "text", "text": block.text})
                if block.text.strip():
                    text_parts.append(block.text.strip())
            elif block.type == "tool_use":
                raw_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=b.input)
            for b in response.content if b.type == "tool_use"
        ]
        return LLMResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=response.usage.input_tokens if response.usage else 0,
            output_tokens=response.usage.output_tokens if response.usage else 0,
            done=response.stop_reason != "tool_use",
            _raw_content=raw_content,
        )

    def _call_streaming(self, messages, system_prompt, tools, on_text, stream_handle_sink):
        text_buf = ""
        with self.client.messages.stream(
            model=self.model, system=system_prompt,
            messages=messages, max_tokens=8192, tools=tools or None,
        ) as stream:
            if stream_handle_sink is not None:
                stream_handle_sink["_stream"] = stream
            for event in stream:
                if event.type == "content_block_start":
                    if hasattr(event, "content_block"):
                        block = event.content_block
                        if block.type == "tool_use" or block.type == "text":
                            if text_buf.strip():
                                on_text(text_buf)
                                text_buf = ""
                elif event.type == "content_block_delta":
                    if hasattr(event, "delta") and event.delta.type == "text_delta":
                        text_buf += event.delta.text
                        while "\n" in text_buf:
                            line, text_buf = text_buf.split("\n", 1)
                            on_text(line + "\n")
                elif event.type == "content_block_stop":
                    if text_buf.strip():
                        on_text(text_buf)
                        text_buf = ""
            if text_buf.strip():
                on_text(text_buf)

        if stream_handle_sink is not None:
            stream_handle_sink.pop("_stream", None)

        response = stream.get_final_message()
        raw_content = []
        for block in response.content:
            if block.type == "text":
                raw_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                raw_content.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": block.input,
                })
        tool_calls = [
            ToolCall(id=b.id, name=b.name, arguments=b.input)
            for b in response.content if b.type == "tool_use"
        ]
        return LLMResponse(
            text="",  # already streamed
            tool_calls=tool_calls,
            input_tokens=response.usage.input_tokens if response.usage else 0,
            output_tokens=response.usage.output_tokens if response.usage else 0,
            done=response.stop_reason != "tool_use",
            _raw_content=raw_content,
        )

    def format_tool_results(self, response, results):
        assistant_msg = {"role": "assistant", "content": response._raw_content}
        tool_results = [
            {"type": "tool_result", "tool_use_id": tc.id, "content": results.get(tc.id, "")}
            for tc in response.tool_calls
        ]
        return [assistant_msg, {"role": "user", "content": tool_results}]

    def initial_messages(self, history, system_prompt):
        return list(history)


# ---------------------------------------------------------------------------
# OpenAI-compatible backend
# ---------------------------------------------------------------------------

class OpenAIBackend(LLMBackend):
    """OpenAI-compatible API backend (non-streaming)."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 max_tokens: int = 4096, tool_use: bool = True):
        if not openai_mod:
            raise RuntimeError("OpenAI SDK not installed")
        self.client = openai_mod.OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.max_tokens = min(max_tokens, 4096)
        self.tool_use = tool_use

    def call(self, messages, system_prompt, tools, on_text=None, stream_handle_sink=None):
        oai_tools = self.convert_tools(tools) if tools and self.tool_use else None
        kwargs = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if oai_tools:
            kwargs["tools"] = oai_tools
        response = self.client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        msg = choice.message
        text = _strip_think_tags(msg.content) if msg.content else ""

        tool_calls = []
        raw_tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
                raw_tool_calls.append({
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                })

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            input_tokens=response.usage.prompt_tokens or 0 if response.usage else 0,
            output_tokens=response.usage.completion_tokens or 0 if response.usage else 0,
            done=not bool(tool_calls),
            _raw_content=raw_tool_calls,
        )

    def format_tool_results(self, response, results):
        assistant_msg = {"role": "assistant", "content": response.text or ""}
        if response._raw_content:
            assistant_msg["tool_calls"] = response._raw_content
        msgs = [assistant_msg]
        for tc in response.tool_calls:
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": results.get(tc.id, "")})
        return msgs

    def initial_messages(self, history, system_prompt):
        return [{"role": "system", "content": system_prompt}] + list(history)

    def convert_tools(self, tools):
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }
        } for t in tools]


# ---------------------------------------------------------------------------
# ChatAgent
# ---------------------------------------------------------------------------

class ChatAgent:
    """Unified agentic loop with pluggable LLM backends."""

    def __init__(self, job_id: str, todo_id: str | None, provider: dict,
                 depth: int = 0, persist: bool = True):
        self.job_id = job_id
        self.todo_id = todo_id
        self.provider = provider
        self.model = provider.get("model", "claude-sonnet-4-20250514")
        self.depth = depth
        self.persist = persist
        self.user_id = state._jobs.get(job_id, {}).get("user_id")
        self.assistant_text_lines: list[str] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._streamed_text = False
        self.backend = self._create_backend(provider)

    def _create_backend(self, provider: dict) -> LLMBackend:
        ptype = provider.get("type", "local")
        if ptype == "anthropic":
            api_key = provider.get("api_key")
            if not api_key:
                raise ValueError("Anthropic API key not configured")
            return AnthropicBackend(api_key, self.model)
        elif ptype == "openai_compat":
            base_url = provider.get("base_url")
            if not base_url:
                raise ValueError("OpenAI-compatible base_url not configured")
            return OpenAIBackend(
                base_url, provider.get("api_key", "none"),
                self.model, provider.get("max_tokens", 4096),
                provider.get("tool_use", True))
        raise ValueError(f"Unknown provider type: {ptype}")

    @property
    def job(self):
        return state._jobs[self.job_id]

    @property
    def assistant_text(self):
        return "\n".join(self.assistant_text_lines)

    def emit(self, line: str, is_text: bool = False) -> None:
        if line.strip():
            self.job["output_lines"].append(line)
            if is_text:
                self.assistant_text_lines.append(line)

    def is_killed(self) -> bool:
        return self.job.get("status") == "killed"

    # ------------------------------------------------------------------
    # History & compaction
    # ------------------------------------------------------------------

    def _build_history(self, message: str) -> list[dict]:
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

    def _maybe_compact(self, messages, threshold=80000, keep_recent=4):
        total_chars = sum(self._msg_size(m) for m in messages)
        if total_chars < threshold or len(messages) <= keep_recent + 1:
            return messages
        old_messages = messages[:-keep_recent]
        recent_messages = messages[-keep_recent:]
        self.job["output_lines"].append({"__status__": "compacting"})
        summary = self._summarize_messages(old_messages)
        if not summary:
            return messages
        compacted = [
            {"role": "user", "content": "[Earlier conversation summary]"},
            {"role": "assistant", "content": summary},
        ] + recent_messages
        old_chars = sum(len(m["content"]) for m in old_messages)
        log.info("Compacted %d messages (%d chars) to summary (%d chars)",
                 len(old_messages), old_chars, len(summary))
        return compacted

    def _summarize_messages(self, messages):
        def _fmt(m):
            c = m.get("content", "")
            if isinstance(c, (list, dict)):
                c = json.dumps(c, ensure_ascii=False)[:2000]
            return f"**{m['role'].upper()}:** {c}"
        conversation_text = "\n\n".join(_fmt(m) for m in messages)
        summary_prompt = (
            "Summarize the following conversation concisely. Preserve:\n"
            "- Key decisions made\n- Action items and their status\n"
            "- Important facts and context\n- Tool calls and their results (briefly)\n"
            "Drop: greetings, filler, repeated information.\n"
            "Output a concise summary in bullet points.\n\n"
            f"CONVERSATION:\n{conversation_text}"
        )
        try:
            if isinstance(self.backend, AnthropicBackend):
                resp = self.backend.client.messages.create(
                    model=self.model, max_tokens=2048,
                    messages=[{"role": "user", "content": summary_prompt}],
                )
                return resp.content[0].text if resp.content else None
            elif isinstance(self.backend, OpenAIBackend):
                resp = self.backend.client.chat.completions.create(
                    model=self.model, max_tokens=2048,
                    messages=[
                        {"role": "system", "content": "You are a concise summarizer."},
                        {"role": "user", "content": summary_prompt},
                    ],
                )
                return resp.choices[0].message.content if resp.choices else None
        except Exception as exc:
            log.warning("Compaction summarization failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _get_tools(self) -> list[dict]:
        tools = _get_tool_definitions(self.depth, self.user_id)
        return tools + _get_mcp_tools(self.user_id)

    def _execute_tools_parallel(self, tool_calls: list[ToolCall]) -> dict[str, str]:
        agent_ctx = {"job_id": self.job_id, "provider": self.provider, "depth": self.depth}
        if len(tool_calls) == 1:
            tc = tool_calls[0]
            return {tc.id: _execute_tool(tc.name, tc.arguments, self.todo_id, agent_ctx)}
        with ThreadPoolExecutor(max_workers=len(tool_calls)) as ex:
            futures = {
                ex.submit(_execute_tool, tc.name, tc.arguments, self.todo_id, agent_ctx): tc.id
                for tc in tool_calls
            }
            results = {}
            for f in as_completed(futures):
                tid = futures[f]
                try:
                    results[tid] = f.result(timeout=60)
                except Exception as exc:
                    results[tid] = json.dumps({"error": str(exc)[:200]})
            return results

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_response(self):
        if not self.todo_id or not self.assistant_text_lines:
            return
        try:
            content = "\n".join(self.assistant_text_lines)
            _db.add_message(self.todo_id, self.user_id, "assistant", content)
        except Exception as exc:
            log.error("Error saving chat for %s: %s", self.todo_id, exc)

    # ------------------------------------------------------------------
    # Unified agentic loop
    # ------------------------------------------------------------------

    def _handle_text_chunk(self, chunk: str):
        self._streamed_text = True
        for line in chunk.splitlines():
            if line.strip():
                self.emit(line, is_text=True)

    def _run_loop(self, message: str) -> None:
        history = self._build_history(message)
        system_prompt = _build_system_prompt(self.todo_id, self.user_id, message)
        tools = self._get_tools()
        messages = self.backend.initial_messages(history, system_prompt)

        while not self.is_killed():
            self._streamed_text = False
            response = self.backend.call(
                messages, system_prompt, tools,
                on_text=self._handle_text_chunk,
                stream_handle_sink=self.job,
            )
            self.total_input_tokens += response.input_tokens
            self.total_output_tokens += response.output_tokens

            if response.text and not self._streamed_text:
                for line in response.text.splitlines():
                    self.emit(line, is_text=True)

            if not response.tool_calls or response.done:
                break

            for tc in response.tool_calls:
                self.job["output_lines"].append({
                    "__tool_call__": True,
                    "name": tc.name,
                    "id": tc.id,
                })
            results = self._execute_tools_parallel(response.tool_calls)
            messages.extend(self.backend.format_tool_results(response, results))

    def run(self, message: str) -> None:
        self.job["status"] = "running"
        try:
            self._run_loop(message)
            if self.is_killed():
                return
            cost = None
            if self.provider.get("type") == "anthropic":
                cost = (self.total_input_tokens * 3.0 + self.total_output_tokens * 15.0) / 1_000_000
            self.job["output_lines"].append({
                "__status__": "done",
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "cost": cost,
            })
            self.job["status"] = "done"
            if self.persist:
                self._persist_response()
        except Exception as exc:
            self.job.pop("_stream", None)
            if not self.is_killed():
                exc_str = str(exc)
                if "max_tokens" in exc_str or "context_length" in exc_str or "too long" in exc_str.lower():
                    code, msg = "context_length", f"Context length exceeded ({self.total_input_tokens} input tokens). Try Restart."
                elif "401" in exc_str or "auth" in exc_str.lower() or "api_key" in exc_str.lower():
                    code, msg = "auth", f"Authentication failed. Check API key. Raw: {exc_str[:200]}"
                elif "429" in exc_str or "rate" in exc_str.lower():
                    code, msg = "rate_limit", f"Rate limited. Wait and retry. Raw: {exc_str[:200]}"
                elif "connection" in exc_str.lower() or "timeout" in exc_str.lower():
                    code, msg = "connection", f"Connection failed. Raw: {exc_str[:200]}"
                else:
                    code, msg = "unknown", exc_str[:300]
                self.job["output_lines"].append({
                    "__error__": True,
                    "code": code,
                    "message": msg,
                })
                self.job["status"] = "error"
