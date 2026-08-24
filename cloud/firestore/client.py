"""Firestore persistence layer for tasks, memory, and skills.

Provides crash-recoverable state so the agent survives restarts
and can resume long-running tasks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agent.config import settings

logger = logging.getLogger(__name__)

_client = None
_db = None


def _get_db():
    """Lazy-init Firestore client."""
    global _client, _db
    if _db is None:
        from google.cloud import firestore

        _client = firestore.Client(project=settings.google_cloud_project)
        _db = _client
    return _db


class FirestoreTaskStore:
    """Persist tasks to Firestore for crash recovery."""

    def __init__(self) -> None:
        self._collection = settings.firestore_collection_tasks

    def _col(self):
        return _get_db().collection(self._collection)

    def save_task(self, task_data: dict[str, Any]) -> None:
        """Save or update a task document."""
        doc_id = task_data.get("id", "unknown")
        task_data["updated_at"] = datetime.now(UTC).isoformat()
        self._col().document(doc_id).set(task_data, merge=True)
        logger.debug("Saved task %s to Firestore", doc_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Retrieve a task by ID."""
        doc = self._col().document(task_id).get()
        if doc.exists:
            return doc.to_dict()
        return None

    def get_pending_tasks(self) -> list[dict[str, Any]]:
        """Get all tasks that are pending or in-progress (crash recovery)."""
        results = []
        for doc in self._col().where("status", "in", ["pending", "planning", "executing"]).stream():
            results.append(doc.to_dict())
        return results

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent tasks."""
        results = []
        for doc in self._col().order_by("created_at", direction="DESCENDING").limit(limit).stream():
            results.append(doc.to_dict())
        return results

    def delete_task(self, task_id: str) -> None:
        """Delete a task document."""
        self._col().document(task_id).delete()


class FirestoreMemoryStore:
    """Persist agent memory to Firestore."""

    def __init__(self) -> None:
        self._collection = settings.firestore_collection_memory

    def _col(self):
        return _get_db().collection(self._collection)

    def save_memory(self, entry: dict[str, Any]) -> None:
        """Save a memory entry."""
        doc_id = entry.get("id", "auto")
        entry["created_at"] = datetime.now(UTC).isoformat()
        self._col().document(doc_id).set(entry, merge=True)

    def search_memories(self, category: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Query memories, optionally filtered by category."""
        query = self._col()
        if category:
            query = query.where("category", "==", category)
        results = []
        for doc in query.order_by("created_at", direction="DESCENDING").limit(limit).stream():
            results.append(doc.to_dict())
        return results

    def get_recent(self, n: int = 20) -> list[dict[str, Any]]:
        """Get most recent memories."""
        results = []
        for doc in self._col().order_by("created_at", direction="DESCENDING").limit(n).stream():
            results.append(doc.to_dict())
        return results


class FirestoreSkillStore:
    """Persist learned skills to Firestore."""

    def __init__(self) -> None:
        self._collection = settings.firestore_collection_skills

    def _col(self):
        return _get_db().collection(self._collection)

    def save_skill(self, skill_data: dict[str, Any]) -> None:
        """Save a skill."""
        name = skill_data.get("name", "unknown")
        skill_data["updated_at"] = datetime.now(UTC).isoformat()
        self._col().document(name).set(skill_data, merge=True)

    def get_skill(self, name: str) -> dict[str, Any] | None:
        doc = self._col().document(name).get()
        return doc.to_dict() if doc.exists else None

    def list_skills(self) -> list[dict[str, Any]]:
        results = []
        for doc in self._col().stream():
            results.append(doc.to_dict())
        return results


# Singleton instances
firestore_tasks = FirestoreTaskStore()
firestore_memory = FirestoreMemoryStore()
firestore_skills = FirestoreSkillStore()
