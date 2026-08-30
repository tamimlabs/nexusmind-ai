# NexusMind AI — What Can This Agent Do?

**An autonomous AI agent that plans, executes, self-corrects, and learns — powered by Gemini Flash and Google Cloud.**

---

## At a Glance

| | |
|---|---|
| **What is it?** | An autonomous task-execution agent |
| **How does it work?** | You give it a goal → it plans, then executes **step by step** (one action at a time, learning from each real result) → learns from results |
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

### 2. Tool Execution — 30 Registered Tools
The agent has **30 registered tools** across 9 skill packages + 2 opencode-parity utilities. Loader `agent/skills/loader.py:15` now loads 9 packages (was 4); `agent/core/executor.py:36` `high_risk` set expanded to cover all mutating integrations. All new skills copy the verified open-source REST API pattern from `agent/skills/github/skill.py` (httpx direct-to-API, no `run_command` curl, no `web_search` fallback for actions).

**Core Tools (12):**

| Tool | What It Does | Risk |
|------|-------------|------|
| `web_search` | Search the web (Google Custom Search primary, DuckDuckGo fallback when Google's quota is hit) | — |
| `fetch_url` | Fetch and extract text from any URL | — |
| `read_file` | Read local files | — |
| `write_file` | Create or update files | — |
| `list_directory` | Browse directory contents | — |
| `parse_json` | Extract structured data from JSON | — |
| `summarize_text` | Condense long text into key points | — |
| `extract_data` | Pull specific data from unstructured text | — |
| `execute_code` | Run Python code | **high-risk** (smart-approval gated) |
| `run_command` | Execute shell commands | **high-risk** (smart-approval gated) |
| `task` | Delegate to `explore`/`general` sub-agent (opencode parity) | — |
| `todowrite` | Overwrite live TODO checklist (opencode parity) | — |

**GitHub Skill (9):**

| Tool | What It Does | Risk |
|------|-------------|------|
| `github_resolve_repo` | Resolves owner/name even from partial repo references | — |
| `github_get_repo` | Fetch repository details | — |
| `github_list_prs` | List open pull requests | — |
| `github_get_pr` | Get details of a specific PR (files + patch) | — |
| `github_review_pr` | Gemini-powered review verdict: merge / reject / skip, each with a confidence score | — |
| `github_verify_pr_locally` | Checkout PR branch and run tests locally before merge decision | — |
| `github_merge_pr` | Merge a pull request | **high-risk** |
| `github_close_pr` | Close a pull request (with optional comment) | **high-risk** |
| `github_apply_decisions` | Applies review verdicts across PRs (sequential, locally verified) | **high-risk** |

**Slack Skill (2) — `agent/skills/slack/skill.py`:**

| Tool | What It Does | Risk | Auth |
|------|-------------|------|------|
| `slack_send_message` | `POST https://slack.com/api/chat.postMessage` — post to channel | **high-risk** | `SLACK_BOT_TOKEN` (xoxb-…) via `settings.slack_bot_token` → `SLACK_BOT_TOKEN` env → project `.env` |
| `slack_reply_thread` | Same endpoint with `thread_ts` — threaded reply | **high-risk** | same |

**Discord Skill (1) — `agent/skills/discord/skill.py`:**

| Tool | What It Does | Risk | Auth |
|------|-------------|------|------|
| `discord_send_message` | `POST https://discord.com/api/v10/channels/{channel_id}/messages` — send message | **high-risk** | `DISCORD_BOT_TOKEN` via `settings.discord_bot_token` → env → `.env` |

**Jira Skill (2) — `agent/skills/jira/skill.py`:**

| Tool | What It Does | Risk | Auth |
|------|-------------|------|------|
| `jira_comment_issue` | `POST https://{JIRA_DOMAIN}/rest/api/3/issue/{key}/comment` (ADF body) — add comment | **high-risk** | `JIRA_DOMAIN` + `JIRA_EMAIL` + `JIRA_TOKEN` (basic auth) via `settings.jira_domain/email/token` → env → `.env` |
| `jira_transition_issue` | `POST https://{JIRA_DOMAIN}/rest/api/3/issue/{key}/transitions` `{"transition":{"id":…}}` — transition status | **high-risk** | same |

**GitLab Skill (3) — `agent/skills/gitlab/skill.py`:**

| Tool | What It Does | Risk | Auth |
|------|-------------|------|------|
| `gitlab_list_mrs` | `GET {base_url}/api/v4/projects/{id}/merge_requests?state=` — list MRs | — | `GITLAB_TOKEN` / `GITLAB_BASE_URL` (or `GITLAB_URL`) via settings → env → `.env`; defaults to `https://gitlab.com`; `PRIVATE-TOKEN` header |
| `gitlab_get_mr` | `GET .../merge_requests/{iid}` — single MR details | — | same |
| `gitlab_merge_mr` | `PUT .../merge_requests/{iid}/merge` — merge MR | **high-risk** | same |

**Email Skill (1) — `agent/skills/email/skill.py`:**

| Tool | What It Does | Risk | Auth |
|------|-------------|------|------|
| `send_email` | `smtplib` TLS `587` — send email; if SMTP not configured or send fails, saves draft to `output/email_drafts/{ts}_{to}.eml` | **high-risk** | `EMAIL_SMTP_SERVER` (or `EMAIL_IMAP_SERVER` fallback) + `EMAIL_ADDRESS` + `EMAIL_PASSWORD`/`EMAIL_IMAP_PASSWORD` (+ optional `EMAIL_SMTP_PORT` default 587) via settings → env → `.env` |

### 3. Deterministic GitHub Pipeline
When a goal mentions repos or pull requests, NexusMind doesn't gamble on free-form planning. It routes through a fixed, reliable pipeline:

**resolve repo → list/fetch PRs → review each PR → apply decisions or summarize**

Action goals never degrade into a generic web search — if you ask it to review your PRs, it reviews your PRs.

### 4. Adaptive Step-by-Step Execution (opencode-style + Router)
NexusMind does **not** run a goal as a one-shot script. It works like a programmer in an IDE: it decides ONE action, executes it, and feeds the **real outcome — including errors — back into the very next decision**. From opencode `session/prompt.ts:1081` + `processor.ts` + `EventV2Bridge`:

- **Complexity Router** — `Tier1` trivial 1-call direct, `Tier2` single-tool direct, `Tier3` full `40→120` elastic loop (lazy roadmap, prompt slicing) — normal tasks single-call fast, heavy builds keep going
- **Decide → Execute → Observe** — one Gemini call per step picks the single next action; the true result (not the hoped-for one) becomes the transcript the next decision sees; streams via `generate_content_stream` → `token` events
- **Self-corrects from errors** — a failed step (missing dependency, approval timeout, bad result) lands in the transcript and the next decision fixes it: install the module, switch tools, verify the files
- **Verifies before done** — steps like `list_directory` / `read_file` confirm the artifacts actually exist before the agent declares success
- **Keeps working until satisfied** — elastic `40→120` budget (`+20` when `pending>0` & `recent_ok>0`), `3` consecutive failures abort, compaction every 90 steps (Gemini summary) lets ambitious builds run
- **Live progress for every work** — `token` + `tool_delta` (4 KiB stdout) + `step_running/tool_output/todo_update` via global `WebSocket /api/ws` + `SSE` + poll, dashboard Thinking/Checklist live
- **Dedicated `todowrite` + `task`** — explicit checklist overwrite (opencode `tool/todo.ts`, priority `high/medium/low`) + `task` `explore`/`general` subagents; collision guard + `tool_delta` streaming
- **Guardrail:** a failed action tool can never quietly get switched to `web_search` during self-correction. If the agent was supposed to act, it keeps trying to act

> **Step 3 fails:** "File /data.csv not found"
> **Agent sees the error and thinks:** "The path might be wrong — let me list the directory first."
> **Step 4:** Lists files → finds the correct path → **Step 5 continues and the goal completes**

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

### 7. Memory-Gated Watchers (11 Platforms, Honest Autonomy — All Fully Actionable)
NexusMind monitors **11 platforms** around the clock — and **every watcher is now fully actionable with write-back** (not just GitHub). Previously GitHub was the only fully actionable watcher; the other 10 were read-only/notify-only. The 5 new skill packages close the gap: Slack, Discord, Jira, GitLab, and Email can now act, mirroring the verified GitHub httpx REST pattern. Every auto-triggering watcher still needs a **matching standing instruction in memory** before it may do anything.

| Platform | What It Monitors | Write-Back Tools | Status |
|----------|-----------------|-----------------|--------|
| GitHub | New PRs, issues | `github_merge_pr`, `github_close_pr`, `github_apply_decisions`, `github_verify_pr_locally` | **Fully actionable** |
| GitLab | New merge requests, issues | `gitlab_list_mrs`, `gitlab_get_mr`, `gitlab_merge_mr` | **Fully actionable** (new) |
| Slack | Channel messages, mentions | `slack_send_message`, `slack_reply_thread` | **Fully actionable** (new) |
| Discord | Channel messages | `discord_send_message` | **Fully actionable** (new) |
| Jira | New/updated issues | `jira_comment_issue`, `jira_transition_issue` | **Fully actionable** (new) |
| Email (IMAP) | Inbox messages | `send_email` (SMTP TLS 587, draft fallback to `output/email_drafts/`) | **Fully actionable** (new) |
| Reddit | New posts in subreddits | Respond via `send_email`/`slack`/`discord` or file-based workflows | **Actionable via skills** |
| Hacker News | New stories, comments | Respond via skills / summarization pipeline | **Actionable via skills** |
| RSS/Atom | Feed items | Respond via skills | **Actionable via skills** |
| Cron | Scheduled tasks (pre-authorized — you wrote the goals) | Full tool access per goal | **Fully actionable** |
| Custom Webhook | Any HTTP event (pre-authorized — you wrote the goals) | Full tool access per goal | **Fully actionable** |

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
Memory persists across restarts — a Hermes-inspired system (adapted) + opencode-style traceability, not a flat list:
- **Local SQLite store** (`data/memory.db`) with **FTS5 full-text search** kept in sync by triggers; zero-config, works out of the box (legacy `memory.json` is migrated automatically on first run)
- **Hybrid retrieval:** BM25 candidates → token-overlap rerank → **holographic phase-vector similarity (HRR)** — no embedding API required → trust weighting
- **Trust scoring:** rate memories helpful (+0.05) or unhelpful (−0.10); good memories rise, outdated ones sink faster than they rise. Retrieval is weighted by trust
- **Compositional recall:** probe all facts about an entity, find related facts, or reason across multiple entities in vector space (`POST /api/memory/query`)
- **Contradiction detection:** flags stored facts that share entities but make conflicting claims
- **Injection-safe context injection:** relevant memories are recalled before planning and injected as a fenced `<memory-context>` block marked "NOT new user input" — recalled memory can never masquerade as fresh instructions; trivial prompts ("ok", "thanks") skip the round-trip entirely
- **Recall hygiene (anti-contamination):** raw `task_outcome` transcripts are never injected into planning prompts — only distilled facts, framed explicitly as "BACKGROUND ONLY, never copy a past task's subject or code"; stored lessons pass a sanitizer that rejects prompt echoes and mid-sentence truncations before they can contaminate future planning
- **Auto-extraction:** durable preferences ("I prefer...") and decisions ("we decided to...") are harvested from task text into long-lived categories
- **Curated:** global cap with instruction protection — standing instructions can never be evicted by task-outcome churn; exact duplicates rejected automatically
- **Full CRUD + feedback:** add manually, delete single or bulk, clear per category, rate after use — via the dashboard or REST API

### 12. Self-Evolving Skill Library (Hermes Adaptation + opencode task delegation)
The agent doesn't just solve tasks — it **remembers how** it solved them (Hermes) and can delegate via `task` subagents (`explore`/`general`) as in opencode `tool/task.ts`:
- **SKILL.md packages:** each skill is a markdown folder (`data/skills/<name>/`) with frontmatter metadata (name, description, version, provenance) and a procedure body
- **Auto-synthesis:** when a task finishes with ≥2 successful steps across ≥2 distinct tools, Gemini distills the workflow into a reusable skill — gated by a similarity dedup check so near-duplicates are never saved; failures never break the task
- **Skills steer planning:** before every plan, the planner receives a fenced `<available-skills>` index (descriptions only); the single best-matched skill's full procedure is auto-injected via lexical scoring with light stemming — no embedding API required
- **Usage telemetry:** every match bumps `use_count` / `last_used_at` in a sidecar JSON (observability only, never destructive)
- **Deterministic lifecycle:** skills idle 30 days go `active → stale`, 90 days → archived (moved to `.archive/`, recoverable); pinned skills exempt
- **Provenance + audit ledger:** every create/patch/archive/restore/delete is appended to a sha256-hashed `.ledger.jsonl`; agent-created skills carry `created_by: agent` + originating task ID, and survive patches forever
- **Hard validation gates:** name slug rules, description budgets (60 chars for agent-created, trigger-first), content caps — the same gates for manual and auto-created skills
- **Full REST API:** list, create, inspect, archive, restore, purge, and audit (`/api/skills*`)

### 13. Deterministic Routing (Command Gate + Plan Validation) + opencode streaming
Adapted from Hermes, OpenClaw **and opencode** (`tool/todo` + `EventV2Bridge`) — the right action happens without wasting model calls, with live `todowrite` + streaming:
- **Zero-cost command gate:** `/status`, `/tasks`, `/tools`, `/skills`, `/memory <query>`, `/pending`, `/help` are resolved deterministically (Telegram + `POST /api/command`) with **zero LLM calls**; unknown or natural-language input falls through to the agent loop
- **Path-safe detection:** `/Users/x/file.md fix this` is correctly treated as a task, not a command (first-token slash heuristic)
- **Dynamic tool catalog:** the planner prompt is generated from the live tool registry (name + docstring), so prompts can never drift from what actually exists
- **Hallucinated-tool repair ladder:** if Gemini plans a step with a nonexistent tool, it's repaired locally — normalize separators → strip `_tool` suffix → alias map (`"search"` → `web_search`) → fuzzy match ≥ 0.7
- **Corrective re-plan with catalog feedback:** unrepairable tools trigger exactly ONE corrective round where the planner receives the valid-tool list; still-invalid steps are dropped deterministically instead of failing at runtime
- **Truncate-tolerant plan salvage:** if Gemini's response is cut off by output-token limits (common for ambitious goals), completed steps are recovered from the partial JSON instead of discarding the whole plan; the planner budget is 16,384 tokens and prompts forbid inlining large file content — the plan is a **best-effort roadmap**, and real execution is the adaptive step-by-step loop that reacts to actual results
- **No-template builds:** every website, app, and landing page is authored by Gemini itself from the user's exact command plus recalled memory context. There are **no hardcoded scaffolds, no invented branding, and no stock images** — plans (or salvaged partial plans) are executed as the model wrote them, and when the planner genuinely cannot respond, the agent reports honestly instead of fabricating output

### 14. Unified Credentials Management
All API keys and secrets managed in one place:
- **10 categories** on a single credentials page
- **Secure:** Secret values masked in UI, stored in `.env` (gitignored)
- **Auto-fill:** Watchers use saved credentials automatically

### 15. Traceability Dashboard
Every step is logged and visible in real-time (opencode `EventV2Bridge`/`session/status.ts` + `tool/todo` live):
- **Tool calls** (blue) — what the agent did
- **Reasoning steps** (purple) — what the agent was thinking
- **Approval gates** (yellow) — where it asked for permission
- **Errors** (red) — what went wrong and how it recovered
- **Token deltas** (purple `▌`) + **tool stdout `tool_delta`** (blue `▸`) streaming + **`todo_update` `☑`** priority badge (`high/medium/low`)
- **Live Thinking tab** = `WebSocket /api/ws` global + `SSE /api/tasks/live/{id}/stream` + poll fallback, deduped, in a minimize-able side panel

### 16. Multi-Step Task Decomposition + Adaptive Loop (opencode-style)
Complex goals are automatically broken into manageable steps (OpenClaw planning + opencode `session/prompt.ts` adaptive loop):
- Gemini produces a **best-effort roadmap** up front (dependencies resolved, each step mapped to a tool, results flowing step-to-step)
- Execution is **adaptive** — the roadmap is a starting point, not a script: unexpected errors just change the path, never kill the task; loop is `40→120` elastic, extends `+20` when `pending>0` & `recent_ok>0`, compacts every 90 steps (Gemini summary)
- If planning fails or truncates, the agent still starts working — the step-by-step loop handles anything the roadmap didn't cover; `Complexity Router` (`Tier1` trivial 1-call / `Tier2` single-tool / `Tier3` heavy) + lazy roadmap + prompt slicing keeps normal tasks fast

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
| **Testing** | 250+ Passing Tests |

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
│ (run steps)  │     │  (30 tools,  │     │   (retry)    │
│              │     │  9 packages) │     │              │
└──────┬──────┘     └──────────────┘     └─────────────┘
       │  loader agent/skills/loader.py:15 · high_risk agent/core/executor.py:36
       ▼
┌───────────────────────┐  ┌──────────────┐  ┌─────────────┐
│        Memory         │◀─│  Reflection  │─▶│  Next Task   │
│ (local SQLite + optional│  │   (learn)    │  │  (improved)  │
│       Firestore)      │  └──────────────┘  └─────────────┘
└───────────────────────┘
  11 watchers fully actionable — GitHub (9 tools) + Slack (2) + Discord (1) + Jira (2) + GitLab (3) + Email (1) all via httpx REST pattern from GitHub skill
```

---

## Why This Matters

Most AI agents today are **reactive** — they wait for instructions.

NexusMind is **autonomous, with guardrails**:
- It plans without being told how — then works **step by step, one action at a time**, reading each real result before the next move
- It recovers from failures without being asked — and never swaps action goals for lazy web searches
- It asks permission before doing anything risky (via dashboard or Telegram)
- It follows your standing instructions — and its watchers refuse to act without them
- It learns from every task and improves over time

Give it a goal or a standing instruction, and it keeps working — deciding, executing, self-correcting, and learning as it goes, visible in the dashboard every step of the way.

This is what "agentic" means — an AI that takes initiative within boundaries you define.

---

## Links

| Resource | Where |
|----------|-------|
| **GitHub** | https://github.com/tamimlabs/nexusmind-ai |
| **Hackathon** | Google "All Things Agentic" 2026 |

---

*Built for the Google "All Things Agentic" Hackathon — Track: The Taskmaster*
