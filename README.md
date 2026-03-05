# Todo App

A lightweight, self-contained todo list manager with a web UI. The todo data is stored in a plain markdown file that can be read and edited by both humans and AI agents.

## Quick Start

```bash
pip install -r requirements.txt
python app.py [path/to/todos.md]
```

Open **http://localhost:5111** in your browser. If no file path is given, it defaults to `todos.md` in the current directory.

---

## File Format Specification

The todo file is a standard Markdown file with the following structure:

### Overall Structure

```markdown
# Todo List

## Active

- [ ] **Task title** <!-- id:abc123 -->
  Optional description line 1
  Optional description line 2

- [ ] [critical] **Urgent task** <!-- id:def456 -->
  This needs immediate attention

## Completed

- [x] **Finished task** <!-- id:ghi789 -->
  This was previously active
```

### Sections

| Section | Purpose |
|---------|---------|
| `## Active` | Contains all non-completed todos, sorted by priority |
| `## Completed` | Contains all completed todos |

The app automatically moves items between sections based on status.

### Todo Item Format

Each todo item follows this pattern:

```
- [CHECKBOX] [STATUS_TAG] **TITLE** <!-- id:ID -->
  DESCRIPTION_LINE_1
  DESCRIPTION_LINE_2
```

#### Fields

| Field | Required | Format | Description |
|-------|----------|--------|-------------|
| Checkbox | Yes | `[ ]` or `[x]` | Unchecked = active, checked = completed |
| Status Tag | No | `[status]` | One of the valid statuses (see below). Omitted for `open` and `completed` items. |
| Title | Yes | `**bold text**` | The todo title, wrapped in `**` |
| ID | Auto | `<!-- id:xxxx -->` | Unique identifier as an HTML comment. Auto-generated if missing. |
| Description | No | Indented lines | Each line indented with 2 spaces below the title line |

#### Valid Statuses

| Status | Priority | Description |
|--------|----------|-------------|
| `critical` | 1 (highest) | Needs immediate attention, shown at top |
| `blocked` | 2 | Waiting on something, cannot proceed |
| `in-progress` | 3 | Currently being worked on |
| `open` | 4 | Default status for new items |
| `completed` | — | Done; moved to Completed section |

Items in the Active section are sorted by priority (critical → blocked → in-progress → open).

### Examples

**Minimal item (open, no description):**
```markdown
- [ ] **Buy groceries** <!-- id:a1b2c3d4 -->
```

**Critical item with description:**
```markdown
- [ ] [critical] **Fix production bug** <!-- id:e5f6g7h8 -->
  Server returning 500 errors on /api/users
  Affecting all customers
```

**Completed item:**
```markdown
- [x] **Set up CI pipeline** <!-- id:i9j0k1l2 -->
  Configured GitHub Actions for tests and deploy
```

### Manual Editing Guidelines

When editing the file by hand:

1. **Adding a todo**: Add a `- [ ] **Your title** <!-- id:any-unique-string -->` line under `## Active`
2. **Completing a todo**: Change `[ ]` to `[x]` and move it under `## Completed` (or just check the box — the app will re-sort on next save)
3. **Changing status**: Add/change the `[status]` tag before the title
4. **IDs are optional when editing by hand** — the app will generate them on next load if missing
5. **Description lines** must be indented with exactly 2 spaces

### Agent Integration

AI agents can maintain this file by:

- Reading the file as plain text and parsing the markdown structure
- Writing new items following the format above
- Updating status tags or checkboxes
- The ID comments ensure items can be tracked across edits

The format is intentionally simple so that `grep`, `sed`, or basic string manipulation can work with it.

---

## API Reference

The web UI communicates via a JSON REST API that can also be used programmatically:

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| `GET` | `/api/todos` | — | List all todos |
| `POST` | `/api/todos` | `{title, description?, status?}` | Create a todo |
| `PUT` | `/api/todos/:id` | `{title?, description?, status?}` | Update a todo |
| `DELETE` | `/api/todos/:id` | — | Delete a todo |

All request/response bodies are JSON.
