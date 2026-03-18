"""Database layer for the todo app.

Provides connection pooling, schema migrations, and CRUD functions
that replace the file-based storage (markdown todos, JSON chats, JSON config).

Usage:
    import db
    db.init(os.environ.get("DATABASE_URL", "postgresql://localhost/todos"))
    todos = db.get_todos(user_id)
"""

import os
import json
import uuid
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
import psycopg2.extras
import bcrypt

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

_pool = None


def init(database_url: str):
    """Initialize the connection pool and run migrations."""
    global _pool
    psycopg2.extras.register_uuid()
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=20,
        dsn=database_url,
    )
    _run_migrations()


def close():
    """Shut down the connection pool."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None


@contextmanager
def _conn():
    """Get a connection from the pool. Auto-commits on success, rolls back on error."""
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _run_migrations():
    """Apply schema if tables don't exist."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables WHERE table_name = 'users'
                )
            """)
            if cur.fetchone()[0]:
                return  # tables exist

            cur.execute(SCHEMA)
            print("[db] Schema created")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
-- Users & Auth
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT UNIQUE NOT NULL,
    password    TEXT NOT NULL,
    name        TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE sessions (
    token       TEXT PRIMARY KEY,
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- Todos
CREATE TABLE todos (
    id          TEXT PRIMARY KEY,
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'open' CHECK (status IN ('open', 'completed')),
    priority    TEXT DEFAULT 'none' CHECK (priority IN ('high', 'medium', 'low', 'none')),
    section     TEXT DEFAULT '',
    position    INT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_todos_user ON todos(user_id, status);

-- Todo version history
CREATE TABLE todo_history (
    id          BIGSERIAL PRIMARY KEY,
    todo_id     TEXT REFERENCES todos(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id),
    action      TEXT NOT NULL,
    snapshot    JSONB NOT NULL,
    changed_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_todo_history_todo ON todo_history(todo_id, changed_at DESC);

-- Chat messages
CREATE TABLE messages (
    id          BIGSERIAL PRIMARY KEY,
    todo_id     TEXT REFERENCES todos(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id),
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_messages_todo ON messages(todo_id, created_at);

-- Chat metadata
CREATE TABLE chats (
    todo_id     TEXT PRIMARY KEY REFERENCES todos(id) ON DELETE CASCADE,
    user_id     UUID REFERENCES users(id),
    unread      BOOLEAN DEFAULT FALSE,
    conversation_id TEXT
);

-- Per-user config
CREATE TABLE user_configs (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    providers       JSONB DEFAULT '{}',
    active_provider TEXT DEFAULT 'local',
    mcp_servers     JSONB DEFAULT '{}',
    tokens          JSONB DEFAULT '{}',
    system_prompt   TEXT,
    context_files   JSONB DEFAULT '{}',
    subagents_enabled BOOLEAN DEFAULT TRUE,
    max_subagents   INT DEFAULT 10,
    updated_at      TIMESTAMPTZ DEFAULT now()
);
"""

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def create_user(email: str, password: str, name: str | None = None) -> dict:
    """Create a new user. Returns user dict. Raises on duplicate email."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, password, name) VALUES (%s, %s, %s) RETURNING id, email, name, created_at",
                (email.lower().strip(), hashed, name),
            )
            row = cur.fetchone()
            user_id = row[0]
            # Create default config for new user
            cur.execute(
                "INSERT INTO user_configs (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
            return {"id": str(user_id), "email": row[1], "name": row[2], "created_at": row[3].isoformat()}


def verify_user(email: str, password: str) -> dict | None:
    """Verify credentials. Returns user dict or None."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, name, password FROM users WHERE email = %s",
                (email.lower().strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            if not bcrypt.checkpw(password.encode(), row[3].encode()):
                return None
            return {"id": str(row[0]), "email": row[1], "name": row[2]}


def create_session(user_id: str, expires_hours: int = 720) -> str:
    """Create a session token. Returns the token string."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=expires_hours)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
                (token, user_id, expires),
            )
    return token


def get_session_user(token: str) -> dict | None:
    """Look up a session token. Returns user dict or None if expired/invalid."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.email, u.name FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = %s AND s.expires_at > now()
            """, (token,))
            row = cur.fetchone()
            if not row:
                return None
            return {"id": str(row[0]), "email": row[1], "name": row[2]}


def delete_session(token: str):
    """Delete a session (logout)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token = %s", (token,))


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------

def get_todos(user_id: str, status_filter: str = "all") -> list[dict]:
    """Get todos for a user. status_filter: 'all', 'open', 'completed'."""
    with _conn() as conn:
        with conn.cursor() as cur:
            if status_filter == "open":
                cur.execute(
                    "SELECT id, title, description, status, priority, section, position FROM todos WHERE user_id = %s AND status = 'open' ORDER BY section, position",
                    (user_id,),
                )
            elif status_filter == "completed":
                cur.execute(
                    "SELECT id, title, description, status, priority, section, position FROM todos WHERE user_id = %s AND status = 'completed' ORDER BY updated_at DESC",
                    (user_id,),
                )
            else:
                cur.execute(
                    "SELECT id, title, description, status, priority, section, position FROM todos WHERE user_id = %s ORDER BY section, position",
                    (user_id,),
                )
            return [
                {"id": r[0], "title": r[1], "description": r[2], "status": r[3],
                 "priority": r[4], "section": r[5], "position": r[6]}
                for r in cur.fetchall()
            ]


def get_todo(user_id: str, todo_id: str) -> dict | None:
    """Get a single todo by ID, scoped to user."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, description, status, priority, section, position FROM todos WHERE id = %s AND user_id = %s",
                (todo_id, user_id),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {"id": r[0], "title": r[1], "description": r[2], "status": r[3],
                    "priority": r[4], "section": r[5], "position": r[6]}


def create_todo(user_id: str, title: str, description: str = "",
                priority: str = "none", section: str = "") -> dict:
    """Create a new todo. Returns the new todo dict."""
    todo_id = str(uuid.uuid4())[:8]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO todos (id, user_id, title, description, priority, section)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id, title, description, status, priority, section, position""",
                (todo_id, user_id, title.strip(), description.strip(), priority, section.strip()),
            )
            r = cur.fetchone()
            todo = {"id": r[0], "title": r[1], "description": r[2], "status": r[3],
                    "priority": r[4], "section": r[5], "position": r[6]}
            # Record history
            _record_history(cur, todo_id, user_id, "create", todo)
            return todo


def update_todo(user_id: str, todo_id: str, **fields) -> dict | None:
    """Update a todo's fields. Returns updated dict or None if not found.

    Allowed fields: title, description, status, priority, section, position
    """
    allowed = {"title", "description", "status", "priority", "section", "position"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_todo(user_id, todo_id)

    with _conn() as conn:
        with conn.cursor() as cur:
            # Snapshot before change
            cur.execute(
                "SELECT id, title, description, status, priority, section, position FROM todos WHERE id = %s AND user_id = %s",
                (todo_id, user_id),
            )
            old = cur.fetchone()
            if not old:
                return None
            old_dict = {"id": old[0], "title": old[1], "description": old[2], "status": old[3],
                        "priority": old[4], "section": old[5], "position": old[6]}
            _record_history(cur, todo_id, user_id, "update", old_dict)

            set_clause = ", ".join(f"{k} = %s" for k in updates)
            values = list(updates.values()) + [todo_id, user_id]
            cur.execute(
                f"UPDATE todos SET {set_clause}, updated_at = now() WHERE id = %s AND user_id = %s "
                f"RETURNING id, title, description, status, priority, section, position",
                values,
            )
            r = cur.fetchone()
            if not r:
                return None
            return {"id": r[0], "title": r[1], "description": r[2], "status": r[3],
                    "priority": r[4], "section": r[5], "position": r[6]}


def delete_todo(user_id: str, todo_id: str) -> bool:
    """Delete a todo. Returns True if deleted."""
    with _conn() as conn:
        with conn.cursor() as cur:
            # Snapshot before delete
            cur.execute(
                "SELECT id, title, description, status, priority, section, position FROM todos WHERE id = %s AND user_id = %s",
                (todo_id, user_id),
            )
            old = cur.fetchone()
            if not old:
                return False
            old_dict = {"id": old[0], "title": old[1], "description": old[2], "status": old[3],
                        "priority": old[4], "section": old[5], "position": old[6]}
            _record_history(cur, todo_id, user_id, "delete", old_dict)
            cur.execute("DELETE FROM todos WHERE id = %s AND user_id = %s", (todo_id, user_id))
            return True


def search_todos(user_id: str, query: str) -> list[dict]:
    """Search todos by text in title and description."""
    pattern = f"%{query}%"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, description, status, priority, section, position
                   FROM todos WHERE user_id = %s AND (title ILIKE %s OR description ILIKE %s)
                   ORDER BY section, position""",
                (user_id, pattern, pattern),
            )
            return [
                {"id": r[0], "title": r[1], "description": r[2], "status": r[3],
                 "priority": r[4], "section": r[5], "position": r[6]}
                for r in cur.fetchall()
            ]


def bulk_update_todos(user_id: str, todos: list[dict]):
    """Bulk upsert todos (for reordering, section moves, etc.)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            for t in todos:
                cur.execute(
                    """INSERT INTO todos (id, user_id, title, description, status, priority, section, position)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET
                           title = EXCLUDED.title, description = EXCLUDED.description,
                           status = EXCLUDED.status, priority = EXCLUDED.priority,
                           section = EXCLUDED.section, position = EXCLUDED.position,
                           updated_at = now()""",
                    (t["id"], user_id, t["title"], t.get("description", ""),
                     t.get("status", "open"), t.get("priority", "none"),
                     t.get("section", ""), t.get("position", 0)),
                )


# ---------------------------------------------------------------------------
# Todo History
# ---------------------------------------------------------------------------

def _record_history(cur, todo_id: str, user_id: str, action: str, snapshot: dict):
    """Insert a history record (called within an existing transaction)."""
    cur.execute(
        "INSERT INTO todo_history (todo_id, user_id, action, snapshot) VALUES (%s, %s, %s, %s)",
        (todo_id, user_id, action, json.dumps(snapshot)),
    )


def get_history(user_id: str, limit: int = 30) -> list[dict]:
    """Get recent history entries for a user."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT h.id, h.todo_id, h.action, h.snapshot, h.changed_at
                   FROM todo_history h
                   WHERE h.user_id = %s
                   ORDER BY h.changed_at DESC LIMIT %s""",
                (user_id, limit),
            )
            return [
                {"id": r[0], "todo_id": r[1], "action": r[2],
                 "snapshot": r[3], "changed_at": r[4].isoformat()}
                for r in cur.fetchall()
            ]


def restore_todo(user_id: str, history_id: int) -> dict | None:
    """Restore a todo from a history snapshot. Returns the restored todo."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT todo_id, snapshot FROM todo_history WHERE id = %s AND user_id = %s",
                (history_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            todo_id, snapshot = row
            s = snapshot if isinstance(snapshot, dict) else json.loads(snapshot)

            # Snapshot current state before restore
            cur.execute(
                "SELECT id, title, description, status, priority, section, position FROM todos WHERE id = %s AND user_id = %s",
                (todo_id, user_id),
            )
            current = cur.fetchone()
            if current:
                current_dict = {"id": current[0], "title": current[1], "description": current[2],
                                "status": current[3], "priority": current[4], "section": current[5], "position": current[6]}
                _record_history(cur, todo_id, user_id, "restore", current_dict)

            # Upsert from snapshot
            cur.execute(
                """INSERT INTO todos (id, user_id, title, description, status, priority, section, position, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (id) DO UPDATE SET
                       title = EXCLUDED.title, description = EXCLUDED.description,
                       status = EXCLUDED.status, priority = EXCLUDED.priority,
                       section = EXCLUDED.section, position = EXCLUDED.position,
                       updated_at = now()
                   RETURNING id, title, description, status, priority, section, position""",
                (todo_id, user_id, s["title"], s.get("description", ""),
                 s.get("status", "open"), s.get("priority", "none"),
                 s.get("section", ""), s.get("position", 0)),
            )
            r = cur.fetchone()
            return {"id": r[0], "title": r[1], "description": r[2], "status": r[3],
                    "priority": r[4], "section": r[5], "position": r[6]}


# ---------------------------------------------------------------------------
# Chat Messages
# ---------------------------------------------------------------------------

def get_messages(todo_id: str, limit: int = 100) -> list[dict]:
    """Get chat messages for a todo."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, content, created_at FROM messages WHERE todo_id = %s ORDER BY created_at LIMIT %s",
                (todo_id, limit),
            )
            return [{"role": r[0], "content": r[1], "created_at": r[2].isoformat()} for r in cur.fetchall()]


def add_message(todo_id: str, user_id: str, role: str, content: str):
    """Add a chat message."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (todo_id, user_id, role, content) VALUES (%s, %s, %s, %s)",
                (todo_id, user_id, role, content),
            )
            # Ensure chat metadata row exists
            cur.execute(
                """INSERT INTO chats (todo_id, user_id, unread) VALUES (%s, %s, %s)
                   ON CONFLICT (todo_id) DO UPDATE SET unread = EXCLUDED.unread""",
                (todo_id, user_id, role == "assistant"),
            )


def delete_messages(todo_id: str):
    """Delete all messages for a todo (restart chat)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE todo_id = %s", (todo_id,))
            cur.execute("DELETE FROM chats WHERE todo_id = %s", (todo_id,))


def get_chat_meta(todo_id: str) -> dict | None:
    """Get chat metadata (unread, conversation_id)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT unread, conversation_id FROM chats WHERE todo_id = %s",
                (todo_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {"unread": r[0], "conversation_id": r[1]}


def mark_chat_read(todo_id: str):
    """Mark a chat as read."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE chats SET unread = FALSE WHERE todo_id = %s", (todo_id,))


def get_unread_todo_ids(user_id: str) -> set[str]:
    """Get set of todo_ids with unread chats for a user."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT todo_id FROM chats WHERE user_id = %s AND unread = TRUE",
                (user_id,),
            )
            return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# User Config
# ---------------------------------------------------------------------------

def get_config(user_id: str) -> dict:
    """Get user config. Returns empty-ish dict if not found."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT providers, active_provider, mcp_servers, tokens,
                          system_prompt, context_files, subagents_enabled, max_subagents
                   FROM user_configs WHERE user_id = %s""",
                (user_id,),
            )
            r = cur.fetchone()
            if not r:
                return {"providers": {}, "active_provider": "local", "mcp_servers": {},
                        "tokens": {}, "system_prompt": None, "context_files": {},
                        "subagents_enabled": True, "max_subagents": 10}
            return {
                "providers": r[0] or {},
                "active_provider": r[1] or "local",
                "mcp_servers": r[2] or {},
                "tokens": r[3] or {},
                "system_prompt": r[4],
                "context_files": r[5] or {},
                "subagents_enabled": r[6] if r[6] is not None else True,
                "max_subagents": r[7] or 10,
            }


def save_config(user_id: str, **fields):
    """Update user config fields. Only updates provided fields."""
    allowed = {"providers", "active_provider", "mcp_servers", "tokens",
               "system_prompt", "context_files", "subagents_enabled", "max_subagents"}
    updates = {}
    for k, v in fields.items():
        if k in allowed:
            if k in ("providers", "mcp_servers", "tokens", "context_files"):
                updates[k] = json.dumps(v) if not isinstance(v, str) else v
            else:
                updates[k] = v

    if not updates:
        return

    with _conn() as conn:
        with conn.cursor() as cur:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            values = list(updates.values()) + [user_id]
            cur.execute(
                f"UPDATE user_configs SET {set_clause}, updated_at = now() WHERE user_id = %s",
                values,
            )
