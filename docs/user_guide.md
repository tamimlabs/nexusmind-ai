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

Give it a goal in plain English — *"summarize today's AI news"* or *"review open PRs in my repo"* — and it **plans the steps, runs the tools, handles errors, and asks you only when something risky needs permission.** 

It can also **watch** GitHub, Slack, Jira, Reddit, Hacker News, Email and 6 more platforms 24/7 and act automatically when something new appears.

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
| **Thinking** | Live reasoning — updates every ~1.5 seconds while a task runs |
| **Approvals** | Buttons to approve or deny risky actions |

**Minimize it:** Click the **—** (minimize) in the top-right of the right panel to collapse it to a slim rail. Click the **›** chevron or **Panel** label on the rail to bring it back. Your choice is remembered after reload.

### Status Colors

| Indicator | Meaning |
|-----------|---------|
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
5. When finished, the result appears in the center panel with a full step breakdown

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

### Supported Platforms (11)

| Platform | What It Watches |
|----------|-----------------|
| **GitHub** | New PRs, issues |
| **GitLab** | Merge requests, issues |
| **Slack** | Channel messages, mentions |
| **Discord** | Channel messages |
| **Jira** | New / updated issues |
| **Reddit** | New posts in subreddits |
| **Hacker News** | New stories, comments |
| **Email (IMAP)** | Inbox messages |
| **RSS / Atom** | Feed items |
| **Cron** | Scheduled tasks (e.g., daily at 9am) |
| **Custom Webhook** | Any HTTP event |

### Create a Watcher (30 seconds)

1. Click **Watchers** in the left sidebar
2. Click **+ New Watcher**
3. Pick a platform (e.g., **GitHub**)
4. Fill in the config — e.g., `repo: owner/repo`, `interval: 300` (seconds)
5. Click **Save** → watcher shows as **Active**

You can **Stop**, **Start**, or **Delete** any watcher from the same page.

### The Safety Rule — Why a New Watcher Stays Quiet

This is intentional and important:

1. When a new event arrives, the watcher first checks your **standing instructions** (Step 6)
2. If **no instruction matches**, the watcher stays quiet — at most **one heads-up per 6 hours** so you're not spammed
3. Once you save a matching standing instruction, it acts automatically from then on

**Example — GitHub PR Automation End-to-End:**

1. Create a GitHub watcher for `your-org/your-repo` — at first, new PRs only produce an occasional heads-up
2. Add this standing instruction: *"when you get a PR in my repo, review it and merge it if clean, otherwise decline with a helpful comment"*
3. Now every future PR is reviewed, tested, commented, and merged/declined — fully automatically

> **Exception:** Cron and Webhook watchers run their configured goal directly on trigger — they don't wait for a standing instruction.

---

## Step 9 — Manage Credentials

All secrets in one place, never exposed in plain text to the frontend.

1. Click **Credentials** in the left sidebar
2. You'll see 10 categories:

| Category | Fields |
|----------|--------|
| **AI & LLM** | Gemini API Keys, Model |
| **Google Cloud** | Project ID, Region |
| **Web Search** | Google Search API Key, CX |
| **GitHub** | Personal Access Token |
| **GitLab** | Token, Base URL |
| **Slack** | Bot Token |
| **Discord** | Bot Token |
| **Jira** | Domain, Email, API Token |
| **Email** | IMAP Server, Address, Password |
| **Telegram** | Bot Token, Chat ID |

3. Fill in what you need — values are masked (••••)
4. Click **Save All**
5. To remove one, click the **Delete** icon next to that key

Credentials are stored locally in `.env` (gitignored) and never committed.

---

## Tips for Best Results

1. **Be specific** — `Search for Python web frameworks and compare Flask vs FastAPI` beats `search stuff`
2. **One task at a time** — let the trace finish before starting the next
3. **Watch the Thinking tab** — it's the best way to see if the agent understood you
4. **Approve carefully** — only approve `run_command` / `execute_code` if you trust the goal
5. **Curate Memory** — delete wrong memories; the agent improves from what's kept
6. **Teach with standing instructions** — use "whenever..." once instead of repeating the same goal
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
