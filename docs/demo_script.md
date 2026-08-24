# NexusMind AI — Demo Video Script (4 minutes)

> **Platform:** Google Cloud Shell + Loom
> **Dashboard:** Cloud Shell Web Preview on port 8080
> **Browser:** Full screen, dark mode, zoom 100%

---

## Pre-Recording Checklist

- [ ] Cloud Shell running: `python -m api.main`
- [ ] Dashboard loads at Web Preview (port 8080)
- [ ] Agent status: green dot, "Agent Online"
- [ ] Loom recording set to full screen + camera
- [ ] Telegram bot running (long-polling active)
- [ ] GitHub watcher pre-configured (from previous session)
- [ ] Terminal visible at bottom, dashboard full screen

---

## Act 1: Hook + Agent Status (0:00 – 0:20)

**Screen:** Dashboard — Tasks page, clean dark UI

**Narration:**
> "Every day, people waste hours on multi-step tasks — researching, summarizing, organizing. What if you could describe what you need in one sentence, and an autonomous AI agent handled everything end to end? This is NexusMind AI."

**Show (5 seconds):**
- Dashboard loads — dark theme, clean layout
- Bottom-left status bar: green dot, `Agent Online`, `Gemini 3.5 Flash`
- Stats bar: `11 Tools`, `0 Tasks` (clean slate for demo)
- Top-right: Live mode toggle, Refresh button

**Transition:** Click into the task input field

---

## Act 2: Autonomous GitHub Monitoring (0:20 – 1:30)

**Action:** Type this exact task:

```
Connect to GitHub repo tamimlabs/nexusmind-ai and monitor all pull requests. For every new PR: run tests, review code quality, check for security issues, and auto-merge if safe or reject with a comment explaining why
```

**Show (30-40 seconds):**

1. **Planning phase (3-5 sec)**
   - Status badge: `idle` → `planning`
   - Thinking panel activates: *"Analyzing your goal..."* → *"Breaking into steps with Gemini Flash..."*
   - Steps appear in timeline (0-indexed):
     - Step 0: `execute_code` — "Create GitHub API integration"
     - Step 1: `run_command` — "Configure PR event watcher"
     - Step 2: `web_search` — "Research best practices for PR review"
   - Each step shows blue `planned` badge

2. **Execution phase (15-25 sec)**
   - Steps turn green `completed` one by one
   - Live events stream in the right panel
   - GitHub watcher starts — monitoring PRs
   - Agent is now running in background

3. **GitHub integration result**
   - Task completes with full trace
   - Show the trace panel — each step has timing, input/output
   - Memory stores the lesson: *"GitHub PR monitoring configured for tamimlabs/nexusmind-ai"*

**Narration:**
> "NexusMind isn't a one-shot tool — it's an always-on autonomous agent. I connected it to my GitHub repo. Now it monitors every pull request automatically: runs tests, reviews code quality with Gemini, checks for security issues, and auto-merges safe PRs or rejects dangerous ones with detailed comments. It runs 24/7, handling events as they arrive."

**Quick cut — Watchers page:**
- GitHub watcher: green dot, `Running`, polling every 5 minutes
- Other available watchers visible: Slack, Discord, Webhook, Email, RSS, Jira, GitLab, Hacker News, Cron, Google Calendar

---

## Act 3: Web Search + Summarization (1:30 – 2:10)

**Action:** Submit:

```
Search the web for the latest AI news this week and summarize the top 3 stories with key details
```

**Show (30-40 seconds):**

1. **Planning**
   - Step 0: `web_search` — "Search for latest AI news"
   - Step 1: `summarize_text` — "Summarize top 3 stories"

2. **Execution**
   - Step 0 completes — web search returns real results (ddgs library)
   - Step 1 completes — Gemini-powered summarization
   - Result shows 3 structured news summaries with titles, key points, sources

3. **Result panel**
   - Clean formatted output with headings
   - Each story: title, 2-3 bullet points, source

**Narration:**
> "Watch this — I ask it to search the web for AI news and summarize. The agent searches using DuckDuckGo, gets real results, then uses Gemini to summarize the top stories into a clean digest. The whole thing takes seconds, and the result is stored in memory for future reference."

**Key point to emphasize:** The agent used TWO tools autonomously — web search then summarization — without being told the intermediate steps.

---

## Act 4: Smart Approval System (2:10 – 2:45)

**Action:** Set approval mode to "Always" first:
1. Go to Settings page
2. Click "Always" mode button
3. Confirm mode changed

**Then submit:**

```
Execute a Python script that calculates the Fibonacci sequence up to 50 and prints each number
```

**Show (25-30 seconds):**

1. Agent plans: `execute_code` step
2. **Approval gate appears** in right panel — yellow border, `Pending` badge
3. Shows: tool name `execute_code`, the Python code preview
4. **Click "Approve"** button on dashboard
5. Code executes, result streams in
6. Fibonacci numbers print out
7. Task completes

**Narration:**
> "High-risk actions like code execution require human approval. In Always mode, every dangerous action asks for permission. You can see exactly what the agent wants to do before approving. Switch to Smart mode and safe commands auto-approve while dangerous ones still ask. Never mode auto-approves everything for maximum speed."

**Quick cut — Settings page:**
- Show 3 mode buttons: Smart (default), Always (active), Never
- Show mode description text under each
- Switch back to Smart mode

---

## Act 5: Memory & Self-Improvement (2:45 – 3:10)

**Action:** Click "Memory" in sidebar

**Show (20-25 seconds):**

1. Memory entries from all previous tasks
2. Category filters at top: `All`, `Task Outcomes`, `Reflections`
3. Click `Task Outcomes` → completed task summaries with timestamps
4. Click `Reflections` → lessons learned:
   - *"GitHub PR monitoring configured for tamimlabs/nexusmind-ai"*
   - *"Web search results summarized successfully"*
5. Memory search bar — type "GitHub" → filtered results
6. Show memory count in stats bar

**Narration:**
> "After every task, the agent reflects and stores what it learned. These insights feed into future planning — so the next time you ask something similar, the agent already knows what worked. Memory is deduplicated and curated automatically. Only meaningful entries are kept."

---

## Act 6: Telegram Remote Approvals (3:10 – 3:45)

**Action:** Submit a task that triggers approval:

```
Run a shell command to check disk space usage on this machine
```

**Show (30-35 seconds):**

1. Agent plans: `run_command` step → approval gate appears
2. **Split screen or cut to phone:**
   - Telegram bot sends approval request
   - Shows: task goal, tool name, command details
   - Two inline buttons: ✅ Approve | ❌ Deny
3. **Tap "Approve" in Telegram**
4. **Cut back to dashboard:**
   - Approval badge disappears
   - Step executes → disk space results appear
   - Task completes with green checkmark

**Narration:**
> "With Telegram integration, the agent sends approval requests directly to your phone. It pauses, shows you exactly what it wants to do, and waits. One tap to approve or deny — the agent continues instantly. You can manage the agent from anywhere while it runs autonomously 24/7."

**Quick cut — Telegram chat history:**
- Show previous approval messages from the bot
- Show task completion notifications
- Show the clean formatted messages with emojis

---

## Act 7: Closing (3:45 – 4:00)

**Screen:** Back to Tasks page — all tasks showing green `completed` badges

**Narration:**
> "NexusMind AI — an autonomous agent that plans, executes, self-corrects, and learns. One sentence becomes a complete workflow. It runs on Google Cloud with Gemini 3.5 Flash, Google ADK, Cloud Run, Firestore, and Pub/Sub. Built for the Google All Things Agentic Hackathon."

**Show (10 seconds):**
- Dashboard overview — tasks completed, memory entries, agent status
- GitHub repo: `github.com/tamimlabs/nexusmind-ai`
- Tech stack: Gemini 3.5 Flash, Google ADK, Cloud Run, Firestore, Pub/Sub
- Final frame: "Built for Google All Things Agentic Hackathon"

---

## Key Features to Emphasize (Judges Care About These)

| Feature | Why It Matters | Where to Show |
|---------|---------------|---------------|
| **Autonomous planning** | Gemini decomposes goals into steps | Act 2, thinking panel |
| **Self-correction** | Agent handles failures gracefully | Act 3 if any step fails |
| **Smart approval** | Balances autonomy with safety | Act 4 approval gate |
| **Memory & learning** | Gets better over time | Act 5 memory page |
| **Always-on watchers** | Event-driven, not polling | Act 2 watchers page |
| **Remote control** | Manage from phone via Telegram | Act 6 phone demo |
| **10+ tools** | Versatile execution capabilities | Status bar tool count |
| **Google Cloud native** | Built on Google's stack | Closing narration |

---

## Backup Plans

| Issue | Fix |
|-------|-----|
| Gemini rate limit (429) | Multi-key rotation kicks in — wait 2 sec, retry |
| Web search returns empty | DDG library handles it — retry with different query |
| Approval doesn't trigger | Switch to "Always" mode in Settings first |
| Telegram buttons don't work | Check long-polling active in terminal logs |
| GitHub watcher fails | Show pre-recorded result from memory |
| Task takes too long | Cut to completed task, show trace panel |
| Dashboard elements missing | Hard refresh browser (Ctrl+Shift+R) |
| Cloud Shell timeout | Server auto-restarts — just reopen Web Preview |

---

## Recording Tips

1. **1080p screen recording**, dark mode dashboard, zoom 100%
2. **Speak clearly** — enthusiastic but not rushed, 150 words/min
3. **Exactly 4 minutes max** — judges stop watching after 4:00
4. **Show live execution** — never static screenshots
5. **Highlight the trace panel** — this differentiates from competitors
6. **Show the approval gate** — this is the "wow" moment
7. **Pause 2 seconds** on each feature — let judges absorb
8. **Show the thinking panel** — watching agent think in real-time is impressive
9. **Navigate sidebar pages** — show the full dashboard breadth
10. **End on the tasks page** — visual proof of completed work
