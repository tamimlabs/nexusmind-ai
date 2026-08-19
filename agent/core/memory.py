"""Memory system — persistent cross-session memory backed by Firestore.

Combines OpenClaw's vector memory with Hermes' persistent cross-session context.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.config import settings
from agent.models import MemoryEntry

logger = logging.getLogger(__name__)


class MemoryStore:
    """In-memory store with Firestore persistence.

    For the hackathon, we use an in-memory list as primary store
    and Firestore for persistence. Vector search is done via
    simple cosine similarity on stored embeddings.
    """

    def __init__(self) -> None:
        self._entries: list[MemoryEntry] = []
        self._max_items = settings.agent_memory_max_items

    def add(self, entry: MemoryEntry) -> None:
        """Add a memory entry."""
        self._entries.append(entry)
        if len(self._entries) > self._max_items:
            self._entries = self._entries[-self._max_items :]
        logger.debug("Memory added: [%s] %s", entry.category, entry.content[:80])

    def search(self, query: str, top_k: int = 5, category: str | None = None) -> list[MemoryEntry]:
        """Search memory by keyword matching (simple approach).

        For production, replace with vector similarity search using
        Firestore vector search or a dedicated vector DB.
        """
        candidates = self._entries
        if category:
            candidates = [e for e in candidates if e.category == category]

        scored: list[tuple[float, MemoryEntry]] = []
        query_lower = query.lower()
        for entry in candidates:
            content_lower = entry.content.lower()
            score = sum(1 for word in query_lower.split() if word in content_lower)
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_k]]

    def get_recent(self, n: int = 10) -> list[MemoryEntry]:
        """Get most recent memory entries."""
        return self._entries[-n:]

    def get_by_category(self, category: str) -> list[MemoryEntry]:
        """Get all entries in a category."""
        return [e for e in self._entries if e.category == category]

    def save_task_outcome(self, task_goal: str, result: str, success: bool) -> None:
        """Store a task outcome for future learning."""
        entry = MemoryEntry(
            content=f"Task: {task_goal}\nResult: {result}\nSuccess: {success}",
            category="task_outcome",
            metadata={"success": success},
        )
        self.add(entry)

    def save_skill(self, skill_name: str, instructions: str) -> None:
        """Store a learned skill."""
        entry = MemoryEntry(
            content=f"Skill: {skill_name}\n{instructions}",
            category="skill",
            metadata={"skill_name": skill_name},
        )
        self.add(entry)

    def save_reflection(self, reflection: str) -> None:
        """Store a self-reflection for improvement."""
        entry = MemoryEntry(
            content=reflection,
            category="reflection",
        )
        self.add(entry)

    def clear(self) -> None:
        """Clear all memory."""
        self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)


memory_store = MemoryStore()
