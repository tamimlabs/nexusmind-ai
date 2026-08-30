# NexusMind AI — User Guide
### Use the autonomous agent without writing a single line of code

> **New to AI agents? Perfect.** This guide takes you from zero to your first automated task in ~5 minutes. No coding, no terminal expertise needed.

<div align="center">

[![Demo Video](https://img.shields.io/badge/🎬_Watch_Demo-YouTube-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/watch?v=woSOCuzfabg)
[![No Code Required](https://img.shields.io/badge/No_Code-Required-brightgreen?style=flat)](#)
[![Gemini](https://img.shields.io/badge/Powered_by-Gemini_3.5_Flash-4285F4?style=flat&logo=google&logoColor=white)](https://aistudio.google.com/apikey)

**[▶️ Watch 4-min Demo](https://www.youtube.com/watch?v=woSOCuzfabg) before you start — see the whole flow in one take.**

</div>

---

## Table of Contents
1. [What NexusMind Does for You](#what-nexusmind-does-for-you)
2. [What You Need](#what-you-need)
3. [Get Your Free Gemini API Key](#step-1--get-your-free-gemini-api-key-2-minutes)
4. [Install NexusMind](#step-2--install-nexusmind)
5. [Understand the Dashboard](#step-3--understand-the-dashboard-60-seconds)
6. [Submit Your First Task](#step-4--submit-your-first-task)
7. [Stay in Control — Approvals](#step-5--stay-in-control--approvals)
8. [Teach It Once — Standing Instructions](#step-6--teach-it-once--standing-instructions)
9. [Manage What It Remembers — Memory](#step-7--manage-what-it-remembers--memory)
10. [Automate Everything — Watchers](#step-8--automate-everything--watchers)
11. [Manage Credentials](#step-9--manage-credentials)
12. [Tips for Best Results](#tips-for-best-results)
13. [Troubleshooting](#troubleshooting)
14. [Need Help?](#need-help)

---

## What NexusMind Does for You

Give it a goal in plain English — *"summarize today's AI news"* or *"review open PRs in my repo"* — and it **works step by step: plans the path, runs one action at a time, reads each real result before the next move, handles errors, and asks you only when something risky needs permission.** 

It can also **watch** GitHub, GitLab, Slack, Discord, Jira, Reddit, Hacker News, Email and 4 more platforms 24/7 — and **act back** automatically when something new appears. All watchers that used to only *watch* can now *do* things for you: reply in Slack/Discord, comment or move Jira tickets, merge GitLab merge requests, and send emails — in addition to the original GitHub PR reviews/merges. You control all of it with a one-time standing instruction (Step 6) — no repeated prompting.

> **You don't prompt it repeatedly. You give it a goal once, walk away, and it works.**

---

## What You Need

| Requirement | Details |
|-------------|---------|
| **Computer** | Windows, Mac, or Linux — any recent version |
| **Internet** | For Gemini, web search, and watchers |
| **Gemini API Key** | Free — takes 2 minutes (see below) |
| **Browser** | Chrome, Edge, Firefox, or Safari |

No credit card. No cloud account. Free tier is enough to start.

---

## Step 1 — Get Your Free Gemini API Key (2 minutes)

1. Go to **https://aistudio.google.com/apikey**
2. Sign in with your Google account
3. Click **Create API key** → **Create API key in new project**
4. Click the copy icon — your key starts with `AIza...`
5. Keep it safe — you'll paste it in the next step

> **Pro tip:** You can add multiple keys separated by commas (e.g. `AIza... , AIza...`). NexusMind rotates between them automatically, so you won't hit rate limits.

---

## Step 2 — Install NexusMind

Choose the easiest option for you:

### Option A — Google Cloud Shell (Recommended, Zero Install)

Best if you don't want to install anything.

1. Open **https://shell.cloud.google.com** and sign in
2. Click the **Terminal** icon (top-right)
3. Paste this command and press **Enter**:

```bash
bash <(curl -s https://raw.githubusercontent.com/tamimlabs/nexusmind-ai/master/scripts/setup_cloud_shell.sh)
```

4. When prompted, paste your Gemini API key and press **Enter**
5. The dashboard opens automatically — you're done!

### Option B — Run on Your Computer

1. Install **Python 3.11+** from https://python.org (check "Add to PATH" during install)
2. Open **Terminal** (Mac/Linux) or **Command Prompt / PowerShell** (Windows)
3. Run these commands one at a time:

```bash
git clone https://github.com/tamimlabs/nexusmind-ai.git
cd nexusmind-ai
python -m venv .venv
```

Activate the environment:
```bash
# Windows
.venv\Scripts\activate
# Mac / Linux
source .venv/bin/activate
```

Install and configure:

```bash
pip install .
cp .env.example .env
# Windows alternative: copy .env.example .env
```

4. Open the `.env` file in Notepad / TextEdit and paste your key:

```
GEMINI_API_KEY=AIza...your-key-here
```

> **Optional — connect watchers later:** You don't need anything else to start. When you're ready to let the agent *act* in Slack, Discord, Jira, GitLab, or Email, add those tokens to the same `.env` file (see [Step 9 — Credentials](#step-9--manage-credentials) for exactly what to paste). Everything is also editable from the dashboard under **Credentials** — no need to edit the file again.

5. Start the dashboard:

```bash
python -m api.main
```

6. Open your browser → **http://localhost:8080**

You should see the NexusMind dashboard with a green **● Agent is online** indicator.

---

## Step 3 — Understand the Dashboard (60 seconds)

### Layout at a Glance

```
┌─────────────┬──────────────────────────────┬─────────────────┐
│  Left Nav   │        Center Panel          │  Right Panel    │
│             │                              │  (Monitoring)   │
│  Tasks      │  Task input + Task list      │  Trace          │
│  Memory     │  Task detail & results       │  Thinking (live)│
│  Approvals  │                              │  Approvals      │
│  Watchers   │                              │                 │
│  Credentials│                              │  [—] minimize  │
│  Settings   │                              │                 │
└─────────────┴──────────────────────────────┴─────────────────┘
```

| Area | What It's For |
|------|---------------|
| **Left Sidebar** | Navigate: Tasks, Memory, Approvals, Watchers, Credentials, Settings |
| **Center Panel** | Type your goal, see all tasks, and read results |
| **Right Panel** | Live monitoring — trace, reasoning, and pending approvals |

### Right Panel — Your Control Tower

| Tab | What You See |
|-----|--------------|
| **Trace** | Color-coded timeline of every step the agent took |
| **Thinking** | Live reasoning — word-to-word streaming bubble (token deltas via WebSocket/SSE) that fills as the agent writes |
| **Approvals** | Buttons to approve or deny risky actions |
| **Checklist** | Live TODO checklist (todowrite) — the agent's plan, updated in real time |

**Minimize it:** Click the **—** (minimize) in the top-right of the right panel to collapse it to a slim rail. Click the **›** chevron or **Panel** label on the rail to bring it back. Your choice is remembered after reload.

### What Makes It Feel Live

- **Word-to-word streaming bubble** — the agent's answer appears token-by-token (not in chunks) via WebSocket/SSE, with a blinking cursor while it writes.
- **Flicker-free Task tab** — the task list keeps your input focused and scroll position, updates via RAF diff with shell persistence, WS-primary with 2.8 s polling fallback, and shared dedup so nothing flashes or re-creates.
- **Smoother under load** — performance throttle scales with key count, and the planning roadmap has a 12 s timeout so a slow Gemini call never freezes the UI.

### Status Colors

| Indicator | Meaning |
|-----------|--------------|
| 🟢 Green dot / badge | Agent online / Task completed |
| 🟡 Yellow badge | Task in progress |
| 🟣 Purple badge | Agent is planning |
| 🔴 Red badge | Task failed or needs approval |

---

## Step 4 — Submit Your First Task

1. In the dashboard, find the **task input box** at the top of the center panel
2. Type your goal in plain English — just like you'd tell a teammate
3. Click the green **Run** button
4. Watch the **Thinking** tab on the right — it streams the agent's reasoning live
5. The agent works **one step at a time**: each decision, tool call, and result (or error) lands in the **Trace** panel as it happens, so you see the agent self-correct and keep going in real time
6. When finished, the result appears in the center panel with a full step breakdown

### Try These Examples (Copy-Paste)

| Copy This | What Happens |
|-----------|--------------|
| `Search for the latest AI news and summarize the top 3 with sources` | Searches the web, reads articles, writes a summary with links |
| `Read the file data.csv and tell me how many rows it has` | Reads your file and counts rows |
| `Calculate 15% tip on a $85 bill` | Does the math and shows the result |
| `Find the top 5 Python tutorials for beginners and rank them` | Searches, evaluates, and ranks |
| `Look at my repository tamimlabs/nexusmind-ai and review the open PRs` | Finds your repo, reviews every open PR with Gemini, and reports back. If you added "merge if clean" it will apply the decision too |

> **One at a time:** Wait for a task to finish before starting the next one for the clearest trace.

---

## Step 5 — Stay in Control — Approvals

NexusMind is autonomous, but **never reckless**. Its **Smart Approval** system decides when to pause and ask you.

### Three Modes (Settings → Approval Mode)

| Mode | Behavior | Best For |
|------|----------|----------|
| **Smart** (default) | Auto-approves safe reads (`ls`, `cat`, `git status`, `web_search`) — asks before risky actions (`rm -rf`, `sudo`, deploys, merging PRs) | Most users — recommended |
| **Always** | Asks you before every high-risk tool | Maximum safety |
| **Never** | Auto-approves everything | Trusted / demo environments |

> You can change the mode anytime in **Settings** — no restart needed.

### Approve from Your Phone (Telegram)

Don't want to watch the dashboard? Approve from anywhere:

1. Open **Telegram** on your phone
2. Search for **@BotFather** → send `/newbot` → follow the prompts → copy the **bot token**
3. Search for **@userinfobot** → send any message → copy your **Chat ID**
4. In NexusMind, go to **Credentials** → **Telegram** section
5. Paste the **Bot Token** and **Chat ID** → click **Save All**

Now when approval is needed, you get a Telegram message like this:

```
🔐 Approval Required

Task: Deploy to production
Tool: run_command
Command: ./deploy.sh --prod

[✅ Approve]  [❌ Deny]
```

Tap **Approve** or **Deny** — the agent continues or stops immediately. The dashboard **Approvals** page also works as a fallback if Telegram is offline.

---

## Step 6 — Teach It Once — Standing Instructions

A normal goal runs once. A **standing instruction** is a lasting rule the agent follows forever — you teach it once, it obeys automatically.

### How It Works

1. Phrase your goal as a rule starting with **"whenever"**, **"when you get..."**, **"every time"**, or **"from now on"**
2. The agent detects the phrasing and **saves it as a standing instruction** instead of running it once
3. It confirms "instruction saved" in the dashboard (and Telegram if connected)
4. From then on, every matching event (e.g., from a watcher) triggers that rule automatically — no need to repeat yourself

### Example

Type this once in the task box:

> **"whenever you get a PR in my repo, review it and merge it if clean, otherwise decline with a helpful comment"**

Result: Every future pull request is reviewed and handled exactly that way — automatically. No further prompting.

### Add or Edit Manually

You don't have to use chat phrasing:

1. Go to **Memory** → **Add Memory** form at the top
2. Type your rule
3. Choose category **instruction** (or leave blank — the agent auto-detects instruction wording)
4. Click **Add**

To update a rule, delete the old one and add the new one. To remove all rules at once, filter by **Instructions** and click **Clear all instructions**.

---

## Step 7 — Manage What It Remembers — Memory

The **Memory** page is your window into what the agent has learned.

1. Click **Memory** in the left sidebar
2. Browse memories grouped by category:

| Category | What It Stores |
|----------|----------------|
| **Instructions** | Your standing rules (see Step 6) |
| **Reflections** | Lessons the agent learned after completing tasks |
| **Task Outcomes** | Summaries of what happened in past tasks |
| **Skills** | Reusable abilities auto-built from solved tasks |

**What you can do:**

- **Search** — type in the search box to find any memory
- **Delete** — click the delete icon on any entry to remove wrong or outdated info
- **Clear a category** — select a filter tab (e.g., Instructions) → **Clear all \<category\>** appears → use it to wipe a whole category
- **Feedback** — rate a memory helpful/unhelpful to train its trust score
- **Contradictions** — check the **Contradictions** view to spot conflicting facts

> **Tip:** Deleting a bad memory is the fastest way to fix behavior. The agent learns from what's in memory, so curating it makes future tasks better.

---

## Step 8 — Automate Everything — Watchers

Watchers let NexusMind monitor platforms 24/7 and trigger itself when something new appears. No polling by hand.

### Supported Platforms (11) — All Can Now Act Back

> **New:** Previously only GitHub could *act* (review/merge). Now **GitLab, Slack, Discord, Jira, and Email** can also act back automatically via your standing instructions — using verified open-source patterns (`httpx` for Slack/Discord/Jira/GitLab, `smtplib` + TLS on port 587 for Email).

| Platform | What It Watches | What It Can Do Back (when your instruction matches) |
|----------|-----------------|------------------------------------------------------|
| **GitHub** | New PRs, issues | Review, comment, merge / close PRs (`github_review_pr`, `github_merge_pr`, etc.) |
| **GitLab** | Merge requests, issues | List MRs and **merge** them (`gitlab_merge_mr`) |
| **Slack** | Channel messages, mentions | **Send message** / threaded reply (`slack_send_message`, `slack_reply_thread`) |
| **Discord** | Channel messages | **Send message** to a channel (`discord_send_message`) |
| **Jira** | New / updated issues | **Comment** and **transition** tickets (`jira_comment_issue`, `jira_transition_issue`) |
| **Email (IMAP)** | Inbox messages | **Send email** via SMTP — or save a draft if SMTP isn't set up (`send_email`) |
| **Reddit** | New posts in subreddits | Trigger only (read) |
| **Hacker News** | New stories, comments | Trigger only (read) |
| **RSS / Atom** | Feed items | Trigger only (read) |
| **Cron** | Scheduled tasks (e.g., daily at 9am) | Runs your configured goal directly |
| **Custom Webhook** | Any HTTP event | Runs your configured goal directly |

New skills backing this: `slack`, `discord`, `jira`, `gitlab`, `email` — each authenticates via a token in `.env` (see Step 9). The agent only uses these write-back tools when a watcher event matches one of your standing instructions.

### Create a Watcher (30 seconds)

1. Click **Watchers** in the left sidebar
2. Click **+ New Watcher**
3. Pick a platform (e.g., **Slack** or **GitHub**)
4. Fill in the config — e.g., `repo: owner/repo`, `channel: #general`, `interval: 300` (seconds)
5. Click **Save** → watcher shows as **Active**

You can **Stop**, **Start**, or **Delete** any watcher from the same page.

> **Try an auto-reply:** Create a Slack watcher for `#support`, then add a standing instruction: *"whenever a new Slack message asks for help, reply with a friendly answer and offer next steps"* — the agent will call `slack_send_message` automatically from then on. Same idea works for Discord (`discord_send_message`), Jira (`jira_comment_issue` / `jira_transition_issue` to move tickets to Done), GitLab (`gitlab_merge_mr`), and Email (`send_email`).

### The Safety Rule — Why a New Watcher Stays Quiet

This is intentional and important:

1. When a new event arrives, the watcher first checks your **standing instructions** (Step 6)
2. If **no instruction matches**, the watcher stays quiet — at most **one heads-up per 6 hours** so you're not spammed
3. Once you save a matching standing instruction, it acts automatically from then on

**Example — GitHub PR Automation End-to-End:**

1. Create a GitHub watcher for `your-org/your-repo` — at first, new PRs only produce an occasional heads-up
2. Add this standing instruction: *"when you get a PR in my repo, review it and merge it if clean, otherwise decline with a helpful comment"*
3. Now every future PR is reviewed, tested, commented, and merged/declined — fully automatically

**More end-to-end examples:**

- **Slack auto-reply:** Watcher on `#support` + instruction *"whenever a Slack message asks for help, send a helpful reply in the same channel"* → `slack_send_message` fires automatically.
- **Jira auto-triage:** Watcher on your Jira project + *"when a Jira issue is marked Needs Review, comment with findings and transition it to Done if clean"* → `jira_comment_issue` + `jira_transition_issue`.
- **GitLab auto-merge:** Watcher on `group/project` + *"when a GitLab MR is clean, merge it"* → `gitlab_merge_mr`.
- **Email auto-responder:** Watcher on inbox + *"whenever you get an email about demo requests, send a friendly reply with next steps"* → `send_email` (saves a draft to `output/email_drafts/` if SMTP isn't configured yet, so nothing is lost).

> **Exception:** Cron and Webhook watchers run their configured goal directly on trigger — they don't wait for a standing instruction.

---

## Step 9 — Manage Credentials

All secrets in one place, never exposed in plain text to the frontend.

1. Click **Credentials** in the left sidebar
2. You'll see 10 categories:

| Category | Fields | Where to Get It |
|----------|--------|-----------------|
| **AI & LLM** | Gemini API Keys, Model | https://aistudio.google.com/apikey |
| **Google Cloud** | Project ID, Region | Google Cloud Console |
| **Web Search** | Google Search API Key, CX | Google Custom Search (optional — falls back to DuckDuckGo) |
| **GitHub** | Personal Access Token | GitHub → Settings → Developer settings → PAT |
| **GitLab** | Token (`GITLAB_TOKEN`), Base URL (`GITLAB_BASE_URL`, default `https://gitlab.com`) | GitLab → Preferences → Access Tokens (needs `api` scope) |
| **Slack** | Bot Token (`SLACK_BOT_TOKEN` = `xoxb-...`) | https://api.slack.com/apps → Create app → OAuth → copy Bot Token |
| **Discord** | Bot Token (`DISCORD_BOT_TOKEN`) | https://discord.com/developers → Your App → Bot → Token |
| **Jira** | Domain (`JIRA_DOMAIN` e.g. `company.atlassian.net`), Email (`JIRA_EMAIL`), API Token (`JIRA_TOKEN`) | https://id.atlassian.com/manage-profile/security/api-tokens |
| **Email** | SMTP Server (`EMAIL_SMTP_SERVER`, or `EMAIL_IMAP_SERVER` as fallback), Port (`EMAIL_SMTP_PORT`, default `587`), Address (`EMAIL_ADDRESS`), App Password (`EMAIL_PASSWORD`) | Gmail: `smtp.gmail.com` + App Password (Google Account → Security → 2-Step → App passwords). If SMTP isn't set, `send_email` safely saves a draft to `output/email_drafts/` instead of failing silently. |
| **Telegram** | Bot Token, Chat ID | @BotFather / @userinfobot (see Step 5) |

3. Fill in what you need — values are masked (••••)
4. Click **Save All**
5. To remove one, click the **Delete** icon next to that key

Credentials are stored locally in `.env` (gitignored) and never committed. The same keys also work as **`.env` variables** if you prefer editing the file directly — add any of these lines and restart:

```
SLACK_BOT_TOKEN=xoxb-...
DISCORD_BOT_TOKEN=MTIz...
JIRA_DOMAIN=company.atlassian.net
JIRA_EMAIL=you@company.com
JIRA_TOKEN=your-jira-api-token
GITLAB_TOKEN=glpat-...
GITLAB_BASE_URL=https://gitlab.com
EMAIL_SMTP_SERVER=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_ADDRESS=you@gmail.com
EMAIL_PASSWORD=your-app-password
```

---

## Tips for Best Results

1. **Be specific** — `Search for Python web frameworks and compare Flask vs FastAPI` beats `search stuff`
2. **One task at a time** — let the trace finish before starting the next
3. **Watch the Thinking tab** — the word-to-word streaming bubble is the fastest way to see if the agent understood you
4. **Approve carefully** — only approve `run_command` / `execute_code` / `send_email` / `gitlab_merge_mr` / `jira_transition_issue` if you trust the goal (Smart mode auto-asks for these)
5. **Curate Memory** — delete wrong memories; the agent improves from what's kept
6. **Teach with standing instructions** — use "whenever..." once instead of repeating the same goal (e.g., auto-reply in Slack/Discord or transition Jira tickets)
7. **Start with Smart approval** — switch to Always/Never only when you know why

---

## Troubleshooting

### Agent is not responding
- Check your internet connection
- Verify your Gemini API key in **Credentials** → **AI & LLM** (try regenerating at https://aistudio.google.com/apikey)
- Refresh the browser (Ctrl+R / Cmd+R)
- Check the terminal for `Uvicorn running on http://0.0.0.0:8080`

### Task failed
- Open the task detail — the error message and trace explain what went wrong
- Try rephrasing more simply: split "do A and B and C" into one goal at a time
- If the task needs a local file, make sure the path exists and is readable

### Dashboard won't open
- Ensure the server is still running (look for `Uvicorn running` in the terminal)
- Try http://localhost:8080 or http://127.0.0.1:8080
- Check if another app uses port 8080: change `API_PORT` in `.env` and restart

### My watcher saw an event but did nothing
- **By design.** It checks standing instructions first. Add one in **Memory** → **Add Memory** (category: instruction). See Step 8.

### Slack / Discord / Jira / GitLab / Email action failed
- Check **Credentials** for that service — missing or wrong token is the #1 cause (`SLACK_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `JIRA_DOMAIN`/`JIRA_EMAIL`/`JIRA_TOKEN`, `GITLAB_TOKEN`/`GITLAB_BASE_URL`, `EMAIL_SMTP_SERVER`/`EMAIL_ADDRESS`/`EMAIL_PASSWORD`).
- For Slack: ensure the bot is invited to the channel. For Discord: bot needs Send Messages permission. For Jira: token needs API scope; domain must be `company.atlassian.net` without `https://`. For GitLab: token needs `api` scope and access to the project. For Email: use an App Password (not your normal password) and `EMAIL_SMTP_SERVER=smtp.gmail.com` with port `587`.
- If Email SMTP isn't set, the agent still saves a draft to `output/email_drafts/` — check there; nothing is lost.

### I keep getting Telegram notices about events
- At most one per watcher per 6 hours — it's a heads-up, not spam
- Save a standing instruction telling the agent how to handle those events to stop the notices

### Telegram not receiving approvals
- Verify **Bot Token** and **Chat ID** in **Credentials** → **Telegram**
- Send `/start` to your bot in Telegram
- Check **Settings** → **Telegram status** shows "Connected"
- Fallback: approve directly in the dashboard → **Approvals** tab

### Rate limit / quota error from Gemini
- Add a second API key in `.env` comma-separated: `GEMINI_API_KEY=key1,key2`
- Or switch model in **Credentials** → **Model** to `gemini-1.5-flash` (higher quota)
- Wait 60 seconds and retry — the client auto-rotates and backs off

---

## Need Help?

- **Video walkthrough:** [Watch 4-min Demo](https://www.youtube.com/watch?v=woSOCuzfabg)
- **Full docs:** [README.md](../README.md) and [Architecture](../README.md#architecture)
- **Issues:** https://github.com/tamimlabs/nexusmind-ai/issues
- **Email:** contact.tamimlabs@gmail.com

---

<div align="center">

**Built for the [Google All Things Agentic Hackathon](https://allthingsagentic.devpost.com) — Track: The Taskmaster**

*Run it once, walk away — it continuously monitors events and executes workflows without repeated prompting.*

</div>
