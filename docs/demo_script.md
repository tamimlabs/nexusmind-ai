# NexusMind AI — Demo Video Script (4 minutes)

## Structure

### Opening: The Problem (0:00 - 0:25)
**Screen:** Dashboard at localhost:8080, clean dark UI
**Narration:**
"Most AI agents wait for you to tell them exactly what to do. You ask a question, they answer. You ask another, they answer again. But what about tasks that need multiple steps — research, analyze, summarize, save? Today you'd do that manually: search, open articles, read, copy, paste, organize. What if you could describe what you need in one sentence, and an autonomous agent handled the entire workflow?"

**Show:** Empty dashboard, ready for input

---

### Act 1: Autonomous Task Execution (0:25 - 1:20)
**Action:** Type "Gather all information about current global trends and summarize them into a report" in the dashboard input, click Run

**Show:**
- Task submitted → status badge changes to "planning"
- Right panel Thinking tab lights up: "Analyzing your goal..." → "Breaking down into steps using Gemini Flash..."
- 4 steps appear in the timeline:
  1. `web_search` — "Search for technology and business trends"
  2. `web_search` — "Search for consumer and market trends"
  3. `summarize_text` — "Combine search results into a report"
  4. `write_file` — "Save report to output/"
- Each step executes with live green checkmarks
- Final result appears — a clean, structured summary

**Narration:**
"NexusMind receives your goal and uses Gemini 3.6 Flash to decompose it into ordered steps. No manual guidance — it plans, then executes each step autonomously. Notice it ran TWO web searches with different angles for broader results, then summarized everything with Gemini, and saved the output. Every step is visible in the traceability dashboard."

**Show:** Click on the task → show the Steps timeline with each step's result

---

### Act 2: Self-Correction on Failure (1:20 - 2:00)
**Action:** Submit "Read the file /nonexistent/data.csv and list the contents of the output directory"

**Show:**
- Agent plans: `read_file` → `list_directory`
- Step 1 fails — red error badge: "File not found: /nonexistent/data.csv"
- Thinking panel: "Analyzing error..."
- Agent continues to step 2 — `list_directory` succeeds with green checkmark
- Task completes with partial result (the directory listing)

**Narration:**
"When a tool fails, the agent doesn't crash. It logs the error, continues with remaining steps, and delivers whatever it could. This resilience is critical — real-world tasks rarely go perfectly. The agent adapts and still produces useful output."

**Show:** The task detail showing step 1 failed, step 2 succeeded, and a meaningful result was still returned

---

### Act 3: Smart Approval System (2:00 - 2:45)
**Action:** Submit "Run a Python script that prints the current date and time"

**Show:**
- Agent plans: `execute_code` step
- Before execution, the approval gate appears in the right panel (yellow border)
- Show the approval request details: tool name, description, the code
- Click "Approve" button
- Code executes, result appears
- Navigate to Settings → show the 3 approval modes: Smart / Always / Never

**Narration:**
"High-risk actions like code execution require human approval. But NexusMind is smart about it — in Smart mode, safe commands like `ls` and `cat` are auto-approved, while dangerous ones like `rm -rf` or `sudo` always ask. You can switch between Smart, Always, and Never modes from the Settings page. And with Telegram integration, approvals come to your phone — approve from anywhere while the agent runs autonomously."

**Show:** Quick cut to Settings page showing the 3 mode buttons

---

### Act 4: Memory & Self-Improvement (2:45 - 3:15)
**Action:** Navigate to Memory tab in sidebar

**Show:**
- Memory entries from the tasks we just ran
- Filter by category: "Task Outcomes" → show task results
- Filter by "Reflections" → show lessons the agent learned
- Click "All" to see everything together

**Narration:**
"After each task, the agent reflects on what it learned and stores actionable lessons. These are fed back into future planning — so the next time you ask for a research summary, the agent already knows what worked before. Memory is deduplicated and curated — only meaningful entries are kept, with hard limits to prevent bloat."

**Show:** Show the reflection entries, then switch back to Tasks to show the full list

---

### Act 5: Credentials & Dashboard Tour (3:15 - 3:40)
**Action:** Navigate to Credentials page

**Show:**
- 10 credential categories: AI, Cloud, Search, Telegram, GitHub, GitLab, Slack, Discord, Jira, Email
- Masked values for security (dots)
- "Save All" button

**Quick dashboard tour:**
- Tasks page — task list with status badges
- Approvals page — pending approvals
- Settings page — approval mode selector

**Narration:**
"All API keys and secrets are managed in one place — the Credentials page. Values are masked in the UI and stored securely in `.env`. The dashboard gives you full visibility: tasks, approvals, memory, and settings — everything you need to monitor and control the agent."

---

### Closing (3:40 - 4:00)
**Screen:** Dashboard showing completed tasks with traces
**Narration:**
"NexusMind AI — an autonomous agent that plans, executes, self-corrects, and learns. One sentence becomes a complete workflow. It runs on Google Cloud with Gemini 3.6 Flash, Google ADK, Cloud Run, Firestore, and Pub/Sub. Built for the Google All Things Agentic Hackathon."

**Show:**
- Repo URL: github.com/tamimlabs/nexusmind-ai
- Tech stack badges
- "Built for Google All Things Agentic Hackathon"

---

## Recording Tips

1. **Screen record** at 1080p, dark mode dashboard
2. **Voice over** — speak clearly, enthusiastic but not rushed
3. **Keep it tight** — exactly 4 minutes max
4. **Show live execution** — don't just show static screenshots
5. **Highlight the trace panel** — this is what differentiates us
6. **Show the approval gate** — this is the "wow" moment
7. **Pause briefly** on each feature** — let judges absorb what they see
8. **Show the thinking panel** — watching the agent think in real-time is impressive

## Files to Have Open During Recording

- Dashboard at `localhost:8080` (full screen, dark mode)
- Terminal showing agent logs (bottom of screen, small)
- VS Code showing orchestrator.py briefly (10 seconds, just the agent loop)

## Task List (Pre-plan these for smooth recording)

1. **First task:** "Gather all information about current global trends and summarize them into a report"
2. **Second task:** "Read the file /nonexistent/data.csv and list the contents of the output directory"
3. **Third task:** "Run a Python script that prints the current date and time"
4. **Memory demo:** Navigate to Memory tab, show filters
5. **Credentials demo:** Navigate to Credentials page, show categories

## Backup Plan

If something fails during recording:
- Have the tasks pre-run so results are cached in memory
- The dashboard shows live events even if the backend has issues
- Skip approval demo if no task triggers it — show the Settings page instead
- The trace panel always works — show it even if tools fail
- If Gemini is rate-limited, the multi-key rotation kicks in automatically — just wait a few seconds
