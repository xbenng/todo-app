# Dossie Todo App -- API Reference

## Table of Contents

1. [Authentication](#1-authentication)
2. [REST API Endpoints](#2-rest-api-endpoints)
   - [Auth](#auth)
   - [Todos](#todos)
   - [Sections](#sections)
   - [Chat](#chat)
   - [Jobs](#jobs)
   - [MCP](#mcp)
   - [Config](#config)
   - [History](#history)
   - [Git](#git)
   - [Terminal](#terminal)
   - [EA (Executive Assistant)](#ea-executive-assistant)
   - [Utility](#utility)
3. [WebSocket Protocol](#3-websocket-protocol)
4. [SSE Job Stream Protocol](#4-sse-job-stream-protocol)
5. [MCP Tool Interface](#5-mcp-tool-interface)
6. [Error Response Format](#6-error-response-format)

---

## 1. Authentication

All endpoints require authentication unless noted otherwise. Authentication is provided via one of:

- **Session cookie**: `session_token` (set by `/api/auth/login` and `/api/auth/register`)
- **Authorization header**: `Authorization: Bearer <token>`

**Unauthenticated endpoints** (no session/token required):

- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /` (returns the login page if unauthenticated in DB mode)

**File mode** (no `DATABASE_URL` environment variable set): All authentication is bypassed. Every request is treated as coming from a stub user `{"id": "local", "email": "local", "name": "Local User"}`. The auth endpoints return errors in this mode since they are not applicable.

---

## 2. REST API Endpoints

### Auth

#### POST /api/auth/register

Create a new user account. Only available in DB mode.

**Request Body:**
```json
{
  "email": "string (required)",
  "password": "string (required, min 6 chars)",
  "name": "string (optional)"
}
```

**Response (200):**
```json
{
  "user": {
    "id": "string",
    "email": "string",
    "name": "string"
  }
}
```
Sets `session_token` cookie (httponly, SameSite=Lax, 30-day expiry).

**Error Responses:**
- `400` -- `{"error": "Email and password required"}` or `{"error": "Password must be at least 6 characters"}` or `{"error": "Auth not available in file mode"}`
- `409` -- `{"error": "Email already registered"}`
- `500` -- `{"error": "<message>"}`

---

#### POST /api/auth/login

Authenticate an existing user. Only available in DB mode.

**Request Body:**
```json
{
  "email": "string (required)",
  "password": "string (required)"
}
```

**Response (200):**
```json
{
  "user": {
    "id": "string",
    "email": "string",
    "name": "string"
  }
}
```
Sets `session_token` cookie (httponly, SameSite=Lax, 30-day expiry).

**Error Responses:**
- `400` -- `{"error": "Auth not available in file mode"}`
- `401` -- `{"error": "Invalid email or password"}`

---

#### POST /api/auth/logout

Invalidate the current session.

**Request Body:** None

**Response (200):**
```json
{"ok": true}
```
Deletes the `session_token` cookie. In DB mode, the server-side session is also deleted.

---

#### GET /api/auth/me

Return the currently authenticated user.

**Response (200):**
```json
{
  "user": {
    "id": "string",
    "email": "string",
    "name": "string"
  }
}
```

**Error Responses:**
- `401` -- `{"error": "Not authenticated"}`

---

### Todos

#### GET /api/todos

List all todos for the current user (active + completed).

**Response (200):**
```json
[
  {
    "id": "string",
    "title": "string",
    "description": "string",
    "status": "open" | "completed",
    "priority": "high" | "medium" | "low" | "none",
    "section": "string"
  }
]
```

**Error Responses:**
- `401` -- Not authenticated (DB mode only)

---

#### POST /api/todos

Create a new todo item.

**Request Body:**
```json
{
  "title": "string (required)",
  "description": "string (optional, default \"\")",
  "priority": "high" | "medium" | "low" | "none" (optional, default "medium"),
  "section": "string (optional, default \"\")",
  "before_id": "string (optional, file mode only -- insert before this item)"
}
```

**Response (201):**
```json
{
  "id": "string",
  "title": "string",
  "description": "string",
  "status": "open",
  "priority": "string",
  "section": "string"
}
```

**Error Responses:**
- `400` -- `{"error": "Title is required"}`
- `401` -- Not authenticated (DB mode only)

---

#### GET /api/todos/\<id\>

Get a single todo by ID.

**Response (200):**
```json
{
  "id": "string",
  "title": "string",
  "description": "string",
  "status": "open" | "completed",
  "priority": "string",
  "section": "string"
}
```

**Error Responses:**
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Not found"}`

---

#### PUT /api/todos/\<id\>

Update a todo item. Only the provided fields are changed; omitted fields are preserved.

**Request Body:**
```json
{
  "title": "string (optional)",
  "description": "string (optional)",
  "status": "open" | "completed" (optional),
  "priority": "high" | "medium" | "low" | "none" (optional),
  "section": "string (optional)",
  "mark_unread": "boolean (optional, DB mode -- marks chat as unread)"
}
```

**Response (200):**
```json
{
  "id": "string",
  "title": "string",
  "description": "string",
  "status": "string",
  "priority": "string",
  "section": "string"
}
```

**Error Responses:**
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Not found"}`

**Notes:** Invalid `status` values (not `"open"` or `"completed"`) and invalid `priority` values (not in `{"high", "medium", "low", "none"}`) are silently ignored.

---

#### DELETE /api/todos/\<id\>

Delete a todo item.

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Not found"}`

---

#### GET /api/todos/search

Search todos by text query across titles and descriptions (case-insensitive substring match).

**Query Parameters:**
- `q` -- Search query string (required)

**Response (200):**
```json
[
  {
    "id": "string",
    "title": "string",
    "description": "string",
    "status": "string",
    "priority": "string",
    "section": "string"
  }
]
```
Returns an empty array if `q` is empty.

**Error Responses:**
- `401` -- Not authenticated (DB mode only)

---

#### POST /api/todos/reorder

Move a todo item up or down within or across sections.

**Request Body:**
```json
{
  "id": "string (required)",
  "direction": "up" | "down" (required)
}
```

**Response (200):**
```json
{"ok": true, "moved": true | false}
```
`moved` is `false` if the item is already at the boundary and cannot move further.

**Error Responses:**
- `400` -- `{"error": "id and direction (up/down) required"}`
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Not found or not an active item"}`

---

#### POST /api/todos/move-to-top

Move a todo to the top of its section.

**Request Body:**
```json
{
  "id": "string (required)"
}
```

**Response (200):**
```json
{"ok": true, "moved": true | false}
```

**Error Responses:**
- `400` -- `{"error": "id required"}`
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Not found or not an active item"}`

---

#### POST /api/todos/sort-priority

Sort all todos within a section by priority (high > medium > low > none).

**Request Body:**
```json
{
  "section": "string (optional, default \"\")"
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `401` -- Not authenticated (DB mode only)

---

#### POST /api/todos/drop

Move a todo to a specific position via drag-and-drop. Inserts before another item, or at the end of a target section.

**Request Body:**
```json
{
  "id": "string (required)",
  "before_id": "string (optional -- insert before this item)",
  "section": "string (optional -- target section if before_id is null)"
}
```
If `before_id` is provided, the item is inserted before that item and inherits its section. If only `section` is provided, the item is appended to the end of that section. If neither is provided, the item is appended to the end of the list.

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "id required"}`
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Not found or not an active item"}`

---

#### POST /api/todos/\<id\>/mark-read

Replace the `updated ...` tag in the todo title with `read <timestamp>`.

**Request Body:** None

**Response (200):**
```json
{
  "id": "string",
  "title": "string (updated)",
  "description": "string",
  "status": "string",
  "priority": "string",
  "section": "string"
}
```

**Error Responses:**
- `404` -- `{"error": "Not found"}`

**Notes:** File mode only. Uses regex to replace backtick-delimited `updated ...` tags in the title.

---

#### GET /api/todos/mtime

Return the latest modification time for change detection (polling).

**Response (200):**
```json
{"mtime": number}
```
In DB mode, returns the max `updated_at` timestamp for the user's todos. In file mode, returns the filesystem mtime of the todo files.

---

### Sections

#### GET /api/sections

Return sections for the current user, ordered by position. DB mode only.

**Response (200):**
```json
[
  {
    "name": "string",
    "directives": "string | null",
    "position": number
  }
]
```
Returns `[]` in file mode or if unauthenticated.

---

#### PUT /api/sections

Update a section's directives. DB mode only.

**Request Body:**
```json
{
  "name": "string (required)",
  "directives": "string (optional)"
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "name required"}` or `{"error": "Not available"}`

---

#### POST /api/sections/rename

Rename a section across all todos.

**Request Body:**
```json
{
  "old_name": "string (required)",
  "new_name": "string (required)"
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "old_name and new_name required"}`
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Section not found"}`

---

#### POST /api/sections/reorder

Move a section (and all its todos) before another section.

**Request Body:**
```json
{
  "section": "string (required)",
  "before_section": "string | null (optional -- null moves to end)"
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "section required"}`
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Section not found"}` (file mode only)

---

### Chat

#### POST /api/todos/\<id\>/chat

Send a chat message for a todo item, starting or resuming an AI conversation.

**Request Body:**
```json
{
  "message": "string (required)",
  "resume_conv": "integer (optional, DB mode -- resume a specific conversation number)"
}
```

**Response (200):**
```json
{
  "job_id": "string",
  "conversation_id": "string | null"
}
```
The `job_id` can be used with the SSE stream endpoint (`/api/jobs/<id>/stream`) to receive the AI's response in real time. The user message is persisted immediately.

**Error Responses:**
- `400` -- `{"error": "message is required"}`
- `401` -- Not authenticated (DB mode only)
- `404` -- `{"error": "Todo not found"}`

---

#### GET /api/chats/\<id\>

Return the persisted chat for a todo item, including any running job reference.

**Query Parameters:**
- `include_tool` -- `"true"` to include tool-use messages (default `"false"`)

**Response (200):**
```json
{
  "conversationId": "string | null",
  "messages": [
    {
      "role": "user" | "assistant",
      "content": "string"
    }
  ],
  "running_job_id": "string (present only if a job is currently running)"
}
```

**Notes:** By default, structured tool-use messages from the assistant are filtered out or have their text content extracted. Pass `include_tool=true` to get raw messages.

---

#### DELETE /api/chats/\<id\>

Restart the chat for a todo -- starts a new conversation. In DB mode, old messages are preserved in the conversation history. In file mode, messages are deleted.

**Response (200):**
```json
{"ok": true}
```

---

#### GET /api/chats/\<id\>/conversations

List all conversations for a todo. DB mode only.

**Response (200):**
```json
{
  "conversations": [
    {
      "conversation_number": number,
      "message_count": number,
      "started_at": "string (ISO timestamp)",
      "last_message_at": "string (ISO timestamp)"
    }
  ],
  "current": number
}
```
Returns `{"conversations": []}` in file mode.

---

#### GET /api/chats/\<id\>/conversations/\<num\>

Get messages from a specific past conversation. DB mode only.

**Response (200):**
```json
{
  "messages": [
    {
      "role": "user" | "assistant",
      "content": "string"
    }
  ]
}
```
Returns `{"messages": []}` in file mode.

---

#### GET /api/chats/unread

Return todo IDs that have unread chat responses.

**Response (200):**
```json
["todo_id_1", "todo_id_2"]
```

---

#### POST /api/chats/\<id\>/read

Mark a chat as read.

**Response (200):**
```json
{"ok": true}
```

---

### Jobs

#### GET /api/jobs

List all jobs. Stale jobs (completed/killed older than 30 minutes) are automatically purged.

**Response (200):**
```json
[
  {
    "id": "string",
    "label": "string",
    "job_key": "string",
    "status": "pending" | "running" | "done" | "error" | "killed",
    "line_count": number,
    "created_at": number (unix timestamp),
    "conversation_id": "string | null"
  }
]
```

---

#### GET /api/jobs/\<id\>/stream

SSE stream of output lines for a job. See [SSE Job Stream Protocol](#4-sse-job-stream-protocol) for details.

**Response:** `Content-Type: text/event-stream`

**Error Responses:**
- `404` -- `{"error": "not found"}`

---

#### POST /api/jobs/\<id\>/kill

Cancel a running job. Kills the subprocess (if local) or closes the API stream (if server-side agent).

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `404` -- `{"error": "not found"}`

---

### MCP

#### GET /api/mcp/status

Return status of all MCP servers from the registry, with per-user preferences and connection state.

**Response (200):**
```json
{
  "servers": [
    {
      "name": "string (registry key, e.g. \"slack\")",
      "label": "string (display name, e.g. \"Slack\")",
      "enabled": boolean,
      "connected": boolean,
      "tool_count": number,
      "error": "string (present only on connection failure)",
      "disabled_tools": ["string"],
      "auto_approved_tools": ["string"],
      "credential_fields": [
        {
          "key": "string",
          "label": "string",
          "type": "text" | "password",
          "has_value": boolean
        }
      ],
      "account_fields": [
        {
          "key": "string",
          "label": "string",
          "type": "text" | "password" | "number" | "boolean" | "select",
          "required": boolean,
          "default": "any (optional)",
          "placeholder": "string (optional)",
          "options": ["string (for select type)"]
        }
      ],
      "account_count": number,
      "oauth_providers": [
        {"id": "string", "label": "string"}
      ],
      "bearer_token_key": "string",
      "bearer_connected": boolean,
      "tools": [
        {"name": "string (bare tool name)", "description": "string (truncated to 120 chars)"}
      ]
    }
  ],
  "available": boolean,
  "auto_approve_all": boolean
}
```

---

#### POST /api/mcp/reconnect

Restart MCP server connections for the current user. Destroys the old manager and lazily creates a new one.

**Response (200):**
```json
{"ok": true, "connected": boolean}
```

---

#### POST /api/mcp/approve

Approve or deny a pending MCP tool execution. Tools that require user approval emit an approval request through the SSE job stream; the UI calls this endpoint to respond.

**Request Body:**
```json
{
  "approval_id": "string (required)",
  "approved": boolean,
  "always_allow": boolean (optional -- if true and approved, auto-approve this tool in the future)
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `404` -- `{"error": "No pending approval with that ID"}`

---

#### PUT /api/mcp/servers

Enable or disable an MCP server for the current user. DB mode only.

**Request Body:**
```json
{
  "server": "string (required -- registry key)",
  "enabled": boolean
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "Unknown server: <name>"}`
- `401` -- Not authenticated

**Notes:** Triggers an MCP reconnect to pick up the change.

---

#### PUT /api/mcp/tools

Set tool disabled or auto-approval state. DB mode only.

**Request Body:**
```json
{
  "server": "string (required)",
  "tool": "string (required -- bare tool name)",
  "disabled": boolean (optional),
  "auto_approved": boolean (optional)
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "server and tool required"}`
- `401` -- Not authenticated

---

#### GET /api/mcp/accounts/\<server\>

Get all accounts for a server. DB mode only.

**Response (200):**
```json
{
  "accounts": [
    {
      "id": "string",
      "config": {
        "name": "string",
        "host": "string",
        "...": "...",
        "oauth_connected": boolean (present if OAuth was used)
      }
    }
  ]
}
```
Passwords are redacted to `"***"`. OAuth token objects are replaced with an `oauth_connected: true` flag.

**Error Responses:**
- `401` -- Not authenticated

---

#### POST /api/mcp/accounts/\<server\>

Add a new account for a server. DB mode only.

**Request Body:** Object with fields matching the server's `account_fields` from the registry.

**Response (201):**
```json
{
  "id": "string",
  "config": { "...": "..." }
}
```

**Error Responses:**
- `400` -- `{"error": "Unknown server: <name>"}`
- `401` -- Not authenticated

**Notes:** Triggers an MCP reconnect.

---

#### PUT /api/mcp/accounts/\<server\>/\<id\>

Update an existing account. DB mode only.

**Request Body:** Object with fields to update. Passwords set to `"***"` are ignored (preserves existing value).

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `401` -- Not authenticated
- `404` -- `{"error": "Not found"}`

**Notes:** Triggers an MCP reconnect.

---

#### DELETE /api/mcp/accounts/\<server\>/\<id\>

Delete an account. DB mode only.

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `401` -- Not authenticated
- `404` -- `{"error": "Not found"}`

**Notes:** Triggers an MCP reconnect.

---

#### GET /api/mcp/oauth/start

Start an OAuth flow. Returns an authorization URL for the UI to open in a popup.

**Query Parameters:**
- `server` -- Registry key (required)
- `provider` -- OAuth provider ID (required, e.g. `"google"`, `"slack"`)
- `account_id` -- Existing account ID to link (optional)

**Response (200):**
```json
{"auth_url": "string (full OAuth authorization URL)"}
```

**Error Responses:**
- `400` -- `{"error": "Unknown server: <name>"}` or `{"error": "Unknown OAuth provider: <id>"}`
- `401` -- Not authenticated
- `500` -- `{"error": "OAuth client_id not configured (check env vars)"}`

---

#### GET /api/mcp/oauth/callback

OAuth callback endpoint. Exchanges the authorization code for tokens, stores them, and returns HTML that closes the popup window. Not called directly by the client; the OAuth provider redirects here.

**Query Parameters:**
- `code` -- Authorization code (provided by OAuth provider)
- `state` -- Signed state parameter (provided by OAuth provider)
- `error` -- Error string (provided by OAuth provider on failure)

**Response:** HTML page that posts `{type: "oauth_complete"}` to the opener window and auto-closes.

---

### Config

#### GET /api/config

Return server configuration with API keys redacted.

**Response (200):**
```json
{
  "anthropic_api_key": "string (redacted)",
  "model": "string",
  "providers": {
    "<name>": {
      "type": "anthropic" | "openai_compat",
      "api_key": "string (redacted)",
      "base_url": "string (for openai_compat)",
      "model": "string",
      "max_tokens": number
    }
  },
  "active_provider": "string",
  "tokens": { "...": "..." },
  "subagents_enabled": boolean,
  "max_subagents": number,
  "auto_approve_all": boolean,
  "_active_provider_name": "string",
  "_active_provider_type": "string"
}
```

**Notes:** The exact shape depends on what has been configured. Redacted API keys use the format `first8...last4`.

---

#### PUT /api/config

Update server configuration. Merges provided fields into existing config.

**Request Body:**
```json
{
  "anthropic_api_key": "string (optional)",
  "model": "string (optional)",
  "active_provider": "string (optional)",
  "subagents_enabled": boolean (optional),
  "max_subagents": number (optional),
  "auto_approve_all": boolean (optional),
  "providers": {
    "<name>": {
      "type": "string",
      "api_key": "string",
      "base_url": "string",
      "model": "string"
    } | null (null deletes the provider)
  },
  "tokens": { "<key>": "string" }
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"ok": false, "error": "API key for '<name>' contains invalid characters. Please paste only the key."}`

**Notes:** When updating providers, API keys that contain `"..."` are treated as redacted and are not overwritten. Tokens are merged (not replaced).

---

### History

#### GET /api/history

Return recent version history for the current user. DB mode only.

**Response (200):**
```json
{
  "entries": [
    {
      "id": number,
      "todo_id": "string",
      "title": "string",
      "snapshot": { "...": "..." },
      "changed_by": "string",
      "created_at": "string (ISO timestamp)"
    }
  ]
}
```
Returns `{"entries": []}` in file mode.

---

#### GET /api/todos/\<id\>/history

Return version history for a specific todo item. DB mode only.

**Response (200):**
```json
{
  "entries": [
    {
      "id": number,
      "todo_id": "string",
      "title": "string",
      "snapshot": { "...": "..." },
      "changed_by": "string",
      "created_at": "string (ISO timestamp)"
    }
  ]
}
```
Returns `{"entries": []}` in file mode.

---

#### POST /api/history/\<id\>/restore

Restore a todo from a history snapshot. DB mode only.

**Response (200):**
```json
{
  "id": "string",
  "title": "string",
  "description": "string",
  "status": "string",
  "priority": "string",
  "section": "string"
}
```

**Error Responses:**
- `400` -- `{"error": "Not available"}`
- `404` -- `{"error": "History entry not found"}`

---

### Git

These endpoints operate on the git repository containing the todo file. File mode only.

#### GET /api/git/log

Return recent git commit log for the todo files (up to 30 entries).

**Response (200):**
```json
{
  "commits": [
    {
      "hash": "string (full SHA)",
      "date": "string (ISO-like date)",
      "message": "string"
    }
  ],
  "git_dir": "string (absolute path to git root)"
}
```

**Error Responses:**
- `404` -- `{"error": "No git repo found for todo file"}`
- `500` -- `{"error": "<message>"}`

---

#### POST /api/git/commit

Commit current todo files to git.

**Request Body:**
```json
{
  "message": "string (optional, defaults to 'Manual save YYYY-MM-DD HH:MM')"
}
```

**Response (200):**
```json
{"ok": true, "message": "string"}
```

**Error Responses:**
- `404` -- `{"error": "No git repo found"}`
- `500` -- `{"error": "<message>"}`

**Notes:** If there are no changes, returns `{"ok": true, "message": "No changes to commit"}`.

---

#### POST /api/git/rollback

Rollback todo files to a specific commit. Checks out the files from that commit and creates a new rollback commit.

**Request Body:**
```json
{
  "hash": "string (required -- commit SHA)"
}
```

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "hash is required"}`
- `404` -- `{"error": "No git repo found"}`
- `500` -- `{"error": "<message>"}`

---

### Terminal

#### POST /api/todos/\<id\>/terminal

Create a tmux-backed interactive terminal session for a todo. Returns existing session if one is alive. Launches Claude Code in the tmux session and auto-sends `/ea workon <id>`.

**Request Body:**
```json
{
  "resume_id": "string (optional -- Claude conversation ID to resume)"
}
```

**Response (200):**
```json
{
  "session_id": "string",
  "title": "string",
  "existing": boolean
}
```

**Error Responses:**
- `404` -- `{"error": "Todo not found"}`
- `500` -- `{"error": "claude binary not found"}`

---

#### GET /api/terminal/sessions

List all terminal sessions. Syncs alive state with tmux and purges dead sessions older than 5 minutes.

**Response (200):**
```json
[
  {
    "session_id": "string",
    "todo_id": "string",
    "title": "string",
    "alive": boolean,
    "created_at": number (unix timestamp)
  }
]
```

---

#### POST /api/terminal/\<id\>/kill

Kill a terminal session by destroying its tmux session.

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `404` -- `{"error": "not found"}`

---

#### WebSocket /api/terminal/\<session_id\>/ws

WebSocket endpoint for interactive terminal I/O. See [WebSocket Protocol](#3-websocket-protocol).

---

### EA (Executive Assistant)

#### POST /api/ea-update

Run an `/ea update` sweep via the ChatAgent. Checks all connected services for updates.

**Request Body:**
```json
{
  "force": boolean (optional -- skip duplicate check, default false)
}
```

**Response (200):**
```json
{"status": "started", "job_id": "string"}
```
or, if already running and `force` is false:
```json
{"status": "already_running", "job_id": "string"}
```

---

#### POST /api/ea-update-item

Run an `/ea checkon <item_id>` on a specific todo via the ChatAgent.

**Request Body:**
```json
{
  "id": "string (required)",
  "message": "string (optional -- custom message, default '/ea checkon <id>')",
  "force": boolean (optional -- skip duplicate check, default false)
}
```

**Response (200):**
```json
{"status": "started", "job_id": "string"}
```
or, if already running and `force` is false:
```json
{"status": "already_running", "job_id": "string"}
```

**Error Responses:**
- `400` -- `{"error": "id required"}`

---

#### POST /api/resume-conv

Resume a Claude conversation in a new tmux window.

**Request Body:**
```json
{
  "conversation_id": "string (required)"
}
```

**Response (200):**
```json
{"status": "resumed", "window": "string (tmux window name)"}
```

**Error Responses:**
- `400` -- `{"error": "No conversation ID provided"}`
- `500` -- `{"error": "tmux is not installed"}` or `{"error": "tmux error: <message>"}`

---

### Utility

#### POST /api/execute-tool

Unified tool execution endpoint. Executes a built-in or MCP tool directly.

**Request Body:**
```json
{
  "tool": "string (required -- tool name)",
  "input": { "...": "..." } (optional, default {}),
  "as_agent": boolean (optional -- if true, marks changes as unread, default false)
}
```

**Response (200):** Raw JSON string returned by the tool (not wrapped in an object). Content-Type is `application/json`.

**Error Responses:**
- `400` -- `{"error": "tool required"}`
- `401` -- `{"error": "Not authenticated"}`

**Notes:** Tool names for MCP tools use the `mcp__{server}__{tool_name}` naming convention.

---

#### POST /api/undo

Restore the previous file state from the undo stack. File mode only.

**Response (200):**
```json
{"ok": true}
```

**Error Responses:**
- `400` -- `{"error": "Nothing to undo"}`

**Notes:** The undo stack holds up to 30 entries. Only applies to file-mode todo operations.

---

## 3. WebSocket Protocol

**Endpoint:** `ws://<host>/api/terminal/<session_id>/ws`

The WebSocket bridges the browser to a tmux-backed terminal session (Claude Code running in a PTY).

### Client to Server

Messages can be sent as either JSON strings or raw bytes:

| Format | Type | Description |
|--------|------|-------------|
| JSON string | `{"type": "input", "data": "string"}` | Terminal input (keystrokes). Written to the PTY. |
| JSON string | `{"type": "resize", "rows": int, "cols": int}` | Resize the terminal. Updates both the PTY and the tmux window/pane. |
| JSON string | `{"type": "close"}` | Close the WebSocket connection. |
| Raw bytes | `bytes` | Direct terminal input. Written to the PTY as-is. |

### Server to Client

| Format | Description |
|--------|-------------|
| Raw bytes | Terminal output from the PTY. Sent as binary WebSocket frames. Contains raw terminal escape sequences (ANSI, xterm-256color). |
| JSON string | `{"type": "error", "msg": "string"}` -- sent if the tmux session is not found, then the connection is closed. |

### Connection Lifecycle

1. Client connects to `/api/terminal/<session_id>/ws`
2. Server verifies the session exists and the tmux session is alive
3. Server attaches to the tmux session via a PTY and begins bridging I/O
4. Connection ends when: the tmux process exits, the client sends `{"type": "close"}`, or the WebSocket is closed

---

## 4. SSE Job Stream Protocol

**Endpoint:** `GET /api/jobs/<id>/stream`

**Content-Type:** `text/event-stream`

The server sends `data:` lines, each containing a JSON-encoded value. The client should parse each `data:` line as JSON.

### Message Types

**Text line (string):**
```
data: "This is a line of assistant output"
```
Plain text output from the AI. May contain markdown.

**Tool call indicator (string):**
```
data: "▶ tool_name..."
```
Indicates a tool is being invoked.

**Tool approval request (object):**
```
data: {"__tool_approval__": true, "approval_id": "string", "tool": "mcp__server__tool_name", "tool_display": "tool_name", "server": "server_name", "args": {...}}
```
Requires the client to call `POST /api/mcp/approve` with the `approval_id`.

**Subagent progress (string):**
```
data: "[label] Progress message..."
```
Output from a subagent, prefixed with its label.

**Terminal event (object):**
```
data: {"__done__": true, "status": "done" | "error" | "killed", "conversation_id": "string | null"}
```
Indicates the job has finished. This is always the last message in the stream. The `conversation_id` field is present only for chat jobs that have a Claude CLI conversation ID.

### Polling Behavior

The server polls at 50ms intervals for new output lines. Lines are sent as they become available. The stream ends after the terminal event is sent.

---

## 5. MCP Tool Interface

### Naming Convention

MCP tools are namespaced using double underscores:

```
mcp__{server}__{tool_name}
```

For example: `mcp__slack__channels_list`, `mcp__caldav__caldav_get_events`

The `server` portion corresponds to the key in `registry.json` (e.g., `slack`, `imap`, `caldav`, `smartsheet`, `atlassian`).

### Built-in Tools (from todo-tools.py)

The `todo-tools` MCP server provides these tools, which proxy through the `/api/execute-tool` endpoint:

| Tool | Description | Required Args | Optional Args |
|------|-------------|---------------|---------------|
| `read_todos` | Read todo items. Returns summaries by default. | -- | `status_filter` (`"all"`, `"open"`, `"completed"`; default `"open"`), `detail` (boolean; default `false`) |
| `get_todo` | Get a single todo by ID with full details. | `todo_id` (string) | -- |
| `update_todo` | Update a todo item's fields. | `todo_id` (string) | `title`, `description`, `status` (`"open"`, `"completed"`), `priority` (`"high"`, `"medium"`, `"low"`, `"none"`), `section` |
| `create_todo` | Create a new todo item. | `title` (string) | `description`, `priority` (default `"none"`), `section` |
| `search_todos` | Search todos by text query. | `query` (string) | -- |

The MCP server reads `TODO_API_BASE` (default `http://localhost:5222`) and `TODO_AUTH_TOKEN` from environment variables to authenticate with the main app.

### Server-Side Tool Definitions (Anthropic API format)

In addition to MCP tools, the ChatAgent has built-in tools defined in Anthropic's tool format:

```json
{
  "name": "string",
  "description": "string",
  "input_schema": {
    "type": "object",
    "properties": { "...": "..." },
    "required": ["..."]
  }
}
```

Built-in server-side tools include: `read_todos`, `get_todo`, `update_todo`, `create_todo`, `search_todos`, `read_chat_history`, and `spawn_agents` (when subagents are enabled and depth < 2).

### Tool Definition Format

All tool definitions (built-in and MCP) follow the Anthropic API tool format:

```json
{
  "name": "mcp__slack__channels_list",
  "description": "List all channels in the Slack workspace",
  "input_schema": {
    "type": "object",
    "properties": {
      "limit": {"type": "integer", "description": "Max channels to return"}
    },
    "required": []
  }
}
```

### Registry Schema (registry.json)

The MCP server registry at `mcp-servers/registry.json` defines available servers. Each entry has the following shape:

```json
{
  "<server_key>": {
    "label": "string (display name)",
    "type": "stdio" | "sse" | "http" | "streamable-http",
    "command": "string (for stdio -- binary to run)",
    "args": ["string (command-line arguments)"],
    "url": "string (for sse/http/streamable-http -- endpoint URL)",
    "credential_fields": [
      {
        "key": "string (env var name)",
        "label": "string (UI label)",
        "type": "text" | "password"
      }
    ],
    "static_env": { "KEY": "VALUE" },
    "exclude_tools": ["tool_name_to_hide"],
    "account_fields": [
      {
        "key": "string",
        "label": "string",
        "type": "text" | "password" | "number" | "boolean" | "select",
        "required": boolean,
        "default": "any",
        "placeholder": "string",
        "options": ["string (for select)"]
      }
    ],
    "config_env": "string (env var for config file path)",
    "config_path": "string (relative path within HOME for config)",
    "config_format": "imap" | "caldav",
    "bearer_token": "string (token key for HTTP auth)",
    "basic_auth_token": "string (token key for Basic auth)",
    "oauth_providers": [
      {
        "id": "string",
        "label": "string",
        "client_id_env": "string (env var for client ID)",
        "client_secret_env": "string (env var for client secret)",
        "auth_uri": "string (authorization URL)",
        "token_uri": "string (token exchange URL)",
        "scope": "string (space-separated scopes)",
        "user_scope": "string (for Slack user tokens)",
        "token_path": "string (dot-path to extract access token from response)",
        "store_as": "string (store raw token under this credential key)",
        "store_as_oauth": "string (store full oauth_token object under this key)",
        "pkce": boolean,
        "account_template": { "...": "..." },
        "extra_auth_params": { "...": "..." }
      }
    ]
  }
}
```

---

## 6. Error Response Format

All API errors use a consistent JSON format:

```json
{"error": "Human-readable error message"}
```

### Common HTTP Status Codes

| Status | Meaning |
|--------|---------|
| `400` | Bad request -- missing required fields, invalid input |
| `401` | Not authenticated -- missing or invalid session/token |
| `404` | Not found -- resource does not exist |
| `409` | Conflict -- duplicate resource (e.g., email already registered) |
| `500` | Server error -- unexpected failure |

### Examples

```json
// 400
{"error": "Title is required"}
{"error": "id and direction (up/down) required"}
{"error": "tool required"}

// 401
{"error": "Not authenticated"}
{"error": "Invalid email or password"}

// 404
{"error": "Not found"}
{"error": "Todo not found"}
{"error": "No pending approval with that ID"}
{"error": "No git repo found for todo file"}

// 409
{"error": "Email already registered"}
```

Some endpoints return `{"ok": false, "error": "..."}` instead of the bare `{"error": "..."}` format (e.g., `PUT /api/config` for invalid API keys).
