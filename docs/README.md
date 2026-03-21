# Dossie — Architecture Documentation

Dossie is a self-hosted, AI-powered executive assistant and todo management system. It combines task management with agentic AI chat, external service integrations (Slack, Email, Calendar, Smartsheet, Atlassian), and WebSocket terminal sessions — all served from a single Flask process backed by PostgreSQL.

*Last updated: 2026-03-20*

## Documentation Index

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture-overview.md) | System overview, high-level component diagram, deployment topology, operational modes, and detailed component architecture with code map |
| [Data Model](data-model.md) | Complete database schema (11 tables with full DDL), entity relationship diagram, in-memory state variables, TypeScript-style data shape interfaces, and CRUD function reference |
| [Data Flows](data-flows.md) | Six Mermaid sequence diagrams covering: Todo CRUD, Chat/Agent loop, MCP tool execution, OAuth flow, Sub-agent spawning, and Terminal/PTY bridge |
| [API Reference](api-reference.md) | All ~55 REST endpoints organized by domain, WebSocket protocol, SSE job stream protocol, and MCP tool interface specification |
| [Deployment](deployment.md) | Turnkey deployment instructions, environment variable reference, Docker and Caddy setup, MCP server build process, current deployment strategy, and deployment gap analysis |
| [Improvement Recommendations](improvement-recommendations.md) | 15 prioritized architecture improvements across 4 tiers: Critical (security), High (maintainability), Medium (performance/reliability), and Lower (enhancement) |

## Key Source Files

| File | Lines | Role |
|------|-------|------|
| `app.py` | 9,713 | Monolithic Flask application — all routes, classes, business logic, and inline HTML/CSS/JS frontend |
| `db.py` | 1,086 | PostgreSQL database layer — connection pooling, schema, migrations, and all CRUD functions |
| `mcp-servers/registry.json` | — | MCP server registry — defines server types, credentials, OAuth providers, and tool exclusions |
| `mcp-servers/todo-tools.py` | — | Built-in MCP tools — `read_todos`, `create_todo`, `update_todo`, `search_todos`, `get_todo` |
| `build-mcp.sh` | 63 | MCP server build script — clones, builds, and patches external MCP servers from pinned git refs |
| `Dockerfile` | 8 | Container definition — Python 3.11-slim with Node.js |
| `Caddyfile` | 3 | Reverse proxy configuration |
