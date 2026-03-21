import json, subprocess, threading, time, uuid
import state
from services.chat_agent import ChatAgent
from services.shell_utils import _resolve_claude_bin, _get_user_shell_env, _kill_process_tree
from services.system_prompt import _build_system_prompt
from services.mcp_utils import _build_cli_mcp_config
import db as _db


def _run_chat_local(job_id: str, message: str, cwd: str,
                    conversation_id: str | None = None,
                    todo_id: str | None = None):
    """Thread target: run claude -p for chat with optional --resume (local CLI fallback)."""
    claude_bin, env = _resolve_claude_bin()
    if not claude_bin:
        state._jobs[job_id]["output_lines"].append("error: claude binary not found")
        state._jobs[job_id]["status"] = "error"
        return

    # Build system prompt and MCP config from DB
    user_id = state._jobs.get(job_id, {}).get("user_id")
    system_prompt = _build_system_prompt(todo_id, user_id)
    cmd = [claude_bin, "-p", message, "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose"]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    # Build per-user MCP config from registry + credentials
    mcp_config = _build_cli_mcp_config(user_id)
    if mcp_config:
        cmd.extend(["--mcp-config", json.dumps(mcp_config)])
    if conversation_id:
        cmd.extend(["--resume", conversation_id])
    state._jobs[job_id]["status"] = "running"

    assistant_text_lines = []  # collect plain text lines for persistence

    def emit(line: str, is_text: bool = False) -> None:
        if line.strip():
            state._jobs[job_id]["output_lines"].append(line)
            if is_text:
                assistant_text_lines.append(line)

    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                start_new_session=True)
        state._jobs[job_id]["proc"] = proc

        text_buf = ""
        got_streaming = False

        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                emit(raw_line[:200])
                continue

            t = data.get("type", "")

            if t == "content_block_start":
                block = data.get("content_block", {})
                if block.get("type") == "tool_use":
                    if text_buf.strip():
                        emit(text_buf.strip(), is_text=True)
                        text_buf = ""
                    emit(f"▶ {block.get('name', '?')}...")
                elif block.get("type") == "text" and text_buf.strip():
                    emit(text_buf.strip(), is_text=True)
                    text_buf = ""

            elif t == "content_block_delta":
                got_streaming = True
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_buf += delta.get("text", "")
                    while "\n" in text_buf:
                        line, text_buf = text_buf.split("\n", 1)
                        emit(line, is_text=True)

            elif t == "content_block_stop":
                if text_buf.strip():
                    emit(text_buf.strip(), is_text=True)
                    text_buf = ""

            elif t == "assistant" and not got_streaming:
                parts = []
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text = block["text"].strip()
                        if text:
                            parts.append(("text", text))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))[:80]
                        parts.append(("tool", f"▶ {name}({inp})"))
                for kind, part in parts:
                    for line in part.splitlines():
                        emit(line, is_text=(kind == "text"))

            elif t == "result":
                session_id = data.get("session_id")
                if session_id:
                    state._jobs[job_id]["conversation_id"] = session_id
                result = data.get("result", "").strip()
                cost = data.get("cost_usd")
                cost_str = f" — ${cost:.4f}" if cost else ""
                emit(f"✓ Done{cost_str}" + (f": {result}" if result else ""))

        if text_buf.strip():
            emit(text_buf.strip(), is_text=True)

        proc.wait()
        if state._jobs[job_id]["status"] != "killed":
            state._jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"


    except Exception as exc:
        state._jobs[job_id]["output_lines"].append(f"error: {exc}")
        state._jobs[job_id]["status"] = "error"


def _get_active_provider(user_id: str | None = None) -> tuple[str, dict]:
    """Return (provider_name, provider_config) for the active provider."""
    if user_id:
        config = _db.get_config(user_id)
    else:
        config = {}
    active = config.get("active_provider", "")

    # Explicit local CLI selection
    if active == "local":
        return "local", {"type": "local"}

    # Named provider from providers dict
    providers = config.get("providers", {})
    if active and active in providers:
        return active, providers[active]

    # Migration: build provider from legacy flat config
    if config.get("openai_compat", {}).get("base_url"):
        oai = config["openai_compat"]
        return "openai_compat", {
            "type": "openai_compat",
            "base_url": oai.get("base_url"),
            "api_key": oai.get("api_key", "none"),
            "model": oai.get("model", "default"),
        }
    if config.get("anthropic_api_key"):
        return "anthropic", {
            "type": "anthropic",
            "api_key": config["anthropic_api_key"],
            "model": config.get("model", "claude-sonnet-4-20250514"),
        }
    return "none", {"type": "none"}


def _run_claude_chat_job(job_id: str, message: str, cwd: str,
                         conversation_id: str | None = None,
                         todo_id: str | None = None):
    """Dispatcher: route to the active provider. Only uses local CLI if explicitly selected."""
    user_id = state._jobs.get(job_id, {}).get("user_id")
    name, provider = _get_active_provider(user_id)
    ptype = provider.get("type", "")
    if ptype in ("anthropic", "openai_compat"):
        agent = ChatAgent(job_id, todo_id, provider)
        agent.run(message)
    elif ptype == "local":
        _run_chat_local(job_id, message, cwd, conversation_id, todo_id)
    else:
        state._jobs[job_id]["output_lines"].append("error: No provider configured. Go to Settings to set up a provider.")
        state._jobs[job_id]["status"] = "error"


def _start_claude_chat_job(label: str, job_key: str, message: str, cwd: str,
                           conversation_id: str | None = None,
                           todo_id: str | None = None,
                           user_id: str | None = None) -> str:
    """Start a headless Claude chat job; return job_id. No dedup — each message is a new job."""
    job_id = str(uuid.uuid4())[:8]
    state._jobs[job_id] = {
        "id": job_id,
        "label": label,
        "job_key": job_key,
        "status": "pending",
        "output_lines": [],
        "proc": None,
        "created_at": time.time(),
        "conversation_id": conversation_id,
        "todo_id": todo_id,
        "user_id": user_id,
    }
    t = threading.Thread(target=_run_claude_chat_job,
                         args=(job_id, message, cwd, conversation_id, todo_id), daemon=True)
    t.start()
    return job_id


def _run_claude_job(job_id: str, prompt: str, cwd: str):
    """Thread target: run claude -p as a subprocess and parse stream-json output."""
    claude_bin, env = _resolve_claude_bin()
    if not claude_bin:
        state._jobs[job_id]["output_lines"].append("error: claude binary not found")
        state._jobs[job_id]["status"] = "error"
        return

    # Append DB context files to the CLI's system prompt
    user_id = state._jobs.get(job_id, {}).get("user_id")
    todo_id = state._jobs.get(job_id, {}).get("todo_id")
    append_prompt = ""
    if user_id and user_id != "local":
        ctx = _db.get_context_files(user_id)
        if ctx:
            append_prompt = "\n\n".join(f"# {name}\n{content.strip()}"
                                        for name, content in sorted(ctx.items())
                                        if content and content.strip())
    cmd = [claude_bin, "-p", prompt, "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose",
           "--effort", "low", "--include-partial-messages"]
    if append_prompt:
        cmd.extend(["--system-prompt", append_prompt])
    state._jobs[job_id]["status"] = "running"

    def emit(line: str) -> None:
        if line.strip():
            state._jobs[job_id]["output_lines"].append(line)

    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                start_new_session=True)
        state._jobs[job_id]["proc"] = proc

        text_buf = ""       # accumulates text_delta fragments until a newline or block end
        got_streaming = False  # True if we receive content_block_delta events

        for raw_line in proc.stdout:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                emit(raw_line[:200])
                continue

            t = data.get("type", "")

            if t == "content_block_start":
                block = data.get("content_block", {})
                if block.get("type") == "tool_use":
                    if text_buf.strip():
                        emit(text_buf.strip())
                        text_buf = ""
                    emit(f"▶ {block.get('name', '?')}...")
                elif block.get("type") == "text" and text_buf.strip():
                    emit(text_buf.strip())
                    text_buf = ""

            elif t == "content_block_delta":
                got_streaming = True
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_buf += delta.get("text", "")
                    while "\n" in text_buf:
                        line, text_buf = text_buf.split("\n", 1)
                        emit(line)

            elif t == "content_block_stop":
                if text_buf.strip():
                    emit(text_buf.strip())
                    text_buf = ""

            elif t == "assistant" and not got_streaming:
                # Fallback: no streaming events, parse the complete assistant message
                parts = []
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text = block["text"].strip()
                        if text:
                            parts.append(text)
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))[:80]
                        parts.append(f"▶ {name}({inp})")
                for part in parts:
                    for line in part.splitlines():
                        emit(line)

            elif t == "result":
                result = data.get("result", "").strip()
                cost = data.get("cost_usd")
                cost_str = f" — ${cost:.4f}" if cost else ""
                emit(f"✓ Done{cost_str}" + (f": {result}" if result else ""))

            # Skip: user, tool_result, system, debug, rate_limit_event

        if text_buf.strip():
            emit(text_buf.strip())

        proc.wait()
        if state._jobs[job_id]["status"] != "killed":
            state._jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        state._jobs[job_id]["output_lines"].append(f"error: {exc}")
        state._jobs[job_id]["status"] = "error"


def _start_claude_job(label: str, job_key: str, prompt: str, cwd: str) -> str:
    """Start a headless Claude job; return job_id. Deduplicates by job_key."""
    for j in state._jobs.values():
        if j["job_key"] == job_key and j["status"] == "running":
            return j["id"]
    job_id = str(uuid.uuid4())[:8]
    state._jobs[job_id] = {
        "id": job_id,
        "label": label,
        "job_key": job_key,
        "status": "pending",
        "output_lines": [],
        "proc": None,
        "created_at": time.time(),
    }
    t = threading.Thread(target=_run_claude_job, args=(job_id, prompt, cwd), daemon=True)
    t.start()
    return job_id
