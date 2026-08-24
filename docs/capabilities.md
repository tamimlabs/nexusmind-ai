# NexusMind AI — What Can This Agent Do?

**An autonomous AI agent that plans, executes, self-corrects, and learns — powered by Gemini 3.5 Flash and Google Cloud.**

---

## At a Glance

| | |
|---|---|
| **What is it?** | An autonomous task-execution agent |
| **How does it work?** | You give it a goal → it plans steps → executes tools → learns from results |
| **What makes it different?** | It self-corrects on failure, asks for permission on risky actions, and improves over time |
| **Built for** | Google "All Things Agentic" Hackathon, Track: The Taskmaster |

---

## Core Capabilities

### 1. Autonomous Task Planning
Give NexusMind a goal in plain English. It uses **Gemini 3.5 Flash** to break it into concrete, ordered steps — no manual guidance needed.

> **User:** "Find the top 3 Python web frameworks and compare their performance"
>
> **Agent plans:**
> 1. Search for Python web framework benchmarks
> 2. Fetch detailed comparison data
> 3. Summarize findings into a structured comparison

### 2. Tool Execution
The agent has 10 registered tools it can use autonomously:

| Tool | What It Does |
|------|-------------|
| `web_search` | Search the web (Google Custom Search + DuckDuckGo fallback) |
| `fetch_url` | Fetch and extract text from any URL |
| `read_file` | Read local files |
| `write_file` | Create or update files |
| `list_directory` | Browse directory contents |
| `parse_json` | Extract structured data from JSON |
| `summarize_text` | Condense long text into key points |
| `extract_data` | Pull specific data from unstructured text |
| `execute_code` | Run Python code in a sandbox |
| `run_command` | Execute shell commands |

### 3. Self-Correction on Failure
When a tool fails, NexusMind doesn't give up. It:
1. Sends the error to Gemini Flash
2. Analyzes what went wrong
3. Adjusts its approach
4. Retries automatically (up to 2 times)

> **Step 1 fails:** "File /data.csv not found"
> **Agent thinks:** "The file path might be wrong. Let me list the directory first."
> **Step 2 retries:** Lists files → finds correct path → continues successfully

### 4. Human-in-the-Loop Approval (Smart + Telegram)
High-risk actions require human approval, but with **smart logic**:

**Smart Approval (default):**
- Auto-approve safe commands (`ls`, `cat`, `git status`)
- Only ask for dangerous commands (`rm -rf`, `sudo`, `eval`)
- Reduces approval fatigue while keeping you safe

**3 Approval Modes:**
- **Smart** — Auto-approve safe, ask for dangerous (recommended)
- **Always** — Ask for every high-risk tool
- **Never** — Auto-approve everything

**Telegram Bot (Remote Approvals):**
- Approve/deny from your phone while agent runs autonomously
- Get notified when tasks start, complete, or fail
- 2-minute setup: create bot → add token to dashboard → done

### 5. Self-Improvement (Learns Over Time)
After every task, NexusMind reflects on what happened and extracts actionable lessons:

> **Reflection:** "When searching for recent news, always specify the year in the query to avoid outdated results."

These lessons are stored in memory and **automatically fed into future planning**. The agent gets better at similar tasks over time.

### 6. Web Search with Smart Fallback
Search the web without worrying about API limits:
- **Primary:** Google Custom Search (100 free queries/day)
- **Fallback:** DuckDuckGo (unlimited, free, no API key needed)
- **Auto-switch:** When Google's daily limit is hit, seamlessly switches to DuckDuckGo

### 7. Event-Driven Architecture
NexusMind can be triggered by external events via Google Cloud Pub/Sub:
- GitHub webhook → auto-review code changes
- Stripe webhook → auto-process payment failures  
- Calendar event → auto-prepare meeting briefs
- Any HTTP endpoint → instant task creation

### 8. Always-Awake Watchers (11 Platforms)
Unlike traditional agents that sleep between tasks, NexusMind can monitor platforms 24/7 and react automatically:

| Platform | What It Monitors |
|----------|-----------------|
| GitHub | New PRs, issues |
| GitLab | New merge requests, issues |
| Slack | Channel messages, mentions |
| Discord | Channel messages |
| Jira | New/updated issues |
| Reddit | New posts in subreddits |
| Hacker News | New stories, comments |
| Email (IMAP) | Inbox messages |
| RSS/Atom | Feed items |
| Cron | Scheduled tasks |
| Custom Webhook | Any HTTP event |

**Token-efficient:** Only calls Gemini when events are detected -- no wasted API calls.

### 9. Unified Credentials Management
All API keys and secrets managed in one place:
- **10 categories:** AI, Cloud, Search, Telegram, GitHub, GitLab, Slack, Discord, Jira, Email
- **Secure:** Secret values masked in UI, stored in `.env` (gitignored)
- **Auto-fill:** Watchers use saved credentials automatically
- **Telegram setup:** Add bot token + chat ID for remote approvals

### 10. Traceability Dashboard
Every step is logged and visible in real-time:
- **Tool calls** (blue) — what the agent did
- **Reasoning steps** (purple) — what the agent was thinking
- **Approval gates** (yellow) — where it asked for permission
- **Errors** (red) — what went wrong and how it recovered

### 11. Persistent Memory (Firestore)
Tasks, reflections, and skills persist across restarts via Google Cloud Firestore:
- Search past tasks by similarity
- Recall lessons learned
- Track what works and what doesn't

### 12. Multi-Step Task Decomposition
Complex goals are automatically broken into manageable steps:
- Dependencies are resolved (step before step B)
- Each step maps to a specific tool
- Results flow between steps (output of step 1 becomes input for step 2)

---

## Example Tasks NexusMind Can Handle

| Task | What the Agent Does |
|------|-------------------|
| "Research competitor pricing and write a summary" | Web search → fetch pages → extract data → write file |
| "Analyze this CSV file and find trends" | Read file → parse data → execute code → summarize |
| "Set up a project scaffold for a Flask app" | Plan structure → write files → verify setup |
| "Monitor this website for changes" | Fetch URL → extract data → compare → alert if changed |
| "Summarize today's AI news from 5 sources" | Web search → fetch 5 URLs → summarize each → combine |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **LLM** | Gemini 3.5 Flash (Google) |
| **Agent Framework** | Google ADK (Agent Development Kit) |
| **State Persistence** | Google Cloud Firestore |
| **Event Routing** | Google Cloud Pub/Sub |
| **Deployment** | Google Cloud Run (scales to zero) |
| **API** | FastAPI (Python) |
| **Frontend** | Real-time traceability dashboard |
| **Language** | Python 3.13 |

---

## Architecture

```
User submits goal
       │
       ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Orchestrator│────▶│   Planner    │────▶│   Gemini    │
│   (main loop)│     │  (decompose) │     │  3.5 Flash  │
└──────┬──────┘     └──────────────┘     └─────────────┘
       │
       ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Executor   │────▶│  Tool Engine │────▶│  Self-Correct│
│ (run steps)  │     │  (10 tools)  │     │   (retry)    │
└──────┬──────┘     └──────────────┘     └─────────────┘
       │
       ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Memory     │◀────│  Reflection  │────▶│  Next Task   │
│ (Firestore)  │     │   (learn)    │     │  (improved)  │
└─────────────┘     └──────────────┘     └─────────────┘
```

---

## Why This Matters

Most AI agents today are **reactive** — they wait for instructions and execute one step at a time.

NexusMind is **autonomous**:
- It plans without being told how
- It recovers from failures without being asked
- It asks permission before doing anything risky (via Telegram on your phone)
- It learns from every task and improves over time
- It monitors platforms 24/7 and reacts to events automatically

**Walk away after giving it a task — it works autonomously for days/weeks.**

This is what "agentic" means — an AI that takes initiative, not just follows orders.

---

## Links

| Resource | URL |
|----------|-----|
| **GitHub** | https://github.com/tamimlabs/nexusmind-ai |
| **Demo** | https://nexusmind-ai-uc.a.run.app |
| **Hackathon** | Google "All Things Agentic" 2026 |

---

*Built for the Google "All Things Agentic" Hackathon — Track: The Taskmaster*
