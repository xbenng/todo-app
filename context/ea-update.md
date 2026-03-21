## Step 6 — Comprehensive update sweep (`/ea update`)

Sweeps Slack, email, and notes to update the todo list. **Applies updates automatically** — the userreviews afterward.

### 6a. Gather updates

1. Read `the todo list (use read_todos, update_todo, create_todo tools)`.
2. **Determine search window.** Run `git -C the tracking directory log -1 --format="%ai" -- the todo list` to get the timestamp of the last commit to the todo list. This is the "since" cutoff — only search for comms newer than this. If the last commit was <2 hours ago, search the last 2 hours (minimum). If >3 days ago, cap at 3 days and note that coverage may be incomplete. Pass this cutoff to each subagent.
3. **Launch subagents in parallel** using the spawn_agents tool to sweep comms. Build the search list from the todos in the todo list. Then launch:

   **Subagent 1 — Slack sweep:** For each project/client name and each todo topic, search Slack for messages since the cutoff. Use `mcp__slack__conversations_search_messages` with `after:YYYY-MM-DD` for each search term. Categorize findings by project/todo and note: who said what, when, which channel, and whether it represents a status change, blocker, resolution, or action item for the user. **For every Slack message found, include the permalink URL** (construct from channel ID + message timestamp per rule 7). Also extract and return any documentation URLs found in the messages (Confluence, Jira, Smartsheet, SharePoint, Google Drive). Return structured results with all permalinks and doc URLs.

   **Subagent 2 — Email sweep:** Connect to IMAP account the IMAP account ID (discovered via `mcp__imap__imap_list_accounts`) via `mcp__imap__imap_connect`, then for each project/client name and each todo topic, search via `mcp__imap__imap_search_emails` with `since` set to the cutoff date. For each match, capture the `messageId` header and construct the Gmail permalink (rule 8). For actionable matches, read the full message. Extract and return any documentation URLs found in the email body (Confluence, Jira, Smartsheet, SharePoint, Google Drive). Categorize findings the same way. Return structured results including the Gmail permalink and all doc URLs for every email referenced.

   **Subagent 3 — Calendar sweep:** Check `mcp__caldav__caldav_get_week_events` for upcoming deadlines. Return events with dates, times, and any relevant prep needs.

   **Subagent 4 (optional) — Notes sweep:** Apple Notes is slow (osascript iterates all notes per call). To avoid timeouts:
   - Do NOT call `mcp__apple_notes__list_notes` — it times out on large libraries.
   - Use `mcp__apple_notes__search_notes` with `search_body: true` on at most **3-4 grouped searches** (e.g., combine related project names into one query like "Mainspring Honda MagniX").
   - If the **first** search times out (60s), **skip all remaining Notes searches** — the Notes app is unresponsive and further calls will also fail.
   - Only read individual notes (`get_note`) for high-signal matches (meeting notes from the last 3 days with clearly relevant titles).
   - Return structured results including any URLs found in the notes (Jira, Confluence, etc.).

   **Subagent 5 — Confluence & Jira sweep:** Search for recently updated issues and pages related to active todos.
   - **Jira:** For each project/client name in the todo list, run `mcp__atlassian__searchJiraIssuesUsingJql` with JQL: `text ~ "TERM" AND updated >= "CUTOFF_DATE" ORDER BY updated DESC`. Also fetch any Jira issue URLs already referenced in todo sub-bullets via `mcp__atlassian__getJiraIssue` to get current status. For each issue found, return: issue key, summary, status, assignee, last updated, and the issue URL (`https://{atlassian-domain}/browse/KEY`).
   - **Confluence:** For each project/client name, run `mcp__atlassian__searchConfluenceUsingCql` with CQL: `text ~ "TERM" AND lastModified >= "CUTOFF_DATE" ORDER BY lastModified DESC`. Also fetch any Confluence page URLs already referenced in todo sub-bullets via `mcp__atlassian__getConfluencePage` to check for recent edits. For each page found, return: page title, space, last modified date, and the page URL.
   - **Classify each finding** by which todo it most likely relates to. Note whether it represents a status change, new blocker, completed work, or action item for the user.
   - Return structured results with issue/page URLs for every finding.

3. Merge results from all subagents. For each project and todo, combine Slack + email + calendar + Confluence/Jira findings.
4. From the merged results, identify **new action items** for the user that don't have a corresponding todo yet (e.g., someone asked the userto do something, a new deliverable surfaced, a follow-up is needed).

### 6b. Active item coverage check

After the delta sweep, ensure every unchecked todo in the todo list has been checked for recent activity. This catches updates the broad sweep may have missed (different terminology, side channels, DMs, etc.).

1. Compare the merged results from 6a against the full list of unchecked (`- [ ]`) todos across all sections (Active, Proposals, Hiring, Tracking, Backlog, etc.).
2. Identify **uncovered items**: active todos that received no findings from the delta sweep.
3. For each uncovered item, launch a **targeted search** — use the spawn_agents tool to batch these efficiently:
   - Extract the most specific search terms from the todo title and sub-bullets (project name, person name, deliverable name)
   - Search Slack via `mcp__slack__conversations_search_messages` using those terms (since the cutoff from 6a, or last 3 days if cutoff is very recent)
   - Search email via `mcp__imap__imap_search_emails` using those terms (same window). Capture `messageId` for Gmail permalink construction (rule 8).
   - If any results are found, add them to the merged results
4. For items that still have no recent activity after the targeted search, note them as **stale** — these will be flagged in the summary (6g) so the usercan decide whether to follow up or archive.

**Efficiency rules:**
- Batch multiple uncovered items into a single subagent call where possible (group by project or client)
- Skip items that are clearly one-time tasks with no external dependency (e.g., "read document X")
- If there are more than 15 uncovered items, prioritize: CRITICAL first, then {high}, then by age (oldest first), and cap at 15 targeted searches per sweep

### 6c. Apply updates to the todo list

**Dedup check — but prefer creating new items when in doubt:**
- Read the entire the todo list (all sections: Review, CRITICAL, Active, Proposals, Tracking)
- **Check `the completed todos (filter read_todos with status_filter: completed)` before creating any new item.** If a candidate new item closely matches a recently completed task in that file, do not recreate it — skip it silently or note it as already done.
- Only consolidate into an existing todo if the new item is **clearly the same action** — same project, same deliverable, same person responsible. A new PO, a new client request, a new work item, or a different sub-project should always be a separate todo even if it's related to an existing one.
- **Do NOT conflate items that are merely in the same project area.** E.g., a firmware fix and a client PO for that fix are separate items. A cell data request and a cell ID question are separate items. Different tugs/vehicles are separate items.
- When adding automated sub-bullet updates to existing todos, only add information that is genuinely an update on **that specific task** — not adjacent or related work.

1. **New items go in a `## Review` section at the top** (right after `# Todo List`, before the first existing section). Create this section if it doesn't exist. Format:
   ```markdown
   ## Review
   <!-- EA auto-generated YYYY-MM-DD. Review, then move to the appropriate section or delete. -->

   - [ ] **Brief task description** <!-- id:ea-XXXXXXXX -->
     - `2025-03-13 09:15` WHO asked for X. ([Slack #channel](permalink))
     - `2025-03-13 10:00` Related: Y is also pending. ([Email, WHO](gmail-permalink))
   ```
   - Generate unique IDs with prefix `ea-` followed by 8 hex chars
   - Use the same `` `YYYY-MM-DD HH:MM` `` timestamp format as update sub-bullets — no `Source:` or `Context:` labels
   - Every sub-bullet MUST include a permalink (rules 7 and 8)
   - Keep descriptions concise — one fact per line, no quoting full messages

2. **Update existing todos in-place.** When comms reveal progress on an existing todo:
   - Add or replace the `` `updated YYYY-MM-DD HH:MM` `` tag on the title line (rule 13)
   - Add a new sub-bullet with the update, prefixed with `` `YYYY-MM-DD HH:MM` `` (backtick-wrapped timestamp, **local system time in local timezone** — use `date` to get current time) so it's clear the update was automated and visually de-emphasized
   - **Every source reference MUST include a permalink** — Slack permalink (rule 7) or Gmail permalink (rule 8). No bare `(Slack, 3/10)` or `(Email, 3/10)` without a link.
   - **No duplicate information.** Before adding a sub-bullet, read all existing sub-bullets on that todo. Do not repeat facts, status, or findings that are already recorded. Only add genuinely new information. If the new finding merely confirms what a prior sub-bullet already says, skip it.
   - **One point per sub-bullet.** When an update covers multiple distinct facts (e.g., a status change AND a new blocker AND a decision), use separate sub-bullets for each — do not pack them into a single long line.
   - Example:
     ```markdown
     - [ ] **coordinate delivery of spare brake parts** <!-- id:882193e5 -->
       - ✓ messaged vendor contact...
       - waiting on tracking number
       - `2025-03-11 14:32` Vendor sent tracking number #12345. ([Slack #project-alpha](https://{workspace}.slack.com/archives/CXXXXXXXX/p1234567890), 3/10)
     ```
   - Email source example:
     ```markdown
       - `2025-03-11 14:32` PO5119 released into production. ([Email, Flora Zhang](https://mail.google.com/mail/u/0/#search/rfc822msgid%3A...), 3/11)
     ```
   - Multi-point update example (separate sub-bullets for each fact):
     ```markdown
       - `2025-03-12 09:15` Lab requires PO before booking April dates. ([Email, Vendor Contact](https://mail.google.com/mail/u/0/#search/rfc822msgid%3A...), 3/12)
       - `2025-03-12 09:15` Testing being pushed in parallel for April availability. ([Slack #project-beta](https://{workspace}.slack.com/archives/CXXXXXXXX/p...), 3/12)
       - `2025-03-12 09:15` Team lead needs to provide revised schedule. ([Slack #project-beta](https://{workspace}.slack.com/archives/CXXXXXXXX/p...), 3/12)
     ```
   - If the todo appears **completed** based on comms, do NOT check it off. Instead add a sub-bullet flagging it:
     ```markdown
     - [ ] **task description** <!-- id:xxx -->
       - `2025-03-11 14:32` ⚑ Possibly complete: WHO confirmed done. ([Slack #channel](permalink), 3/10) — verify and check off manually
     ```

### 6d. Final dedup and refine pass

After applying all updates to the todo list, **launch a subagent** to re-read the entire file and do a final check:
1. **Remove exact duplicates only:** Only delete a Review item if it is **clearly the same specific action** as an existing todo (same deliverable, same person, same ask). Related-but-different items should stay separate.
2. **Merge related items:** If two Review items are really the same task from different sources, merge them into one item with both sources listed.
3. **Check for repopulated completed tasks:** Read `the completed todos (filter read_todos with status_filter: completed)` and compare against all new Review items. If a Review item matches a task already in the completed file, **remove it** — do not repopulate completed work. As a secondary check, also scan git history (`git log -p -- the todo list`) for items previously checked off (`- [x]`) that may not yet be in the completed file.
4. **Check for already-resolved items:** If a Review item's own description or the existing sub-bullets on a related todo indicate the action has already been taken (e.g., "$463.62 cost increase approved" already exists as a sub-bullet), remove the Review item as stale.
5. **Verify no misattributed updates:** Ensure every automated sub-bullet is about **that specific task**, not merely a related project or adjacent work item. A new PO, a new request, or a different sub-project should be its own todo.
6. **Coherency and conciseness pass:** Run the **Conciseness Pass** (see Shared Procedures) on ALL items in the file (not just new ones).
7. **Title refactoring pass:** For each existing todo, evaluate whether the title should be rewritten. Rewrite only when the current title is clearly too vague to be self-explanatory, or when the status revealed by sub-bullets materially changes what action is needed. Use the format: `[action verb] [specific subject/deliverable] — [status]` where the status suffix is only included if the item is blocked, waiting on someone, or at risk. Drop it when the item is simply in-progress.

   **Rewrite when:**
   - Title uses generic subjects ("the project", "follow up", "check on", "deal with") without specifics
   - Title doesn't reflect who/what is involved
   - Sub-bullets show the item is now blocked or waiting on a specific person/thing and the title doesn't reflect that

   **Do NOT rewrite when:**
   - Title is already specific and actionable
   - Only minor updates were added (one new Slack message, routine status)
   - The rewrite would be essentially the same title

   **Examples:**
   - `follow up with client` → `follow up with Honda re: BMS2000 cell spec sign-off`
   - `coordinate testing` → `schedule lab time for April — waiting on PO from team lead`
   - `firmware issue` → `fix device firmware CAN timeout — blocked on peer review`

   When a title is rewritten, add or replace the `` `updated YYYY-MM-DD HH:MM` `` tag (rule 13).

8. **Clean up empty sections:** If the Review section ends up empty after dedup, remove it entirely.

### 6e. Meeting prep check

After the final dedup pass, run Step 12 (meeting prep escalation). This surfaces upcoming meetings that need preparation and creates or flags todos in-place before the commit. Results are included in the Step 6g summary under "Meeting Prep".

### 6f. Version control

After all updates are applied to the todo list, commit the changes:

```bash
cd the tracking directory && git add the todo list && git commit -m "EA update YYYY-MM-DD HH:MM — <brief summary of changes>"
```

- Use the current date/time and a short summary (e.g., "2 new items, 3 updates")
- If there are no changes to the todo list (git reports nothing to commit), skip silently
- Do NOT push — this is a local-only history

### 6g. Present summary

After the final pass, present a concise summary of what changed:
```
## EA Update Summary — YYYY-MM-DD

### New Items Added to Review (the todo list)
- "task description" — from Slack #channel
- "task description" — from email

### Existing Todos Updated
- "task description" — added: tracking number received
- "task description" — marked complete

### Stale — No Recent Activity (from 6b coverage check)
- "task description" — last sub-bullet dated X, no Slack/email activity in 14 days. Follow up or archive?
- "task description" — no comms found. Still active?

### Meeting Prep
- "Meeting name" — DATE, heavy lift, 3 days out — no prep found, added to Review
- "Meeting name" — DATE, tomorrow — existing todo "[todo title]" flagged (stale)
- "Meeting name" — DATE, tomorrow — covered by "[todo title]", active
```

Tell the userto review the `## Review` section in the todo list and move items to the appropriate section or delete them. Flag stale items for decision: follow up, delegate, or move to Backlog.

