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
    """Apply schema if tables don't exist, and run incremental migrations."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables WHERE table_name = 'users'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute(SCHEMA)
                print("[db] Schema created")

            # Incremental migrations
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables WHERE table_name = 'user_context_files'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("""
                    CREATE TABLE user_context_files (
                        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        name        TEXT NOT NULL,
                        content     TEXT NOT NULL,
                        created_at  TIMESTAMPTZ DEFAULT now(),
                        updated_at  TIMESTAMPTZ DEFAULT now(),
                        UNIQUE(user_id, name)
                    );
                    CREATE INDEX idx_context_files_user ON user_context_files(user_id);
                """)
                print("[db] Created user_context_files table")

            # Migration: user_mcp_preferences table
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables WHERE table_name = 'user_mcp_preferences'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("""
                    CREATE TABLE user_mcp_preferences (
                        user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        server_name         TEXT NOT NULL,
                        enabled             BOOLEAN DEFAULT FALSE,
                        disabled_tools      TEXT[] DEFAULT '{}',
                        auto_approved_tools TEXT[] DEFAULT '{}',
                        created_at          TIMESTAMPTZ DEFAULT now(),
                        updated_at          TIMESTAMPTZ DEFAULT now(),
                        PRIMARY KEY (user_id, server_name)
                    );
                    CREATE INDEX idx_mcp_prefs_user ON user_mcp_preferences(user_id);
                """)
                print("[db] Created user_mcp_preferences table")

            # Migration: user_server_accounts table
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables WHERE table_name = 'user_server_accounts'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("""
                    CREATE TABLE user_server_accounts (
                        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        server_name TEXT NOT NULL,
                        config      JSONB NOT NULL DEFAULT '{}',
                        created_at  TIMESTAMPTZ DEFAULT now(),
                        updated_at  TIMESTAMPTZ DEFAULT now()
                    );
                    CREATE INDEX idx_server_accounts_user ON user_server_accounts(user_id, server_name);
                """)
                print("[db] Created user_server_accounts table")

            # Migration: auto_approve_all column on user_configs
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'user_configs' AND column_name = 'auto_approve_all'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("ALTER TABLE user_configs ADD COLUMN auto_approve_all BOOLEAN DEFAULT FALSE")
                print("[db] Added auto_approve_all to user_configs")

            # Migration: conversation_num on messages + current_conversation on chats
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.columns
                    WHERE table_name = 'messages' AND column_name = 'conversation_num'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("""
                    ALTER TABLE messages ADD COLUMN conversation_num INT DEFAULT 0;
                    ALTER TABLE chats ADD COLUMN current_conversation INT DEFAULT 0;
                """)
                print("[db] Added conversation_num to messages, current_conversation to chats")


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
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
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


def get_todos_mtime(user_id: str) -> float:
    """Get the most recent update timestamp across all todos for a user."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM MAX(updated_at)) FROM todos WHERE user_id = %s",
                (user_id,),
            )
            r = cur.fetchone()
            return float(r[0]) if r and r[0] else 0.0


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


def get_todo_history(user_id: str, todo_id: str, limit: int = 30) -> list[dict]:
    """Get history entries for a specific todo item."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, action, snapshot, changed_at
                   FROM todo_history
                   WHERE user_id = %s AND todo_id = %s
                   ORDER BY changed_at DESC LIMIT %s""",
                (user_id, todo_id, limit),
            )
            return [
                {"id": r[0], "action": r[1], "snapshot": r[2],
                 "changed_at": r[3].isoformat()}
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

def get_messages(todo_id: str, limit: int = 100, include_tool: bool = True) -> list[dict]:
    """Get chat messages for the current conversation of a todo."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_conversation FROM chats WHERE todo_id = %s", (todo_id,))
            row = cur.fetchone()
            conv_num = row[0] if row and row[0] is not None else 0
            if include_tool:
                cur.execute(
                    """SELECT role, content, created_at FROM messages
                       WHERE todo_id = %s AND conversation_num = %s
                       ORDER BY created_at LIMIT %s""",
                    (todo_id, conv_num, limit),
                )
            else:
                cur.execute(
                    """SELECT role, content, created_at FROM messages
                       WHERE todo_id = %s AND conversation_num = %s AND role != 'tool'
                       ORDER BY created_at LIMIT %s""",
                    (todo_id, conv_num, limit),
                )
            return [{"role": r[0], "content": r[1], "created_at": r[2].isoformat()} for r in cur.fetchall()]


def add_message(todo_id: str, user_id: str, role: str, content: str):
    """Add a chat message to the current conversation."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chats (todo_id, user_id, unread, current_conversation)
                   VALUES (%s, %s, %s, 0)
                   ON CONFLICT (todo_id) DO UPDATE SET unread = EXCLUDED.unread
                   RETURNING current_conversation""",
                (todo_id, user_id, role == "assistant"),
            )
            conv_num = cur.fetchone()[0] or 0
            cur.execute(
                "INSERT INTO messages (todo_id, user_id, role, content, conversation_num) VALUES (%s, %s, %s, %s, %s)",
                (todo_id, user_id, role, content, conv_num),
            )


def restart_conversation(todo_id: str):
    """Start a new conversation for a todo (preserves old messages)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE chats SET current_conversation = COALESCE(current_conversation, 0) + 1,
                   unread = FALSE, conversation_id = NULL
                   WHERE todo_id = %s""",
                (todo_id,),
            )


def resume_conversation(todo_id: str, conv_num: int):
    """Set the current conversation to a specific number (resume a past conversation)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE chats SET current_conversation = %s,
                   unread = FALSE, conversation_id = NULL
                   WHERE todo_id = %s""",
                (conv_num, todo_id),
            )


def get_conversations(todo_id: str) -> tuple[int, list[dict]]:
    """List all conversations for a todo (newest first). Returns (current_num, conversations)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_conversation FROM chats WHERE todo_id = %s", (todo_id,))
            row = cur.fetchone()
            current_num = row[0] if row and row[0] is not None else 0
            cur.execute(
                """SELECT conversation_num, MIN(created_at) as started, MAX(created_at) as last_msg,
                          COUNT(*) as message_count
                   FROM messages WHERE todo_id = %s
                   GROUP BY conversation_num ORDER BY conversation_num DESC""",
                (todo_id,),
            )
            convs = [{"num": r[0], "started": r[1].isoformat(), "last_msg": r[2].isoformat(),
                      "message_count": r[3]} for r in cur.fetchall()]
            return current_num, convs


def get_conversation_messages(todo_id: str, conversation_num: int, limit: int = 100) -> list[dict]:
    """Get messages from a specific conversation."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT role, content, created_at FROM messages
                   WHERE todo_id = %s AND conversation_num = %s
                   ORDER BY created_at LIMIT %s""",
                (todo_id, conversation_num, limit),
            )
            return [{"role": r[0], "content": r[1], "created_at": r[2].isoformat()} for r in cur.fetchall()]


def delete_messages(todo_id: str):
    """Delete all messages for a todo."""
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


def mark_chat_unread(todo_id: str, user_id: str):
    """Mark a todo's chat as unread (e.g., after a tool modifies the todo)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chats (todo_id, user_id, unread) VALUES (%s, %s, TRUE)
                   ON CONFLICT (todo_id) DO UPDATE SET unread = TRUE""",
                (todo_id, user_id),
            )


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
                          system_prompt, context_files, subagents_enabled, max_subagents,
                          auto_approve_all
                   FROM user_configs WHERE user_id = %s""",
                (user_id,),
            )
            r = cur.fetchone()
            if not r:
                return {"providers": {}, "active_provider": "local", "mcp_servers": {},
                        "tokens": {}, "system_prompt": None, "context_files": {},
                        "subagents_enabled": True, "max_subagents": 10,
                        "auto_approve_all": False}
            return {
                "providers": r[0] or {},
                "active_provider": r[1] or "local",
                "mcp_servers": r[2] or {},
                "tokens": r[3] or {},
                "system_prompt": r[4],
                "context_files": r[5] or {},
                "subagents_enabled": r[6] if r[6] is not None else True,
                "max_subagents": r[7] or 10,
                "auto_approve_all": r[8] if r[8] is not None else False,
            }


def save_config(user_id: str, **fields):
    """Update user config fields. Only updates provided fields."""
    allowed = {"providers", "active_provider", "mcp_servers", "tokens",
               "system_prompt", "context_files", "subagents_enabled", "max_subagents",
               "auto_approve_all"}
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


# ---------------------------------------------------------------------------
# User Context Files
# ---------------------------------------------------------------------------

def get_context_files(user_id: str) -> dict[str, str]:
    """Get all context files for a user. Returns {name: content}."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, content FROM user_context_files WHERE user_id = %s ORDER BY name",
                (user_id,),
            )
            return {r[0]: r[1] for r in cur.fetchall()}


def get_context_file(user_id: str, name: str) -> str | None:
    """Get a single context file by name. Returns content or None."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM user_context_files WHERE user_id = %s AND name = %s",
                (user_id, name),
            )
            r = cur.fetchone()
            return r[0] if r else None


def upsert_context_file(user_id: str, name: str, content: str) -> None:
    """Create or update a context file."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_context_files (user_id, name, content)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, name)
                   DO UPDATE SET content = EXCLUDED.content, updated_at = now()""",
                (user_id, name, content),
            )


def delete_context_file(user_id: str, name: str) -> bool:
    """Delete a context file. Returns True if it existed."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_context_files WHERE user_id = %s AND name = %s",
                (user_id, name),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# MCP Preferences
# ---------------------------------------------------------------------------

def get_mcp_preferences(user_id: str) -> dict:
    """Get MCP server preferences. Returns {server_name: {enabled, disabled_tools, auto_approved_tools}}."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT server_name, enabled, disabled_tools, auto_approved_tools
                   FROM user_mcp_preferences WHERE user_id = %s""",
                (user_id,),
            )
            return {
                r[0]: {
                    "enabled": r[1],
                    "disabled_tools": list(r[2] or []),
                    "auto_approved_tools": list(r[3] or []),
                }
                for r in cur.fetchall()
            }


def set_server_enabled(user_id: str, server_name: str, enabled: bool) -> None:
    """Enable or disable a server for a user."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_mcp_preferences (user_id, server_name, enabled)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, server_name)
                   DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()""",
                (user_id, server_name, enabled),
            )


def set_tool_disabled(user_id: str, server_name: str, tool_name: str, disabled: bool) -> None:
    """Add or remove a tool from the disabled list."""
    with _conn() as conn:
        with conn.cursor() as cur:
            if disabled:
                cur.execute(
                    """INSERT INTO user_mcp_preferences (user_id, server_name, disabled_tools)
                       VALUES (%s, %s, ARRAY[%s])
                       ON CONFLICT (user_id, server_name)
                       DO UPDATE SET disabled_tools = array_append(
                           array_remove(user_mcp_preferences.disabled_tools, %s), %s
                       ), updated_at = now()""",
                    (user_id, server_name, tool_name, tool_name, tool_name),
                )
            else:
                cur.execute(
                    """UPDATE user_mcp_preferences
                       SET disabled_tools = array_remove(disabled_tools, %s), updated_at = now()
                       WHERE user_id = %s AND server_name = %s""",
                    (tool_name, user_id, server_name),
                )


def set_tool_auto_approved(user_id: str, server_name: str, tool_name: str, auto_approved: bool) -> None:
    """Add or remove a tool from the auto-approved list."""
    with _conn() as conn:
        with conn.cursor() as cur:
            if auto_approved:
                cur.execute(
                    """INSERT INTO user_mcp_preferences (user_id, server_name, auto_approved_tools)
                       VALUES (%s, %s, ARRAY[%s])
                       ON CONFLICT (user_id, server_name)
                       DO UPDATE SET auto_approved_tools = array_append(
                           array_remove(user_mcp_preferences.auto_approved_tools, %s), %s
                       ), updated_at = now()""",
                    (user_id, server_name, tool_name, tool_name, tool_name),
                )
            else:
                cur.execute(
                    """UPDATE user_mcp_preferences
                       SET auto_approved_tools = array_remove(auto_approved_tools, %s), updated_at = now()
                       WHERE user_id = %s AND server_name = %s""",
                    (tool_name, user_id, server_name),
                )


# ---------------------------------------------------------------------------
# Server Accounts (per-user, per-server configs like IMAP/CalDAV accounts)
# ---------------------------------------------------------------------------

def get_server_accounts(user_id: str, server_name: str) -> list[dict]:
    """Get all accounts for a user+server. Returns list of {id, config}."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, config FROM user_server_accounts WHERE user_id = %s AND server_name = %s ORDER BY created_at",
                (user_id, server_name),
            )
            return [{"id": str(r[0]), "config": r[1] or {}} for r in cur.fetchall()]


def add_server_account(user_id: str, server_name: str, config: dict) -> dict:
    """Add an account. Returns {id, config}."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_server_accounts (user_id, server_name, config)
                   VALUES (%s, %s, %s) RETURNING id""",
                (user_id, server_name, json.dumps(config)),
            )
            account_id = str(cur.fetchone()[0])
            return {"id": account_id, "config": config}


def update_server_account(user_id: str, account_id: str, config: dict) -> bool:
    """Update an account's config. Returns True if found."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE user_server_accounts SET config = %s, updated_at = now()
                   WHERE id = %s AND user_id = %s""",
                (json.dumps(config), account_id, user_id),
            )
            return cur.rowcount > 0


def delete_server_account(user_id: str, account_id: str) -> bool:
    """Delete an account. Returns True if found."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_server_accounts WHERE id = %s AND user_id = %s",
                (account_id, user_id),
            )
            return cur.rowcount > 0
