## Step 8 — Work on a todo (`/ea workon <query>`)

Lightweight deep-dive on a specific todo item. **Follows existing references first** — only broadens search if the item's description doesn't provide enough context.

### 8a. Find the todo

1. Use `search_todos` with the query, or `read_todos` and match manually.
2. Match `<query>` against todos — try in order:
   - **Exact ID match**: if query looks like a hex ID
   - **Title keyword match**: search title text
   - **Description match**: search description content
3. If multiple items match, present them and ask the user to pick one.
4. If no items match, say so and suggest similar items.

### 8b. Gather context — reference-first approach

**Principle: follow the breadcrumbs already in the todo.** The description contains Slack permalinks, email permalinks, Smartsheet URLs, Confluence links, channel names, and person names. Use those directly.

**Step 1 — Extract references from the todo's description:**
- Slack channel IDs and thread timestamps → use `mcp__slack__conversations_replies` or `mcp__slack__conversations_history`
- Email permalinks → search IMAP for same subject/sender if thread may have new replies
- Smartsheet/Confluence/Jira URLs → fetch directly
- Person names → note for draft addressing

**Step 2 — Check referenced sources directly:**
- For each Slack thread: get replies since the last recorded date
- For each channel: get recent history (3-7 days)
- For linked documents: fetch current state
- Do NOT launch broad sweeps unless Step 1 yields fewer than 2 actionable references

**Step 3 — Broaden only if needed:**
- If the todo has no description, no links, and no references → fall back to targeted search: 2-3 key terms from title, search Slack + email (last 7 days)

**Efficiency rules:**
- Delegate comms lookups to a subagent (per rule 12). Review and deepen per rule 13.

### 8c. Analyze and determine next actions

From the gathered context, determine:
1. **Current state**: What's the latest status?
2. **What's blocking**: Missing info? Waiting on someone?
3. **Who's involved**: Key people? Who needs to hear from the user?
4. **What the user needs to do**: Concrete next steps, in priority order
5. **What can be drafted now**: Messages, emails, documents

### 8d. Draft deliverables

Draft concrete outputs:
- **Slack messages**: Draft in the user's voice. Include target channel/person. Present for confirmation.
- **Emails**: Draft with To/CC/Subject/Body. Present for confirmation.
- **Meeting agendas**: Bullet-point agenda with discussion points and decisions needed.
- **Follow-up questions**: Specific questions and who to ask.
- **Decision summaries**: Options with pros/cons from gathered context.

### 8e. Present

```
## Workon: <todo title>

### Current State
- Brief status summary
- Last activity: WHO did WHAT on DATE

### Context Gathered
- [Slack] key finding (link)
- [Email] key finding (link)

### Blockers
- What's preventing progress (if any)

### Recommended Next Steps
1. ACTION — why, who
2. ACTION — why, who

### Drafts

#### Draft 1: Slack message to WHO in #CHANNEL
> message content

#### Draft 2: Email to WHO re: SUBJECT
> To: ...
> Subject: ...
> email body

---
Want me to send any of these, revise a draft, or dig deeper?
```

**Draft rules:**
- Never send without explicit confirmation
- Label each draft with recipient and medium
- Keep drafts short and actionable

### 8f. Live todo updates during workon session

The todo must be kept current as the conversation progresses.

**After 8b:** If new information was found, use `update_todo` to append it to the description immediately.

**After each follow-up from the user:** Scan for:
- **Decisions made**: "let's go with X", "approved", "not doing Y"
- **Next steps**: concrete actions committed to or delegated
- **Status changes**: blocker resolved, new blocker, waiting on someone
- **Sent messages**: if user confirms a draft was sent, record it

Use `update_todo` to append each finding to the description. Update the title if status materially changed.



