# NexusMind AI — What Can This Agent Do?

**An autonomous AI agent that plans, executes, self-corrects, and learns — powered by Gemini Flash and Google Cloud.**

---

## At a Glance

| | |
|---|---|
| **What is it?** | An autonomous task-execution agent |
| **How does it work?** | You give it a goal → it plans steps → executes tools → learns from results |
| **What makes it different?** | It self-corrects on failure, asks for permission on risky actions, follows your standing instructions, and improves over time |
| **Built for** | Google "All Things Agentic" Hackathon, Track: The Taskmaster |

---

## Core Capabilities

### 1. Autonomous Task Planning
Give NexusMind a goal in plain English. It uses **Gemini Flash** to break it into concrete, ordered steps — no manual guidance needed.

> **User:** "Find the top 3 Python web frameworks and compare their performance"
>
> **Agent plans:**
> 1. Search for Python web framework benchmarks
> 2. Fetch detailed comparison data
> 3. Summarize findings into a structured comparison

### 2. Tool Execution — 18 Registered Tools
The agent has **18 registered tools** across two groups.

**Core Tools (10):**

| Tool | What It Does |
|------|-------------|
| `web_search` | Search the web (Google Custom Search primary, DuckDuckGo fallback when Google's quota is hit) |
| `fetch_url` | Fetch and extract text from any URL |
| `read_file` | Read local files |
| `write_file` | Create or update files |
| `list_directory` | Browse directory contents |
| `parse_json` | Extract structured data from JSON |
| `summarize_text` | Condense long text into key points |
| `extract_data` | Pull specific data from unstructured text |
| `execute_code` | Run Python code (approval required) |
| `run_command` | Execute shell commands (approval required) |

**GitHub Skill (8):**

| Tool | What It Does |
|------|-------------|
| `github_resolve_repo` | Resolves owner/name even from partial repo references |
| `github_get_repo` | Fetch repository details |
| `github_list_prs` | List open pull requests |
| `github_get_pr` | Get details of a specific PR |
| `github_review_pr` | Gemini-powered review verdict: merge / reject / skip, each with a confidence score |
| `github_merge_pr` | Merge a pull request (high-risk, approval-gated) |
| `github_close_pr` | Close a pull request (high-risk, approval-gated) |
| `github_apply_decisions` | Applies review verdicts across PRs (mutating operations are high-risk and gated by approvals) |

### 3. Deterministic GitHub Pipeline
When a goal mentions repos or pull requests, NexusMind doesn't gamble on free-form planning. It routes through a fixed, reliable pipeline:

**resolve repo → list/fetch PRs → review each PR → apply decisions or summarize**

Action goals never degrade into a generic web search — if you ask it to review your PRs, it reviews your PRs.

### 4. Self-Correction on Failure
When a tool fails, NexusMind doesn't give up. It:
1. Sends the error to Gemini Flash
2. Analyzes what went wrong
3. Adjusts its arguments and approach
4. Retries automatically (up to 2 times)

And there's a guardrail: **a failed action tool can never quietly get switched to `web_search` during self-correction.** If the agent was supposed to act, it keeps trying to act.

> **Step 1 fails:** "File /data.csv not found"
> **Agent thinks:** "The file path might be wrong. Let me list the directory first."
> **Step 2 retries:** Lists files → finds correct path → continues successfully

### 5. Human-in-the-Loop Approval (Smart, Dashboard + Telegram)
High-risk actions require human approval, but with **smart logic**:

**Smart Approval (default):**
- Auto-approve safe commands on the allowlist (`ls`, `cat`, `git status`)
- Only ask for dangerous operations (`rm -rf`, merges, deletes)
- Reduces approval fatigue while keeping you safe

**3 Approval Modes:**
- **Smart** — Auto-approve safe, ask for dangerous (recommended)
- **Always** — Ask for every high-risk tool
- **Never** — Auto-approve everything

**Approve From Anywhere:**
- Dashboard approvals — every gate shows up right in the execution trace
- Telegram bot — approve/deny buttons straight from your phone
- Every approval gate is recorded in the trace, so nothing happens invisibly

### 6. Self-Governing Rules — Standing Instructions (Flagship)
This is where NexusMind stops being a chatbot and starts being an agent. You can give it **durable directives** like:

> "Whenever a PR arrives, test and merge it or decline with a comment."

Instead of executing that sentence once and forgetting it, NexusMind detects the phrasing, **saves it as a standing instruction in memory**, and honors it going forward.

- **Automatic detection:** Type a directive-style goal and it gets saved instead of executed
- **Manual control:** Add instructions yourself on the Memory page, which auto-detects instruction phrasing
- **Persistent:** Directives survive restarts and shape future behavior

### 7. Memory-Gated Watchers (11 Platforms, Honest Autonomy)
NexusMind monitors 11 platforms around the clock — but here's the honest part: **watchers don't act on their own.** Every auto-triggering watcher needs a **matching standing instruction in memory** before it may do anything.

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
| Cron | Scheduled tasks (pre-authorized — you wrote the goals) |
| Custom Webhook | Any HTTP event (pre-authorized — you wrote the goals) |

How the gating works:
- **No matching instruction?** The watcher does *nothing* except notify you — at most once every 6 hours per watcher
- **Matching logic:** Domain-scoped keywords; the most recent instruction wins
- **Cron & Webhook exceptions:** These are pre-authorized because you authored their goals yourself
- **Token-efficient:** Gemini is only called when there's actual work to do

You get 24/7 coverage without surrendering control.

### 8. Self-Improvement (Learns Over Time)
After every task, NexusMind reflects on what happened and extracts actionable lessons:

> **Reflection:** "When searching for recent news, always specify the year in the query to avoid outdated results."

These lessons are stored in memory and **automatically fed into future planning**. The agent gets better at similar tasks over time.

### 9. Web Search with Smart Fallback
Search the web without worrying about API limits:
- **Primary:** Google Custom Search
- **Fallback:** DuckDuckGo (free, no API key needed)
- **Auto-switch:** When Google's daily quota is hit, seamlessly switches to DuckDuckGo

### 10. Event-Driven Architecture
NexusMind fits into event-driven workflows:
- **Pub/Sub** publishes task events as work happens
- A **webhook endpoint** accepts external events and turns them into tasks

### 11. Persistent Memory (SQLite + Hybrid Retrieval + Trust)
Memory persists across restarts — a Hermes-inspired system, not a flat list:
- **Local SQLite store** (`data/memory.db`) with **FTS5 full-text search** kept in sync by triggers; zero-config, works out of the box (legacy `memory.json` is migrated automatically on first run)
- **Hybrid retrieval:** BM25 candidates → token-overlap rerank → **holographic phase-vector similarity (HRR)** — no embedding API required → trust weighting
- **Trust scoring:** rate memories helpful (+0.05) or unhelpful (−0.10); good memories rise, outdated ones sink faster than they rise. Retrieval is weighted by trust
- **Compositional recall:** probe all facts about an entity, find related facts, or reason across multiple entities in vector space (`POST /api/memory/query`)
- **Contradiction detection:** flags stored facts that share entities but make conflicting claims
- **Injection-safe context injection:** relevant memories are recalled before planning and injected as a fenced `<memory-context>` block marked "NOT new user input" — recalled memory can never masquerade as fresh instructions; trivial prompts ("ok", "thanks") skip the round-trip entirely
- **Auto-extraction:** durable preferences ("I prefer...") and decisions ("we decided to...") are harvested from task text into long-lived categories
- **Curated:** global cap with instruction protection — standing instructions can never be evicted by task-outcome churn; exact duplicates rejected automatically
- **Full CRUD + feedback:** add manually, delete single or bulk, clear per category, rate after use — via the dashboard or REST API

### 12. Self-Evolving Skill Library (Hermes Adaptation)
The agent doesn't just solve tasks — it **remembers how** it solved them:
- **SKILL.md packages:** each skill is a markdown folder (`data/skills/<name>/`) with frontmatter metadata (name, description, version, provenance) and a procedure body
- **Auto-synthesis:** when a task finishes with ≥2 successful steps across ≥2 distinct tools, Gemini distills the workflow into a reusable skill — gated by a similarity dedup check so near-duplicates are never saved; failures never break the task
- **Skills steer planning:** before every plan, the planner receives a fenced `<available-skills>` index (descriptions only); the single best-matched skill's full procedure is auto-injected via lexical scoring with light stemming — no embedding API required
- **Usage telemetry:** every match bumps `use_count` / `last_used_at` in a sidecar JSON (observability only, never destructive)
- **Deterministic lifecycle:** skills idle 30 days go `active → stale`, 90 days → archived (moved to `.archive/`, recoverable); pinned skills exempt
- **Provenance + audit ledger:** every create/patch/archive/restore/delete is appended to a sha256-hashed `.ledger.jsonl`; agent-created skills carry `created_by: agent` + originating task ID, and survive patches forever
- **Hard validation gates:** name slug rules, description budgets (60 chars for agent-created, trigger-first), content caps — the same gates for manual and auto-created skills
- **Full REST API:** list, create, inspect, archive, restore, purge, and audit (`/api/skills*`)

### 13. Unified Credentials Management
All API keys and secrets managed in one place:
- **10 categories** on a single credentials page
- **Secure:** Secret values masked in UI, stored in `.env` (gitignored)
- **Auto-fill:** Watchers use saved credentials automatically

### 14. Traceability Dashboard
Every step is logged and visible in real-time:
- **Tool calls** (blue) — what the agent did
- **Reasoning steps** (purple) — what the agent was thinking
- **Approval gates** (yellow) — where it asked for permission
- **Errors** (red) — what went wrong and how it recovered
- **Live Thinking tab** polls while a task runs, in a minimize-able side panel

### 15. Multi-Step Task Decomposition
Complex goals are automatically broken into manageable steps:
- Dependencies are resolved (step A before step B)
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
| "Review my repo's open PRs and merge or reject as needed" | Deterministic GitHub pipeline: resolve repo → list PRs → Gemini review verdicts → apply decisions (merges/closes gated by approvals) |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **LLM** | Gemini Flash (via Gemini API, model configurable through `GEMINI_MODEL`, multi-key rotation with rate-limit backoff) |
| **Agent Framework** | Google ADK (Agent Development Kit) wrapper |
| **State Persistence** | Local SQLite memory store (FTS5 + HRR vector recall) + SKILL.md skill packages; optional Google Cloud Firestore in cloud deployments |
| **Event Routing** | Google Cloud Pub/Sub |
| **Deployment** | Google Cloud Run (scales to zero) |
| **API** | FastAPI (Python) |
| **Frontend** | Real-time traceability dashboard |
| **Language** | Python 3.11+ |
| **Testing** | 113 passing tests |

---

## Architecture

```
User submits goal
       │
       ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Orchestrator│────▶│   Planner    │────▶│   Gemini    │
│   (main loop)│     │  (decompose) │     │    Flash    │
└──────┬──────┘     └──────────────┘     └─────────────┘
       │
       ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Executor   │────▶│  Tool Engine │────▶│  Self-Correct│
│ (run steps)  │     │  (18 tools)  │     │   (retry)    │
└──────┬──────┘     └──────────────┘     └─────────────┘
       │
       ▼
┌───────────────────────┐  ┌──────────────┐  ┌─────────────┐
│        Memory         │◀─│  Reflection  │─▶│  Next Task   │
│ (local JSON + optional│  │   (learn)    │  │  (improved)  │
│       Firestore)      │  └──────────────┘  └─────────────┘
└───────────────────────┘
```

---

## Why This Matters

Most AI agents today are **reactive** — they wait for instructions and execute one step at a time.

NexusMind is **autonomous, with guardrails**:
- It plans without being told how
- It recovers from failures without being asked — and never swaps action goals for lazy web searches
- It asks permission before doing anything risky (via dashboard or Telegram)
- It follows your standing instructions — and its watchers refuse to act without them
- It learns from every task and improves over time

Give it a goal or a standing instruction, and it keeps working — planning, self-correcting, and learning as it goes.

This is what "agentic" means — an AI that takes initiative within boundaries you define.

---

## Links

| Resource | Where |
|----------|-------|
| **GitHub** | https://github.com/tamimlabs/nexusmind-ai |
| **Hackathon** | Google "All Things Agentic" 2026 |

---

*Built for the Google "All Things Agentic" Hackathon — Track: The Taskmaster*
