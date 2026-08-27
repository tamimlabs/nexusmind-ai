"""Agent memory — persistent cross-session fact store.

Architecture adapted from Hermes Agent's memory system (agent/memory_provider.py,
agent/memory_manager.py, plugins/memory/holographic/):

- SQLite storage with FTS5 full-text search kept in sync by triggers
- Hybrid retrieval: BM25 candidates → Jaccard rerank → HRR phase-vector
  similarity (no embedding API) → trust weighting → optional temporal decay
- Asymmetric trust scoring (+0.05 helpful / -0.10 unhelpful feedback)
- Entity extraction/resolution with compositional HRR queries
  (probe / related / reason) and contradiction detection
- Prefetch context injected behind a fenced ``<memory-context>`` block with a
  system note, so recalled memory can never masquerade as fresh user input
- Trivial-prompt gating: no recall round-trip for "ok", "thanks", slash text
- Regex auto-extraction of user preferences and project decisions

Public surface keeps the pre-existing MemoryStore API (add/search/save_*/
delete/clear*) so the API layer, orchestrator, and watchers are unaffected.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from agent.core.memory import hrr
from agent.core.memory.retrieval import FactRetriever
from agent.core.memory.store import FactStore
from agent.models import MemoryEntry

logger = logging.getLogger(__name__)

# Persistence location (tests patch this to redirect storage).
_DB_PATH = pathlib.Path("data/memory.db")
_LEGACY_JSON_PATH = pathlib.Path("data/memory.json")

# Curated-memory limits: storage is indexed now, but an unbounded store still
# degrades recall quality. Standing instructions are POLICY and are never
# evicted by episodic churn.
_MAX_ENTRIES = 500

# Trust floor for prefetch injection (search() itself stays unfiltered).
_PREFETCH_MIN_TRUST = 0.15
# Categories never injected into planning prompts: raw transcripts of past
# tasks are for self-improvement stats, not templates (see prefetch()).
_PREFETCH_EXCLUDE = frozenset({"task_outcome"})


# ---------------------------------------------------------------------------
# Prompt-injection fencing (adapted from Hermes' MemoryManager)
# ---------------------------------------------------------------------------

_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)


def sanitize_context(text: str) -> str:
    """Strip fence tags so provider output cannot forge context blocks."""
    return _FENCE_TAG_RE.sub("", text)


def build_memory_context_block(raw_context: str) -> str:
    """Wrap prefetched memory in a fenced block with a system note."""
    if not raw_context or not raw_context.strip():
        return ""
    clean = sanitize_context(raw_context)
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory and should inform "
        "planning and responses.]\n\n"
        f"{clean}\n"
        "</memory-context>"
    )


# ---------------------------------------------------------------------------
# Trivial-prompt gate (adapted from Hermes' memory_provider.py)
# ---------------------------------------------------------------------------

_TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|sup|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)"
    r"[\s!?.:;,\"'"
    + r"'"
    + r"~\u2018\u2019\u201c\u201d\u2014\u2013\u2026()\[\]{}<>*&^%$#@!+=`\u00a0]*$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: str | None) -> bool:
    """True when a prompt carries no semantic signal worth recalling against."""
    if not text:
        return True
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return True
    return bool(_TRIVIAL_PROMPT_RE.match(stripped))


# ---------------------------------------------------------------------------
# Auto-extraction patterns (adapted from Hermes' holographic plugin)
# ---------------------------------------------------------------------------

_PREF_PATTERNS = [
    re.compile(r"\bI\s+(?:prefer|like|love|use|want|need)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bmy\s+(?:favorite|preferred|default)\s+\w+\s+is\s+(.+)", re.IGNORECASE),
    re.compile(r"\bI\s+(?:always|never|usually)\s+(.+)", re.IGNORECASE),
]
_DECISION_PATTERNS = [
    re.compile(r"\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)", re.IGNORECASE),
    re.compile(r"\bthe\s+project\s+(?:uses|needs|requires)\s+(.+)", re.IGNORECASE),
]


class MemoryStore:
    """SQLite-backed curated memory with hybrid retrieval and trust scoring."""

    def __init__(self, db_path: str | pathlib.Path | None = None) -> None:
        self._path = pathlib.Path(db_path) if db_path else _DB_PATH
        self._store = FactStore(db_path=self._path)
        self._retriever = FactRetriever(store=self._store)
        self._migrate_legacy_json()

    # ------------------------------------------------------------------
    # Legacy JSON migration
    # ------------------------------------------------------------------

    def _migrate_legacy_json(self) -> None:
        """One-time import of the old data/memory.json store.

        The legacy file is RENAMED after handling, so an intentionally
        emptied database can never be silently repopulated from stale
        JSON on a later start (user-reported resurrection bug).
        """
        if not _LEGACY_JSON_PATH.exists():
            return
        imported_path = _LEGACY_JSON_PATH.with_suffix(".json.imported")
        try:
            if self._store.count() == 0:
                data = json.loads(_LEGACY_JSON_PATH.read_text(encoding="utf-8"))
                migrated = 0
                for item in data:
                    entry = MemoryEntry(**item)
                    _, created = self._store.add_fact(
                        entry_uid=entry.id,
                        content=entry.content.strip(),
                        category=entry.category,
                        tags="",
                    )
                    if created:
                        migrated += 1
                logger.info("Migrated %d memory entries from %s", migrated, _LEGACY_JSON_PATH)
            # Retire the file whether or not we just imported: if the DB is
            # already populated the migration clearly ran before.
            _LEGACY_JSON_PATH.rename(imported_path)
            logger.info("Retired legacy memory file -> %s", imported_path.name)
        except Exception:
            logger.exception("Legacy memory migration failed (%s)", _LEGACY_JSON_PATH)

    # ------------------------------------------------------------------
    # Row ↔ entry conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: dict[str, Any]) -> MemoryEntry:
        created = row.get("created_at")
        created_at = None
        if created:
            try:
                created_at = datetime_from_sqlite(str(created))
            except ValueError:
                created_at = None
        extra_metadata: dict[str, Any] = (
            {"score": row["score"]} if "score" in row else {}
        )
        return MemoryEntry(
            id=str(row["entry_uid"]),
            content=str(row["content"]),
            category=str(row.get("category") or "general"),
            metadata={
                "trust_score": float(row.get("trust_score") or 0.5),
                "retrieval_count": int(row.get("retrieval_count") or 0),
                "helpful_count": int(row.get("helpful_count") or 0),
                **extra_metadata,
            },
            created_at=created_at or datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # Core write API (legacy-compatible)
    # ------------------------------------------------------------------

    def add(self, entry: MemoryEntry) -> bool:
        """Add a memory entry. Returns False when rejected (duplicate)."""
        content = entry.content.strip()
        if not content:
            return False
        existing = self._store.get_fact_by_uid(entry.id)
        if existing is not None:
            return False
        tags = str(entry.metadata.get("skill_name") or entry.metadata.get("type") or "")
        _, created = self._store.add_fact(
            entry_uid=entry.id,
            content=content,
            category=entry.category,
            tags=tags[:200],
        )
        if not created:
            return False
        self._enforce_limits()
        logger.debug("Memory added: [%s] %s", entry.category, content[:80])
        return True

    def _enforce_limits(self) -> None:
        """Global cap: evict lowest-trust oldest entries, protecting instructions."""
        with self._store.lock:
            conn = self._store.conn
            total = self._store.count()
            overflow = total - _MAX_ENTRIES
            if overflow <= 0:
                return
            rows = conn.execute(
                """
                SELECT fact_id FROM facts
                WHERE category != 'instruction'
                ORDER BY trust_score ASC, created_at ASC
                LIMIT ?
                """,
                (overflow,),
            ).fetchall()
        for row in rows:
            fact = self._store.get_fact(int(row["fact_id"]))
            if fact is not None:
                self._store.remove_fact_by_uid(str(fact["entry_uid"]))

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def search(
        self, query: str, top_k: int = 5, category: str | None = None
    ) -> list[MemoryEntry]:
        """Hybrid search (BM25 + Jaccard + HRR), trust-weighted."""
        query = query.strip()
        if not query:
            return []
        results = self._retriever.search(
            query, category=category, min_trust=0.0, limit=top_k
        )
        return [self._row_to_entry(r) for r in results]

    def get_recent(self, n: int = 10) -> list[MemoryEntry]:
        """Most recently created entries, returned in chronological order."""
        with self._store.lock:
            rows = self._store.conn.execute(
                "SELECT * FROM facts ORDER BY created_at DESC, fact_id DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [self._row_to_entry(dict(r)) for r in reversed(rows)]

    def get_by_category(self, category: str) -> list[MemoryEntry]:
        """All entries in a category, insertion order preserved."""
        with self._store.lock:
            rows = self._store.conn.execute(
                "SELECT * FROM facts WHERE category = ? ORDER BY fact_id ASC",
                (category,),
            ).fetchall()
        return [self._row_to_entry(dict(r)) for r in rows]

    # ------------------------------------------------------------------
    # Convenience writers (content formats kept stable for prompts)
    # ------------------------------------------------------------------

    def save_task_outcome(self, task_goal: str, result: str, success: bool) -> bool:
        result_short = result[:200] if result else ""
        content = f"Task: {task_goal}\nResult: {result_short}\nSuccess: {success}"
        return self.add(MemoryEntry(
            content=content,
            category="task_outcome",
            metadata={"success": success},
        ))

    def save_skill(self, skill_name: str, instructions: str) -> bool:
        return self.add(MemoryEntry(
            content=f"Skill: {skill_name}\n{instructions[:300]}",
            category="skill",
            metadata={"skill_name": skill_name},
        ))

    def save_reflection(self, reflection: str) -> bool:
        return self.add(MemoryEntry(content=reflection[:500], category="reflection"))

    def save_instruction(self, instruction: str) -> bool:
        return self.add(MemoryEntry(
            content=f"User instruction: {instruction[:500]}",
            category="instruction",
            metadata={"type": "user_instruction"},
        ))

    def extract_and_store(self, text: str) -> int:
        """Auto-extract preferences/decisions from text (Hermes session-harvest).

        Stores durable facts under 'user_pref' and 'project'. Returns count stored.
        """
        if not text or len(text.strip()) < 10:
            return 0
        stored = 0
        for pattern in _PREF_PATTERNS:
            if pattern.search(text):
                if self.add(MemoryEntry(content=text[:400], category="user_pref")):
                    stored += 1
                break
        for pattern in _DECISION_PATTERNS:
            if pattern.search(text):
                if self.add(MemoryEntry(content=text[:400], category="project")):
                    stored += 1
                break
        return stored

    # ------------------------------------------------------------------
    # Trust feedback + compositional queries
    # ------------------------------------------------------------------

    def record_feedback(self, entry_id: str, helpful: bool) -> dict[str, float | int]:
        """Rate a memory after using it — good memories rise, bad ones sink."""
        fact = self._store.get_fact_by_uid(entry_id)
        if fact is None:
            raise KeyError(f"Memory entry not found: {entry_id}")
        result = self._store.record_feedback(int(fact["fact_id"]), helpful=helpful)
        logger.info(
            "Memory feedback (%s): %s trust %.2f → %.2f",
            "helpful" if helpful else "unhelpful",
            entry_id,
            result["old_trust"],
            result["new_trust"],
        )
        return result

    def probe(self, entity: str, limit: int = 10) -> list[MemoryEntry]:
        """ALL facts where an entity plays a structural role (HRR algebra)."""
        return [self._row_to_entry(r) for r in self._retriever.probe(entity, limit=limit)]

    def related(self, entity: str, limit: int = 10) -> list[MemoryEntry]:
        """Facts connected to an entity through shared context."""
        return [self._row_to_entry(r) for r in self._retriever.related(entity, limit=limit)]

    def reason(self, entities: list[str], limit: int = 10) -> list[MemoryEntry]:
        """Compositional multi-entity intersection (vector-space JOIN)."""
        return [
            self._row_to_entry(r) for r in self._retriever.reason(entities, limit=limit)
        ]

    def find_contradictions(self, limit: int = 10) -> list[dict[str, Any]]:
        """Facts sharing entities but making conflicting claims."""
        contradictions = self._retriever.contradict(limit=limit)
        cleaned: list[dict[str, Any]] = []
        for c in contradictions:
            fa, fb = c["fact_a"], c["fact_b"]
            cleaned.append({
                "fact_a": {"id": fa["entry_uid"], "content": fa["content"]},
                "fact_b": {"id": fb["entry_uid"], "content": fb["content"]},
                "contradiction_score": c["contradiction_score"],
                "shared_entities": c["shared_entities"],
            })
        return cleaned

    # ------------------------------------------------------------------
    # Prefetch (per-turn recall injection)
    # ------------------------------------------------------------------

    def prefetch(self, query: str, top_k: int = 5) -> str:
        """Fenced recall context for an upcoming turn ('' when nothing applies).

        Skips trivial prompts entirely (no wasted retrieval, no stale context
        derailing one-word replies).

        Raw ``task_outcome`` transcripts are EXCLUDED (Hermes lesson: planning
        receives distilled context, not past-task dumps — otherwise a new
        "make a product landing page" goal recalls an old landing-page build
        and copies it wholesale).
        """
        if is_trivial_prompt(query):
            return ""
        # Over-fetch so category filtering below still fills top_k slots.
        results = self._retriever.search(
            query, min_trust=_PREFETCH_MIN_TRUST, limit=top_k * 3
        )
        results = [
            r for r in results if r.get("category") not in _PREFETCH_EXCLUDE
        ][:top_k]
        if not results:
            return ""
        lines = []
        for r in results:
            trust = float(r.get("trust_score", 0.5))
            lines.append(f"- [{trust:.1f}] {r.get('content', '')}")
        return build_memory_context_block(
            "## Recalled memory — BACKGROUND ONLY\n"
            "These notes may come from DIFFERENT goals. Use them as context; "
            "never copy a past task's subject, branding, filenames, or code "
            "into the current goal.\n" + "\n".join(lines)
        )

    def system_prompt_block(self) -> str:
        """Static memory capability summary for the agent's system prompt."""
        total = self.size
        if total == 0:
            return (
                "# Persistent Memory\n"
                "Active and empty. Store durable facts (preferences, decisions, "
                "outcomes) so future tasks benefit."
            )
        breakdown = ", ".join(
            f"{cat}: {count}" for cat, count in self.categories().items()
        )
        return (
            f"# Persistent Memory\n"
            f"Active. {total} facts stored ({breakdown}) with trust scoring.\n"
            f"Relevant memories are recalled automatically each turn; rate them "
            f"helpful/unhelpful to train ranking."
        )

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def delete(self, entry_id: str) -> bool:
        removed = self._store.remove_fact_by_uid(entry_id)
        if removed:
            logger.info("Memory entry deleted: %s", entry_id)
        return removed

    def clear_category(self, category: str) -> int:
        removed = self._store.remove_category(category)
        if removed:
            logger.info("Cleared %d memory entries in category '%s'", removed, category)
        return removed

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return self._store.count()

    def categories(self) -> dict[str, int]:
        with self._store.lock:
            rows = self._store.conn.execute(
                "SELECT category, COUNT(*) AS n FROM facts GROUP BY category ORDER BY n DESC"
            ).fetchall()
        return {str(r["category"]): int(r["n"]) for r in rows}

    def close(self) -> None:
        self._store.close()

    @staticmethod
    def new_entry_id() -> str:
        return str(uuid.uuid4())


def datetime_from_sqlite(value: str) -> datetime:
    """Parse SQLite CURRENT_TIMESTAMP (or ISO) into an aware UTC datetime."""
    normalized = value.replace("Z", "+00:00")
    if " " in normalized and "+" not in normalized.split(" ", 1)[1]:
        normalized = normalized.replace(" ", "T", 1) + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _create_memory_store():
    """Factory: pick SQLite or Firestore based on DATABASE_BACKEND config."""
    from agent.config import settings

    backend = settings.database_backend.lower()
    if backend == "firestore":
        try:
            from cloud.firestore.client import FirestoreMemoryStore, _is_available
            if _is_available():
                logger.info("Using Firestore memory backend")
                return FirestoreMemoryStore()
            else:
                logger.warning(
                    "DATABASE_BACKEND=firestore but Firestore not configured "
                    "(missing GOOGLE_CLOUD_PROJECT). Falling back to SQLite."
                )
        except ImportError:
            logger.warning(
                "DATABASE_BACKEND=firestore but google-cloud-firestore not installed. "
                "Falling back to SQLite."
            )
    return MemoryStore()


memory_store = _create_memory_store()


__all__ = [
    "FactRetriever",
    "FactStore",
    "MemoryStore",
    "build_memory_context_block",
    "hrr",
    "is_trivial_prompt",
    "memory_store",
    "sanitize_context",
]
