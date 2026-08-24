<div align="center">

<div style="display: inline-block; background: rgba(0,0,0,0.7); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border-radius: 20px; padding: 24px 48px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 8px 32px rgba(0,0,0,0.4);">
<img src="docs/nexusmind-dark-full.png" alt="NexusMind AI" width="400" style="border-radius: 12px; filter: drop-shadow(0 0 20px rgba(66,133,244,0.3));" />
</div>

### Autonomous Task-Execution Agent on Google Cloud

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg?style=flat&logo=python&logoColor=white)](https://python.org)
[![Gemini](https://img.shields.io/badge/Gemini-3.6%20Flash-4285F4?style=flat&logo=google&logoColor=white)](https://ai.google.dev)
[![Google Cloud](https://img.shields.io/badge/Google%20Cloud-Run%20%7C%20Firestore%20%7C%20Pub%2FSub-4285F4?style=flat&logo=googlecloud&logoColor=white)](https://cloud.google.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-21%20passing-brightgreen)](#testing)

**Built for the [Google "All Things Agentic" Hackathon](https://allthingsagentic.devpost.com) -- Track: The Taskmaster**

</div>

---

## Overview

Most AI today waits for you to ask. **NexusMind AI doesn't.**

It's an autonomous agent that receives goals -- via API, webhooks, or a live dashboard -- and handles multi-step workflows end-to-end without hand-holding. It plans, executes, self-corrects on failure, asks permission for risky actions, and learns from every task. **Run it once, walk away -- it works autonomously for days/weeks.**

> **New to coding?** Check out our [Non-Coder User Guide](docs/user_guide.md) -- use NexusMind AI without writing any code.

---

## Architecture

```
                        ┌──────────────────────────────┐
                        │      Dashboard (Live UI)      │
                        │   Reasoning chain + approvals  │
                        └──────────────┬───────────────┘
                                       │
┌──────────────┐     ┌─────────────────▼─────────────────┐     ┌──────────────┐
│   GitHub      │────>│       REST API (FastAPI)          │<────│   Stripe      │
│   Webhooks    │     │    POST /api/webhooks             │     │   Webhooks    │
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
                        │  │  Gemini 3.6 Planner    │──┼-- Step decomposition
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
     │    Firestore     │     │    Vertex AI     │     │    Cloud Run    │
     │  (State/Memory)  │     │  (Gemini LLM)   │     │  (Deployment)   │
     └─────────────────┘     └─────────────────┘     └─────────────────┘
```

---

## The Agent Loop

```
   Goal --> Plan (Gemini) --> Execute Tool --> Success? --Yes--> Next Step
               ^                                  |
               |                                  No
               |                                  v
               +---- Self-Correct (Gemini) <-- Analyze Error
                                                 |
                                        Smart Approval Check
                                        |                  |
                                   Dangerous           Safe command
                                        |                  |
                                  Telegram Bot        Auto-approve
                                  (or Dashboard)
                                        |
                                   Approve / Deny
                                        |
                                   Continue / Stop
```

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

### Dashboard Settings

- **Settings page** — Select approval mode (Smart/Always/Never)
- **Credentials page** — Add Telegram bot token + chat ID
- **Approvals page** — View pending approvals (dashboard fallback)

---

## Always-Awake Event-Driven Watchers

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
3. When new events are detected, agent is triggered automatically
4. Agent processes the event (reviews PR, summarizes article, etc.)
5. Token-efficient: only calls Gemini when there's actual work to do

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
| `GET` | `/api/memory` | Search/list memory entries |
| `GET` | `/api/watchers` | List active event watchers |
| `POST` | `/api/watchers` | Create a new watcher |
| `POST` | `/api/watchers/{id}/start` | Start a stopped watcher |
| `POST` | `/api/watchers/{id}/stop` | Stop a running watcher |
| `DELETE` | `/api/watchers/{id}` | Remove a watcher |
| `GET` | `/api/credentials` | List all API keys and credentials |
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

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **LLM** | Gemini 3.6 Flash (4-key rotation) |
| **Agent Framework** | Google ADK 2.x |
| **Cloud Run** | Serverless deployment (scales to zero) |
| **Firestore** | Persistent task state + agent memory |
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
│   │   ├── executor.py             # Tool execution + smart approval
│   │   ├── gemini_client.py        # Multi-key Gemini client
│   │   └── memory.py               # Hermes-inspired memory store
│   ├── skills/
│   │   ├── web_research/           # Web search + URL fetch
│   │   ├── file_management/        # File read/write/list
│   │   └── data_processing/        # JSON, summarization, extraction
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
├── tests/                          # 21 passing tests
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
| Persistent cross-session memory | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | Firestore-backed store with category filtering |
| Self-improvement reflection | Hermes | Post-task Gemini reflection saves learnings |
| Event-driven scheduling | Hermes | Cloud Pub/Sub replaces in-process cron |
| Memory lifecycle (active -> stale) | Hermes | Skill states with automatic transitions |
| Self-correcting retry loops | Custom | Error analysis -> Gemini -> parameter adjustment -> retry |
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

All **21 tests** covering models, memory, executor, orchestrator, and API endpoints.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Gemini API key(s), comma-separated for rotation |
| `GEMINI_MODEL` | Yes | Model name (default: `gemini-3.6-flash`) |
| `GOOGLE_CLOUD_PROJECT` | For cloud | GCP project ID |
| `GOOGLE_CLOUD_REGION` | For cloud | GCP region (default: `us-central1`) |
| `GOOGLE_SEARCH_API_KEY` | No | Google Custom Search API key |
| `GOOGLE_SEARCH_CX` | No | Google Custom Search engine ID |
| `APPROVAL_MODE` | No | `smart` (default), `always`, or `never` |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | No | Your Telegram chat ID from @userinfobot |
| `GITHUB_TOKEN` | No | GitHub Personal Access Token |
| `GITLAB_TOKEN` | No | GitLab Personal Access Token |
| `SLACK_BOT_TOKEN` | No | Slack Bot Token (`xoxb-...`) |
| `DISCORD_BOT_TOKEN` | No | Discord Bot Token |
| `JIRA_DOMAIN` | No | Jira domain (e.g., `company.atlassian.net`) |
| `JIRA_EMAIL` | No | Jira account email |
| `JIRA_TOKEN` | No | Jira API token |
| `EMAIL_IMAP_SERVER` | No | IMAP server (e.g., `imap.gmail.com`) |
| `EMAIL_ADDRESS` | No | Email address |
| `EMAIL_PASSWORD` | No | App password for email |
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

</div>
