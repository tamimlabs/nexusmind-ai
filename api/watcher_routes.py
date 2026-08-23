"""Watcher API endpoints - manage event-driven watchers."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/watchers", tags=["watchers"])


class CreateWatcherRequest(BaseModel):
    type: str  # "github", "cron", "webhook"
    config: dict[str, Any] = {}


class WatcherResponse(BaseModel):
    watcher_id: str
    type: str
    running: bool
    last_check: str | None = None
    events_processed: int = 0
    state: dict[str, Any] = {}


@router.get("")
async def list_watchers():
    """List all active watchers."""
    from agent.watchers.manager import list_watchers
    return list_watchers()


@router.post("")
async def create_watcher(req: CreateWatcherRequest):
    """Create and start a new watcher."""
    from agent.watchers.manager import create_watcher, start_watcher
    try:
        watcher = create_watcher(req.type, req.config)
        await start_watcher(watcher.watcher_id)
        return {"status": "created", "watcher_id": watcher.watcher_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{watcher_id}/start")
async def start(watcher_id: str):
    """Start a stopped watcher."""
    from agent.watchers.manager import start_watcher
    success = await start_watcher(watcher_id)
    if not success:
        raise HTTPException(status_code=404, detail="Watcher not found")
    return {"status": "started", "watcher_id": watcher_id}


@router.post("/{watcher_id}/stop")
async def stop(watcher_id: str):
    """Stop a running watcher."""
    from agent.watchers.manager import stop_watcher
    success = await stop_watcher(watcher_id)
    if not success:
        raise HTTPException(status_code=404, detail="Watcher not found")
    return {"status": "stopped", "watcher_id": watcher_id}


@router.delete("/{watcher_id}")
async def remove(watcher_id: str):
    """Stop and remove a watcher."""
    from agent.watchers.manager import remove_watcher
    success = await remove_watcher(watcher_id)
    if not success:
        raise HTTPException(status_code=404, detail="Watcher not found")
    return {"status": "removed", "watcher_id": watcher_id}


@router.get("/{watcher_id}")
async def get_watcher(watcher_id: str):
    """Get watcher status."""
    from agent.watchers.manager import get_watcher
    watcher = get_watcher(watcher_id)
    if not watcher:
        raise HTTPException(status_code=404, detail="Watcher not found")
    return watcher.get_status()
