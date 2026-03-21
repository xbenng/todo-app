# Architecture Improvement Recommendations

15 specific, actionable recommendations organized by priority tier. Each includes the finding, risk assessment, recommended fix, and estimated effort.

---

## Priority 1: Critical (Security + Stability)

### 1. Hardcoded OAuth State Secret

- **Location:** `app.py` — `_OAUTH_SECRET` defaults to `"todo-app-oauth-state-secret"` if the `OAUTH_STATE_SECRET` env var is unset.
- **Risk:** Anyone who knows the default secret can forge OAuth state parameters, enabling CSRF attacks on the OAuth callback endpoint. Since the default is committed in source, it's effectively public.
- **Fix:** Require `OAUTH_STATE_SECRET` as a mandatory environment variable. Fail loudly at startup if missing:
  ```python
  _OAUTH_SECRET = os.environ["OAUTH_STATE_SECRET"]  # crash if unset
  ```
- **Effort:** Small — 1 line change + env var documentation.

### 2. No CSRF Protection

> **Status: COMPLETE** — SameSite=Strict cookies + Content-Type: application/json enforcement on POST/PUT/DELETE.

- **Finding:** Session tokens are stored as cookies, but no CSRF tokens are validated on state-changing endpoints (POST, PUT, DELETE).
- **Risk:** Cross-site request forgery on all state-changing API endpoints. A malicious page could trigger todo creation, deletion, or configuration changes via the user's session cookie.
- **Fix:** Add `SameSite=Strict` attribute to the `session_token` cookie + validate a custom header (e.g., `X-Requested-With: XMLHttpRequest`) on state-changing routes. The inline JS already sends JSON with `Content-Type: application/json` which provides partial protection, but explicit validation is needed.
- **Effort:** Small — cookie attribute change + a small middleware function.

### 3. No Rate Limiting

- **Finding:** No rate limits on any endpoint. Auth endpoints (`/api/auth/login`, `/api/auth/register`) are vulnerable to brute-force. Chat endpoints can trigger unbounded LLM API calls. No `MAX_CONTENT_LENGTH` is set on the Flask app.
- **Risk:** Credential stuffing attacks, accidental LLM cost runaway, denial of service via large request bodies.
- **Fix:**
  - Add Flask-Limiter with rules like `5/minute` on login, `10/minute` on register.
  - Set `app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024` (10MB).
  - Cap the agentic loop iterations for the Anthropic provider (currently unbounded until `end_turn`).
- **Effort:** Medium — add dependency, decorator on routes, config constant.

### 4. Global Mutable State Not Process-Safe

- **Finding:** `_jobs`, `_mcp_managers`, `_pending_approvals`, `_pty_sessions`, and `_undo_stacks` are all in-process Python dicts. They are not shared across OS processes.
- **Risk:** Multi-worker deployment (e.g., `gunicorn -w 4`) silently breaks — a job created in worker 1 is invisible to worker 2. Tool approval requests cross workers. Terminal sessions orphaned.
- **Fix:**
  - **Short-term:** Document the single-worker requirement. Add a startup check that warns/fails if `WEB_CONCURRENCY > 1`.
  - **Medium-term:** Move job state and pending approvals to PostgreSQL (a `jobs` table).
  - **Long-term:** Use Redis for ephemeral state, or adopt a task queue (Celery, dramatiq) for agent jobs.
- **Effort:** Small (docs) to Large (Redis/task queue migration).

---

## Priority 2: High (Maintainability)

### 5. Monolithic `app.py` (9,713 Lines)

> **Status: COMPLETE** — Decomposed into 21 files. app.py is now 127 lines. Services, routes, state, and schemas in separate modules.

- **Finding:** All routes, classes (`MCPManager`, `ChatAgent`), business logic, and ~5,137 lines of inline HTML/CSS/JS are in a single file. The inline `HTML_PAGE` string (lines 4528-9665) alone is larger than most web applications.
- **Risk:** Merge conflicts on any change, extremely hard to navigate, impossible to test individual components in isolation, no separation of concerns.
- **Fix:** Extract into a Flask Blueprint package structure:
  ```
  dossie/
    __init__.py           # Flask app factory
    routes/
      auth.py             # Auth routes (register, login, logout, me)
      todos.py            # Todo CRUD + sections
      chat.py             # Chat, conversations, unread
      mcp.py              # MCP status, reconnect, approve, accounts, OAuth
      config.py           # Config get/put
      history.py          # History + git
      terminal.py         # Terminal + WebSocket
      jobs.py             # Job list, stream, kill
      ea.py               # EA update endpoints
    services/
      mcp_manager.py      # MCPManager class
      chat_agent.py       # ChatAgent class
      job_system.py       # Job lifecycle management
      tool_executor.py    # _execute_tool + _get_tool_definitions
      subagent.py         # Sub-agent spawning
    templates/
      login.html          # Extracted LOGIN_PAGE
      app.html            # Extracted HTML_PAGE
    static/
      js/app.js           # Extracted frontend JavaScript
      css/app.css         # Extracted frontend CSS
  ```
- **Effort:** Large — mechanical but high-touch. Every import, global reference, and circular dependency needs resolution.

### 6. Dual-Mode Branching Throughout Codebase

> **Status: COMPLETE** — File mode deprecated. DATABASE_URL required. 816 lines of branching removed.

- **Finding:** `if _USE_DB` conditionals are scattered across route handlers and tool execution. Every operation has two code paths (file-based vs database), roughly doubling maintenance burden.
- **Risk:** Bugs in one mode go undetected when testing the other. Feature additions require implementing in both modes. The file mode lacks many DB mode features (sections, history restore, MCP preferences).
- **Fix:** Define a `StorageBackend` protocol/ABC with `FileBackend` and `DBBackend` implementations. Inject the active backend at startup via app config. Route handlers call `backend.create_todo()` without branching. Consider deprecating file mode if only DB mode is used in production.
- **Effort:** Large — requires defining the interface, implementing both backends, and updating all call sites.

### 7. No Request Validation Framework

> **Status: COMPLETE** — Pydantic v2 with 18 request models and @validate_request decorator in schemas.py.

- **Finding:** Every route manually parses `request.json` with `data.get("field", "").strip()`. Validation is inconsistent — some routes check for empty titles, others don't. Invalid priority/status values silently default rather than returning errors.
- **Risk:** Silent data corruption, inconsistent error messages across endpoints, no auto-generated API documentation.
- **Fix:** Adopt Pydantic or marshmallow for request/response schemas:
  ```python
  class CreateTodoRequest(BaseModel):
      title: str = Field(..., min_length=1)
      description: str = ""
      priority: Literal["high", "medium", "low", "none"] = "none"
      section: str = ""
  ```
  This also enables auto-generated OpenAPI/Swagger docs.
- **Effort:** Medium — schema definition per route, but can be done incrementally.

### 8. No Test Coverage for `app.py` or `db.py`

- **Finding:** The only tests in the project are for the CalDAV MCP server (an external dependency). The core application (`app.py`, `db.py`) has zero test coverage.
- **Risk:** Every change risks regressions. Refactoring (e.g., recommendation #5) is dangerous without tests. No confidence in deployments.
- **Fix:** Add pytest + Flask test client. Priority test targets:
  1. Auth flow: register -> login -> me -> logout -> 401 on protected routes
  2. Todo CRUD: create -> read -> update (status, priority, section) -> search -> delete
  3. Chat message persistence: add_message -> get_messages -> restart_conversation
  4. MCP tool permission flow: auto-approve vs manual approval
  5. History: create -> update -> restore from snapshot
  Target: 50% coverage of non-HTML lines as first milestone.
- **Effort:** Large — but highest ROI for future development velocity.

---

## Priority 3: Medium (Performance + Reliability)

### 9. Unbounded Job Accumulation

- **Finding:** The `_jobs` dict only purges stale entries when `GET /api/jobs` is called. If the frontend doesn't poll that endpoint, jobs accumulate forever. Each job stores `output_lines` — an unbounded list of strings that can grow to megabytes for long agent conversations.
- **Risk:** Memory leak over time. Server eventually OOMs after enough chat sessions.
- **Fix:**
  - Add a background cleanup thread (daemon, runs every 5 minutes) that purges completed/killed/error jobs older than 30 minutes.
  - Cap `output_lines` at 10,000 entries using a ring buffer (`collections.deque(maxlen=10000)`).
- **Effort:** Small — ~20 lines of code.

### 10. MCPManager Lifecycle Leaks

- **Finding:** Per-user `MCPManager` instances spawn background daemon threads with asyncio event loops. These threads run forever — there's no idle timeout or cleanup when users log out or become inactive. `_user_temp_dirs` creates temporary directories that are never cleaned up during runtime.
- **Risk:** Thread and file descriptor accumulation over time. Each MCPManager holds connections to up to 5 external processes.
- **Fix:**
  - Track `last_tool_call` timestamp on each MCPManager.
  - Add idle timeout: stop and remove managers after 30 minutes of no tool calls.
  - Clean temp dirs on `manager.stop()`.
  - The cleanup can run in the same background thread as job cleanup (recommendation #9).
- **Effort:** Small — ~30 lines of code.

### 11. Connection Pool Has No `getconn()` Timeout

- **Finding:** The `_conn()` context manager calls `_pool.getconn()` which blocks indefinitely if all 20 connections are in use. Under load, this can deadlock Flask worker threads.
- **Risk:** Server hangs under concurrent load. No visibility into pool exhaustion.
- **Fix:** psycopg2's `ThreadedConnectionPool.getconn()` doesn't natively support timeouts. Options:
  - Wrap with a `threading.Event` and timeout.
  - Switch to psycopg3 which has native async pool support with timeouts.
  - Add pool utilization logging to detect saturation early.
- **Effort:** Small-Medium.

### 12. Inconsistent Error Handling

> **Status: COMPLETE** — Python logging module replaces print(). Global error handlers for 500/404/405. No tracebacks exposed.

- **Finding:** Errors are logged via `print()` statements (no structured logging). Error responses vary in shape: some return `{"error": "..."}` with HTTP 400/401/404, others return `{"ok": false}`. The OAuth callback returns raw HTML with Python tracebacks on error. Some exceptions are silently caught and ignored.
- **Risk:** Debugging is difficult. Error tracebacks leak internal details to users. Silent failures mask bugs.
- **Fix:**
  - Adopt Python `logging` module with structured JSON output.
  - Define a standard error response format: `{"error": "Human-readable message", "code": "ERROR_CODE"}`.
  - Add a Flask `@app.errorhandler(Exception)` for unhandled exceptions — return 500 with generic message, log full traceback server-side.
  - Never expose tracebacks to the client.
- **Effort:** Medium — touches many files but each change is small.

---

## Priority 4: Lower (Enhancement)

### 13. Inline Frontend with No Build Tooling

> **Status: PARTIAL** — Phase 1 complete: HTML/CSS/JS extracted to templates/ and static/. Phase 2 (Vite/TypeScript) not done.

- **Finding:** ~5,137 lines of HTML, CSS, and JavaScript are embedded as a Python string literal in `app.py` (lines 4528-9665). There's no type checking, no minification, no tree-shaking, no hot reload for frontend development. External dependencies (marked, xterm.js, CodeMirror, ldrs) are loaded via CDN `<script>` tags.
- **Risk:** Frontend bugs are hard to catch. No IDE support (syntax highlighting, autocomplete) for JS inside a Python string. Every frontend change requires restarting the Python server.
- **Fix:**
  - **Phase 1** (lowest friction): Extract to `templates/app.html` and `static/js/app.js` + `static/css/app.css`. Use Flask's `render_template()` and `url_for('static', ...)`. This alone gives IDE support and file-level caching.
  - **Phase 2** (optional): Add Vite + TypeScript for type safety, bundling, and hot module replacement.
- **Effort:** Medium (Phase 1), Large (Phase 2).

### 14. Session Management Gaps

- **Finding:** Sessions expire after 720 hours (30 days) with no rotation. There's no session invalidation on password change — old sessions remain valid. Session tokens are random strings stored in the `sessions` table.
- **Risk:** Compromised session tokens remain valid for 30 days. Password changes don't revoke existing sessions.
- **Fix:**
  - Shorter default TTL (24 hours) with an optional "remember me" checkbox (30 days).
  - Invalidate all sessions on password change: `DELETE FROM sessions WHERE user_id = %s`.
  - Rotate session token on sensitive actions (password change, provider config update).
- **Effort:** Small.

### 15. History Compaction May Lose Context

- **Finding:** When a conversation exceeds the threshold (~100K chars and >8 messages), the `ChatAgent._maybe_compact()` method calls the LLM to summarize older messages into a single summary (2048 token max). Tool call results — which may contain critical data like email contents, calendar events, or spreadsheet values — can be lost in the summarization.
- **Risk:** Agent loses access to important prior context. Decisions made based on tool results become unjustifiable when the original results are compacted away.
- **Fix:**
  - Preserve tool call results separately from compaction (exclude `role: "tool"` messages from the summary input, keep them as-is).
  - Store the full uncompacted history in the database even when sending the compacted version to the LLM context window.
  - Allow users to "expand" compacted history in the chat UI to see the original messages.
- **Effort:** Medium.

---

## Summary

| Priority | # | Issue | Effort | Impact | Status |
|----------|---|-------|--------|--------|--------|
| Critical | 1 | Hardcoded OAuth secret | Small | Security | |
| Critical | 2 | No CSRF protection | Small | Security | COMPLETE |
| Critical | 3 | No rate limiting | Medium | Security + Cost | |
| Critical | 4 | Global state not process-safe | Small-Large | Stability | |
| High | 5 | Monolithic app.py | Large | Maintainability | COMPLETE |
| High | 6 | Dual-mode branching | Large | Maintainability | COMPLETE |
| High | 7 | No request validation | Medium | Data integrity | COMPLETE |
| High | 8 | No test coverage | Large | Reliability | |
| Medium | 9 | Unbounded job accumulation | Small | Memory | |
| Medium | 10 | MCPManager lifecycle leaks | Small | Resources | |
| Medium | 11 | No connection pool timeout | Small-Medium | Stability | |
| Medium | 12 | Inconsistent error handling | Medium | Debuggability | COMPLETE |
| Lower | 13 | Inline frontend | Medium-Large | Developer experience | PARTIAL |
| Lower | 14 | Session management gaps | Small | Security | |
| Lower | 15 | History compaction context loss | Medium | AI quality | |

### Recommended Starting Order

1. **Items 1-2** (hardcoded secret + CSRF) — smallest effort, highest security impact
2. **Item 9** (job cleanup) — prevents memory leaks, ~20 lines
3. **Item 10** (MCPManager cleanup) — prevents resource leaks, ~30 lines
4. **Item 8** (test coverage) — unblocks all future refactoring
5. **Item 5** (monolith extraction) — once tests exist, decompose safely
