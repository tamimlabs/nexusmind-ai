"""SQLite-backed fact store with entity resolution and trust scoring.

Adapted from Hermes Agent's holographic memory plugin: a single-file,
zero-infrastructure memory store combining:

- UNIQUE-content deduplication at the database level
- FTS5 full-text search kept in sync by triggers
- Entity extraction + resolution with alias support
- Asymmetric trust scoring (+0.05 helpful / -0.10 unhelpful)
- Optional HRR vectors for semantic-ish retrieval without an embedding API

All instances targeting the same database file share ONE connection and ONE
re-entrant lock (refcounted), so concurrent writers can never contend on the
SQLite write lock.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, ClassVar

from agent.core.memory import hrr

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_uid       TEXT NOT NULL UNIQUE,
    content         TEXT NOT NULL,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_content  ON facts(content);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id) ON DELETE CASCADE,
    entity_id INTEGER REFERENCES entities(entity_id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_entities_entity ON fact_entities(entity_id);
"""

# Trust adjustment constants — asymmetric so bad memories sink faster than
# good ones rise (one "unhelpful" outweighs one "helpful", matching Hermes).
HELPFUL_DELTA = 0.05
UNHELPFUL_DELTA = -0.10
TRUST_MIN = 0.0
TRUST_MAX = 1.0

# Entity extraction patterns (regex rules from Hermes' store).
_RE_CAPITALIZED = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA = re.compile(
    r"(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)",
    re.IGNORECASE,
)


def _clamp_trust(value: float) -> float:
    return max(TRUST_MIN, min(TRUST_MAX, value))


class FactStore:
    """SQLite-backed fact store with entity resolution and trust scoring."""

    # Process-wide shared connection registry (see module docstring).
    # Process-wide shared connection registry (see module docstring).
    _shared: ClassVar[dict[str, dict[str, Any]]] = {}
    _shared_guard = threading.Lock()

    def __init__(
        self,
        db_path: str | Path,
        default_trust: float = 0.5,
        hrr_dim: int = 512,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust = _clamp_trust(default_trust)
        self.hrr_dim = hrr_dim

        try:
            self._key = str(self.db_path.resolve())
        except OSError:
            self._key = str(self.db_path)
        with FactStore._shared_guard:
            entry = FactStore._shared.get(self._key)
            if entry is None:
                conn = sqlite3.connect(
                    self._key,
                    check_same_thread=False,
                    timeout=10.0,
                    # Autocommit: every statement is its own transaction, so a
                    # write that raises mid-method never leaves a dangling
                    # transaction (and its write lock) open.
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                entry = {"conn": conn, "lock": threading.RLock(), "refs": 0, "ready": False}
                FactStore._shared[self._key] = entry
            entry["refs"] += 1
            self._entry: dict[str, Any] = entry
            self._conn: sqlite3.Connection = entry["conn"]
            self._lock: threading.RLock = entry["lock"]

        with self._lock:
            if not self._entry["ready"]:
                self._init_db()
                self._entry["ready"] = True

    def _init_db(self) -> None:
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def add_fact(
        self,
        entry_uid: str,
        content: str,
        category: str = "general",
        tags: str = "",
    ) -> tuple[int, bool]:
        """Insert a fact; deduplicates on ``entry_uid`` and exact content.

        Returns ``(fact_id, created)`` — ``created`` is False when an
        existing fact was matched instead of inserted.
        """
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")

            existing = self._conn.execute(
                "SELECT fact_id FROM facts WHERE entry_uid = ? OR (content = ? AND category = ?)",
                (entry_uid, content, category),
            ).fetchone()
            if existing is not None:
                return int(existing["fact_id"]), False

            cur = self._conn.execute(
                """
                INSERT INTO facts (entry_uid, content, category, tags, trust_score)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry_uid, content, category, tags.strip(), self.default_trust),
            )
            last_id: int | None = cur.lastrowid
            fact_id = last_id if last_id is not None else 0

            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)

            self._compute_hrr_vector(fact_id, content)
            return fact_id, True

    def update_fact_content(self, fact_id: int, content: str) -> bool:
        """Replace a fact's content, re-extracting entities and recomputing vectors."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                return False

            self._conn.execute(
                "UPDATE facts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                (content.strip(), fact_id),
            )
            self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)
            self._compute_hrr_vector(fact_id, content)
            return True

    def remove_fact_by_uid(self, entry_uid: str) -> bool:
        """Delete a fact and its entity links by entry UID. True if it existed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id FROM facts WHERE entry_uid = ?", (entry_uid,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "DELETE FROM fact_entities WHERE fact_id = ?", (row["fact_id"],)
            )
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (row["fact_id"],))
            return True

    def remove_category(self, category: str) -> int:
        """Delete ALL facts in a category. Returns number removed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT fact_id FROM facts WHERE category = ?", (category,)
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "DELETE FROM fact_entities WHERE fact_id = ?", (row["fact_id"],)
                )
            cur = self._conn.execute("DELETE FROM facts WHERE category = ?", (category,))
            return int(cur.rowcount)

    def record_feedback(self, fact_id: int, helpful: bool) -> dict[str, float | int]:
        """Record usage feedback; adjusts trust asymmetrically.

        helpful=True  -> trust += 0.05, helpful_count += 1
        helpful=False -> trust -= 0.10
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT trust_score, helpful_count FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")

            delta = HELPFUL_DELTA if helpful else UNHELPFUL_DELTA
            new_trust = _clamp_trust(float(row["trust_score"]) + delta)
            increment = 1 if helpful else 0
            self._conn.execute(
                """
                UPDATE facts
                SET trust_score = ?, helpful_count = helpful_count + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE fact_id = ?
                """,
                (new_trust, increment, fact_id),
            )
            return {
                "fact_id": fact_id,
                "old_trust": float(row["trust_score"]),
                "new_trust": new_trust,
                "helpful_count": int(row["helpful_count"]) + increment,
            }

    def clear(self) -> None:
        """Remove every fact and entity link."""
        with self._lock:
            self._conn.execute("DELETE FROM fact_entities")
            self._conn.execute("DELETE FROM facts")

    def count(self, category: str | None = None) -> int:
        with self._lock:
            if category is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM facts").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM facts WHERE category = ?", (category,)
                ).fetchone()
            return int(row["n"])

    # ------------------------------------------------------------------
    # Read helpers used by the retriever and wrapper
    # ------------------------------------------------------------------

    def get_fact_by_uid(self, entry_uid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM facts WHERE entry_uid = ?", (entry_uid,)
            ).fetchone()
            return self._row_to_dict(row) if row is not None else None

    def get_fact(self, fact_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            return self._row_to_dict(row) if row is not None else None

    def all_fact_ids_for_entities(self, entity_name: str) -> list[int]:
        """Fact ids linked to an entity (case-insensitive name or alias)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT f.fact_id FROM facts f
                JOIN fact_entities fe ON fe.fact_id = f.fact_id
                JOIN entities e ON e.entity_id = fe.entity_id
                WHERE LOWER(e.name) = LOWER(?)
                   OR (',' || e.aliases || ',') LIKE '%,' || ? || ',%'
                ORDER BY f.trust_score DESC
                """,
                (entity_name, entity_name.lower()),
            ).fetchall()
            return [int(r["fact_id"]) for r in rows]

    def bump_retrieval_counts(self, fact_ids: list[int]) -> None:
        if not fact_ids:
            return
        with self._lock:
            placeholders = ",".join("?" * len(fact_ids))
            self._conn.execute(
                f"UPDATE facts SET retrieval_count = retrieval_count + 1 "
                f"WHERE fact_id IN ({placeholders})",
                fact_ids,
            )

    @property
    def conn(self) -> sqlite3.Connection:
        """Direct connection access (retriever runs under the same shared lock)."""
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    # ------------------------------------------------------------------
    # Entities + HRR vectors
    # ------------------------------------------------------------------

    def _extract_entities(self, text: str) -> list[str]:
        """Extract entity candidates: capitalized phrases, quoted terms, AKA patterns."""
        seen: set[str] = set()
        candidates: list[str] = []

        def _add(name: str) -> None:
            stripped = name.strip()
            if stripped and stripped.lower() not in seen:
                seen.add(stripped.lower())
                candidates.append(stripped)

        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))
        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))
        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))
        for m in _RE_AKA.finditer(text):
            _add(m.group(1))
            _add(m.group(2))

        return candidates

    def _resolve_entity(self, name: str) -> int:
        """Find an entity by name or alias (case-insensitive), or create one."""
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
        if row is not None:
            return int(row["entity_id"])

        alias_row = self._conn.execute(
            """
            SELECT entity_id FROM entities
            WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'
            """,
            (name.lower(),),
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])

        cur = self._conn.execute(
            "INSERT INTO entities (name, aliases) VALUES (?, '')", (name,)
        )
        last_id = cur.lastrowid
        return int(last_id) if last_id is not None else 0

    def _link_fact_entity(self, fact_id: int, entity_id: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)",
            (fact_id, entity_id),
        )

    def _compute_hrr_vector(self, fact_id: int, content: str) -> None:
        """Compute and persist the HRR vector for a fact. No-op without numpy."""
        with self._lock:
            if not hrr.HAS_NUMPY:
                return
            rows = self._conn.execute(
                """
                SELECT e.name FROM entities e
                JOIN fact_entities fe ON fe.entity_id = e.entity_id
                WHERE fe.fact_id = ?
                """,
                (fact_id,),
            ).fetchall()
            entities = [str(row["name"]) for row in rows]
            vector = hrr.encode_fact(content, entities, self.hrr_dim)
            self._conn.execute(
                "UPDATE facts SET hrr_vector = ? WHERE fact_id = ?",
                (hrr.phases_to_bytes(vector), fact_id),
            )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release this instance's reference to the shared connection (idempotent)."""
        if getattr(self, "_entry", None) is None:
            return
        with FactStore._shared_guard:
            entry = self._entry
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                finally:
                    if FactStore._shared.get(self._key) is entry:
                        FactStore._shared.pop(self._key, None)
            self._entry = None  # type: ignore[assignment]

    def __enter__(self) -> FactStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
