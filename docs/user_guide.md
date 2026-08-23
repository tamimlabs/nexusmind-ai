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
4. You can create up to 4 keys for faster performance

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

### Option B: Local Installation (Advanced Users)

1. Install Python from https://python.org (version 3.11 or higher)
2. Open Terminal (Windows) or Command Prompt
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

5. Open `.env` in any text editor and add your API key:

```
GEMINI_API_KEY=your-api-key-here
```

6. Start the dashboard:

```bash
python -m api.main
```

7. Open your browser and go to: http://localhost:8080

---

## Step 3: Use the Dashboard

### Submit a Task

1. Open the dashboard in your browser
2. You'll see a dark dashboard with an input box at the top
3. Type what you want the agent to do in plain English
4. Click the green **Run** button

### Example Tasks

| What to Type | What the Agent Does |
|---|---|
| "Search for the latest AI news and summarize the top 3" | Searches the web, reads articles, summarizes for you |
| "What is the weather in Tokyo right now?" | Searches the web for current weather data |
| "Read the file data.csv and tell me how many rows it has" | Reads a file and counts the rows |
| "Calculate 15% tip on a $85 bill" | Calculates the math for you |
| "Find the top 5 Python tutorials for beginners" | Searches and ranks tutorials |

### Watch the Agent Think

1. After submitting a task, click the **Thinking** tab on the right panel
2. You'll see the agent's thought process in real-time:
   - "Analyzing your goal..."
   - "Breaking down into steps using Gemini Flash..."
   - "Step 1: web_search..."
   - "Step 2: fetch_url..."
3. Each step shows what the agent is doing and the result

### Approve Risky Actions

Sometimes the agent needs to run code or shell commands. When this happens:

1. The **Approvals** tab will show a red badge
2. Click the **Approvals** tab
3. You'll see what the agent wants to do
4. Click **Approve** or **Deny**
5. The agent continues or stops based on your choice

### View Results

1. After the task completes, click on the task in the left panel
2. You'll see:
   - The **Result** — what the agent found
   - The **Steps** — each action the agent took
   - The **Trace** — detailed timing for each step

### Search Memory

1. Click **Memory** in the left sidebar
2. The agent remembers past tasks
3. Use the search box to find previous results
4. Filter by category: Reflections, Task Outcomes, Skills

### Create a Watcher (Always-Awake Mode)

Watchers let the agent monitor platforms and react to events automatically.

1. Click **Watchers** in the left sidebar
2. Click **+ New Watcher**
3. Select a platform (GitHub, Slack, Discord, etc.)
4. Enter the required credentials
5. Click **Create**
6. The watcher starts monitoring immediately

**Example: Monitor GitHub PRs**
- Select "GitHub"
- Enter repository: `owner/repo`
- Enter your GitHub token (get one from Settings > Developer Settings > Personal Access Tokens)
- Click Create
- Agent now monitors for new PRs and reviews them automatically

### Manage Credentials

1. Click **Credentials** in the left sidebar
2. You'll see all API keys organized by category
3. Fill in your keys (they're masked for security)
4. Click **Save All**
5. Credentials are stored locally in `.env` (never sent to git)

**Available Categories:**
- **AI & LLM** — Gemini API keys
- **Google Cloud** — Project ID, region
- **Web Search** — Google Search API key
- **GitHub** — Personal access token
- **GitLab** — Token, base URL
- **Slack** — Bot token
- **Discord** — Bot token
- **Jira** — Domain, email, API token
- **Email (IMAP)** — Server, address, password

---

## Step 4: Understand the Dashboard

### Left Sidebar (Navigation)

- **Tasks** — View and submit tasks
- **Memory** — Browse agent's learned knowledge
- **Approvals** — Manage pending approval requests
- **Watchers** — Create and manage event monitors
- **Settings** — Agent status and configuration
- **Credentials** — Manage all API keys and secrets

### Center Panel (Main Content)

- **Task Input** — Type your goal here
- **Task List** — All submitted tasks with status
- **Task Detail** — Full results and step breakdown

### Right Panel (Monitoring)

- **Trace** — Color-coded execution timeline
- **Thinking** — Live agent reasoning (watch in real-time)
- **Approvals** — Pending human approval requests

### Status Indicators

| Color | Meaning |
|---|---|
| Green dot | Agent is online |
| Green badge | Task completed |
| Yellow badge | Task in progress |
| Purple badge | Task is planning |
| Red badge | Task failed or approval needed |

---

## Tips for Best Results

1. **Be specific** — "Search for Python web frameworks" is better than "search stuff"
2. **One task at a time** — Wait for one task to finish before starting another
3. **Check the Thinking tab** — See what the agent is doing in real-time
4. **Approve carefully** — Only approve code execution if you trust the task
5. **Use Memory** — The agent learns from past tasks, so similar tasks get better over time

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

---

## Need Help?

- Open an issue at: https://github.com/tamimlabs/nexusmind-ai/issues
- Email: tamim@example.com
