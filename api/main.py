"""REST API — FastAPI application with task submission, approval, and traceability.

Powers the demo dashboard and provides endpoints for webhook triggers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.config import settings
from agent.core.executor import get_pending_approvals, resolve_approval
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
    trace: list[dict[str, Any]] = []


class ApprovalRequest(BaseModel):
    approved: bool


class WebhookPayload(BaseModel):
    event_type: str
    payload: dict[str, Any] = {}


# ── Endpoints ─────────────────────────────────────────────────────


@app.get("/")
async def root():
    """Serve the traceability dashboard."""
    return HTMLResponse(dashboard_html())


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "nexusmind-ai", "version": "0.1.0"}


@app.post("/api/tasks", response_model=TaskResponse)
async def submit_task(req: SubmitTaskRequest):
    """Submit a new task for the agent to handle."""
    from agent.orchestrator import orchestrator

    priority = TaskPriority(req.priority) if req.priority in [p.value for p in TaskPriority] else TaskPriority.MEDIUM
    task = Task(goal=req.goal, priority=priority, context=req.context)

    # Create trace
    trace = create_trace(task.id)
    trace.start_span("task_received", kind="reasoning")
    trace.end_span("success")

    # Publish event
    try:
        publish_task_event(task.id, task.goal, "pending", priority=task.priority.value)
    except Exception:
        logger.debug("Pub/Sub publish skipped (not configured)")

    # Execute
    task = await orchestrator.handle_task(task)

    # Record trace
    for step in task.steps:
        trace.record_tool_call(
            step.tool_name or "planning",
            step.tool_args,
            step.result or step.error or "",
            step.status.value == "success",
        )

    return TaskResponse(
        id=task.id,
        goal=task.goal,
        status=task.status.value,
        result=task.result,
        error=task.error,
        steps_count=len(task.steps),
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


# ── Webhook Receiver ──────────────────────────────────────────────


@app.post("/api/webhooks")
async def receive_webhook(req: WebhookPayload):
    """Receive external webhooks and route them as agent tasks."""
    from cloud.pubsub.events import EVENT_CUSTOM

    # Convert webhook to agent task
    goal = f"Handle {req.event_type} event: {req.payload}"
    task = Task(goal=goal, context={"event_type": req.event_type, **req.payload})

    try:
        publish_task_event(task.id, task.goal, "pending", context=req.payload)
    except Exception:
        pass

    from agent.orchestrator import orchestrator
    task = await orchestrator.handle_task(task)

    return {"task_id": task.id, "status": task.status.value}


# ── Dashboard ─────────────────────────────────────────────────────


def dashboard_html() -> str:
    """Return the traceability dashboard HTML."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NexusMind AI — Traceability Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'SF Mono', 'Fira Code', monospace; background: #0a0a0a; color: #e0e0e0; }
        .header { background: #111; border-bottom: 1px solid #333; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; }
        .header h1 { font-size: 18px; color: #4ade80; }
        .header .status { color: #888; font-size: 12px; }
        .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
        .input-section { background: #111; border: 1px solid #333; border-radius: 8px; padding: 20px; margin-bottom: 24px; }
        .input-section h2 { font-size: 14px; color: #888; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
        .input-row { display: flex; gap: 12px; }
        .input-row input { flex: 1; background: #1a1a1a; border: 1px solid #444; border-radius: 6px; padding: 10px 14px; color: #fff; font-family: inherit; font-size: 14px; }
        .input-row input:focus { outline: none; border-color: #4ade80; }
        .btn { background: #4ade80; color: #000; border: none; border-radius: 6px; padding: 10px 20px; font-weight: 600; cursor: pointer; font-family: inherit; font-size: 14px; }
        .btn:hover { background: #22c55e; }
        .btn.danger { background: #ef4444; }
        .btn.danger:hover { background: #dc2626; }
        .tasks-list { display: flex; flex-direction: column; gap: 12px; }
        .task-card { background: #111; border: 1px solid #333; border-radius: 8px; padding: 16px; cursor: pointer; transition: border-color 0.2s; }
        .task-card:hover { border-color: #4ade80; }
        .task-card .task-header { display: flex; justify-content: space-between; margin-bottom: 8px; }
        .task-card .goal { font-size: 14px; color: #fff; }
        .task-card .status { font-size: 12px; padding: 2px 8px; border-radius: 4px; }
        .status-completed { background: #166534; color: #4ade80; }
        .status-failed { background: #7f1d1d; color: #fca5a5; }
        .status-executing { background: #713f12; color: #fbbf24; }
        .status-pending { background: #1e3a5f; color: #93c5fd; }
        .trace-panel { background: #111; border: 1px solid #333; border-radius: 8px; padding: 20px; margin-top: 24px; }
        .trace-panel h2 { font-size: 14px; color: #888; margin-bottom: 16px; text-transform: uppercase; letter-spacing: 1px; }
        .span { border-left: 2px solid #444; padding: 8px 16px; margin-bottom: 4px; }
        .span.tool_call { border-color: #60a5fa; }
        .span.reasoning { border-color: #a78bfa; }
        .span.approval { border-color: #fbbf24; }
        .span.error { border-color: #ef4444; }
        .span .span-name { font-size: 12px; color: #888; }
        .span .span-detail { font-size: 13px; color: #ccc; margin-top: 4px; }
        .span .span-time { font-size: 11px; color: #666; }
        .empty { color: #555; font-style: italic; text-align: center; padding: 40px; }
        #loading { display: none; color: #4ade80; padding: 20px; text-align: center; }
    </style>
</head>
<body>
    <div class="header">
        <h1>NexusMind AI</h1>
        <div class="status">Traceability Dashboard | Google Cloud</div>
    </div>
    <div class="container">
        <div class="input-section">
            <h2>Submit Task</h2>
            <div class="input-row">
                <input type="text" id="goalInput" placeholder="Enter a goal for the agent..." />
                <button class="btn" onclick="submitTask()">Execute</button>
            </div>
        </div>
        <div id="loading">Agent is thinking...</div>
        <div id="tasksList" class="tasks-list"></div>
        <div id="tracePanel" class="trace-panel" style="display:none">
            <h2>Reasoning Chain</h2>
            <div id="traceContent"></div>
        </div>
    </div>
    <script>
        async function submitTask() {
            const goal = document.getElementById('goalInput').value.trim();
            if (!goal) return;
            document.getElementById('loading').style.display = 'block';
            document.getElementById('tasksList').innerHTML = '';
            try {
                const res = await fetch('/api/tasks', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({goal})
                });
                const task = await res.json();
                document.getElementById('loading').style.display = 'none';
                renderTask(task);
                if (task.trace && task.trace.length > 0) renderTrace(task.trace);
            } catch(e) {
                document.getElementById('loading').style.display = 'none';
                document.getElementById('tasksList').innerHTML = '<div class="empty">Error: ' + e.message + '</div>';
            }
        }
        function renderTask(task) {
            const statusClass = 'status-' + task.status;
            document.getElementById('tasksList').innerHTML = `
                <div class="task-card">
                    <div class="task-header">
                        <div class="goal">${task.goal}</div>
                        <span class="status ${statusClass}">${task.status}</span>
                    </div>
                    <div style="font-size:12px;color:#888">Steps: ${task.steps_count} | ID: ${task.id.slice(0,8)}</div>
                    ${task.result ? '<div style="margin-top:8px;font-size:13px;color:#4ade80">'+task.result.slice(0,300)+'</div>' : ''}
                    ${task.error ? '<div style="margin-top:8px;font-size:13px;color:#fca5a5">'+task.error+'</div>' : ''}
                </div>`;
        }
        function renderTrace(spans) {
            const panel = document.getElementById('tracePanel');
            const content = document.getElementById('traceContent');
            panel.style.display = 'block';
            content.innerHTML = spans.map(s => `
                <div class="span ${s.kind}">
                    <div class="span-name">${s.name} <span class="span-time">${s.duration_ms}ms</span></div>
                    <div class="span-detail">${JSON.stringify(s.output || s.input || {}).slice(0,200)}</div>
                </div>`).join('');
        }
        document.getElementById('goalInput').addEventListener('keypress', e => { if(e.key==='Enter') submitTask(); });
    </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
