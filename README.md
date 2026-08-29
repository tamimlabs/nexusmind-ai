<div align="center">

<div style="display: inline-block; background: rgba(0,0,0,0.7); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border-radius: 20px; padding: 24px 48px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 8px 32px rgba(0,0,0,0.4);">
<img src="docs/nexusmind-dark-full.png" alt="NexusMind AI" width="400" style="border-radius: 12px; filter: drop-shadow(0 0 20px rgba(66,133,244,0.3));" />
</div>

### Autonomous Task-Execution Agent on Google Cloud

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg?style=flat&logo=python&logoColor=white)](https://python.org)
[![Gemini](https://img.shields.io/badge/Gemini-3.5%20Flash-4285F4?style=flat&logo=google&logoColor=white)](https://ai.google.dev)
[![Google Cloud](https://img.shields.io/badge/Google%20Cloud-Run%20%7C%20Firestore%20%7C%20Pub%2FSub-4285F4?style=flat&logo=googlecloud&logoColor=white)](https://cloud.google.com)
[![Google ADK](https://img.shields.io/badge/Google%20ADK-2.x-4285F4?style=flat&logo=google&logoColor=white)](https://cloud.google.com/vertex-ai/generative-ai/docs/agent-development-kit)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Demo Video](https://img.shields.io/badge/🎬_Demo_Video-Watch_on_YouTube-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/watch?v=woSOCuzfabg)

**Built for the [Google "All Things Agentic" Hackathon](https://allthingsagentic.devpost.com) -- Track: The Taskmaster**

</div>

<div align="center">

### 🎬 Demo Video — See NexusMind AI in Action (4 min)

[![▶️ Watch Demo on YouTube](https://img.youtube.com/vi/woSOCuzfabg/maxresdefault.jpg)](https://www.youtube.com/watch?v=woSOCuzfabg)

**[▶️ WATCH DEMO ON YOUTUBE — 4 MIN](https://www.youtube.com/watch?v=woSOCuzfabg)** &nbsp;|&nbsp; *Autonomous PR reviews, multi-step workflows, Telegram approvals & self-learning — all in one take*

</div>

---

## The Problem

**Context switching is killing developer productivity.**

On any given day, a developer or team lead bounces between 5+ tools just to stay informed:

| Task | Time spent | Tool |
|------|-----------|------|
| GitHub PRs: review code, check CI/tests, comment, merge/reject | ~45-60 min | GitHub |
| Scan Slack for mentions / decisions | ~30 min | Slack |
| Review Jira for blockers | ~15 min | Jira |
| Monitor Reddit / Hacker News for relevant news | ~10 min | Reddit / HN |
| Triage email | ~20 min | Email |

That's **~2–2.5 hours per developer per day** -- not building, not thinking, just **reading and reacting**. A PR alone isn't "20 min" -- it's open diff, understand context, verify CI/tests pass, check for conflicts, write a review comment, then merge or request changes and follow up. Multiply by a 10-person team and you're burning **20+ hours/week** on context switching alone.

Most of this work is repetitive, low-judgment, and high-volume. It doesn't require a human -- it requires **attention**.

## The Solution

**One AI agent that watches everything and acts automatically.**

NexusMind AI monitors your GitHub, Slack, Jira, Reddit, Hacker News, email, and RSS feeds simultaneously. When something needs attention, it handles it -- reviews the PR, summarizes the thread, triages the issue -- and only escalates to you when it genuinely matters.

> **Run it once, walk away -- it continuously monitors events and executes workflows without repeated prompting.**

It's not a chatbot waiting for instructions. It's an always-on teammate that knows your tools, learns your preferences, and takes action on your behalf.

## Overview

Most AI today waits for you to ask. **NexusMind AI doesn't.**

It's an autonomous agent that receives goals -- via API, webhooks, or a live dashboard -- and handles multi-step workflows end-to-end without hand-holding. It plans, executes, self-corrects on failure, asks permission for risky actions, and learns from every task. **Run it once, walk away -- it continuously monitors events and executes workflows without repeated prompting.**

> **New to coding?** Check out our [Non-Coder User Guide](docs/user_guide.md) -- use NexusMind AI without writing any code.

### What It Can Do

- **Step-by-step adaptive execution** (opencode-style) -- Gemini decides ONE action at a time, the real result (or error) of each step drives the next decision: self-correct, verify, and keep working until the goal is actually met
- **Human-in-the-loop safety** -- dangerous actions pause for approval via Telegram or dashboard
- **Persistent cross-session memory** -- SQLite locally; Firestore on Cloud Run, with hybrid retrieval (BM25 + Jaccard + HRR), trust scoring (`+0.05`/`-0.10`), fenced `<memory-context>`, contradiction detection, and compositional recall (`search`/`probe`/`related`/`reason`)
- **Self-evolving skills** -- solved tasks become reusable SKILL.md packages with usage telemetry and an audit ledger
- **11 automated watchers** -- continuously monitors GitHub, GitLab, Slack, Discord, Jira, Reddit, Hacker News, Email, RSS, Cron, and Webhooks, triggering workflows when new events are detected
- **Builds real artifacts** -- generates complete multi-file projects (`projects/<name>/`: HTML/CSS/JS + backend server)
- **Zero-cost commands** -- `/help`, `/start`, `/status`, `/tasks`, `/pending`, `/tools`, `/skills`, `/memory` answered deterministically without an LLM call (path-safe: `/Users/x/file.md` is a task, not a command)
- **Full observability** -- live reasoning chain, audit trails, and **each step appearing in the dashboard as it runs** -- you watch the agent work in real time, not after the fact

### Example: Autonomous GitHub PR Operations

The strongest demonstrated workflow -- a GitHub PR watcher detects a new pull request and handles the full review cycle without human intervention:

```
New PR detected (watcher)
      |
Resolve repository
      |
Find open PRs
      |
Analyze PR with Gemini (code review, risk assessment)
      |
Generate review decisions (merge / reject / skip)
      |
Risky action?
   /          \
 No            Yes
 |              |
Execute       Request approval (Telegram / Dashboard)
 |              |
Apply decision  Approve / Deny
 |              |
Store task outcome + reflect into memory
```

**What happens:** the agent reviews code quality, checks for conflicts, assesses merge risk, and generates a verdict with confidence score. Merges and closes require human approval. Every review is logged with full reasoning traces in the dashboard.

**Setup:** one API call to create a watcher on your repo. The agent runs continuously, reviewing every new PR as it arrives.

---

## Architecture

```
                        ┌──────────────────────────────┐
                        │      Dashboard (Live UI)      │
                        │   Reasoning chain + approvals  │
                        └──────────────┬───────────────┘
                                       │
┌──────────────┐     ┌─────────────────▼─────────────────┐     ┌──────────────┐
│   GitHub      │────>│       REST API (FastAPI)          │<────│  External     │
│   Webhooks    │     │    POST /api/webhooks             │     │   Events    │
└──────────────┘     └─────────────────┬─────────────────┘     └──────────────┘
                                       │
                        ┌──────────────▼──────────────┐
                        │      Cloud Pub/Sub            │
                        │    (Event-Driven Routing)     │
                        └──────────────┬──────────────┘
                                       │
                        ┌──────────────▼──────────────┐
                        │        Orchestrator           │
                        │  ┌────────────────────────┐  │
                        │  │  Gemini 3.5 Planner    │──┼-- Step decomposition
                        │  │  Adaptive Step Loop    │──┼-- Decide -> Execute -> Observe
                        │  │  Tool Executor         │──┼-- Sandboxed execution
                        │  │  Self-Correction       │──┼-- Error -> Gemini -> Retry
                        │  │  Approval Gate         │──┼-- Human-in-the-loop
                        │  │  Memory System         │──┼-- Persistent context
                        │  │  Trace Collector       │──┼-- Reasoning chain
                        │  │  Self-Improvement      │──┼-- Post-task reflection
                        │  └────────────────────────┘  │
                        └──────────────┬──────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
     ┌────────▼────────┐     ┌────────▼────────┐     ┌────────▼────────┐
     │    Firestore     │     │   Gemini API     │     │    Cloud Run    │
     │  (State/Memory)  │     │  (Gemini LLM)   │     │  (Deployment)   │
     └─────────────────┘     └─────────────────┘     └─────────────────┘
```

---

## The Agent Loop

NexusMind does not run a goal like a one-shot script. It works **step by step, like a programmer in an IDE**: it decides ONE action, executes it, and feeds the real outcome — including errors — back into the very next decision. It corrects what failed, verifies results before finishing, and keeps going for as long as a big project needs (bounded only by a generous step budget).

```
   Goal + recalled memory + workspace state
          |
          v
   LOOK: transcript of everything done so far
          |
          v
   DECIDE the single next action (Gemini) ----------------+
          |                                              |
    write_file / execute_code / web_search / ...         |
          |                                              |
    Execute THAT ONE action (smart approval applies)     |
          |                                              |
   Record the real RESULT (or ERROR) into transcript ----+
          |
          v
   Goal satisfied & verified?  --> No --> back to DECIDE
          |
         Yes
          v
     DONE: written summary + saved file locations
```

**How this plays out in practice:** the agent creates a folder, writes `styles.css`, writes `main.js`, then writes `index.html` — resolving each step's success (or error) before the next one. If a step fails (a module is missing, an approval times out, a tool returns a bad result), the error lands in the transcript and the next decision fixes it: install the dependency, switch from `execute_code` to `write_file`, verify the files with `list_directory`/`read_file` — then declare the goal done. Steps appear **live in the dashboard** as they happen.

> **Budgets & guardrails:** 40-step budget (`MAX_STEPS`), abort after 3 consecutive failures, 20k chars per `write_file`, bounded transcript (last 25 entries / 12k chars) + live TODO `todo_updates` (`add`/`complete`/`skip`, max 5/step, 30 cap). **Project collision guard:** `projects/<name>/` that existed before the task is blocked for `write_file` until you prove intent with `read_file`/`list_directory` — otherwise `BLOCKED: "…is an EXISTING project — read its files first or pick a new name (-2/v2)"`.

---

## Quick Start

### Prerequisites

- Python 3.11+
- A [Gemini API key](https://aistudio.google.com/apikey) (free tier works)

### Local Setup

```bash
# Clone
git clone https://github.com/tamimlabs/nexusmind-ai.git
cd nexusmind-ai

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install .

# Configure
cp .env.example .env
# Edit .env -- add your Gemini API key(s), comma-separated for rotation

# Run the API + dashboard
python -m api.main
# Open http://localhost:8080
```

### Cloud Shell (No GCP Account Needed)

```bash
# One command setup in Google Cloud Shell:
bash <(curl -s https://raw.githubusercontent.com/tamimlabs/nexusmind-ai/master/scripts/setup_cloud_shell.sh)
```

### Deploy to Cloud Run

```bash
# Windows
.\scripts\deploy.ps1

# Linux/Mac
bash scripts/deploy.sh
```

---

## Smart Approval + Telegram Bot

Agent runs autonomously. When it needs permission, it asks via Telegram -- approve from your phone.

### 3 Approval Modes

| Mode | Behavior | Best For |
|------|----------|----------|
| **Smart** (default) | Auto-approve safe commands (`ls`, `cat`, `git status`), ask only for dangerous ones | Most users |
| **Always** | Ask for every high-risk tool | Maximum safety |
| **Never** | Auto-approve everything | Trusted environments |

### How Smart Approval Works

**Auto-approved (safe):**
- `ls`, `cat`, `head`, `tail`, `grep`, `find`, `pwd`, `whoami`
- `git status`, `git log`, `git diff`
- `pip list`, `pip show`
- `python -c "print(...)"` (read-only)

**Always asks (dangerous):**
- `rm -rf`, `del /f`, `format`, `shutdown`, `reboot`
- `sudo`, `chmod 777`, `chown`
- `eval`, `exec`, `pipe to bash`
- `deploy`, `transfer_funds`

### Telegram Bot Setup (2 minutes)

1. Open Telegram, search for `@BotFather`
2. Send `/newbot` → name it → copy the **bot token**
3. Search for `@userinfobot` → copy your **Chat ID**
4. Open Dashboard → **Credentials** → paste both → **Save All**
5. Done! Approvals now come to your phone

### Telegram Messages

```
🔐 Approval Required

Task: Deploy to production
Tool: run_command
Command: ./deploy.sh --prod

[✅ Approve] [❌ Deny]
```

### One-Approval-Per-Task Trust

Once you approve one risky step in a task, the remaining risky steps in **that same task** auto-approve (reduces fatigue for multi-step goals). Trust is per-task and cleared on completion. Diagnostics & explicit control: `GET /api/approvals/trusted`, `GET /api/tasks/{id}/trust`, `POST /api/tasks/{id}/trust`.

### Dashboard Settings

- **Settings page** — Select approval mode (Smart/Always/Never)
- **Credentials page** — Add Telegram bot token + chat ID
- **Approvals page** — View pending approvals (dashboard fallback)
- Smart gate details: side-effect regex (`; | \` > && || $(`), 22 dangerous patterns + `is_dangerous_code` (`os.system`, `subprocess`, `shutil.rmtree`, `eval`/`exec`), `pathlib` scaffold to `projects/`/`output/` auto-approved when safe

---

## Automated Event-Driven Watchers

NexusMind can monitor external platforms and react to events automatically -- no manual polling needed.

### Supported Platforms

| Platform | What It Monitors |
|----------|-----------------|
| **GitHub** | New PRs, issues |
| **GitLab** | New merge requests, issues |
| **Slack** | Channel messages, mentions |
| **Discord** | Channel messages |
| **Jira** | New/updated issues |
| **Reddit** | New posts in subreddits |
| **Hacker News** | New stories, comments |
| **Email (IMAP)** | Inbox messages |
| **RSS/Atom** | Feed items |
| **Cron** | Scheduled tasks |
| **Custom Webhook** | Any HTTP event |

### How It Works

1. User creates a watcher via Dashboard or API
2. Watcher polls the platform every N minutes (configurable)
3. When new events are detected, watcher checks **memory-gated autonomy**: needs a matching standing instruction (`INSTRUCTION_KEYWORDS` per watcher, most-recent-wins substring match) — see `docs/capabilities.md` & `docs/user_guide.md`
4. **No match?** Event is NOT silently dropped — it creates a `needs_instruction` task panel entry + hint, Telegram at most once per 6h per watcher. **Cron & Webhook** are pre-authorized (owner-authored goals skip the gate)
5. With a matching instruction, agent processes the event (reviews PR, summarizes article, etc.)
6. Token-efficient: only calls Gemini when there's actual work to do; deduped (`processed_ids` 1000→500) + dual persistence (`data/watcher_state.json` locally, Firestore on Cloud Run). Manual restore: `POST /api/watchers/restore` (also on startup)

### Example: Monitor GitHub PRs

```bash
curl -X POST http://localhost:8080/api/watchers \
  -H "Content-Type: application/json" \
  -d '{"type": "github", "config": {"repo": "owner/repo", "interval_seconds": 300}}'
```

---

## Unified Credentials Settings

Manage all API keys and secrets in one place via the Dashboard:

| Category | Fields |
|----------|--------|
| AI & LLM | Gemini API Keys, Model |
| Google Cloud | Project ID, Region |
| Web Search | Google Search API Key, CX |
| GitHub | Personal Access Token |
| GitLab | Token, Base URL |
| Slack | Bot Token |
| Discord | Bot Token |
| Jira | Domain, Email, API Token |
| Email (IMAP) | Server, Address, Password |

Credentials are stored in `.env` (gitignored) and never exposed to the frontend in plain text.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Dashboard (live UI) |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/agent/status` | Agent status (model, tools, memory) |
| `POST` | `/api/tasks` | Submit a new task |
| `GET` | `/api/tasks` | List recent tasks |
| `GET` | `/api/tasks/{id}` | Get task details + trace |
| `GET` | `/api/tasks/live/{id}` | Poll live task updates |
| `DELETE` | `/api/tasks/{id}` | Delete a task |
| `GET` | `/api/approvals` | List pending approvals |
| `POST` | `/api/approvals/{id}` | Approve or deny an action |
| `GET` | `/api/approvals/trusted` | List trusted task IDs (one-approval-per-task diagnostics) |
| `GET` | `/api/tasks/{id}/trust` | Check per-task trust flag |
| `POST` | `/api/tasks/{id}/trust` | Trust/untrust a task (`{"trusted": bool}`) |
| `GET` | `/api/traces` | List all execution traces |
| `GET` | `/api/traces/{task_id}` | Detailed trace chain + summary |
| `GET` | `/api/memory` | Search/list memory entries |
| `POST` | `/api/memory` | Add a memory entry (auto-detects instruction phrasing) |
| `DELETE` | `/api/memory/{id}` | Delete a memory entry |
| `POST` | `/api/memory/delete` | Bulk delete memory entries |
| `POST` | `/api/memory/clear/{category}` | Clear a memory category |
| `POST` | `/api/memory/{id}/feedback` | Rate a memory helpful/unhelpful (trains trust score) |
| `POST` | `/api/memory/query` | Compositional recall: `search` / `probe` (entity-role) / `related` (structural) / `reason` (vector JOIN) — see `docs/capabilities.md` |
| `GET` | `/api/memory/contradictions` | Facts sharing entities but conflicting (score=`overlap*(1-sim)`, ≥0.3) |
| `GET` | `/api/skills` | Procedural skill index with usage telemetry + lifecycle states (stale 30d → archived 90d, pinned exempt, `.archive/`) |
| `POST` | `/api/skills` | Create a skill (full SKILL.md or bare markdown) |
| `GET` | `/api/skills/{name}` | Skill detail: frontmatter, markdown body, usage stats |
| `DELETE` | `/api/skills/{name}` | Archive a skill (recoverable); `?purge=true` hard-deletes |
| `POST` | `/api/skills/{name}/restore` | Restore the newest archived copy |
| `GET` | `/api/skills/ledger` | Audit trail of every skill mutation (sha256-chained) |
| `POST` | `/api/command` | Zero-cost deterministic commands (`/help`, `/start`, `/status`, `/tasks`, `/pending`, `/tools`, `/skills`, `/memory`...) — no LLM call |
| `GET` | `/api/watchers` | List active event watchers |
| `GET` | `/api/watchers/{id}` | Get single watcher status |
| `POST` | `/api/watchers` | Create a new watcher |
| `POST` | `/api/watchers/{id}/start` | Start a stopped watcher |
| `POST` | `/api/watchers/{id}/stop` | Stop a running watcher |
| `DELETE` | `/api/watchers/{id}` | Remove a watcher |
| `POST` | `/api/watchers/restore` | Re-hydrate persisted watchers (also on startup) |
| `POST` | `/api/webhooks` | Generic webhook ingress `{event_type, payload}` → task |
| `GET` | `/api/credentials` | List all API keys and credentials |
| `GET` | `/api/credentials/{key}` | Get single masked credential |
| `POST` | `/api/credentials` | Save credentials to .env |
| `DELETE` | `/api/credentials/{key}` | Remove a credential |
| `GET` | `/api/approval-mode` | Get approval mode + Telegram status |
| `POST` | `/api/approval-mode` | Set approval mode (smart/always/never) |
| `POST` | `/api/telegram/webhook` | Receive Telegram updates |
| `POST` | `/api/telegram/setup` | Set up Telegram webhook |
| `GET` | `/api/telegram/status` | Get Telegram connection status |

### Example: Submit a Task

```bash
curl -X POST http://localhost:8080/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "Search the web for latest AI news and summarize the top 3"}'
```

### Example: Approve a High-Risk Action

```bash
curl -X POST http://localhost:8080/api/approvals/{step_id} \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'
```

---

## Available Tools

### Core Tools (10)

| Tool | Description | Risk Level |
|------|-------------|------------|
| `web_search` | Search the web via Google/DuckDuckGo | Safe |
| `fetch_url` | Fetch and parse web page content | Safe |
| `read_file` | Read file contents | Safe |
| `write_file` | Write content to a file | Safe |
| `list_directory` | List files in a directory | Safe |
| `parse_json` | Extract data from JSON | Safe |
| `summarize_text` | Summarize long text with Gemini | Safe |
| `extract_data` | Extract structured data from text | Safe |
| `execute_code` | Run Python code in sandbox | **Approval Required** |
| `run_command` | Run shell command | **Approval Required** |

### GitHub Skill (8)

| Tool | Description | Risk Level |
|------|-------------|------------|
| `github_resolve_repo` | Resolve owner/name from partial repo references | Safe |
| `github_get_repo` | Fetch repository details | Safe |
| `github_list_prs` | List open pull requests | Safe |
| `github_get_pr` | Get details of a specific PR | Safe |
| `github_review_pr` | Gemini-powered review: merge/reject/skip with confidence | Safe |
| `github_merge_pr` | Merge a pull request | **Approval Required** |
| `github_close_pr` | Close a pull request | **Approval Required** |
| `github_apply_decisions` | Apply review verdicts across PRs | **Approval Required** |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **LLM** | Gemini 3.5 Flash (4-key rotation) |
| **Agent Framework** | Google ADK 2.x |
| **Cloud Run** | Serverless deployment (scales to zero) |
| **Firestore** | Persistent task state + agent memory on Cloud Run; SQLite used locally |
| **Pub/Sub** | Event-driven task routing |
| **API** | FastAPI + Uvicorn |
| **Language** | Python 3.11+ |
| **Testing** | pytest + pytest-asyncio |
| **Watchers** | 11 platforms (GitHub, GitLab, Slack, Discord, Jira, Reddit, HN, Email, RSS, Cron, Webhook) |
| **Approvals** | Smart approval + Telegram bot (remote approve from phone) |

---

## Project Structure

```
nexusmind-ai/
├── agent/                          # Core agent logic
│   ├── core/
│   │   ├── planner.py              # Task decomposition via Gemini
│   │   ├── agent_loop.py           # Adaptive step-by-step execution loop (opencode-style)
│   │   ├── executor.py             # Tool execution + smart approval
│   │   ├── gemini_client.py        # Multi-key Gemini client
│   │   └── memory/                 # Hermes-inspired memory system
│   │       ├── hrr.py              # Holographic Reduced Representations (phase vectors)
│   │       ├── store.py            # SQLite facts + FTS5 + entity resolution + trust
│   │       └── retrieval.py        # Hybrid BM25/Jaccard/HRR retriever
│   │   ├── skill_library.py        # Self-evolving SKILL.md packages (Hermes adaptation)
│   │   ├── command_gate.py         # Zero-cost /command dispatch before the LLM
│   ├── skills/
│   │   ├── web_research/           # Web search + URL fetch
│   │   ├── file_management/        # File read/write/list
│   │   ├── data_processing/        # JSON, summarization, extraction
│   │   └── github/                 # GitHub PR/issue/review tools
│   ├── watchers/                   # Always-awake event monitors
│   │   ├── base.py                 # Abstract watcher with poll loop
│   │   ├── github.py               # GitHub PR/issue watcher
│   │   ├── gitlab.py               # GitLab MR/issue watcher
│   │   ├── slack.py                # Slack channel watcher
│   │   ├── discord.py              # Discord channel watcher
│   │   ├── jira.py                 # Jira issue watcher
│   │   ├── reddit.py               # Reddit subreddit watcher
│   │   ├── hackernews.py           # Hacker News story watcher
│   │   ├── email_watcher.py        # Email inbox watcher
│   │   ├── rss.py                  # RSS/Atom feed watcher
│   │   ├── cron.py                 # Scheduled task watcher
│   │   ├── webhook.py              # Custom webhook watcher
│   │   └── manager.py              # Watcher lifecycle manager
│   ├── orchestrator.py             # Main agent loop + self-improvement
│   ├── observability.py            # Trace collector for dashboard
│   ├── telegram.py                 # Telegram bot (remote approvals)
│   ├── models.py                   # Pydantic data models
│   └── config.py                   # Environment-based settings
├── cloud/                          # Google Cloud integrations
│   ├── vertex_ai/agent.py          # Google ADK agent wrapper
│   ├── firestore/client.py         # Firestore persistence layer
│   ├── pubsub/events.py            # Pub/Sub event routing
│   └── cloud_run/                  # Dockerfile + cloudbuild.yaml
├── api/
│   ├── main.py                     # FastAPI app + background task runner
│   ├── dashboard.html              # Live traceability dashboard
│   ├── watcher_routes.py           # Watcher CRUD API endpoints
│   └── credentials_routes.py       # Credentials management API
├── tests/                          # 250+ passing tests
├── projects/                       # Agent-generated multi-file builds (websites, apps)
├── data/                           # SQLite memory store (gitignored)
├── scripts/                        # Deploy scripts (bash + PowerShell)
├── pyproject.toml                  # Dependencies + tool config
└── README.md
```

---

## Open Source Patterns Used

| Pattern | Inspired By | How We Adapted It |
|---------|-------------|-------------------|
| Multi-step task decomposition | [OpenClaw](https://github.com/openclaw/openclaw) | Gemini-powered planner with JSON step extraction |
| Tool registry + sandboxed execution | OpenClaw | `@register_tool` decorator + subprocess isolation |
| Persistent cross-session memory | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | SQLite + FTS5 store, hybrid BM25/Jaccard/HRR retrieval, trust scoring |
| Self-evolving skill library | Hermes | Auto-synthesized SKILL.md packages from solved tasks, usage telemetry, stale/archive lifecycle, audit ledger |
| Zero-cost command gate | Hermes + OpenClaw | `/commands` resolved deterministically before the LLM (Telegram + REST); unknown input falls through to the agent |
| Hallucinated-tool repair ladder | Hermes | Planned tool names normalized/alias/fuzzy-matched against the live registry; one corrective re-plan with the valid-tool catalog |
| Plan salvage + JSON repair | Hermes + OpenClaw | Truncated plans recovered step-by-step; **zero templates** — every site/app is authored by Gemini from the user's command + recalled memory, with no hardcoded scaffolds or stock imagery |
| Self-improvement reflection | Hermes | Post-task Gemini reflection saves learnings |
| Event-driven scheduling | Hermes | Cloud Pub/Sub replaces in-process cron |
| Adaptive step-by-step loop | Custom (opencode-style) | The agent decides one action at a time; each real result or error feeds the next decision — self-correcting, verifying files before done, kept working up to a generous budget |
| Human-in-the-loop approval | Custom | Smart approval gate with Telegram bot for remote control |
| Transparent traceability | Custom | In-memory trace collector -> live dashboard |
| Multi-key rotation | Custom | Round-robin with rate-limit backoff |

---

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=agent --cov-report=term-missing
```

All **250+ tests** covering models, memory (hybrid retrieval, HRR, trust scoring, hygiene/contradictions), the self-evolving skill library (validation gates, lifecycle, matching, ledger), deterministic routing (command gate, tool-name repair ladder), the **adaptive step-by-step agent loop** (result feedback, self-correction, verification before done, failure guards, budget bounds, project collision guard, live TODO checklist), executor (smart approval, one-approval-per-task trust), orchestrator, API endpoints, watchers, and the ADK integration (agent creation, callbacks, Runner path, API routing).

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Gemini API key(s), comma-separated for rotation |
| `GEMINI_MODEL` | Yes | Model name (default: `gemini-3.5-flash`) |
| `GEMINI_FULL_CONTROL` | No | `true` (default) — Gemini controls tool/file naming & memory policy; heuristics are fallback |
| `DATABASE_BACKEND` | No | `sqlite` (local) or `firestore` (Cloud Run) |
| `GITHUB_DEFAULT_REPO` | No | Default `owner/repo` when goal says "my repository" |
| `GOOGLE_CLOUD_PROJECT` | For cloud | GCP project ID |
| `GOOGLE_CLOUD_REGION` | For cloud | GCP region (default: `us-central1`) |
| `GOOGLE_SEARCH_API_KEY` | No | Google Custom Search API key |
| `GOOGLE_SEARCH_CX` | No | Google Custom Search engine ID |
| `APPROVAL_MODE` | No | `smart` (default), `always`, or `never` (aliases: `ask_everytime`→`always`) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | No | Your Telegram chat ID from @userinfobot |
| `TELEGRAM_APPROVAL_TIMEOUT` | No | Approval timeout seconds (default: `300`) |
| `GITHUB_TOKEN` | No | GitHub Personal Access Token |
| `GITLAB_TOKEN` | No | GitLab Personal Access Token |
| `GITLAB_BASE_URL` | No | GitLab self-hosted base URL (default: `https://gitlab.com`) |
| `SLACK_BOT_TOKEN` | No | Slack Bot Token (`xoxb-...`) |
| `DISCORD_BOT_TOKEN` | No | Discord Bot Token |
| `JIRA_DOMAIN` | No | Jira domain (e.g., `company.atlassian.net`) |
| `JIRA_EMAIL` | No | Jira account email |
| `JIRA_TOKEN` | No | Jira API token |
| `EMAIL_IMAP_SERVER` | No | IMAP server (e.g., `imap.gmail.com`) |
| `EMAIL_ADDRESS` | No | Email address |
| `EMAIL_PASSWORD` | No | App password for email |
| `AGENT_MAX_STEPS` | No | Max steps per task (default: `20`, loop budget `40`) |
| `AGENT_TIMEOUT_SECONDS` | No | Step timeout seconds (default: `300`) |
| `AGENT_MEMORY_MAX_ITEMS` | No | Memory cap (default: `1000`) |
| `WATCHER_DEFAULT_INTERVAL` | No | Watcher poll seconds (default: `300`) |
| `WATCHER_MAX_CONCURRENT` | No | Max concurrent watchers (default: `10`) |
| `API_HOST` | No | API host (default: `0.0.0.0`) |
| `API_PORT` | No | API port (default: `8080`) |
| `ENVIRONMENT` | No | `development` or `production` |

---

## License

[MIT](https://opensource.org/licenses/MIT)

---

<div align="center">

**Built for the [Google All Things Agentic Hackathon](https://allthingsagentic.devpost.com) -- August 2026**

Patterns adapted from [OpenClaw](https://github.com/openclaw/openclaw) and [Hermes Agent](https://github.com/NousResearch/hermes-agent)

Made with ❤️ by Tamim Hasan

<!-- demo marker -->

</div>
