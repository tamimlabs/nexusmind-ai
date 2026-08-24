# NexusMind AI — User Guide (No Coding Required)

This guide is for anyone who wants to use NexusMind AI without writing code.

---

## What You Need

1. A computer (Windows, Mac, or Linux)
2. Internet connection
3. A free Gemini API key (5 minutes to get)

---

## Step 1: Get a Free Gemini API Key

1. Go to https://aistudio.google.com/apikey
2. Click **"Create API Key"**
3. Copy the key (starts with `AI...`)
4. The free tier works fine. You can also add several keys separated by commas — the agent rotates between them automatically for better performance.

---

## Step 2: Install NexusMind AI

### Option A: Google Cloud Shell (Easiest — No Installation)

1. Go to https://shell.cloud.google.com
2. Click the **Terminal** icon (top right)
3. Paste this command and press Enter:

```bash
bash <(curl -s https://raw.githubusercontent.com/tamimlabs/nexusmind-ai/master/scripts/setup_cloud_shell.sh)
```

4. When asked, paste your Gemini API key
5. The dashboard will open automatically

### Option B: Local Installation

1. Install Python from https://python.org (version 3.11 or higher)
2. Open Terminal (Mac/Linux) or Command Prompt (Windows)
3. Run these commands one by one:

```bash
git clone https://github.com/tamimlabs/nexusmind-ai.git
cd nexusmind-ai
python -m venv .venv
.venv\Scripts\activate
pip install .
```

4. Copy the environment file:

```bash
cp .env.example .env
```

5. Open `.env` in any text editor and add your Gemini API key(s). Multiple keys can be comma-separated for rotation:

```
GEMINI_API_KEY=your-api-key-here
```

6. Start the dashboard:

```bash
python -m api.main
```

7. Open your browser and go to: http://localhost:8080

---

## Step 3: Understand the Dashboard

### Left Sidebar (Navigation)

- **Tasks** — View and submit tasks
- **Memory** — Browse and manage what the agent remembers
- **Approvals** — Manage pending approval requests
- **Watchers** — Create and manage event monitors
- **Settings** — Agent status and configuration
- **Credentials** — Manage all API keys and secrets

### Center Panel (Main Content)

- **Task Input** — Type your goal here
- **Task List** — All submitted tasks with status
- **Task Detail** — Full results and step breakdown

### Right Panel (Monitoring)

The right panel has three tabs:

- **Trace** — Color-coded execution timeline
- **Thinking** — Live agent reasoning (watch in real-time)
- **Approvals** — Pending human approval requests

### Minimize the Right Panel

1. Look at the top-right corner of the right panel — there is a minimize button
2. Click it to collapse the panel into a slim rail along the edge of the screen
3. To bring it back, click the chevron arrow or the vertical "Panel" label on the rail
4. Your choice is remembered — if you minimized the panel, it stays minimized even after you reload the browser page

### Status Indicators

| Color | Meaning |
|---|---|
| Green dot | Agent is online |
| Green badge | Task completed |
| Yellow badge | Task in progress |
| Purple badge | Task is planning |
| Red badge | Task failed or approval needed |

---

## Step 4: Submit a Task

1. Open the dashboard in your browser
2. Type what you want the agent to do in plain English in the input box
3. Click the green **Run** button
4. Watch progress in the **Thinking** tab on the right panel — it updates roughly every 1.5 seconds while the task runs

### Example Tasks

| What to Type | What the Agent Does |
|---|---|
| "Search for the latest AI news and summarize the top 3" | Searches the web, reads articles, summarizes for you |
| "Read the file data.csv and tell me how many rows it has" | Reads a file and counts the rows |
| "Calculate 15% tip on a $85 bill" | Calculates the math for you |
| "Find the top 5 Python tutorials for beginners" | Searches and ranks tutorials |
| "Look at my repository tamimlabs/nexusmind-ai and review the open PRs" | Resolves your repository, reviews each open pull request, and reports back. If your goal said to merge or reject, it applies those decisions too. |

---

## Step 5: Approvals (Staying in Control)

The agent has a **Smart Approval** system that decides when to ask you.

### 3 Approval Modes (Settings page)

| Mode | Behavior |
|---|---|
| **Smart** (default) | Auto-approves safe reads (`ls`, `cat`, `git status`, ...) and asks you before risky things (deleting files, sudo, deploys) |
| **Always** | Asks you about everything |
| **Never** | Asks nothing, auto-approves everything |

### Approve from Your Phone (Telegram Bot)

Instead of watching the dashboard, you can approve from Telegram:

1. Go to the **Credentials** page
2. Find the **Telegram** section
3. Get a bot token:
   - Open Telegram and message **@BotFather**
   - Send `/newbot` and follow the steps
   - Copy the bot token it gives you
4. Get your chat ID:
   - Message **@userinfobot** on Telegram
   - Copy the chat ID it shows
5. Paste both into the Credentials page and click **Save All**

Now when the agent needs approval, you get a Telegram message with **Approve/Deny buttons**. Tap one and the agent continues automatically.

---

## Step 6: Standing Instructions (Teach It Once, It Does It Forever)

Normally, a goal you type runs once and that's it. But NexusMind can also follow **standing instructions** — lasting rules that apply automatically to future events.

### How It Works

1. Phrase your goal as a rule, starting with words like **"whenever"**, **"when you get..."**, **"every time"**, or **"from now on"**
2. Instead of running it once, the agent **saves it as a standing instruction**
3. The agent confirms the instruction was saved (via Telegram or the dashboard)
4. From then on, whenever a matching event happens (for example, from a watcher), the agent follows that rule automatically — no need to ask again

### Example

Type this:

> "when you get a PR in my repo, review it and merge it if clean, otherwise decline with a helpful comment"

From now on, every new pull request gets reviewed, merged if clean, or declined with a helpful comment — automatically.

### Adding One Manually

You don't have to phrase things as chat messages. You can add a standing instruction by hand:

1. Go to the **Memory** page
2. Use the **Add Memory** form at the top
3. Type your instruction
4. Pick the category **"instruction"** (or leave the category blank — the agent detects instruction-style wording automatically)

---

## Step 7: Memory Page (Manage What the Agent Remembers)

The Memory page lists the agent's recent memories so you stay in control.

1. Click **Memory** in the left sidebar
2. You'll see recent memories grouped by category:
   - **Instructions** — standing rules the agent follows
   - **Reflections** — lessons the agent learned from its own work
   - **Task Outcomes** — what happened in past tasks
   - **Skills** — reusable abilities the agent built
3. Use the **Search box** to find specific memories
4. Each entry has a **Delete button** — remove wrong or outdated memories directly to improve behavior
5. When you select a filter tab (e.g., Instructions), a **"Clear all \<category\>"** button appears — useful for wiping a whole category at once
6. Add new memories (like standing instructions) with the **Add Memory** form at the top

---

## Step 8: Watchers (Event Monitoring) and the Safety Rule

Watchers let the agent monitor platforms for events automatically.

### Create a Watcher

1. Click **Watchers** in the left sidebar
2. Click **+ New Watcher**
3. Pick a platform
4. Fill in the configuration
5. Save

There are **11 platform types**: GitHub, GitLab, Slack, Discord, Jira, Reddit, Hacker News, Email, RSS feeds, Cron schedules, and custom Webhooks.

### Important: Watchers Wait for Your Orders

A brand-new watcher does **not** act on events by itself. This is a deliberate safety feature:

1. When an event arrives, the watcher first checks your **standing instructions**
2. If no instruction matches, the watcher stays quiet — at most it sends you **one heads-up per 6 hours** so you know something happened
3. Once you tell it what to do (by saving a standing instruction), it acts automatically from then on

So if a PR arrives and nothing happens, that's not a bug — the agent is waiting for your orders.

**Exception:** Cron and Webhook watchers run their owner-configured goal directly when triggered.

### Example: Monitor GitHub PRs

1. Create a GitHub watcher for your repository (`owner/repo`)
2. At first, new PRs only produce an occasional heads-up
3. Add a standing instruction like: *"when you get a PR in my repo, review it and merge it if clean, otherwise decline with a helpful comment"*
4. Now every future PR is handled exactly the way you asked

---

## Step 9: Credentials

1. Click **Credentials** in the left sidebar
2. You'll see API keys organized into 10 categories
3. Fill in your keys — values are masked for security
4. Click **Save All**
5. Credentials are stored locally in your `.env` file and are never committed to git

---

## Tips for Best Results

1. **Be specific** — "Search for Python web frameworks" is better than "search stuff"
2. **One task at a time** — Wait for one task to finish before starting another
3. **Check the Thinking tab** — See what the agent is doing in real-time
4. **Approve carefully** — Only approve risky actions if you trust the task
5. **Use Memory** — Delete wrong or outdated memories to improve behavior; the agent learns from past tasks, so similar tasks get better over time
6. **Teach with standing instructions** — Set rules once ("whenever...", "every time...") instead of repeating yourself

---

## Troubleshooting

### "Agent is not responding"

- Check your internet connection
- Make sure your API key is valid
- Try refreshing the browser page

### "Task failed"

- Look at the error message in the task detail
- Try rephrasing your goal more simply
- Check if the task requires files that exist on your computer

### "Dashboard won't open"

- Make sure the server is running (you should see "Uvicorn running" in the terminal)
- Try going to http://localhost:8080 again
- Check if another program is using port 8080

### "My watcher saw an event but did nothing"

- This is by design. A new watcher checks your standing instructions first and stays quiet if none match
- Go to the Memory page and add a standing instruction telling it what to do

### "I keep getting Telegram notices about events"

- The agent sends at most one notice per watcher per 6 hours
- To make the notices stop, save a standing instruction telling the agent how to handle those events

---

## Need Help?

- Open an issue at: https://github.com/tamimlabs/nexusmind-ai/issues
- Email: contact.tamimlabs@gmail.com
