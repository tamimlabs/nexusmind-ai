"""Memory system — persistent cross-session memory backed by Firestore.

Inspired by Hermes Agent's curated memory approach:
- Hard size limits (don't grow unbounded)
- Deduplication (don't store duplicates)
- Only meaningful entries (not routine task logs)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.config import settings
from agent.models import MemoryEntry

logger = logging.getLogger(__name__)

# Hard limits per category (inspired by Hermes' 2,200 char budget)
_MAX_ENTRIES = 100
_MAX_REFLECTIONS = 30
_MAX_TASK_OUTCOMES = 20


class MemoryStore:
    """In-memory store with Firestore persistence.

    For the hackathon, we use an in-memory list as primary store
    and Firestore for persistence. Memory is curated — not everything
    gets stored, only meaningful entries.
    """

    def __init__(self) -> None:
        self._entries: list[MemoryEntry] = []

    def add(self, entry: MemoryEntry) -> bool:
        """Add a memory entry. Returns False if rejected (duplicate/full)."""
        # Deduplication: skip if very similar content exists
        content_lower = entry.content.lower().strip()
        for existing in self._entries:
            if existing.category == entry.category:
                existing_lower = existing.content.lower().strip()
                # Exact match
                if content_lower == existing_lower:
                    return False
                # High word overlap (>85%) = duplicate
                existing_words = set(existing_lower.split())
                new_words = set(content_lower.split())
                if existing_words and new_words:
                    overlap = len(existing_words & new_words) / max(len(existing_words | new_words), 1)
                    if overlap > 0.85:
                        return False

        # Category-specific limits
        cat = entry.category
        cat_entries = [e for e in self._entries if e.category == cat]
        max_for_cat = {
            "reflection": _MAX_REFLECTIONS,
            "task_outcome": _MAX_TASK_OUTCOMES,
        }.get(cat, _MAX_ENTRIES)

        if len(cat_entries) >= max_for_cat:
            # Remove oldest entry in this category
            for i, e in enumerate(self._entries):
                if e.category == cat:
                    self._entries.pop(i)
                    break

        self._entries.append(entry)

        # Global limit
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

        logger.debug("Memory added: [%s] %s", entry.category, entry.content[:80])
        return True

    def search(self, query: str, top_k: int = 5, category: str | None = None) -> list[MemoryEntry]:
        """Search memory by keyword matching."""
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

    def save_task_outcome(self, task_goal: str, result: str, success: bool) -> bool:
        """Store a task outcome. Returns False if rejected."""
        # Truncate result to keep memory lean
        result_short = result[:200] if result else ""
        content = f"Task: {task_goal}\nResult: {result_short}\nSuccess: {success}"
        entry = MemoryEntry(
            content=content,
            category="task_outcome",
            metadata={"success": success},
        )
        return self.add(entry)

    def save_skill(self, skill_name: str, instructions: str) -> bool:
        """Store a learned skill. Returns False if rejected."""
        entry = MemoryEntry(
            content=f"Skill: {skill_name}\n{instructions[:300]}",
            category="skill",
            metadata={"skill_name": skill_name},
        )
        return self.add(entry)

    def save_reflection(self, reflection: str) -> bool:
        """Store a self-reflection. Returns False if rejected."""
        entry = MemoryEntry(
            content=reflection[:500],
            category="reflection",
        )
        return self.add(entry)

    def clear(self) -> None:
        """Clear all memory."""
        self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)


memory_store = MemoryStore()
