# Deployment Guide

Turnkey instructions for deploying Dossie from scratch, plus documentation of the current deployment strategy and known gaps.

---

## Prerequisites

| Dependency | Version | Required | Purpose |
|------------|---------|----------|---------|
| Python | 3.11+ | Yes | Flask application runtime |
| Node.js | 18+ | Yes | Building MCP servers (Slack, IMAP, Smartsheet) |
| npm | 9+ | Yes | Package management for MCP servers |
| git | 2.x | Yes | Cloning MCP server repos during build |
| PostgreSQL | 14+ | Yes | Database backend (schema auto-created on first run) |
| tmux | 3.x | For terminals | WebSocket terminal session persistence |
| Caddy | 2.x | Optional | Reverse proxy with automatic TLS |

---

## Quick Start (Local Development)

```bash
# 1. Clone and install Python dependencies
git clone <repo-url> todo-app && cd todo-app
pip install -r requirements.txt

# 2. Build MCP servers (clones from GitHub, builds TypeScript, ~2 minutes)
./build-mcp.sh

# 3. Create PostgreSQL database (schema auto-created on first run)
createdb todos

# 4. Configure environment
cp .env.example .env
# Edit .env with your values (see Environment Variables below)

# 5. Run
python app.py --host 0.0.0.0 --port 5111
# Open http://localhost:5111
```

---

## Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string. Required — the app exits with an error if unset. | `postgresql://user:pass@localhost/todos` |
| `OAUTH_STATE_SECRET` | Random secret for HMAC-signing OAuth state parameters. **Must be set in production** — the default is a hardcoded insecure string. Generate with `openssl rand -hex 32`. | `a1b2c3d4e5f6...` |
| `DOMAIN_NAME` | Public domain for OAuth redirect URIs. Required if using OAuth integrations (Slack, Google Calendar, Atlassian). | `yourdomain.com` |

### Optional (MCP integrations)

| Variable | Description |
|----------|-------------|
| `SLACK_CLIENT_ID` | Slack OAuth app client ID. Required for Slack MCP server. |
| `SLACK_CLIENT_SECRET` | Slack OAuth app client secret. |
| `GOOGLE_CALDAV_CLIENT_ID` | Google Calendar OAuth client ID. Required for CalDAV MCP server with Google Calendar. |
| `GOOGLE_CALDAV_CLIENT_SECRET` | Google Calendar OAuth client secret. |
| `ATLASSIAN_CLIENT_ID` | Atlassian OAuth client ID. Required for Jira/Confluence MCP server. |
| `ATLASSIAN_CLIENT_SECRET` | Atlassian OAuth client secret. |

### Optional (advanced)

| Variable | Default | Description |
|----------|---------|-------------|
| `TODO_API_BASE` | `http://localhost:5222` | Base URL for the MCP todo-tools proxy server. |
| `TODO_AUTH_TOKEN` | — | Bearer token for MCP tool authentication. |

### `.env` File Format

The app loads `.env` from its own directory at startup using a simple parser (not python-dotenv). Format:

```bash
# Comments start with #
DATABASE_URL=postgresql://localhost/todos
OAUTH_STATE_SECRET=your-random-secret-here
DOMAIN_NAME=yourdomain.com

# Slack OAuth (optional)
SLACK_CLIENT_ID=your-client-id
SLACK_CLIENT_SECRET=your-client-secret

# Google Calendar OAuth (optional)
GOOGLE_CALDAV_CLIENT_ID=your-client-id
GOOGLE_CALDAV_CLIENT_SECRET=your-client-secret

# Atlassian OAuth (optional)
ATLASSIAN_CLIENT_ID=your-client-id
ATLASSIAN_CLIENT_SECRET=your-client-secret
```

---

## Database Setup

### Initial Setup

```bash
createdb todos
```

The schema is auto-created on first run via `db.init()` -> `_run_migrations()`. No manual SQL or migration commands needed.

### Schema Migrations

Migrations are applied automatically on every startup. The system uses an **existence-check pattern** — each migration checks whether its changes already exist before applying:

```python
# Example migration pattern (db.py)
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='user_configs' AND column_name='auto_approve_all'")
if not cur.fetchone():
    cur.execute("ALTER TABLE user_configs ADD COLUMN auto_approve_all BOOLEAN DEFAULT FALSE")
```

**Limitations:**
- No migration version tracking — relies on column/table existence checks
- No rollback capability — forward-only
- No migration history table

### Backup

```bash
# Full database backup
pg_dump todos > backup_$(date +%Y%m%d).sql

# Restore
psql todos < backup_20260320.sql
```

---

## Docker Deployment

### Build

```bash
docker build -t dossie .
```

The Dockerfile:
1. Starts from `python:3.11-slim`
2. Installs Node.js, npm, and git
3. Installs Python dependencies from `requirements.txt`
4. Runs `./build-mcp.sh` to clone and build all MCP servers
5. Sets `CMD ["python3", "app.py"]`

### Run

```bash
docker run -d --name dossie \
  -e DATABASE_URL=postgresql://user:pass@host:5432/todos \
  -e OAUTH_STATE_SECRET=$(openssl rand -hex 32) \
  -e DOMAIN_NAME=yourdomain.com \
  -p 5111:5111 \
  dossie
```

**Note:** Requires an external PostgreSQL instance. There is no docker-compose file yet (see [Deployment Gaps](#deployment-gaps)).

### Port Configuration

The app defaults to port 5111. Override with:

```bash
docker run ... dossie python3 app.py --port 5222
```

---

## Production Setup with Caddy

Caddy provides automatic TLS certificate provisioning via Let's Encrypt.

### Caddyfile

```
yourdomain.com {
    reverse_proxy localhost:5111
}
```

### Run Caddy

```bash
caddy run --config /path/to/Caddyfile
```

Caddy will automatically obtain and renew TLS certificates for `yourdomain.com`.

**Note:** The current `Caddyfile` in the repo is hardcoded to `benng.click` and proxies to port `5222` — update both the domain and port for your deployment.

---

## MCP Server Build

The `build-mcp.sh` script clones and builds 4 MCP servers from pinned git refs, plus installs 1 via pip:

| Server | Source | Pinned Version | Type |
|--------|--------|---------------|------|
| Slack | npm `slack-mcp-server` | v1.2.3 | Go binary via npm |
| IMAP | GitHub `nikolausm/imap-mcp-server` | `ed133aa` | TypeScript (clone + build) |
| Smartsheet | GitHub `xbenng/smartsheet-mcp-server` | `d262e91` | TypeScript (clone + build) |
| CalDAV | GitHub `xbenng/caldav-mcp` | `b63f3d4` | Python (clone + pip install) |
| Atlassian | PyPI `mcp-atlassian` | `>=0.21` | Python (pip install via requirements.txt) |

### Build Process

```bash
./build-mcp.sh
```

This script:
1. Clones each repo at its pinned commit
2. Runs `npm ci && npm run build` for TypeScript servers
3. Copies `dist/` + production deps to `mcp-servers/<name>/`
4. **Patches IMAP** server to support OAuth2 `accessToken` in connect auth (via sed)
5. Pip-installs CalDAV server locally

**Requirements:** Internet access (clones from GitHub), Node.js + npm, git, Python + pip.

Built server directories are gitignored — only `mcp-servers/registry.json` and `mcp-servers/todo-tools.py` are tracked.

### Offline Build

To avoid network access at deploy time, run `build-mcp.sh` once and include the built `mcp-servers/` directories in your Docker image. The Dockerfile already does this (runs `build-mcp.sh` at build time).

---

## Context File Sync

Context files are markdown documents that enrich the AI agent's system prompt with domain knowledge.

### Global Context Files

Stored in `todos-config/context/` on disk. Loaded by the app at runtime. Not stored in the database.

### Per-User Context Files

Synced to all users in the database via:

```bash
python update-context.sh                    # sync all files
python update-context.sh specific-file.md   # sync one file
```

This script reads markdown files from `todos-config/context/` and upserts them into the `user_context_files` table for every user.

---

## CLI Arguments

```
python app.py [OPTIONS]

Options:
  --host HOST        Host to bind to (default: 0.0.0.0)
  --port PORT        Port to listen on (default: 5111)
```

---

## Startup Sequence

1. Load `.env` file (manual parser, not python-dotenv)
2. Require `DATABASE_URL` — exit with error if unset
3. Initialize connection pool, run migrations
4. Register `atexit` handler to stop all MCPManagers on shutdown
5. Recover any surviving tmux sessions from previous runs (`_tmux_recover_sessions()`)
6. Start Flask with `debug=False, use_reloader=True` on configured host:port

**Note:** MCP servers are **not** started at boot. They are lazily initialized per-user on first access via `_get_mcp_manager(user_id)`.

---

## Current Deployment Strategy

What exists in the repo today:

| Aspect | Current State |
|--------|--------------|
| **Container** | Minimal `Dockerfile` — python:3.11-slim + Node.js + git |
| **Orchestration** | None — no docker-compose, no Kubernetes manifests |
| **Reverse proxy** | Single-line `Caddyfile` with hardcoded domain and port |
| **Process management** | None — `python app.py` with `use_reloader=True`, no supervisor |
| **Secrets management** | `.env` file (gitignored, not tracked) |
| **Database migrations** | Auto-applied on startup, existence-check pattern, no versioning |
| **Monitoring** | None — no health check endpoint, no metrics, no alerting |
| **Logging** | Python logging module with structured output |
| **Backup** | None — no automated backup strategy |
| **CI/CD** | None — no automated testing or deployment pipeline |

---

## Deployment Gaps

Known gaps between the current state and a production-ready deployment:

| Gap | Impact | Recommendation |
|-----|--------|----------------|
| **No docker-compose** | Can't deploy app + PostgreSQL together. Manual PostgreSQL provisioning required. | Create `docker-compose.yml` with `app` + `postgres` services, named volume for `pgdata`, health checks, and `restart: unless-stopped`. |
| **No `.env.example`** | New deployers don't know what environment variables are needed or what values are valid. | Create `.env.example` with all variables, descriptions, and safe placeholder values. |
| **No process supervisor** | App crash = downtime until manual restart. No automatic recovery. | Add systemd service file, or use gunicorn with `--workers 1 --timeout 120`. With Docker: `restart: unless-stopped`. |
| **`use_reloader=True`** | Forks a child process (doubling memory), restarts on any file change. Inappropriate for production. | Detect production mode via env var (e.g., `FLASK_ENV=production`) and set `use_reloader=False`. |
| **No health check endpoint** | Can't monitor app liveness. Docker/Kubernetes health checks not possible. | Add `GET /api/health` returning `{status: "ok", db: true/false, mcp_servers: <count>}`. |
| **No migration versioning** | Can't roll back database migrations. Existence-check pattern is fragile for complex schema changes. | Consider Alembic, or implement a simple `schema_version` table tracking applied migration numbers. |
| **No backup strategy** | Data loss risk. No automated `pg_dump`. | Add a cron-based backup script. Document restore procedure. |
| **Port mismatch** | `Caddyfile` proxies to `localhost:5222` but app defaults to port `5111`. Confusing for deployers. | Standardize on one port. Update Caddyfile or default port. |
| **Single-worker only** | In-memory state (`_jobs`, `_mcp_managers`, `_pty_sessions`) prevents horizontal scaling or multi-worker deployment. | Document this limitation explicitly. Future: externalize state to Redis or PostgreSQL. |
| **No TLS docs** | Security gap if Caddy is not used. No guidance for manual cert setup. | Document Caddy auto-TLS (current approach) and alternatives (nginx + certbot). |
| **No log rotation** | `todo-app.log` grows unbounded (already 54MB+). | Add logrotate configuration, or switch to structured logging with built-in rotation. |
| **MCP build requires internet** | `build-mcp.sh` clones from GitHub at build time. Fails in air-gapped environments. | Pre-build MCP servers in a separate Docker layer. Cache built artifacts. |
| **No CI/CD pipeline** | No automated testing before deployment. No deployment automation. | Add GitHub Actions (or equivalent) for: lint, test, build Docker image, push to registry. |
