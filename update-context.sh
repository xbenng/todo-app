#!/usr/bin/env python3
"""Sync per-user context files from disk to the DB for all users.

Global context (ea-skill.md etc.) lives in context/ and is loaded from disk — not stored in DB.

Usage: ./update-context.sh [file.md ...]
  No args = sync all per-user context files (todos-config/context/*.md)
  With args = sync only the specified files
"""
import os, sys, glob, psycopg2

DB = os.environ.get("DATABASE_URL", "postgresql://localhost/todos")
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Build source map: db_name -> file_path (per-user context only)
sources = {}
ctx_dir = os.path.join(APP_DIR, "todos-config", "context")
for f in sorted(glob.glob(os.path.join(ctx_dir, "*.md"))):
    sources[os.path.basename(f)] = f

# Connect
conn = psycopg2.connect(DB)
cur = conn.cursor()
cur.execute("SELECT id FROM users")
user_ids = [r[0] for r in cur.fetchall()]
if not user_ids:
    print("No users found"); sys.exit(1)

# Filter if args provided
if len(sys.argv) > 1:
    names = []
    for arg in sys.argv[1:]:
        base = os.path.basename(arg)
        if base in sources:
            names.append(base)
        elif os.path.isfile(arg):
            sources[base] = arg
            names.append(base)
        else:
            print(f"Unknown file: {arg}"); sys.exit(1)
else:
    names = list(sources.keys())

count = 0
for name in sorted(names):
    path = sources[name]
    if not os.path.isfile(path):
        print(f"SKIP {name} — not found: {path}")
        continue
    with open(path) as f:
        content = f.read()
    for uid in user_ids:
        cur.execute("""
            INSERT INTO user_context_files (user_id, name, content)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, name) DO UPDATE SET content = EXCLUDED.content, updated_at = now()
        """, (str(uid), name, content))
    conn.commit()
    print(f"OK {name} → {len(user_ids)} users")
    count += 1

cur.close()
conn.close()
print(f"Done — {count} files synced")
