"""REST API — FastAPI application with task submission, approval, and traceability.

Powers the demo dashboard and provides endpoints for webhook triggers.
Task execution is non-blocking — dashboard polls for live updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent.config as _cfg
from agent.core import command_gate
from agent.core.executor import (
    get_pending_approvals,
    get_trusted_tasks,
    is_task_trusted,
    list_tools,
    resolve_approval,
    trust_task,
    untrust_task,
)
from agent.core.memory import memory_store
from agent.core.skill_library import SkillError
from agent.core.skill_library import skill_library as _skill_library
from agent.models import MemoryEntry, Task, TaskPriority, TaskStatus
from agent.observability import create_trace, get_trace, list_traces
from api.credentials_routes import router as credentials_router
from api.watcher_routes import router as watcher_router
from cloud.pubsub.events import publish_task_event

logger = logging.getLogger(__name__)

# Strong references to background tasks (prevents GC cancelling mid-run)
_bg_tasks: set[asyncio.Task] = set()
# Map task_id -> background asyncio.Task for cancellation (opencode-like stop)
_bg_task_by_id: dict[str, asyncio.Task] = {}
_last_cleanup: float = 0.0

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


@app.on_event("startup")
async def check_storage_backend():
    """Log which storage backend is active and verify Firestore connectivity."""
    from agent.config import settings

    backend = settings.database_backend.lower()
    if backend == "firestore":
        try:
            from cloud.firestore.client import _is_available

            if _is_available():
                logger.info(
                    "Storage backend: Firestore (project=%s)", settings.google_cloud_project
                )
            else:
                logger.warning(
                    "DATABASE_BACKEND=firestore but Firestore not configured. "
                    "Falling back to SQLite."
                )
        except Exception:
            logger.exception("Firestore connectivity check failed")
    else:
        logger.info("Storage backend: SQLite")


@app.on_event("startup")
async def start_telegram_polling():
    """Start Telegram long-polling in background for approval buttons."""
    import httpx

    from agent.telegram import _get_api_url, process_update

    if not _cfg.settings.telegram_bot_token:
        logger.info("Telegram not configured — skipping polling")
        return

    async def _poll_loop():
        offset = 0
        logger.info("Telegram long-polling started")
        while True:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        _get_api_url("getUpdates"),
                        params={"offset": offset, "timeout": 10},
                    )
                    data = resp.json()
                    if data.get("ok"):
                        if data.get("result"):
                            for update in data["result"]:
                                offset = update["update_id"] + 1
                                try:
                                    await process_update(update)
                                except Exception:
                                    logger.exception("Error processing Telegram update")
                    else:
                        # 409 Conflict = another getUpdates instance; back off
                        err = data.get("description", "")
                        if "409" in str(data) or "Conflict" in err:
                            logger.warning(
                                "Telegram polling 409 Conflict (another instance), backing off 5s"
                            )
                            await asyncio.sleep(5)
                        else:
                            logger.warning("Telegram getUpdates not ok: %s", data)
            except asyncio.CancelledError:
                logger.info("Telegram polling stopped")
                break
            except Exception:
                logger.exception("Telegram polling error, retrying in 5s")
                await asyncio.sleep(5)

    task = asyncio.create_task(_poll_loop())
    import atexit

    atexit.register(lambda: task.cancel())


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

# Command gate provider (dependency inversion — gate never imports api)
command_gate.register_provider("recent_tasks", lambda: list(_live_tasks.values()))


def _cleanup_stale_tasks() -> None:
    """Evict tasks older than _LIVE_TTL to prevent memory leaks."""
    now = time.time()
    stale = [tid for tid, ts in _live_tasks_created.items() if now - ts > _LIVE_TTL]
    for tid in stale:
        _live_tasks.pop(tid, None)
        _live_events.pop(tid, None)
        _live_tasks_created.pop(tid, None)


_global_events: list[dict[str, Any]] = []
_GLOBAL_MAX = 600
_ws_clients: set[Any] = set()

def _emit(task_id: str, event_type: str, message: str, detail: str = "", *, source: str = "task", extra: dict[str, Any] | None = None) -> None:
    """Emit a live event for the dashboard thinking panel (per-task + global bus)."""
    entry = {
        "type": event_type,
        "message": message,
        "detail": detail[:500],
        "time": time.time(),
        "task_id": task_id,
        "source": source,
        "extra": extra or {},
    }
    if task_id not in _live_events:
        _live_events[task_id] = []
    _live_events[task_id].append(entry)
    _global_events.append(entry)
    if len(_global_events) > _GLOBAL_MAX:
        del _global_events[: len(_global_events) - _GLOBAL_MAX]
    # fan-out to WebSocket clients (best-effort)
    with contextlib.suppress(Exception):
        for q in list(_ws_clients):
            q.put_nowait(entry)


def _update_task_status(task_id: str, status: str, **extra) -> None:
    """Update live task status for polling."""
    global _last_cleanup
    if task_id not in _live_tasks:
        _live_tasks[task_id] = {}
        _live_tasks_created[task_id] = time.time()
    _live_tasks[task_id].update({"status": status, "updated_at": time.time(), **extra})
    # Periodic cleanup: every 50 tasks or every 5 minutes
    now = time.time()
    if len(_live_tasks_created) % 50 == 0 or (now - _last_cleanup) > 300:
        _cleanup_stale_tasks()
        _last_cleanup = now


async def _register_watcher_unhandled(
    watcher_id: str, watcher_type: str, summary: str, event: dict[str, Any] | None = None
) -> str:
    """Create a visible Task Panel entry for a watcher event with no matching instruction.

    Called from BaseWatcher.notify_unhandled_event — ensures nothing is silent.
    Returns task_id. Deduplicates by watcher_id + external_id.
    """
    import hashlib

    event = event or {}
    external_id = event.get("external_id", "") or hashlib.md5(summary.encode()).hexdigest()[:8]
    task_id = f"watcher-{watcher_id}-{external_id}"
    # Deduplicate: don't spam panel with same external_id
    if task_id in _live_tasks:
        return task_id
    payload = event.get("payload", {}) if isinstance(event, dict) else {}
    # Detect wrong-type instruction hint
    from agent.core.memory import memory_store

    all_instructions = [e.content[:120] for e in memory_store.get_by_category("instruction")]
    hint = ""
    if all_instructions:
        hint = f"\n\nYou have {len(all_instructions)} instruction(s) but none matched this watcher type ({watcher_type}). Example: {all_instructions[-1][:100]}"
        hint += f"\nTip: Add instruction containing keywords {watcher_type} / event_type to handle these automatically."
    else:
        hint = "\n\nNo standing instruction found. Go to Memory -> add e.g.: 'when a pr arrives, review and merge or decline with comment'"

    goal = f"⚠️ Unhandled {watcher_type} event: {summary[:200]}"
    detail = f"Watcher: {watcher_id} ({watcher_type})\nEvent: {event.get('event_type', 'unknown')}\nExternal ID: {external_id}\nPayload: {str(payload)[:400]}{hint}"

    _live_tasks[task_id] = {
        "id": task_id,
        "goal": goal,
        "status": "needs_instruction",
        "result": detail,
        "error": None,
        "steps_count": 0,
        "steps": [],
        "watcher_id": watcher_id,
        "watcher_type": watcher_type,
        "external_id": external_id,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _live_tasks_created[task_id] = time.time()
    _live_events[task_id] = [
        {
            "type": "received",
            "message": f"Watcher {watcher_id} detected: {summary[:120]}",
            "detail": detail[:500],
            "time": time.time(),
        },
        {
            "type": "error",
            "message": "No matching instruction — add one in Memory to auto-handle",
            "detail": hint[:500],
            "time": time.time(),
        },
    ]
    # Also send lightweight trace so right panel shows something
    from agent.observability import create_trace

    trace = create_trace(task_id)
    trace.start_span("watcher_unhandled", kind="reasoning")
    trace.end_span("needs_instruction")

    logger.info("Registered unhandled watcher task %s for panel: %s", task_id, summary[:80])
    return task_id


# ── Request/Response Models ───────────────────────────────────────


class SubmitTaskRequest(BaseModel):
    goal: str
    priority: str = "medium"
    context: dict[str, Any] = {}
    max_steps: int | None = None  # opencode-like elastic: override budget per task


class TaskResponse(BaseModel):
    id: str
    goal: str
    status: str
    result: str | None = None
    error: str | None = None
    steps_count: int = 0
    steps: list[dict[str, Any]] = []
    todos: list[dict[str, Any]] = []
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
    if not html_path.exists():
        return HTMLResponse("<html><body><h1>NexusMind AI</h1><p>Dashboard not found — API is running. See /docs</p></body></html>", status_code=200)
    try:
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("Failed to read dashboard.html")
        return HTMLResponse(f"<html><body><h1>Dashboard error</h1><p>{exc}</p></body></html>", status_code=500)


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
    try:
        from agent.skills.github.skill import _token_fingerprint

        github_token_fp = _token_fingerprint()
    except Exception:
        github_token_fp = "<unknown>"
    return {
        "online": True,
        "model": _cfg.settings.gemini_model,
        "tools": [{"name": t, "risk": "high" if t in high_risk else "normal"} for t in tools],
        "tools_count": len(tools),
        "memory_size": memory_store.size,
        "memory_categories": memory_store.categories(),
        "github": {
            "token_fingerprint": github_token_fp,
            "token_loaded": github_token_fp not in ("<none>", "<unknown>"),
            "default_repo": _cfg.settings.github_default_repo or None,
            "hint": (
                "A stale GITHUB_TOKEN set in your OS/shell environment overrides "
                ".env — unset it in the shell that launches this server."
            ),
        },
    }


# ── Tasks (Non-blocking) ─────────────────────────────────────────


async def _run_task_background(task_id: str, task: Task) -> None:
    """Run task in background and emit live events — opencode-like full visibility."""
    _task_started_at: float = time.time()

    def _snapshot() -> dict[str, Any]:
        elapsed = time.time() - _task_started_at
        return {
            "steps_count": len(task.steps),
            "elapsed_seconds": round(elapsed, 1),
            "steps": [
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
            "todos": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value,
                    "order": t.order,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                }
                for t in task.todos
            ],
        }

    def _live_emit(event_type: str, message: str, detail: str = "") -> None:
        extra: dict[str, Any] | None = None
        if event_type == "todo_update":
            # The dashboard's live Checklist pane must NOT poll — ship the
            # current (already-mutated) todo list on every todo_update event.
            try:
                extra = {"todos": _snapshot().get("todos", [])}
            except Exception:
                logger.debug("todo live snapshot failed", exc_info=True)
        _emit(task_id, event_type, message, detail, extra=extra)
        try:
            _update_task_status(task_id, task.status.value, goal=task.goal, **_snapshot())
        except Exception:
            logger.debug("Live status refresh failed", exc_info=True)

    from agent.config import settings

    # Explicit engine switch: the orchestrator's adaptive loop is the AUTONOMOUS
    # hackathon flagship and must never be silently swapped out because the
    # storage backend (Firestore on Cloud Run) happens to be cloud. ADK Runner
    # is now lazy through EXECUTION_ENGINE=adk ONLY.
    use_adk = getattr(settings, "execution_engine", "orchestrator").lower() == "adk"

    try:
        _emit(task_id, "thinking", "Analyzing your goal...")
        _update_task_status(task_id, "planning", goal=task.goal)

        _emit(task_id, "thinking", "Breaking down into steps using Gemini Flash...")
        await asyncio.sleep(0.3)

        if use_adk:
            # ADK Runner is the primary execution path on Cloud Run
            from cloud.vertex_ai.agent import run_task_via_adk

            result_text = await run_task_via_adk(task.goal, task_id)
            task.status = TaskStatus.COMPLETED
            task.result = result_text
            task.steps = []  # ADK handles steps internally
            _emit(task_id, "done", "Task completed via ADK Runner")
        else:
            # Orchestrator fallback for local development. Every step, todo
            # change and final verdict is streamed LIVE via the emit sink.
            from agent.orchestrator import orchestrator

            # Opencode-like: pass per-task max_steps if caller overrode it
            if req_max := getattr(task, "context", {}).get("max_steps"):
                try:
                    req_max = int(req_max)
                    if 1 <= req_max <= 200:
                        from agent.core.agent_loop import _MAX_STEPS_HARD

                        # validated — orchestrator will honor via handle_task
                        task.context["max_steps_override"] = min(req_max, _MAX_STEPS_HARD)
                except Exception:
                    pass
            task = await orchestrator.handle_task(task, emit=_live_emit)

        # Final snapshot after the task settled (status, result, steps, todos).
        _update_task_status(
            task_id,
            task.status.value,
            result=task.result,
            error=task.error,
            goal=task.goal,
            **_snapshot(),
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

        # Persist completed/failed tasks to Firestore for crash recovery
        try:
            from agent.config import settings

            if settings.database_backend.lower() == "firestore":
                from cloud.firestore.client import firestore_tasks

                firestore_tasks.save_task(
                    {
                        "id": task_id,
                        "goal": task.goal,
                        "status": task.status.value,
                        "result": task.result,
                        "error": task.error,
                        "steps_count": len(task.steps),
                        "steps": [
                            {
                                "id": s.id,
                                "description": s.description,
                                "tool_name": s.tool_name,
                                "status": s.status.value,
                                "result": s.result,
                                "error": s.error,
                                "order": s.order,
                            }
                            for s in task.steps
                        ],
                        "todos": _snapshot()["todos"],
                        "created_at": _live_tasks_created.get(task_id, time.time()),
                    }
                )
                logger.debug("Persisted task %s to Firestore", task_id)
        except Exception:
            logger.debug("Firestore task write skipped (not configured)")

    except asyncio.CancelledError:
        logger.info("Background task %s cancelled by user", task_id)
        _update_task_status(task_id, "failed", error="Cancelled by user")
        _emit(task_id, "error", "Cancelled by user")
        raise
    except Exception as exc:
        logger.exception("Background task %s failed", task_id)
        _update_task_status(task_id, "failed", error=str(exc))
        _emit(task_id, "error", f"Exception: {exc}")
    finally:
        _bg_task_by_id.pop(task_id, None)


@app.post("/api/tasks")
async def submit_task(req: SubmitTaskRequest):
    """Submit a new task — returns immediately, runs in background."""
    priority = (
        TaskPriority(req.priority)
        if req.priority in [p.value for p in TaskPriority]
        else TaskPriority.MEDIUM
    )
    ctx = dict(req.context or {})
    if req.max_steps is not None:
        try:
            ms = int(req.max_steps)
            if 1 <= ms <= 200:
                ctx["max_steps"] = ms
        except Exception:
            pass
    task = Task(goal=req.goal, priority=priority, context=ctx)

    trace = create_trace(task.id)
    trace.start_span("task_received", kind="reasoning")
    trace.end_span("success")

    _update_task_status(task.id, "pending", goal=task.goal)
    _emit(task.id, "received", f"Goal: {task.goal}")

    try:
        publish_task_event(task.id, task.goal, "pending", priority=task.priority.value)
    except Exception:
        logger.debug("Pub/Sub publish skipped (not configured)")

    # Run in background — don't await (keep a ref so GC can't cancel it)
    bg = asyncio.create_task(_run_task_background(task.id, task))
    _bg_tasks.add(bg)
    _bg_task_by_id[task.id] = bg
    bg.add_done_callback(_bg_tasks.discard)
    bg.add_done_callback(lambda t: _bg_task_by_id.pop(task.id, None))

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
    """Poll for live task updates + thinking events — opencode-like full visibility."""
    events = _live_events.get(task_id, [])
    status = _live_tasks.get(task_id, {"status": "unknown"})

    # Also check trace for any new spans
    trace = get_trace(task_id)
    trace_chain = trace.get_chain() if trace else []

    return {
        "task_id": task_id,
        "events": events,
        "events_count": len(events),
        "status": status.get("status", "unknown"),
        "result": status.get("result"),
        "error": status.get("error"),
        "steps_count": status.get("steps_count", 0),
        "elapsed_seconds": status.get("elapsed_seconds", 0),
        "steps": status.get("steps", []),
        "todos": status.get("todos", []),
        "trace": trace_chain,
    }


@app.get("/api/tasks/live/{task_id}/stream")
async def get_task_live_stream(task_id: str):
    """SSE stream for live events — opencode-style push (fallback to polling if EventSource unavailable)."""
    import json as _json

    from fastapi.responses import StreamingResponse

    async def _event_gen():
        last = 0
        # Send snapshot immediately
        status = _live_tasks.get(task_id, {"status": "unknown"})
        yield f"data: {_json.dumps({'type': 'snapshot', 'status': status.get('status'), 'steps_count': status.get('steps_count', 0)})}\n\n"
        for _ in range(600):  # up to ~10 min
            events = _live_events.get(task_id, [])
            if len(events) > last:
                for ev in events[last:]:
                    yield f"data: {_json.dumps(ev)}\n\n"
                last = len(events)
            status = _live_tasks.get(task_id, {})
            if status.get("status") in ("completed", "failed"):
                yield f"data: {_json.dumps({'type': 'done', 'status': status.get('status')})}\n\n"
                break
            await asyncio.sleep(0.5)
        yield "data: [DONE]\n\n"

    return StreamingResponse(_event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/events/live")
async def get_global_events(limit: int = 100):
    """Global live bus — every work (tasks/watchers/memory/skills/approvals) streams here."""
    lim = max(1, min(limit, 500))
    return {"events": _global_events[-lim:], "count": len(_global_events)}


@app.websocket("/api/ws")
async def websocket_global(ws: WebSocket):
    """WebSocket global fan-out for realtime dashboard (every work)."""
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _ws_clients.add(q)
    try:
        # replay last 30
        for ev in _global_events[-30:]:
            await ws.send_json(ev)
        while True:
            ev = await q.get()
            await ws.send_json(ev)
    except Exception:
        pass
    finally:
        _ws_clients.discard(q)


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a long-running task — opencode-like stop. Running loop will abort on next decision."""
    bg = _bg_task_by_id.get(task_id)
    if bg and not bg.done():
        bg.cancel()
        _emit(task_id, "error", "Cancelled by user")
        _update_task_status(task_id, "failed", error="Cancelled by user")
        return {"task_id": task_id, "cancelled": True}
    # Also mark live status even if no bg task (planning phase)
    if task_id in _live_tasks:
        _update_task_status(task_id, "failed", error="Cancelled by user")
        _emit(task_id, "error", "Cancelled by user")
        return {"task_id": task_id, "cancelled": True}
    raise HTTPException(status_code=404, detail="Task not found or already finished")


@app.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks():
    """List recent tasks — merges live tasks + Firestore."""
    live = []
    for tid, data in _live_tasks.items():
        live.append(
            TaskResponse(
                id=tid,
                goal=data.get("goal", ""),
                status=data.get("status", "unknown"),
                result=data.get("result"),
                error=data.get("error"),
                steps_count=data.get("steps_count", 0),
                steps=data.get("steps", []),
                todos=data.get("todos", []),
            )
        )

    try:
        from agent.config import settings

        if settings.database_backend.lower() == "firestore":
            from cloud.firestore.client import firestore_tasks

            fs_tasks = firestore_tasks.list_tasks(limit=20)
            live_ids = {lt.id for lt in live}
            for t in fs_tasks:
                if t.get("id") not in live_ids:
                    try:
                        live.append(TaskResponse(**t))
                    except Exception:
                        logger.debug("Skipping malformed Firestore task: %s", t.get("id"))
    except ImportError:
        pass
    except Exception:
        logger.debug("Firestore task list unavailable")

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
            todos=live.get("todos", []),
            trace=trace.get_chain() if trace else [],
        )

    trace = get_trace(task_id)
    try:
        from agent.config import settings

        if settings.database_backend.lower() == "firestore":
            from cloud.firestore.client import firestore_tasks

            task_data = firestore_tasks.get_task(task_id)
            if task_data:
                return TaskResponse(
                    **task_data,
                    trace=trace.get_chain() if trace else [],
                )
    except ImportError:
        pass
    except Exception:
        logger.debug("Firestore task read unavailable for %s", task_id)

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
    """Approve or deny a pending action.

    A granted approval also trusts the rest of its task: later risky steps in
    the same task auto-approve (one approval per task).
    """
    resolve_approval(step_id, req.approved)
    return {"step_id": step_id, "status": "approved" if req.approved else "denied"}


class TaskTrustRequest(BaseModel):
    trusted: bool


@app.get("/api/approvals/trusted")
async def get_trusted():
    """List task ids currently trusted for auto-approval (diagnostics)."""
    return {"trusted_tasks": get_trusted_tasks()}


@app.get("/api/tasks/{task_id}/trust")
async def get_task_trust(task_id: str):
    """Check whether a task's risky steps auto-approve (one approval per task)."""
    return {"task_id": task_id, "trusted": is_task_trusted(task_id)}


@app.post("/api/tasks/{task_id}/trust")
async def set_task_trust(task_id: str, req: TaskTrustRequest):
    """Explicitly trust (or untrust) a task so its risky steps auto-approve."""
    if req.trusted:
        trust_task(task_id)
    else:
        untrust_task(task_id)
    return {"task_id": task_id, "trusted": req.trusted}


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
    mode: str  # "always" | "ask_everytime" | "smart" | "never" (aliases accepted)


def _canonical_approval_mode(raw: str) -> str:
    m = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if m in {"always", "ask", "ask_everytime", "everytime", "ask_every_time", "always_ask"}:
        return "always"
    if m in {"never", "none", "no_ask", "disabled", "off"}:
        return "never"
    if m in {"smart", "auto", "intelligent"}:
        return "smart"
    return ""


@app.post("/api/approval-mode")
async def set_approval_mode(req: ApprovalModeRequest):
    """Set approval mode (always/smart/never) and persist to .env.

    Accepts aliases: 'ask_everytime', 'ask everytime', 'everytime' -> 'always'.
    """
    canonical = _canonical_approval_mode(req.mode)
    if not canonical:
        raise HTTPException(
            status_code=400,
            detail="Mode must be 'always' (or 'ask_everytime'), 'smart', or 'never'",
        )
    # Update runtime setting (canonical)
    _cfg.settings.approval_mode = canonical
    req_mode = canonical
    # Persist to .env so refresh/restart keeps the choice
    try:
        env_file = _cfg._ENV_FILE
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
        found = False
        new_lines: list[str] = []
        for line in lines:
            if line.strip().startswith("APPROVAL_MODE="):
                new_lines.append(f"APPROVAL_MODE={req_mode}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"APPROVAL_MODE={req_mode}")
        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        import os

        os.environ["APPROVAL_MODE"] = req_mode
    except Exception:
        logger.debug("Failed to persist APPROVAL_MODE to .env", exc_info=True)
    return {"mode": req_mode, "message": f"Approval mode set to {req_mode}"}


# ── Gemini Rate Limit ─────────────────────────────────────────────


@app.get("/api/rate-limit")
async def get_rate_limit():
    """Get the client-side Gemini request throttle (RPS/RPM) and tier presets."""
    return {
        "rps": _cfg.settings.gemini_rps,
        "rpm": _cfg.settings.gemini_rpm,
        "presets": _cfg.RATE_LIMIT_PRESETS,
    }


class RateLimitRequest(BaseModel):
    preset: str | None = None  # "free" | "standard" | "unlimited"
    rps: float | None = None  # when preset is omitted
    rpm: int | None = None  # when preset is omitted


def _persist_env_rate_limit(rps: float, rpm: int) -> None:
    """Persist GEMINI_RPS/GEMINI_RPM to .env so a restart keeps the choice."""
    try:
        env_file = _cfg._ENV_FILE
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
        values = {"GEMINI_RPS": str(rps), "GEMINI_RPM": str(rpm)}
        kept: list[str] = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            kept.append(f"{key}={values[key]}" if key in values else line)
        for key, value in values.items():
            if not any(k.split("=", 1)[0].strip() == key for k in kept):
                kept.append(f"{key}={value}")
        env_file.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except Exception:
        logger.debug("Failed to persist rate limit to .env", exc_info=True)


@app.post("/api/rate-limit")
async def set_rate_limit(req: RateLimitRequest):
    """Set the client-side Gemini request throttle and persist to .env.

    Accepts a tier preset ('free' | 'standard' | 'unlimited') or explicit
    rps/rpm values (0 = unlimited for that bound). Takes effect on the very
    next Gemini call — no restart needed.
    """
    preset_values = _cfg.RATE_LIMIT_PRESETS.get((req.preset or "").strip().lower())
    if req.preset and preset_values is None:
        raise HTTPException(status_code=400, detail=f"Unknown rate-limit preset '{req.preset}'")
    rps = float(req.rps) if req.rps is not None else float(preset_values.get("rps", 0))
    rpm = int(req.rpm) if req.rpm is not None else int(preset_values.get("rpm", 0))
    rps = min(max(rps, 0.0), 100.0)
    rpm = min(max(rpm, 0), 10000)

    _cfg.settings.gemini_rps = rps
    _cfg.settings.gemini_rpm = rpm
    _persist_env_rate_limit(rps, rpm)
    import os

    os.environ["GEMINI_RPS"] = str(rps)
    os.environ["GEMINI_RPM"] = str(rpm)

    effective = "unlimited" if (rps or rpm) == 0 else f"{rps} req/s, {rpm} req/min"
    return {"rps": rps, "rpm": rpm, "message": f"Rate limit set to {effective}"}


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
    webhook_url = (
        f"https://nexusmind-ai-{_cfg.settings.google_cloud_project}.a.run.app/api/telegram/webhook"
    )

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
                raise HTTPException(
                    status_code=500, detail=data.get("description", "Failed to set webhook")
                )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to connect to Telegram: {exc}"
        ) from exc


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
    """Search or list memory entries (with ids so they can be deleted)."""
    if query:
        entries = memory_store.search(query, top_k=limit, category=category or None)
    elif category:
        entries = memory_store.get_by_category(category)[:limit]
    else:
        entries = memory_store.get_recent(limit)

    return [
        {
            "id": e.id,
            "content": e.content[:500],
            "category": e.category,
            "metadata": e.metadata,
            "created_at": e.created_at.isoformat() if hasattr(e, "created_at") else "",
        }
        for e in entries
    ]


class DeleteMemoryRequest(BaseModel):
    ids: list[str] = []


@app.delete("/api/memory/{entry_id}")
async def delete_memory(entry_id: str):
    """Delete a single memory entry by id."""
    deleted = memory_store.delete(entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Memory entry not found: {entry_id}")
    return {"deleted": entry_id}


@app.post("/api/memory/delete")
async def delete_memory_bulk(req: DeleteMemoryRequest):
    """Delete multiple memory entries by id."""
    deleted = [eid for eid in req.ids if memory_store.delete(eid)]
    return {"deleted": deleted, "count": len(deleted)}


@app.post("/api/memory/clear/{category}")
async def clear_memory_category(category: str):
    """Delete ALL memory entries in a category (e.g. old instructions)."""
    removed = memory_store.clear_category(category)
    return {"cleared": removed, "category": category}


VALID_MEMORY_CATEGORIES = {
    "instruction",
    "reflection",
    "task_outcome",
    "skill",
    "general",
    "user_pref",  # auto-extracted ("I prefer...")
    "project",  # auto-extracted ("we decided to...")
}


class AddMemoryRequest(BaseModel):
    content: str
    category: str = ""  # empty = auto-detect


@app.post("/api/memory")
async def add_memory(req: AddMemoryRequest):
    """Manually add a memory entry (e.g. a standing instruction or a fact).

    Entries in category 'instruction' act as durable policy the watcher obeys.
    With no category given, standing-instruction phrasing ("whenever a PR...")
    is detected automatically.
    """
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content must not be empty")

    if req.category:
        category = req.category if req.category in VALID_MEMORY_CATEGORIES else "general"
    else:
        from agent.orchestrator import _is_standing_instruction

        category = "instruction" if _is_standing_instruction(content) else "general"

    entry = MemoryEntry(content=content, category=category)
    memory_store.add(entry)
    logger.info("Manual memory added (%s): %.80s", category, content)
    return {"id": entry.id, "category": category}


class MemoryFeedbackRequest(BaseModel):
    helpful: bool


@app.post("/api/memory/{entry_id}/feedback")
async def memory_feedback(entry_id: str, req: MemoryFeedbackRequest):
    """Rate a memory after use — trains asymmetric trust scores.

    helpful=True raises trust by 0.05; helpful=False drops it by 0.10 so
    outdated memories sink faster than good ones rise.
    """
    try:
        result = memory_store.record_feedback(entry_id, helpful=req.helpful)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Memory entry not found: {entry_id}") from None
    return result


class MemoryQueryRequest(BaseModel):
    mode: str = "search"  # search | probe | related | reason
    query: str = ""
    entity: str = ""
    entities: list[str] = []
    limit: int = 10


@app.post("/api/memory/query")
async def memory_query(req: MemoryQueryRequest):
    """Compositional memory recall (Hermes holographic pattern).

    - search: hybrid BM25 + Jaccard + HRR vector retrieval
    - probe: ALL facts where an entity plays a structural role
    - related: facts structurally connected to an entity
    - reason: multi-entity intersection (vector-space JOIN)
    """
    mode = req.mode.lower()
    limit = max(1, min(req.limit, 50))
    if mode == "search":
        entries = memory_store.search(req.query, top_k=limit) if req.query.strip() else []
    elif mode == "probe" and req.entity.strip():
        entries = memory_store.probe(req.entity.strip(), limit=limit)
    elif mode == "related" and req.entity.strip():
        entries = memory_store.related(req.entity.strip(), limit=limit)
    elif mode == "reason" and req.entities:
        entries = memory_store.reason([e for e in req.entities if e.strip()], limit=limit)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"mode '{req.mode}' requires its input (query/entity/entities)",
        )
    return [
        {
            "id": e.id,
            "content": e.content[:500],
            "category": e.category,
            "score": e.metadata.get("score"),
            "trust_score": e.metadata.get("trust_score"),
            "created_at": e.created_at.isoformat(),
        }
        for e in entries
    ]


@app.get("/api/memory/contradictions")
async def memory_contradictions(limit: int = Query(default=10)):
    """Facts that share entities but make conflicting claims (memory hygiene)."""
    return memory_store.find_contradictions(limit=max(1, min(limit, 50)))


# ── Skill Library (self-evolving procedures) ──────────────────────


def _skill_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "description": entry["description"],
        "version": entry["version"],
        "created_by": entry.get("created_by"),
        "category": entry.get("category") or "",
        "state": entry.get("state", "active"),
        "archived": entry.get("archived", False),
        "use_count": entry.get("use_count", 0),
        "patch_count": entry.get("patch_count", 0),
        "last_used_at": entry.get("last_used_at"),
        "origin_task": entry.get("origin_task"),
    }


@app.get("/api/skills")
async def list_skills(include_archived: bool = Query(default=False)):
    """Procedural skill index with usage telemetry and lifecycle states."""
    _skill_library.apply_transitions()
    return [
        _skill_summary(e) for e in _skill_library.list_skills(include_archived=include_archived)
    ]


class CreateSkillRequest(BaseModel):
    name: str
    description: str = ""  # used only when content has no frontmatter
    content: str


@app.post("/api/skills")
async def create_skill(req: CreateSkillRequest):
    """Create a procedural skill manually.

    Accepts either a full SKILL.md document (with frontmatter) or a bare
    markdown body — frontmatter is then synthesized from name/description.
    """
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Content must not be empty")
    if not content.startswith("---"):
        description = (req.description or content.splitlines()[0]).strip().replace('"', "'")
        content = (
            "---\n"
            f"name: {req.name.strip().lower()}\n"
            f'description: "{description[:1024]}"\n'
            "version: 1.0.0\n"
            "---\n\n" + content
        )
    try:
        name = _skill_library.create(name=req.name, content=content, actor="user")
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"created": name}


@app.get("/api/skills/ledger")
async def skill_ledger(limit: int = Query(default=50)):
    """Audit trail of every skill mutation (who/what/when/sha256)."""
    return _skill_library.read_ledger(limit=max(1, min(limit, 200)))


@app.get("/api/skills/{name}")
async def get_skill(name: str):
    """Full skill detail: frontmatter metadata, markdown body, and usage stats."""
    try:
        path, meta, body = _skill_library._read(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}") from None
    return {
        **_skill_summary({**meta, **_skill_library.usage_of(name), "path": str(path)}),
        "body": body,
    }


@app.delete("/api/skills/{name}")
async def delete_skill(name: str, purge: bool = Query(default=False)):
    """Archive a skill (recoverable), or hard-delete with ?purge=true."""
    try:
        removed = (
            _skill_library.delete(name, actor="user")
            if purge
            else _skill_library.archive(name, actor="user")
        )
    except SkillError as exc:  # restore collision etc.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not removed:
        raise HTTPException(status_code=404, detail=f"Skill not found: {name}")
    return {"removed": name, "purged": purge}


@app.post("/api/skills/{name}/restore")
async def restore_skill(name: str):
    """Restore the newest archived copy of a skill."""
    try:
        restored = _skill_library.restore(name, actor="user")
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not restored:
        raise HTTPException(status_code=404, detail=f"No archived copy of: {name}")
    return {"restored": name}


# ── Command Gate ───────────────────────────────────────────────────


class CommandRequest(BaseModel):
    text: str


@app.post("/api/command")
async def run_command(req: CommandRequest):
    """Zero-cost deterministic commands — handled WITHOUT any LLM call.

    Hermes/OpenClaw pattern: explicit ``/command`` syntax is intercepted
    before the agent loop. Unknown or non-command text returns
    ``handled: false`` so the caller can fall through to normal task flow.
    """
    response = await command_gate.handle_command(req.text)
    return {"handled": response is not None, "response": response}


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

    with contextlib.suppress(Exception):
        publish_task_event(task.id, task.goal, "pending", context=req.payload)

    bg2 = asyncio.create_task(_run_task_background(task.id, task))
    _bg_tasks.add(bg2)
    bg2.add_done_callback(_bg_tasks.discard)

    return {"task_id": task.id, "status": "pending"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=_cfg.settings.api_host, port=_cfg.settings.api_port)
