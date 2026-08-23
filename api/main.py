"""REST API — FastAPI application with task submission, approval, and traceability.

Powers the demo dashboard and provides endpoints for webhook triggers.
Task execution is non-blocking — dashboard polls for live updates.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent.config as _cfg
from agent.core.executor import get_pending_approvals, list_tools, resolve_approval
from agent.core.memory import memory_store
from agent.models import Task, TaskPriority, TaskStatus
from agent.observability import create_trace, get_trace, list_traces
from api.credentials_routes import router as credentials_router
from api.watcher_routes import router as watcher_router
from cloud.pubsub.events import publish_task_event

logger = logging.getLogger(__name__)

app = FastAPI(
    title="NexusMind AI",
    description="Autonomous task-execution agent on Google Cloud",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Event-driven watcher routes (always-awake agent)
app.include_router(watcher_router)

# Credentials management routes
app.include_router(credentials_router)


@app.on_event("startup")
async def restore_watchers_on_startup():
    """Restore persisted watchers on startup so the agent stays always-awake."""
    try:
        from agent.watchers.manager import restore_watchers
        result = await restore_watchers()
        logger.info("Watcher restoration complete: %s", result)
    except ImportError:
        logger.warning("Watcher manager unavailable; skipping watcher restoration")
    except Exception:
        logger.exception("Failed to restore watchers on startup")

# Serve docs folder for logos
docs_path = pathlib.Path(__file__).parent.parent / "docs"
if docs_path.exists():
    app.mount("/docs", StaticFiles(directory=str(docs_path)), name="docs")


# ── Live Event Store ──────────────────────────────────────────────

# Per-task live events that the dashboard polls
_live_events: dict[str, list[dict[str, Any]]] = {}
_live_tasks: dict[str, dict[str, Any]] = {}
_live_tasks_created: dict[str, float] = {}  # task_id -> creation timestamp
_LIVE_TTL = 3600  # Evict completed tasks after 1 hour


def _cleanup_stale_tasks() -> None:
    """Evict tasks older than _LIVE_TTL to prevent memory leaks."""
    now = time.time()
    stale = [tid for tid, ts in _live_tasks_created.items() if now - ts > _LIVE_TTL]
    for tid in stale:
        _live_tasks.pop(tid, None)
        _live_events.pop(tid, None)
        _live_tasks_created.pop(tid, None)


def _emit(task_id: str, event_type: str, message: str, detail: str = "") -> None:
    """Emit a live event for the dashboard thinking panel."""
    if task_id not in _live_events:
        _live_events[task_id] = []
    _live_events[task_id].append({
        "type": event_type,
        "message": message,
        "detail": detail[:500],
        "time": time.time(),
    })


def _update_task_status(task_id: str, status: str, **extra) -> None:
    """Update live task status for polling."""
    if task_id not in _live_tasks:
        _live_tasks[task_id] = {}
        _live_tasks_created[task_id] = time.time()
    _live_tasks[task_id].update({"status": status, "updated_at": time.time(), **extra})
    # Periodic cleanup (every 50 new tasks)
    if len(_live_tasks_created) % 50 == 0:
        _cleanup_stale_tasks()


# ── Request/Response Models ───────────────────────────────────────


class SubmitTaskRequest(BaseModel):
    goal: str
    priority: str = "medium"
    context: dict[str, Any] = {}


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: str
    result: str | None = None
    error: str | None = None
    steps_count: int = 0
    steps: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []


class ApprovalRequest(BaseModel):
    approved: bool


class WebhookPayload(BaseModel):
    event_type: str
    payload: dict[str, Any] = {}


# ── Dashboard ─────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the traceability dashboard."""
    html_path = pathlib.Path(__file__).parent / "dashboard.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ── Health ────────────────────────────────────────────────────────


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "nexusmind-ai", "version": "0.1.0"}


# ── Agent Status ──────────────────────────────────────────────────


@app.get("/api/agent/status")
async def agent_status():
    """Return agent status for the dashboard sidebar."""
    tools = list_tools()
    high_risk = ["execute_code", "run_command"]
    return {
        "online": True,
        "model": _cfg.settings.gemini_model,
        "tools": [
            {"name": t, "risk": "high" if t in high_risk else "normal"}
            for t in tools
        ],
        "tools_count": len(tools),
        "memory_size": memory_store.size,
        "memory_categories": {
            cat: len(memory_store.get_by_category(cat))
            for cat in ["task_outcome", "reflection", "skill"]
        },
    }


# ── Tasks (Non-blocking) ─────────────────────────────────────────


async def _run_task_background(task_id: str, task: Task) -> None:
    """Run task in background and emit live events."""
    from agent.orchestrator import orchestrator

    try:
        _emit(task_id, "thinking", "Analyzing your goal...")
        _update_task_status(task_id, "planning", goal=task.goal)

        _emit(task_id, "thinking", "Breaking down into steps using Gemini Flash...")
        await asyncio.sleep(0.3)

        task = await orchestrator.handle_task(task)

        # Emit per-step events so thinking panel shows progress
        if task.steps:
            _emit(task_id, "thinking", f"Planned {len(task.steps)} steps")
            for i, step in enumerate(task.steps):
                status_type = {"success": "done", "failed": "error"}.get(step.status.value, "thinking")
                step_msg = f"Step {i+1}: {step.description[:80]}"
                if step.tool_name:
                    step_msg += f" [{step.tool_name}]"
                _emit(task_id, status_type, step_msg,
                      step.result[:200] if step.result else (step.error[:200] if step.error else ""))

        _update_task_status(
            task_id,
            task.status.value,
            result=task.result,
            error=task.error,
            goal=task.goal,
            steps_count=len(task.steps),
            steps=[
                {
                    "id": s.id,
                    "description": s.description,
                    "tool_name": s.tool_name,
                    "tool_args": s.tool_args,
                    "status": s.status.value,
                    "result": s.result,
                    "error": s.error,
                    "order": s.order,
                }
                for s in task.steps
            ],
        )

        trace = get_trace(task_id)
        if trace:
            for step in task.steps:
                trace.record_tool_call(
                    step.tool_name or "planning",
                    step.tool_args,
                    step.result or step.error or "",
                    step.status.value == "success",
                )

        if task.status == TaskStatus.COMPLETED:
            _emit(task_id, "done", "Task completed successfully")
        else:
            _emit(task_id, "error", f"Task failed: {task.error or 'Unknown error'}")

    except Exception as exc:
        logger.exception("Background task %s failed", task_id)
        _update_task_status(task_id, "failed", error=str(exc))
        _emit(task_id, "error", f"Exception: {exc}")


@app.post("/api/tasks")
async def submit_task(req: SubmitTaskRequest):
    """Submit a new task — returns immediately, runs in background."""
    priority = TaskPriority(req.priority) if req.priority in [p.value for p in TaskPriority] else TaskPriority.MEDIUM
    task = Task(goal=req.goal, priority=priority, context=req.context)

    trace = create_trace(task.id)
    trace.start_span("task_received", kind="reasoning")
    trace.end_span("success")

    _update_task_status(task.id, "pending", goal=task.goal)
    _emit(task.id, "received", f"Goal: {task.goal}")

    try:
        publish_task_event(task.id, task.goal, "pending", priority=task.priority.value)
    except Exception:
        logger.debug("Pub/Sub publish skipped (not configured)")

    # Run in background — don't await
    asyncio.create_task(_run_task_background(task.id, task))

    return {
        "id": task.id,
        "goal": task.goal,
        "status": "pending",
        "result": None,
        "error": None,
        "steps_count": 0,
        "steps": [],
        "trace": [],
    }


@app.get("/api/tasks/live/{task_id}")
async def get_task_live(task_id: str):
    """Poll for live task updates + thinking events."""
    events = _live_events.get(task_id, [])
    status = _live_tasks.get(task_id, {"status": "unknown"})

    # Also check trace for any new spans
    trace = get_trace(task_id)
    trace_chain = trace.get_chain() if trace else []

    return {
        "task_id": task_id,
        "events": events,
        "status": status.get("status", "unknown"),
        "result": status.get("result"),
        "error": status.get("error"),
        "steps_count": status.get("steps_count", 0),
        "steps": status.get("steps", []),
        "trace": trace_chain,
    }


@app.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks():
    """List recent tasks — merges live tasks + Firestore."""
    live = []
    for tid, data in _live_tasks.items():
        live.append(TaskResponse(
            id=tid,
            goal=data.get("goal", ""),
            status=data.get("status", "unknown"),
            result=data.get("result"),
            error=data.get("error"),
            steps_count=data.get("steps_count", 0),
            steps=data.get("steps", []),
        ))

    try:
        from cloud.firestore.client import firestore_tasks
        fs_tasks = firestore_tasks.list_tasks(limit=20)
        for t in fs_tasks:
            if t.get("id") not in {lt.id for lt in live}:
                live.append(TaskResponse(**t))
    except Exception:
        pass

    return live


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get a specific task with its trace."""
    # Check live tasks first
    live = _live_tasks.get(task_id)
    if live:
        trace = get_trace(task_id)
        return TaskResponse(
            id=task_id,
            goal=live.get("goal", ""),
            status=live.get("status", "unknown"),
            result=live.get("result"),
            error=live.get("error"),
            steps_count=live.get("steps_count", 0),
            steps=live.get("steps", []),
            trace=trace.get_chain() if trace else [],
        )

    trace = get_trace(task_id)
    try:
        from cloud.firestore.client import firestore_tasks
        task_data = firestore_tasks.get_task(task_id)
        if task_data:
            return TaskResponse(
                **task_data,
                trace=trace.get_chain() if trace else [],
            )
    except Exception:
        pass

    if trace:
        return TaskResponse(
            id=task_id,
            goal="",
            status="completed",
            trace=trace.get_chain(),
        )
    raise HTTPException(status_code=404, detail="Task not found")


# ── Human-in-the-Loop ─────────────────────────────────────────────


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """Delete a task from live store."""
    _live_tasks.pop(task_id, None)
    _live_events.pop(task_id, None)
    from agent.observability import _traces
    _traces.pop(task_id, None)
    return {"deleted": True, "task_id": task_id}


@app.get("/api/approvals")
async def get_approvals():
    """Get pending approval requests."""
    return get_pending_approvals()


@app.post("/api/approvals/{step_id}")
async def resolve(step_id: str, req: ApprovalRequest):
    """Approve or deny a pending action."""
    resolve_approval(step_id, req.approved)
    return {"step_id": step_id, "status": "approved" if req.approved else "denied"}


# ── Approval Mode ─────────────────────────────────────────────────


@app.get("/api/approval-mode")
async def get_approval_mode():
    """Get current approval mode and Telegram status."""
    from agent.telegram import get_config_status
    return {
        "mode": _cfg.settings.approval_mode,
        "telegram": get_config_status(),
    }


class ApprovalModeRequest(BaseModel):
    mode: str  # "always", "smart", "never"


@app.post("/api/approval-mode")
async def set_approval_mode(req: ApprovalModeRequest):
    """Set approval mode (always/smart/never)."""
    if req.mode not in ("always", "smart", "never"):
        raise HTTPException(status_code=400, detail="Mode must be 'always', 'smart', or 'never'")
    # Update runtime setting
    _cfg.settings.approval_mode = req.mode
    return {"mode": req.mode, "message": f"Approval mode set to {req.mode}"}


# ── Telegram Webhook ──────────────────────────────────────────────


@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates (callback queries, messages).

    Set this URL as your Telegram bot webhook:
    https://your-domain.com/api/telegram/webhook
    """
    try:
        body = await request.json()
        from agent.telegram import process_update
        await process_update(body)
        return {"ok": True}
    except Exception as exc:
        logger.exception("Telegram webhook error")
        return {"ok": False, "error": str(exc)}


@app.post("/api/telegram/setup")
async def setup_telegram_webhook():
    """Set up Telegram webhook automatically."""
    import httpx

    from agent.telegram import _get_api_url

    if not _cfg.settings.telegram_bot_token:
        raise HTTPException(status_code=400, detail="Telegram bot token not configured")

    # Determine webhook URL from request
    webhook_url = f"https://nexusmind-ai-{_cfg.settings.google_cloud_project}.a.run.app/api/telegram/webhook"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _get_api_url("setWebhook"),
                data={"url": webhook_url},
            )
            data = resp.json()
            if data.get("ok"):
                return {"status": "webhook_set", "url": webhook_url}
            else:
                raise HTTPException(status_code=500, detail=data.get("description", "Failed to set webhook"))
    except httpx.RequestError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to connect to Telegram: {exc}") from exc


@app.get("/api/telegram/status")
async def telegram_status():
    """Get Telegram bot connection status."""
    from agent.telegram import get_config_status, is_configured
    status = get_config_status()
    status["connected"] = is_configured()
    return status


# ── Traceability ──────────────────────────────────────────────────


@app.get("/api/traces")
async def traces():
    """List all execution traces."""
    return list_traces()


@app.get("/api/traces/{task_id}")
async def trace_detail(task_id: str):
    """Get detailed trace for a task."""
    trace = get_trace(task_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"task_id": task_id, "chain": trace.get_chain(), "summary": trace.get_summary()}


# ── Memory ────────────────────────────────────────────────────────


@app.get("/api/memory")
async def list_memory(
    query: str = Query(default=""),
    category: str = Query(default=""),
    limit: int = Query(default=20),
):
    """Search or list memory entries."""
    if query:
        entries = memory_store.search(query, top_k=limit, category=category or None)
    elif category:
        entries = memory_store.get_by_category(category)[:limit]
    else:
        entries = memory_store.get_recent(limit)

    return [
        {
            "content": e.content[:500],
            "category": e.category,
            "metadata": e.metadata,
            "created_at": e.created_at.isoformat() if hasattr(e, "created_at") else "",
        }
        for e in entries
    ]


# ── Watchers ──────────────────────────────────────────────────────


@app.post("/api/watchers/restore")
async def restore_watchers_endpoint():
    """Manually restore persisted watchers (also runs automatically on startup)."""
    try:
        from agent.watchers.manager import restore_watchers
        result = await restore_watchers()
        payload = result if isinstance(result, dict) else {"result": result}
        return {"status": "restored", **payload}
    except ImportError:
        raise HTTPException(status_code=503, detail="Watcher manager not available") from None
    except Exception as exc:
        logger.exception("Watcher restoration failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Webhook Receiver ──────────────────────────────────────────────


@app.post("/api/webhooks")
async def receive_webhook(req: WebhookPayload):
    """Receive external webhooks and route them as agent tasks."""
    goal = f"Handle {req.event_type} event: {req.payload}"
    task = Task(goal=goal, context={"event_type": req.event_type, **req.payload})

    try:
        publish_task_event(task.id, task.goal, "pending", context=req.payload)
    except Exception:
        pass

    asyncio.create_task(_run_task_background(task.id, task))

    return {"task_id": task.id, "status": "pending"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=_cfg.settings.api_host, port=_cfg.settings.api_port)
