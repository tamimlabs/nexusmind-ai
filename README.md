# NexusMind AI

> Autonomous task-execution agent built for the Google "All Things Agentic" Hackathon.
> Track: **The Taskmaster** | Tech: Gemini 1.5 Flash + Google ADK + Cloud Run + Firestore + Pub/Sub

## What It Does

NexusMind AI receives goals (via API, webhooks, or dashboard) and autonomously:
1. **Plans** — decomposes goals into executable steps using Gemini Flash
2. **Executes** — runs tools (web search, file ops, code execution, data processing)
3. **Self-corrects** — on failure, analyzes errors with Gemini and retries with adjusted parameters
4. **Learns** — stores outcomes and reflections for future improvement
5. **Asks permission** — pauses for human approval on high-risk actions

## Architecture

```
                    ┌─────────────┐
                    │   Dashboard  │  (Traceability UI)
                    └──────┬──────┘
                           │
┌──────────┐    ┌──────────▼──────────┐    ┌──────────┐
│  GitHub   │───▶│    REST API         │◀───│  Stripe   │
│  Webhooks │    │    (FastAPI)        │    │  Webhooks │
└──────────┘    └──────────┬──────────┘    └──────────┘
                           │
              ┌────────────▼────────────┐
              │     Cloud Pub/Sub       │  (Event Router)
              └────────────┬────────────┘
                           │
              ┌────────────▼────────────┐
              │   Orchestrator          │  (Agent Loop)
              │   ┌──────────────────┐  │
              │   │ Gemini Planner   │──┼──▶ Step decomposition
              │   │ Tool Executor    │──┼──▶ Sandboxed execution
              │   │ Self-Correction  │──┼──▶ Error analysis + retry
              │   │ Approval Gate    │──┼──▶ Human-in-the-loop
              │   │ Memory System    │──┼──▶ Persistent context
              │   │ Trace Collector  │──┼──▶ Reasoning chain
              │   └──────────────────┘  │
              └────────────┬────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    ┌────▼────┐    ┌───────▼──────┐   ┌──────▼──────┐
    │Firestore│    │  Vertex AI   │   │  Cloud Run  │
    │(State)  │    │  (Gemini)    │   │ (Deploy)    │
    └─────────┘    └──────────────┘   └─────────────┘
```

## Key Features

| Feature | Score Impact |
|---------|-------------|

## Quick Start

```bash
# 1. Clone and setup
git clone <repo-url>
cd nexusmind-ai
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install .

# 2. Configure
cp .env.example .env
# Edit .env with your Google Cloud project ID and Gemini API key

# 3. Run locally
python -m api.main
# Open http://localhost:8080 for dashboard

# 4. Deploy to Cloud Run
bash scripts/deploy.sh
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Traceability dashboard |
| `GET` | `/api/health` | Health check |
| `POST` | `/api/tasks` | Submit a task |
| `GET` | `/api/tasks` | List recent tasks |
| `GET` | `/api/tasks/{id}` | Get task + trace |
| `GET` | `/api/approvals` | List pending approvals |
| `POST` | `/api/approvals/{id}` | Approve/deny action |
| `POST` | `/api/webhooks` | Receive external events |
| `GET` | `/api/traces` | List execution traces |

## Tech Stack

- **LLM:** Gemini 1.5 Flash (via Google GenAI SDK)
- **Framework:** Google ADK 2.x
- **Cloud:** Cloud Run + Firestore + Pub/Sub
- **API:** FastAPI + Uvicorn
- **Language:** Python 3.11+

## Patterns Adapted from Open Source

| Pattern | Source | Implementation |
|---------|--------|----------------|
| Multi-step task decomposition | OpenClaw | Gemini-powered planner |
| Tool registry + sandboxed execution | OpenClaw | Decorator-based registration + subprocess isolation |
| Persistent cross-session memory | Hermes | Firestore-backed memory store |
| Self-improvement reflection | Hermes | Post-task Gemini reflection + skill generation |
| Event-driven cron/scheduling | Hermes | Cloud Pub/Sub + Cloud Scheduler |
| Self-correcting retry loops | Custom | Error analysis → parameter adjustment → retry |
| Human-in-the-loop approval | Custom | Async approval gate for high-risk tools |
| Transparent traceability | Custom | In-memory trace collector → live dashboard |

## License

MIT
