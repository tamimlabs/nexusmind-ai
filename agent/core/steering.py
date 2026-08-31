"""Steering queue — Option A (minimal, zero-risk).

In-memory per-task follow-up store for `POST /api/tasks/{id}/steer`.
No import of agent_loop/orchestrator to avoid cycles.
Option A does NOT inject into the running adaptive loop; it either
queues a live `user_steer` event for visibility or spawns a linked
follow-up task when the target is already terminal.
"""

from __future__ import annotations

import time
from typing import Any

# task_id -> list[{message, time, id}]
_store: dict[str, list[dict[str, Any]]] = {}


def push(task_id: str, message: str) -> dict[str, Any]:
    entry = {"message": message, "time": time.time(), "task_id": task_id}
    _store.setdefault(task_id, []).append(entry)
    # cap per-task history to prevent unbounded growth
    if len(_store[task_id]) > 50:
        _store[task_id] = _store[task_id][-50:]
    return entry


def list_for(task_id: str) -> list[dict[str, Any]]:
    return list(_store.get(task_id, []))


def clear(task_id: str) -> None:
    _store.pop(task_id, None)


def all_tasks() -> dict[str, list[dict[str, Any]]]:
    return {k: list(v) for k, v in _store.items()}
