## Step 11 — Targeted item check (`/ea checkon <query>`)

Lightweight update check on a single todo. **Follows existing references first** — checks the specific threads, channels, and email chains already linked in the description. Applies updates and confirms. No full sweep, no drafting. **Never ask questions or offer follow-ups — just update and confirm.**

### 11a. Find the todo

1. Use `get_todo` for each of the ids in query

### 11b. Follow existing references

**Principle: check what's already linked before searching broadly.** The description contains Slack permalinks, email permalinks, channel names, and person names. Use those directly.

**Extract from the todo's description:**
- Slack channel IDs and thread timestamps → check those threads/channels for new messages since the last recorded date. Also do a brief search for messages with relevant parties and topic.
- Email permalinks or sender names → search IMAP for same subject/sender if thread may have new replies
- Smartsheet/Confluence/Jira URLs → fetch if status may have changed

**Delegate multiple a subagents** (per rule 12) to check these references:
- Slack threads/channels for new messages since the last recorded date
- IMAP for new replies to referenced email chains
- Confluence/Jira for status changes on linked pages/issues
- **Include permalink for every finding** (rules 8 and 9)

Then review subagent findings and pull full context for relevant items (per rule 13). Only broaden to keyword search if the item has no description references at all.

### 11c. Filter and apply updates

1. From results, identify only findings **genuinely about this specific todo** — not adjacent work.
2. Compare against existing description to avoid duplicates.
3. If there are new findings, use `update_todo` to append new lines to the description:
   ```
   `YYYY-MM-DD HH:MM` One fact per line. ([Slack #channel](permalink))
   ```
   - Every line MUST include a permalink (rules 8 and 9)
   - One fact per line
   - If the todo appears completed, append: `` `YYYY-MM-DD HH:MM` ⚑ Possibly complete: WHO confirmed done. ([source](link)) — verify and complete manually ``
4. If nothing new, report "No new activity" and stop.

### 11d. Consolidate description

Always do this! Aggressively attempt to consolidate.

After applying updates, rewrite the description to be concise. Use `update_todo` to replace the full description. 

Recontextualize all relative dates to be current (today, tomorrow, yesterday, next/last week).

**Keep:**
- Current status and actionable next steps
- Only most critical updates and key events
- Active blockers and who owns them
- Document links (Confluence, Jira, Smartsheet, SharePoint, Google Drive)
- The most recent timestamped update(s) that reflect current state
- Key decisions and commitments with source links

**Remove:**
- Superseded status lines — if a newer update covers the same fact, drop the older one
- Intermediate back-and-forth that led to a resolved outcome (keep only the resolution)
- Stale "waiting on X" lines where X has since responded or the item moved forward
- Redundant source links — if the same thread is cited 3 times, keep the latest
- Verbose detail that can be summarized in fewer words

**Format:** Keep the consolidated description compact — aim for 3–8 lines for a typical item. Each line should be either a current fact, an action item, or a document link. Preserve the `` `YYYY-MM-DD HH:MM` `` timestamp + permalink format for the most recent updates.

### 11e. Confirm

One-line confirmation: "Checked On**<todo title>** — N new updates." or "No new activity on **<todo title>** since <last date>."

Provide a summary of removed/consolidated items from the todo description.

Do NOT present a formatted summary or repeat the updates — they're already in the todo.

