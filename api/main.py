"""REST API — FastAPI application with task submission, approval, and traceability.

Powers the demo dashboard and provides endpoints for webhook triggers.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agent.config import settings
from agent.core.executor import get_pending_approvals, resolve_approval, list_tools
from agent.core.memory import memory_store
from agent.models import Task, TaskPriority, TaskStatus
from agent.observability import create_trace, get_trace, list_traces
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
        "model": settings.gemini_model,
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


# ── Tasks ─────────────────────────────────────────────────────────


@app.post("/api/tasks", response_model=TaskResponse)
async def submit_task(req: SubmitTaskRequest):
    """Submit a new task for the agent to handle."""
    from agent.orchestrator import orchestrator

    priority = TaskPriority(req.priority) if req.priority in [p.value for p in TaskPriority] else TaskPriority.MEDIUM
    task = Task(goal=req.goal, priority=priority, context=req.context)

    trace = create_trace(task.id)
    trace.start_span("task_received", kind="reasoning")
    trace.end_span("success")

    try:
        publish_task_event(task.id, task.goal, "pending", priority=task.priority.value)
    except Exception:
        logger.debug("Pub/Sub publish skipped (not configured)")

    task = await orchestrator.handle_task(task)

    for step in task.steps:
        trace.record_tool_call(
            step.tool_name or "planning",
            step.tool_args,
            step.result or step.error or "",
            step.status.value == "success",
        )

    steps_data = [
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
    ]

    return TaskResponse(
        id=task.id,
        goal=task.goal,
        status=task.status.value,
        result=task.result,
        error=task.error,
        steps_count=len(task.steps),
        steps=steps_data,
        trace=trace.get_chain(),
    )


@app.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks():
    """List recent tasks."""
    try:
        from cloud.firestore.client import firestore_tasks
        tasks = firestore_tasks.list_tasks(limit=20)
        return [TaskResponse(**t) for t in tasks]
    except Exception:
        return []


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get a specific task with its trace."""
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


@app.get("/api/approvals")
async def get_approvals():
    """Get pending approval requests."""
    return get_pending_approvals()


@app.post("/api/approvals/{step_id}")
async def resolve(step_id: str, req: ApprovalRequest):
    """Approve or deny a pending action."""
    resolve_approval(step_id, req.approved)
    return {"step_id": step_id, "status": "approved" if req.approved else "denied"}


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

    from agent.orchestrator import orchestrator
    task = await orchestrator.handle_task(task)

    return {"task_id": task.id, "status": task.status.value}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
