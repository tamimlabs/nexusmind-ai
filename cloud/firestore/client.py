"""Firestore persistence layer for tasks, memory, and skills.

Provides crash-recoverable state so the agent survives restarts
and can resume long-running tasks. Used when DATABASE_BACKEND=firestore.
"""

from __future__ import annotations

import logging
import uuid
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


def _is_available() -> bool:
    """Check if Firestore is configured and importable."""
    if not settings.google_cloud_project:
        return False
    try:
        from google.cloud import firestore  # noqa: F401
        return True
    except ImportError:
        return False


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
        if "created_at" not in task_data:
            task_data["created_at"] = datetime.now(UTC).isoformat()
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
    """Persist agent memory to Firestore.

    Implements the MemoryStore API surface used by the API layer,
    orchestrator, and watchers. Advanced features (HRR vectors, hybrid
    retrieval, trust scoring) are SQLite-only; Firestore provides
    durable category-filtered storage sufficient for Cloud Run deployments.
    """

    def __init__(self) -> None:
        self._collection = settings.firestore_collection_memory

    def _col(self):
        return _get_db().collection(self._collection)

    def _entry_to_doc(self, entry) -> dict[str, Any]:
        """Convert a MemoryEntry-like object to a Firestore document."""
        return {
            "id": entry.id,
            "content": entry.content,
            "category": entry.category,
            "metadata": entry.metadata if hasattr(entry, "metadata") else {},
            "created_at": (
                entry.created_at.isoformat()
                if hasattr(entry, "created_at") and entry.created_at
                else datetime.now(UTC).isoformat()
            ),
        }

    def add(self, entry) -> bool:
        """Add a memory entry. Returns False on duplicate."""
        doc = self._entry_to_doc(entry)
        doc_id = doc["id"]
        existing = self._col().document(doc_id).get()
        if existing.exists:
            return False
        self._col().document(doc_id).set(doc)
        return True

    def search(self, query: str, top_k: int = 5, category: str | None = None) -> list:
        """Query memories — Firestore doesn't support full-text, so filter by category."""
        q = self._col()
        if category:
            q = q.where("category", "==", category)
        q = q.order_by("created_at", direction="DESCENDING").limit(top_k * 3)
        results = []
        query_lower = query.lower()
        for doc in q.stream():
            data = doc.to_dict()
            if query_lower and query_lower not in data.get("content", "").lower():
                continue
            results.append(self._doc_to_entry(data))
            if len(results) >= top_k:
                break
        return results

    def get_recent(self, n: int = 10) -> list:
        """Most recently created entries."""
        results = []
        for doc in self._col().order_by("created_at", direction="DESCENDING").limit(n).stream():
            results.append(self._doc_to_entry(doc.to_dict()))
        return list(reversed(results))

    def get_by_category(self, category: str) -> list:
        """All entries in a category."""
        results = []
        for doc in (
            self._col()
            .where("category", "==", category)
            .order_by("created_at", direction="ASCENDING")
            .stream()
        ):
            results.append(self._doc_to_entry(doc.to_dict()))
        return results

    def save_task_outcome(self, task_goal: str, result: str, success: bool) -> bool:
        """Store a task outcome."""
        from agent.models import MemoryEntry

        result_short = result[:200] if result else ""
        content = f"Task: {task_goal}\nResult: {result_short}\nSuccess: {success}"
        entry = MemoryEntry(
            content=content,
            category="task_outcome",
            metadata={"success": success},
        )
        return self.add(entry)

    def save_reflection(self, reflection: str) -> bool:
        """Store a post-task reflection."""
        from agent.models import MemoryEntry

        entry = MemoryEntry(content=reflection[:500], category="reflection")
        return self.add(entry)

    def save_instruction(self, instruction: str) -> bool:
        """Store a standing instruction."""
        from agent.models import MemoryEntry

        entry = MemoryEntry(
            content=f"User instruction: {instruction[:500]}",
            category="instruction",
            metadata={"type": "user_instruction"},
        )
        return self.add(entry)

    def save_skill(self, skill_name: str, instructions: str) -> bool:
        """Store a skill entry."""
        from agent.models import MemoryEntry

        entry = MemoryEntry(
            content=f"Skill: {skill_name}\n{instructions[:300]}",
            category="skill",
            metadata={"skill_name": skill_name},
        )
        return self.add(entry)

    def extract_and_store(self, text: str) -> int:
        """Auto-extract preferences/decisions from text."""
        import re

        from agent.models import MemoryEntry

        if not text or len(text.strip()) < 10:
            return 0
        stored = 0
        pref_patterns = [
            re.compile(r"\bI\s+(?:prefer|like|love|use|want|need)\s+(.+)", re.IGNORECASE),
        ]
        decision_patterns = [
            re.compile(r"\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)", re.IGNORECASE),
        ]
        for pattern in pref_patterns:
            if pattern.search(text):
                entry = MemoryEntry(content=text[:400], category="user_pref")
                if self.add(entry):
                    stored += 1
                break
        for pattern in decision_patterns:
            if pattern.search(text):
                entry = MemoryEntry(content=text[:400], category="project")
                if self.add(entry):
                    stored += 1
                break
        return stored

    def record_feedback(self, entry_id: str, helpful: bool) -> dict[str, float | int]:
        """Rate a memory. Firestore doesn't have trust scoring; return static values."""
        doc = self._col().document(entry_id).get()
        if not doc.exists:
            raise KeyError(f"Memory entry not found: {entry_id}")
        return {"old_trust": 0.5, "new_trust": 0.5, "entry_id": entry_id}

    def prefetch(self, query: str, top_k: int = 5) -> str:
        """Fenced recall context for planning (simplified for Firestore)."""
        from agent.core.memory import build_memory_context_block, is_trivial_prompt

        if is_trivial_prompt(query):
            return ""
        results = self.search(query, top_k=top_k)
        if not results:
            return ""
        lines = []
        for e in results:
            trust = float(e.metadata.get("trust_score", 0.5)) if hasattr(e, "metadata") else 0.5
            lines.append(f"- [{trust:.1f}] {e.content}")
        return build_memory_context_block(
            "## Recalled memory — BACKGROUND ONLY\n"
            "These notes may come from DIFFERENT goals. Use them as context; "
            "never copy a past task's subject, branding, filenames, or code "
            "into the current goal.\n" + "\n".join(lines)
        )

    def system_prompt_block(self) -> str:
        """Static memory capability summary for the agent's system prompt."""
        return (
            "# Persistent Memory\n"
            "Active (Firestore backend). Memory persists across Cloud Run restarts.\n"
            "Hybrid retrieval and trust scoring are available with the SQLite backend."
        )

    def delete(self, entry_id: str) -> bool:
        """Delete a memory entry."""
        doc = self._col().document(entry_id).get()
        if doc.exists:
            self._col().document(entry_id).delete()
            return True
        return False

    def clear_category(self, category: str) -> int:
        """Delete ALL entries in a category."""
        docs = list(
            self._col().where("category", "==", category).stream()
        )
        for doc in docs:
            doc.reference.delete()
        return len(docs)

    def clear(self) -> None:
        """Delete all memory entries."""
        docs = list(self._col().stream())
        for doc in docs:
            doc.reference.delete()

    @property
    def size(self) -> int:
        """Count all memory entries (Firestore aggregation query)."""
        return len(list(self._col().limit(1000).stream()))

    def categories(self) -> dict[str, int]:
        """Count entries per category."""
        cats: dict[str, int] = {}
        for doc in self._col().limit(1000).stream():
            data = doc.to_dict()
            cat = data.get("category", "general")
            cats[cat] = cats.get(cat, 0) + 1
        return cats

    def close(self) -> None:
        """No-op for Firestore (stateless client)."""
        pass

    @staticmethod
    def new_entry_id() -> str:
        return str(uuid.uuid4())

    def _doc_to_entry(self, data: dict[str, Any]):
        """Convert a Firestore document to a MemoryEntry."""
        from agent.models import MemoryEntry

        created_at = None
        if data.get("created_at"):
            try:
                created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                created_at = None
        return MemoryEntry(
            id=data.get("id", ""),
            content=data.get("content", ""),
            category=data.get("category", "general"),
            metadata=data.get("metadata", {}),
            created_at=created_at or datetime.now(UTC),
        )


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


class FirestoreWatcherStateStore:
    """Persist watcher state to Firestore so watchers survive scale-to-zero.

    Cloud Run's container filesystem is ephemeral — `data/watcher_state.json`
    disappears on every scale-down/cold start, silently stopping watchers.
    This store keeps the same {watcher_id: {type, config, status}} shape in a
    durable Firestore collection instead.
    """

    def __init__(self) -> None:
        self._collection = "watcher_state"

    def _col(self):
        return _get_db().collection(self._collection)

    def load_all(self) -> dict[str, Any]:
        """Load every persisted watcher keyed by watcher_id."""
        results: dict[str, Any] = {}
        for doc in self._col().stream():
            data = doc.to_dict()
            if data:
                results[doc.id] = data
        return results

    def save_all(self, state: dict[str, Any]) -> None:
        """Upsert all active watchers and delete docs no longer active."""
        col = self._col()
        for wid, data in state.items():
            col.document(wid).set(data)
        live = set(state.keys())
        for doc in col.stream():
            if doc.id not in live:
                doc.reference.delete()
                logger.debug("Cleaned up stale watcher doc: %s", doc.id)


# Singleton instances (created lazily on first access)
firestore_tasks = FirestoreTaskStore()
firestore_memory = FirestoreMemoryStore()
firestore_skills = FirestoreSkillStore()
firestore_watcher_state = FirestoreWatcherStateStore()
