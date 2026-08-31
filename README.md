<div align="center">

<img src="docs/assets/icons/app-icon-192.png" alt="NexusMind AI" width="140" style="border-radius: 28px; box-shadow: 0 8px 32px rgba(0,0,0,0.3);" />

# NexusMind AI
### Autonomous Task-Execution Agent on Google Cloud

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg?style=flat&logo=python&logoColor=white)](https://python.org)
[![Gemini](https://img.shields.io/badge/Gemini-3.5%20Flash-4285F4?style=flat&logo=google&logoColor=white)](https://ai.google.dev)
[![Google Cloud](https://img.shields.io/badge/Google%20Cloud-Run%20%7C%20Firestore%20%7C%20Pub%2FSub-4285F4?style=flat&logo=googlecloud&logoColor=white)](https://cloud.google.com)
[![Google ADK](https://img.shields.io/badge/Google%20ADK-2.x-4285F4?style=flat&logo=google&logoColor=white)](https://cloud.google.com/vertex-ai/generative-ai/docs/agent-development-kit)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Demo Video](https://img.shields.io/badge/🎬_Demo_Video-Watch_on_YouTube-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/watch?v=woSOCuzfabg)

**Built for the [Google "All Things Agentic" Hackathon](https://allthingsagentic.devpost.com) — Track: The Taskmaster**

</div>

<div align="center">

### Demo Video — See NexusMind AI in Action (4 min)

[![Watch Demo on YouTube](https://img.youtube.com/vi/woSOCuzfabg/maxresdefault.jpg)](https://www.youtube.com/watch?v=woSOCuzfabg)

**[WATCH DEMO ON YOUTUBE — 4 MIN](https://www.youtube.com/watch?v=woSOCuzfabg)** &nbsp;|&nbsp; *Autonomous PR reviews, multi-step builds, Telegram approvals and self-learning — all in one take*

</div>

---

## What NexusMind Is (30-Second Pitch for Judges)

NexusMind AI is an **autonomous, event-driven task-execution agent** — not a chatbot, not an API wrapper, not a tool collection.

| You give it | It does | Without you |
|---|---|---|
| A goal in plain English (`"review my open PRs and merge the safe ones"`) | Plans, executes step-by-step, self-corrects on errors, verifies artifacts, asks for approval only when risky | Continuously — via watchers on GitHub, Slack, Jira, Email, RSS and 6 more platforms |
| A standing instruction (`"whenever a PR arrives, test and merge or decline with a comment"`) | Saves it as durable memory, enforces **memory-gated autonomy** on every future event | 24/7, token-efficient (only calls Gemini when there is real work) |
| Nothing — an external event fires | Detects the event, checks for a matching instruction, runs the full workflow, reflects and improves | Learns from every task; distilled lessons steer future planning |

**Core differentiators:** adaptive step-by-step execution with live streaming, persistent hybrid memory (BM25 + HRR vectors + trust scoring), self-evolving skills, human-in-the-loop safety via Telegram, and a fully observable trace of every decision.

> **Judge checklist:** Gemini 3.5 Flash primary + ADK + Cloud Run + Firestore + Pub/Sub — all mandatory hackathon requirements are implemented and exercised in the demo. See [Tech Stack](#tech-stack) and [Architecture](#architecture).

---

## The Problem

**Context switching is killing developer productivity.**

| Daily task | Time | Tool |
|---|---|---|
| Review PRs — open diff, check CI/tests, write review, merge/reject | ~45-60 min | GitHub |
| Scan Slack for mentions and decisions | ~30 min | Slack |
| Review Jira blockers | ~15 min | Jira |
| Monitor Reddit / Hacker News | ~10 min | Reddit / HN |
| Triage email | ~20 min | Email |

**~2-2.5 hours/day per developer** on reading and reacting — not building. For a 10-person team, that is **20+ hours/week** of low-judgment, high-volume attention work that does not require a human.

## The Solution

**One agent that watches everything and acts automatically.** NexusMind monitors your connected platforms simultaneously. When something needs attention it handles it — reviews the PR, summarizes the thread, triages the issue — and escalates only when judgment genuinely matters.

> Run it once, walk away — it continuously monitors events and executes workflows without repeated prompting.

---

## Key Capabilities

Every item below is implemented and tested (`285 tests`). Counts are verified against the codebase.

| # | Capability | What it does | Where |
|---|---|---|---|
| 1 | **Adaptive step-by-step execution** | `Decide → Execute → Observe` loop: Gemini picks ONE action, the real result (including errors) drives the next decision. Self-corrects, verifies files before declaring done. | `agent/core/agent_loop.py:530` |
| 2 | **Complexity Router** | Tier1 trivial → single Gemini call; Tier2 single-tool → direct execution; Tier3 heavy → full planner + elastic loop. Normal tasks ~0.6s instead of ~2.5s. | `agent/orchestrator.py` |
| 3 | **Elastic budget + failure guards** | 40 steps → auto-extends by 20 while making progress, hard ceiling 120; abort after 3 consecutive failures; 20k chars per `write_file`; bounded transcript (25 entries / 12k chars). | `agent/core/agent_loop.py:50` |
| 4 | **Compaction @ 90** | Gemini summarizes the transcript every 90 steps so long builds never overflow context. | `agent/core/agent_loop.py:865` |
| 5 | **Project collision guard** | `projects/<name>/` that existed before the task is blocked for `write_file` until proven intent via `read_file`/`list_directory`. | `agent/core/agent_loop.py:445` |
| 6 | **Live streaming + global event bus** | Gemini `token` deltas + tool stdout `tool_delta` (4 KiB chunks) + `todo_update` stream via `WebSocket /api/ws`, `SSE /api/tasks/live/{id}/stream`, poll fallback, and `GET /api/events/live`. Dashboard Thinking/Checklist tabs update in real time. | `agent/core/gemini_client.py:660`, `api/main.py:195` |
| 7 | **`todowrite` + `task` subagents** | `todowrite` overwrites the full checklist (`content`, `status`, `priority high/medium/low`, 30 cap); `task` delegates to `explore` (read-only) / `general` (8-step loop) subagents. | `agent/core/executor.py:753`, `agent/models.py:86` |
| 8 | **Smart approval + Telegram** | 3 modes: `smart` (default, only dangerous ops need approval), `always`, `never`. Side-effect regex + 22 dangerous patterns + `is_dangerous_code` detection. Telegram inline Approve/Deny; per-task trust (one approval covers remaining risky steps in that task). | `agent/core/executor.py:106`, `agent/telegram.py` |
| 9 | **In-task steering** | `POST /api/tasks/{id}/steer` queues follow-up instructions for a live task or spawns a linked task when already terminal. | `agent/core/steering.py`, `api/main.py` |
| 10 | **11 event-driven watchers** | GitHub, GitLab, Slack, Discord, Jira, Reddit, Hacker News, Email (IMAP), RSS/Atom, Cron, Webhook. All share a poll loop, dedup (`processed_ids` 1000→500), dual persistence (`data/watcher_state.json` + Firestore), restore on startup. | `agent/watchers/` |
| 11 | **Write-back actions** | GitHub merge/close/review, GitLab merge, Slack send/thread reply, Discord send, Jira comment/transition, Email send — all via `httpx` REST (GitHub API as reference). Reddit/HN/RSS are detect-only; Cron/Webhook are pre-authorized triggers. | `agent/skills/` |
| 12 | **Memory-gated autonomy** | A watcher only acts when a matching **standing instruction** exists in memory (per-watcher keyword scope, most-recent-wins). No match → `needs_instruction` task + hint, Telegram at most once per 6h per watcher. Cron/Webhook skip the gate (owner-authored). | `agent/watchers/base.py:40` |
| 13 | **Persistent hybrid memory** | SQLite + FTS5 + triggers; HRR phase-vector retrieval; hybrid BM25 (0.4) + Jaccard (0.3) + HRR (0.3) reranking; trust weighting (`+0.05` helpful / `-0.10` unhelpful) + temporal decay. Fenced `<memory-context>` injection, sanitized, trivial prompts skip recall. | `agent/core/memory/` |
| 14 | **Compositional recall** | `POST /api/memory/query` — `search` / `probe` (entity-role) / `related` (structural) / `reason` (vector JOIN). | `agent/core/memory/retrieval.py:170` |
| 15 | **Contradiction detection** | Flags facts sharing entities but conflicting (`score=overlap*(1-sim) ≥ 0.3`). `GET /api/memory/contradictions`. | `agent/core/memory/retrieval.py:299` |
| 16 | **Self-evolving skills** | Solved tasks (≥2 steps, ≥2 tools) auto-synthesize `SKILL.md` packages; `stale` at 30d → archived at 90d (pinned exempt, `.archive/`); usage telemetry; sha256-chained audit ledger. | `agent/core/skill_library.py` |
| 17 | **Zero-cost command gate** | `/help`, `/start`, `/status`, `/tasks`, `/pending`, `/tools`, `/skills`, `/memory` answered deterministically with zero LLM calls; path-safe (`/Users/x/file.md` is a task, not a command). | `agent/core/command_gate.py` |
| 18 | **Tool-name repair ladder** | Hallucinated tool names → normalize → strip `_tool` → alias map (`search`→`web_search`) → fuzzy ≥0.7; one corrective re-plan with valid-tool catalog. | `agent/core/executor.py` |
| 19 | **Sandboxed execution** | `execute_code` / `run_command` run isolated, stream 4 KiB `tool_delta` chunks, guarded by smart approval. | `agent/core/executor.py:908` |
| 20 | **Dual storage backend** | `DATABASE_BACKEND=sqlite` locally, `firestore` on Cloud Run (tasks, memory, skills, watcher state). Auto-selected. | `agent/core/memory/store.py`, `cloud/firestore/client.py` |
| 21 | **Google ADK integration** | ADK `Agent` + `Runner` with `before_agent` (memory prefetch), `before_tool` (approval gate), `after_agent` (reflection) callbacks; `run_task_via_adk` with orchestrator fallback. | `cloud/vertex_ai/agent.py` |
| 22 | **Pub/Sub event routing** | `publish_task_event` / `subscribe_to_tasks` on `nexusmind-tasks` topic; webhook ingress `POST /api/webhooks`. | `cloud/pubsub/events.py` |
| 23 | **Credential management** | Single Credentials page (9 categories, API + dashboard); Gemini key rotation (dashboard `POST /api/gemini-keys`, `GEMINI_ACTIVE_KEY_INDEX`); masked reads, `.env` as single source of truth. | `api/credentials_routes.py`, `api/gemini_keys_routes.py` |
| 24 | **Observability** | `TraceSpan` (reasoning/tool_call/approval/error) with durations; dashboard Timeline/Thinking/Approvals/Checklist tabs; `GET /api/traces`. | `agent/observability.py` |
| 25 | **Testing** | 285 tests covering models, memory (HRR/trust/hygiene), skills (lifecycle/ledger), routing, adaptive loop (elastic/compaction/collision/streaming), executor, orchestrator, API (WS/SSE), watchers, ADK, and all skills. | `tests/` |

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
                        │  │  Complexity Router     │──┼-- Tier1/2 fast-path vs Tier3 heavy
                        │  │  Gemini 3.5 Planner    │──┼-- Step decomposition (lazy, Tier3 only)
                        │  │  Adaptive Step Loop    │──┼-- Decide -> Execute -> Observe (+streaming)
                        │  │  Tool Executor         │──┼-- Sandboxed + todowrite/task + streaming
                        │  │  Self-Correction       │──┼-- Error -> Gemini -> Retry
                        │  │  Approval Gate         │──┼-- Human-in-the-loop
                        │  │  Memory System         │──┼-- Persistent context
                        │  │  Trace Collector       │──┼-- Reasoning chain + compaction@90
                        │  │  Self-Improvement      │──┼-- Post-task reflection
                        │  └────────────────────────┘  │
                        └──────────────┬──────────────┘
                                       │
               ┌────────────────────────┼────────────────────────┐
               │                        │                        │
      ┌────────▼────────┐     ┌────────▼────────┐     ┌────────▼────────┐
      │    Firestore     │     │   Gemini API     │     │    Cloud Run    │
      │  (State/Memory)  │     │  (Flash stream)  │     │  (Deployment)   │
      └─────────────────┘     └─────────────────┘     └─────────────────┘
               │                        │
               └────────┬───────────────┘
                        ▼
               ┌─────────────────┐
               │ Global Event Bus│  WS /api/ws + SSE + poll for every work
               │ token/tool_delta│  todo_update, watcher/memory/skill fans
               └─────────────────┘
```

---

## The Agent Loop

NexusMind does not run a goal as a one-shot script. It works **step by step, like a programmer in an IDE**: it decides ONE action, executes it, and feeds the real outcome — including errors — back into the next decision.

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
     DONE: summary + saved file locations
```

In practice the agent creates a folder, writes `styles.css`, writes `main.js`, then writes `index.html` — resolving each step's success (or error) before the next. If a step fails (missing module, approval timeout, bad result), the error lands in the transcript and the next decision fixes it: install the dependency, switch from `execute_code` to `write_file`, verify with `list_directory`/`read_file` — then declare done. Steps appear **live in the dashboard** as they happen.

> **Budgets & guardrails:** 40 → elastic 120 (`+20` when `pending>0` & `recent_ok>0`), abort after 3 consecutive failures, 20k chars per `write_file`, bounded transcript (last 25 entries / 12k chars) + collapsed `EARLIER PROGRESS` + **compaction every 90 steps** (Gemini summary). Live TODO via `todowrite` full-overwrite (priority `high/medium/low`, 30 cap) + legacy `todo_updates` compat. **Project collision guard** blocks `write_file` into a pre-existing `projects/<name>/` until intent is proven with `read_file`/`list_directory`. **Streaming:** LLM `token` + tool `tool_delta` (4 KiB stdout chunks) + global `WebSocket /api/ws`.

### Example: Autonomous GitHub PR Workflow

The strongest demonstrated workflow — a watcher detects a new PR and handles the full review cycle without human intervention:

```
New PR detected (watcher)
      |
Resolve repository → Find open PRs → Analyze with Gemini (code review, risk assessment)
      |
Generate review decisions (merge / reject / skip)
      |
Risky action?
   /          \
 No            Yes
 |              |
Execute       Request approval (Telegram / Dashboard) → Approve / Deny
 |              |
Store task outcome + reflect into memory
```

Merges and closes require human approval. Every review is logged with full reasoning traces in the dashboard. Setup: one API call to create a watcher on your repo.

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
# Edit .env — add your Gemini API key(s), comma-separated for rotation

# Run the API + dashboard
python -m api.main
# Open http://localhost:8080
```

### Cloud Shell (No GCP Account Needed)

```bash
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

### 3 Approval Modes

| Mode | Behavior | Best for |
|------|----------|----------|
| **Smart** (default) | Auto-approve safe commands (`ls`, `cat`, `git status`), ask only for dangerous ones | Most users |
| **Always** | Ask for every high-risk tool | Maximum safety |
| **Never** | Auto-approve everything | Trusted environments |

**Auto-approved (safe):** `ls`, `cat`, `head`, `grep`, `find`, `pwd`, `git status/log/diff`, `pip list/show`, `python -c "print(...)"` (read-only).

**Always asks (dangerous):** `rm -rf`, `del /f`, `sudo`, `chmod 777`, `eval`/`exec`, `pipe to bash`, `deploy`, `transfer_funds`, plus `is_dangerous_code` (`os.system`, `subprocess`, `shutil.rmtree`, `eval`/`exec`). Side-effect regex (`; | \` > && || $(`) and pathlib scaffold to `projects/`/`output/` are auto-approved when safe.

### Telegram Bot Setup (2 minutes)

1. Open Telegram → `@BotFather` → `/newbot` → copy the **bot token**
2. `@userinfobot` → copy your **Chat ID**
3. Dashboard → **Credentials** → paste both → **Save All**

```
🔐 Approval Required

Task: Deploy to production
Tool: run_command
Command: ./deploy.sh --prod

[Approve] [Deny]
```

**One-approval-per-task trust:** once you approve one risky step in a task, remaining risky steps in that same task auto-approve. Trust is per-task and cleared on completion. Diagnostics: `GET /api/approvals/trusted`, `GET /api/tasks/{id}/trust`, `POST /api/tasks/{id}/trust`.

Dashboard settings: **Settings** (approval mode), **Credentials** (Telegram token + chat ID), **Approvals** (pending list / dashboard fallback).

---

## Automated Event-Driven Watchers

### Supported Platforms

| Platform | What it monitors | Write-back actions |
|----------|-----------------|-------------------|
| **GitHub** | New PRs, issues | review, merge, close, comment (`github_*` 9 tools) |
| **GitLab** | New merge requests, issues | list / get / merge (`gitlab_*` 3 tools) |
| **Slack** | Channel messages, mentions | send message, reply in thread (`slack_*` 2 tools) |
| **Discord** | Channel messages | send message (`discord_*` 1 tool) |
| **Jira** | New/updated issues | comment, transition (`jira_*` 2 tools) |
| **Reddit** | New posts in subreddits | detect + summarize (read-only) |
| **Hacker News** | New stories, comments | detect + summarize (read-only) |
| **Email (IMAP)** | Inbox messages | send email (`send_email`) |
| **RSS/Atom** | Feed items | detect + summarize (read-only) |
| **Cron** | Scheduled tasks | trigger autonomous task (pre-authorized) |
| **Custom Webhook** | Any HTTP event | trigger autonomous task (pre-authorized) |

### How It Works

1. Create a watcher via Dashboard or API.
2. Watcher polls the platform every N minutes (configurable).
3. On new events, checks **memory-gated autonomy**: needs a matching standing instruction (per-watcher keywords, most-recent-wins substring match).
4. **No match?** Event is not silently dropped — creates a `needs_instruction` task + hint, Telegram at most once per 6h per watcher. **Cron & Webhook** are pre-authorized (owner-authored goals skip the gate).
5. With a matching instruction, the agent processes the event (reviews PR, summarizes article, etc.).
6. Token-efficient: only calls Gemini when there is actual work; deduped (`processed_ids` 1000→500) + dual persistence (`data/watcher_state.json` locally, Firestore on Cloud Run). Manual restore: `POST /api/watchers/restore` (also on startup).

```bash
curl -X POST http://localhost:8080/api/watchers \
  -H "Content-Type: application/json" \
  -d '{"type": "github", "config": {"repo": "owner/repo", "interval_seconds": 300}}'
```

---

## Persistent Memory

Hermes-inspired, reimplemented for Gemini/SQLite/Firestore:

- **Store:** SQLite `facts` + FTS5 virtual table + triggers; `entities` + `fact_entities`; `hrr_vector BLOB` (deterministic SHA-256 phase vectors, `HRR1` float32 prefix). Firestore mirror on Cloud Run.
- **Retrieval:** BM25 candidates → Jaccard + **HRR holographic** rerank → `trust_score * relevance` → temporal decay `0.5^(age/half_life)`. Degraded `0.6/0.4/0.0` without numpy.
- **Trust scoring:** `+0.05` helpful / `-0.10` unhelpful per `POST /api/memory/{id}/feedback`; weighted at retrieval; cap 500 entries (evicts lowest trust, `instruction` protected); dedup on `entry_uid|content+category`.
- **Compositional recall:** `POST /api/memory/query` — `search` / `probe` (unbind role·entity) / `related` (structural) / `reason` (vector JOIN).
- **Contradiction detection:** `GET /api/memory/contradictions` — `score=overlap*(1-sim) ≥ 0.3`.
- **Safety:** fenced `<memory-context>` ("NOT new user input"), trivial-prompt gate (`hi`/`thanks` skip), `task_outcome` excluded from prefetch, auto-extraction patterns for `user_pref`/`project` only.
- **CRUD:** `GET/POST /api/memory`, `DELETE /api/memory/{id}`, `POST /api/memory/delete` (bulk), `POST /api/memory/clear/{category}`.

---

## Self-Evolving Skills

- **Format:** `data/skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description`, `version`, `author`, `created_by`, `origin_task`).
- **Auto-synthesis:** task with ≥2 successful steps across ≥2 distinct tools → Gemini distills a reusable skill (similarity dedup 0.3 blocks near-duplicates; failures never break the task).
- **Steers planning:** every plan receives a fenced `<available-skills>` index; best lexical match (Jaccard 0.12 + stemming) auto-injects its procedure body.
- **Lifecycle:** `active` → `stale` (30d idle) → `.archive/` (90d idle, timestamp suffix); pinned exempt; `.usage.json` sidecar (`use_count`, `last_used_at`).
- **Audit:** every mutation appended to `.ledger.jsonl` (sha256 before/after); `GET /api/skills/ledger`.
- **Validation:** name `^[a-z0-9][a-z0-9._-]*$` ≤64 chars, description ≤1024 (agent ≤60 trigger-first), content ≤100k.
- **API:** `GET /api/skills`, `POST /api/skills`, `GET /api/skills/{name}`, `DELETE /api/skills/{name}` (archive; `?purge=true` hard-deletes), `POST /api/skills/{name}/restore`.

Skills also include **in-task delegation** via the `task` tool (`explore` read-only / `general` 8-step loop).

---

## Available Tools — 30 Total

9 skill packages + 2 opencode-parity utilities; all skill tools use `httpx` REST patterns (GitHub API as reference). `bitbucket`/`linear`/`trello` directories exist as placeholders with no tools registered.

### Core Tools (12 — file/web/data + executor parity)

| Tool | Description | Risk |
|------|-------------|------|
| `web_search` | Search the web (Google Custom Search primary, DuckDuckGo fallback) | Safe |
| `fetch_url` | Fetch and parse web page content | Safe |
| `read_file` | Read file contents | Safe |
| `write_file` | Write content to a file | Safe |
| `list_directory` | List files in a directory | Safe |
| `parse_json` | Extract data from JSON | Safe |
| `summarize_text` | Summarize long text with Gemini | Safe |
| `extract_data` | Extract structured data from text | Safe |
| `execute_code` | Run Python code in sandbox (streams `tool_delta` 4 KiB chunks) | **Approval Required** |
| `run_command` | Run shell command (streams `tool_delta`) | **Approval Required** |
| `todowrite` | Overwrite live TODO checklist (full `todos[]` with `content`/`title`, `status`, `priority`) | Safe |
| `task` | Delegate to subagent `explore` (read-only) / `general` (full 8-step loop) | Safe |

### GitHub Skill (9)

| Tool | Description | Risk |
|------|-------------|------|
| `github_resolve_repo` | Resolve owner/name from partial references | Safe |
| `github_get_repo` | Fetch repository details | Safe |
| `github_list_prs` | List open pull requests | Safe |
| `github_get_pr` | Get details of a specific PR | Safe |
| `github_review_pr` | Gemini-powered review: merge/reject/skip with confidence | Safe |
| `github_verify_pr_locally` | Checkout PR branch and run tests locally before merge decision | Safe |
| `github_merge_pr` | Merge a pull request | **Approval Required** |
| `github_close_pr` | Close a pull request | **Approval Required** |
| `github_apply_decisions` | Apply review verdicts across PRs (sequential, locally verified) | **Approval Required** |

### Slack Skill (2) — `httpx` Slack Web API

| Tool | Description | Risk |
|------|-------------|------|
| `slack_send_message` | Send a message to a Slack channel (`chat.postMessage`) | **Approval Required** |
| `slack_reply_thread` | Reply in a Slack thread (`chat.postMessage` + `thread_ts`) | **Approval Required** |

### Discord Skill (1) — `httpx` Discord REST

| Tool | Description | Risk |
|------|-------------|------|
| `discord_send_message` | Send a message to a Discord channel | **Approval Required** |

### Jira Skill (2) — `httpx` Jira Cloud REST (`/rest/api/3`)

| Tool | Description | Risk |
|------|-------------|------|
| `jira_comment_issue` | Add a comment to a Jira issue | **Approval Required** |
| `jira_transition_issue` | Transition a Jira issue to a new status | **Approval Required** |

### GitLab Skill (3) — `httpx` GitLab REST (`/api/v4`)

| Tool | Description | Risk |
|------|-------------|------|
| `gitlab_list_mrs` | List open merge requests for a project | Safe |
| `gitlab_get_mr` | Get details of a specific merge request | Safe |
| `gitlab_merge_mr` | Merge a merge request | **Approval Required** |

### Email Skill (1)

| Tool | Description | Risk |
|------|-------------|------|
| `send_email` | Send an email via SMTP (draft fallback to `output/email_drafts/` if not configured) | **Approval Required** |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **LLM** | Gemini 3.5 Flash (`gemini-3.5-flash`, `generate_content_stream` token deltas); fallback chain `gemini-3.5-flash-lite, gemini-2.5-flash, gemini-2.5-flash-lite, gemini-1.5-flash` + `gemini-3.5-pro` on truncation |
| **Agent Framework** | Google ADK 2.x (`Agent` + `Runner` + `FunctionTool` + `InMemorySessionService`) |
| **Cloud Run** | Serverless deployment, scales to zero (`cloud/cloud_run/Dockerfile`, `cloudbuild.yaml`) |
| **Firestore** | Persistent tasks + memory + skills + watcher state on Cloud Run; **SQLite locally** |
| **Pub/Sub** | Event-driven task routing (`nexusmind-tasks` topic) + webhook ingress |
| **API** | FastAPI + Uvicorn + WebSocket global bus (`/api/ws` replay 30, queue 200) |
| **Language** | Python 3.11+ |
| **Testing** | pytest + pytest-asyncio + pytest-xdist + pytest-cov (285 tests) |
| **Tools** | 30 tools (12 core + 18 skill: GitHub 9, Slack 2, Discord 1, Jira 2, GitLab 3, Email 1) |
| **Watchers** | 11 platforms with write-back |
| **Approvals** | Smart gate + Telegram bot + per-task trust |

---

## Project Structure

```
nexusmind-ai/
├── agent/                          # Core agent logic
│   ├── core/
│   │   ├── planner.py              # Task decomposition via Gemini (lazy, Tier3 only)
│   │   ├── agent_loop.py           # Adaptive step-by-step loop (40→120 elastic, compaction@90)
│   │   ├── executor.py             # Tool execution + smart approval + todowrite/task
│   │   ├── gemini_client.py        # Multi-key Gemini client (streaming, fallback chain)
│   │   ├── memory/                 # Persistent memory
│   │   │   ├── hrr.py              # Holographic Reduced Representations (phase vectors)
│   │   │   ├── store.py            # SQLite facts + FTS5 + entity resolution + trust
│   │   │   └── retrieval.py        # Hybrid BM25/Jaccard/HRR retriever + compositional queries
│   │   ├── skill_library.py        # Self-evolving SKILL.md packages + lifecycle + ledger
│   │   ├── command_gate.py         # Zero-cost /command dispatch
│   │   └── steering.py             # In-task steering queue
│   ├── skills/                     # 9 active skill packages (httpx REST)
│   │   ├── web_research/           # web_search, fetch_url
│   │   ├── file_management/        # read_file, write_file, list_directory
│   │   ├── data_processing/        # parse_json, summarize_text, extract_data
│   │   ├── github/                 # 9 tools (incl. github_verify_pr_locally)
│   │   ├── slack/                  # 2 tools
│   │   ├── discord/                # 1 tool
│   │   ├── jira/                   # 2 tools
│   │   ├── gitlab/                 # 3 tools
│   │   └── email/                  # 1 tool
│   ├── watchers/                   # 11 event monitors + base + manager
│   ├── orchestrator.py             # Task lifecycle + Complexity Router + self-improvement
│   ├── observability.py            # Trace collector
│   ├── telegram.py                 # Telegram bot (remote approvals)
│   ├── models.py                   # Pydantic data models (Task, Todo, Memory, Skill)
│   └── config.py                   # Environment-based settings
├── cloud/
│   ├── vertex_ai/agent.py          # Google ADK agent wrapper (callbacks + Runner)
│   ├── firestore/client.py         # Firestore persistence (tasks/memory/skills/watchers)
│   ├── pubsub/events.py            # Pub/Sub event routing
│   └── cloud_run/                  # Dockerfile + cloudbuild.yaml
├── api/
│   ├── main.py                     # FastAPI app + global event bus + background runner
│   ├── dashboard.html              # Live traceability dashboard (WS/SSE/poll)
│   ├── watcher_routes.py           # Watcher CRUD (6 routes)
│   ├── credentials_routes.py       # Credentials management (4 routes)
│   └── gemini_keys_routes.py       # Gemini key management (5 routes)
├── tests/                          # 285 tests
├── projects/                       # Agent-generated multi-file builds (gitignored)
├── data/                           # SQLite memory store (gitignored)
├── docs/
│   ├── ATTRIBUTIONS.md             # Detailed open-source pattern attributions
│   ├── capabilities.md             # Full capability reference
│   └── user_guide.md               # Non-coder user guide
├── scripts/                        # Deploy scripts (bash + PowerShell)
├── pyproject.toml
└── README.md
```

---

## Credentials

All API keys and secrets are managed in one place via the Dashboard **Credentials** page (or `POST /api/credentials`):

| Category | Fields |
|----------|--------|
| AI & LLM | Gemini API Keys (multi-key, active index), Model |
| Google Cloud | Project ID, Region |
| Web Search | Google Search API Key, CX |
| GitHub | Personal Access Token |
| GitLab | Token, Base URL |
| Slack | Bot Token (`xoxb-...`) |
| Discord | Bot Token |
| Jira | Domain, Email, API Token |
| Email (IMAP/SMTP) | Server, Address, Password |

Stored in `.env` (gitignored), never exposed in plain text to the frontend. Gemini keys are also manageable via `GET/POST/PUT/DELETE /api/gemini-keys` with `GEMINI_ACTIVE_KEY_INDEX` selection.

---

## API Reference

~52 REST endpoints + 1 WebSocket. Routers: `api/main.py` (core) + `api/watcher_routes.py` + `api/credentials_routes.py` + `api/gemini_keys_routes.py`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Dashboard (live UI) |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/agent/status` | Agent status (model, tools, memory, key health) |
| `POST` | `/api/tasks` | Submit a new task |
| `GET` | `/api/tasks` | List recent tasks |
| `GET` | `/api/tasks/{id}` | Get task details + trace |
| `GET` | `/api/tasks/live/{id}` | Poll live task updates |
| `GET` | `/api/tasks/live/{id}/stream` | SSE push for live events |
| `GET` | `/api/events/live?limit=100` | Global event bus (tasks/watchers/memory/skills) |
| `WS` | `/api/ws` | WebSocket fan-out (replay last 30) |
| `POST` | `/api/tasks/{id}/cancel` | Cancel a running task |
| `DELETE` | `/api/tasks/{id}` | Delete a task |
| `GET` | `/api/tasks/{id}/steer` | List steering messages for a task |
| `POST` | `/api/tasks/{id}/steer` | Push a steering message / follow-up |
| `GET` | `/api/approvals` | List pending approvals |
| `POST` | `/api/approvals/{id}` | Approve or deny an action |
| `GET` | `/api/approvals/trusted` | List trusted task IDs |
| `GET` | `/api/tasks/{id}/trust` | Check per-task trust flag |
| `POST` | `/api/tasks/{id}/trust` | Trust / untrust a task |
| `GET` | `/api/traces` | List all execution traces |
| `GET` | `/api/traces/{task_id}` | Detailed trace chain + summary |
| `GET` | `/api/memory` | Search / list memory entries |
| `POST` | `/api/memory` | Add a memory entry (auto-detects instruction phrasing) |
| `DELETE` | `/api/memory/{id}` | Delete a memory entry |
| `POST` | `/api/memory/delete` | Bulk delete memory entries |
| `POST` | `/api/memory/clear/{category}` | Clear a memory category |
| `POST` | `/api/memory/{id}/feedback` | Rate helpful / unhelpful (trains trust score) |
| `POST` | `/api/memory/query` | Compositional recall: `search` / `probe` / `related` / `reason` |
| `GET` | `/api/memory/contradictions` | Facts sharing entities but conflicting |
| `GET` | `/api/skills` | Skill index with usage telemetry + lifecycle states |
| `POST` | `/api/skills` | Create a skill (full SKILL.md or bare markdown) |
| `GET` | `/api/skills/{name}` | Skill detail (frontmatter + body + usage stats) |
| `DELETE` | `/api/skills/{name}` | Archive a skill (`?purge=true` hard-deletes) |
| `POST` | `/api/skills/{name}/restore` | Restore the newest archived copy |
| `GET` | `/api/skills/ledger` | Audit trail of every skill mutation (sha256-chained) |
| `POST` | `/api/command` | Zero-cost deterministic commands (`/help`, `/status`, ...) |
| `GET` | `/api/watchers` | List active watchers |
| `GET` | `/api/watchers/{id}` | Get single watcher status |
| `POST` | `/api/watchers` | Create a new watcher |
| `POST` | `/api/watchers/{id}/start` | Start a stopped watcher |
| `POST` | `/api/watchers/{id}/stop` | Stop a running watcher |
| `DELETE` | `/api/watchers/{id}` | Remove a watcher |
| `POST` | `/api/watchers/restore` | Re-hydrate persisted watchers (also on startup) |
| `POST` | `/api/webhooks` | Generic webhook ingress `{event_type, payload}` → task |
| `GET` | `/api/credentials` | List all credentials (masked) |
| `GET` | `/api/credentials/{key}` | Get single masked credential |
| `POST` | `/api/credentials` | Save credentials to .env |
| `DELETE` | `/api/credentials/{key}` | Remove a credential |
| `GET` | `/api/gemini-keys` | List Gemini keys (masked) + active index |
| `POST` | `/api/gemini-keys` | Add a Gemini key |
| `PUT` | `/api/gemini-keys/{index}` | Update a Gemini key |
| `DELETE` | `/api/gemini-keys/{index}` | Remove a Gemini key |
| `POST` | `/api/gemini-keys/active` | Set active Gemini key |
| `GET` | `/api/approval-mode` | Get approval mode + Telegram status |
| `POST` | `/api/approval-mode` | Set approval mode (smart/always/never) |
| `GET` | `/api/rate-limit` | Get rate-limit presets + current RPS/RPM |
| `POST` | `/api/rate-limit` | Set RPS/RPM throttling |
| `POST` | `/api/telegram/webhook` | Receive Telegram updates |
| `POST` | `/api/telegram/setup` | Set up Telegram webhook |
| `GET` | `/api/telegram/status` | Get Telegram connection status |

### Examples

```bash
# Submit a task
curl -X POST http://localhost:8080/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"goal": "Search the web for latest AI news and summarize the top 3"}'

# Approve a high-risk action
curl -X POST http://localhost:8080/api/approvals/{step_id} \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'

# Steer a running task
curl -X POST http://localhost:8080/api/tasks/{id}/steer \
  -H "Content-Type: application/json" \
  -d '{"message": "also add dark mode"}'
```

---

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=agent --cov-report=term-missing

# Parallel
python -m pytest tests/ -n auto
```

**285 tests** covering models, memory (hybrid retrieval, HRR, trust scoring, hygiene/contradictions), skill library (validation, lifecycle, matching, ledger), deterministic routing (command gate, tool-name repair, Complexity Router), adaptive loop (result feedback, self-correction, verification, failure guards, elastic budget, compaction, collision guard, `todowrite`/`task`/`token`/`tool_delta` streaming), executor (smart approval, per-task trust), orchestrator (lazy roadmap), API endpoints (including `WebSocket /api/ws`, `GET /api/events/live`, `SSE`), watchers (all 11), ADK integration, and all skills (Slack, Discord, Jira, GitLab, Email via `httpx` mocks).

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Gemini API key(s), comma-separated for multi-key |
| `GEMINI_ACTIVE_KEY_INDEX` | No | 1-based active key index (default `1`) |
| `GEMINI_MODEL` | Yes | Model name (default `gemini-3.5-flash`) |
| `GEMINI_MODEL_PRO` | No | Stronger fallback on truncation (default `gemini-3.5-pro`) |
| `GEMINI_FALLBACK_MODELS` | No | Comma-separated quota fallback chain |
| `GEMINI_FULL_CONTROL` | No | `true` (default) — Gemini controls tool/file naming & memory policy |
| `GEMINI_RPS` / `GEMINI_RPM` | No | Client-side rate gate (default `1` / `15`; presets free/standard/unlimited) |
| `DATABASE_BACKEND` | No | `sqlite` (local) or `firestore` (Cloud Run) |
| `GITHUB_DEFAULT_REPO` | No | Default `owner/repo` when goal says "my repository" |
| `GOOGLE_CLOUD_PROJECT` | For cloud | GCP project ID |
| `GOOGLE_CLOUD_REGION` | For cloud | GCP region (default `us-central1`) |
| `GOOGLE_SEARCH_API_KEY` | No | Google Custom Search API key |
| `GOOGLE_SEARCH_CX` | No | Google Custom Search engine ID |
| `APPROVAL_MODE` | No | `smart` (default), `always`, `never` (alias `ask_everytime`→`always`) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | No | Your Telegram chat ID |
| `TELEGRAM_APPROVAL_TIMEOUT` | No | Approval timeout seconds (default `300`) |
| `GITHUB_TOKEN` | No | GitHub Personal Access Token |
| `GITLAB_TOKEN` | No | GitLab Personal Access Token |
| `GITLAB_BASE_URL` | No | GitLab base URL (default `https://gitlab.com`) |
| `SLACK_BOT_TOKEN` | No | Slack Bot Token (`xoxb-...`) |
| `DISCORD_BOT_TOKEN` | No | Discord Bot Token |
| `JIRA_DOMAIN` | No | Jira domain (e.g. `company.atlassian.net`) |
| `JIRA_EMAIL` | No | Jira account email |
| `JIRA_TOKEN` | No | Jira API token |
| `EMAIL_IMAP_SERVER` | No | IMAP server (e.g. `imap.gmail.com`) |
| `EMAIL_ADDRESS` | No | Email address |
| `EMAIL_PASSWORD` | No | App password |
| `AGENT_MAX_STEPS` | No | Max steps per task (default `40`, loop budget `40→120`) |
| `AGENT_TIMEOUT_SECONDS` | No | Step timeout seconds (default `0` = unlimited) |
| `AGENT_MEMORY_MAX_ITEMS` | No | Memory cap (default `1000`) |
| `WATCHER_DEFAULT_INTERVAL` | No | Watcher poll seconds (default `300`) |
| `WATCHER_MAX_CONCURRENT` | No | Max concurrent watchers (default `10`) |
| `API_HOST` | No | API host (default `0.0.0.0`) |
| `API_PORT` | No | API port (default `8080`) |
| `ENVIRONMENT` | No | `development` or `production` |

---

## Open-Source Acknowledgments

NexusMind incorporates selected agent patterns inspired by [OpenClaw](https://github.com/openclaw/openclaw), [Hermes Agent](https://github.com/NousResearch/hermes-agent), and [opencode](https://github.com/sst/opencode). These patterns were reimplemented and integrated into NexusMind's Python/FastAPI/Gemini architecture.

> **Full attribution details & source-file mapping → [`docs/ATTRIBUTIONS.md`](docs/ATTRIBUTIONS.md)**

---

## License

[MIT](https://opensource.org/licenses/MIT)

---

<div align="center">

**Built for the [Google All Things Agentic Hackathon](https://allthingsagentic.devpost.com) — August 2026**

Made with care by Tamim Hasan

</div>
