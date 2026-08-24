"""Hybrid multi-strategy retrieval for the memory store.

Adapted from Hermes Agent's holographic retriever. Pipeline:

1. FTS5 candidates (BM25 rank, stopword-aware OR expansion for recall)
2. Jaccard token-overlap rerank
3. HRR phase-vector similarity (optional numpy — weights auto-redistribute)
4. Trust weighting: final_score = relevance * trust_score
5. Optional temporal decay: 0.5^(age_days / half_life)
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent.core.memory import hrr

try:
    import numpy as np

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover - exercised only without numpy
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

if TYPE_CHECKING:
    from agent.core.memory.store import FactStore

_FTS_WEIGHT = 0.4
_JACCARD_WEIGHT = 0.3
_HRR_WEIGHT = 0.3

# Short English function words with no retrieval signal; dropped before FTS5
# OR-expansion so natural-language queries don't AND-match to zero results.
_FTS_STOPWORDS = frozenset({
    "a", "about", "above", "after", "again", "all", "am", "an", "and",
    "any", "are", "as", "at", "be", "because", "been", "before", "being",
    "between", "both", "but", "by", "can", "could", "did", "do", "does",
    "doing", "down", "during", "each", "few", "for", "from", "further",
    "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it",
    "its", "itself", "just", "me", "more", "most", "my", "myself", "no",
    "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other",
    "our", "ours", "ourselves", "out", "over", "own", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "these", "they",
    "this", "those", "through", "to", "too", "under", "until", "up",
    "very", "was", "we", "were", "what", "when", "where", "which", "while",
    "who", "whom", "why", "will", "with", "would", "you", "your", "yours",
    "yourself", "yourselves",
})

_FTS_SPECIAL = '"()*^:-+'


def _sanitize_fts_query(query: str) -> str:
    """Convert a natural-language query into an FTS5-safe OR expression."""
    if not query:
        return ""
    tokens: list[str] = []
    for raw in query.lower().split():
        cleaned = raw.strip(".,;:!?\"'()[]{}#@<>").translate(
            str.maketrans("", "", _FTS_SPECIAL)
        )
        if len(cleaned) < 2 or cleaned in _FTS_STOPWORDS:
            continue
        tokens.append(f'"{cleaned}"')
    if not tokens:
        return query
    return " OR ".join(tokens)


def _tokenize(text: str) -> set[str]:
    """Lowercase whitespace tokenization with punctuation stripping."""
    if not text:
        return set()
    tokens: set[str] = set()
    for word in text.lower().split():
        cleaned = word.strip(".,;:!?\"'()[]{}#@<>")
        if cleaned:
            tokens.add(cleaned)
    return tokens


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    if not set_a or not set_b:
        return 0.0
    union = len(set_a | set_b)
    return len(set_a & set_b) / union if union > 0 else 0.0


class FactRetriever:
    """Multi-strategy fact retrieval with trust-weighted scoring."""

    def __init__(
        self,
        store: FactStore,
        temporal_decay_half_life: int = 0,  # days, 0 = disabled
    ) -> None:
        self.store = store
        self.half_life = temporal_decay_half_life

        # Auto-redistribute weights when numpy is unavailable (graceful
        # degradation to FTS + Jaccard, exactly like Hermes).
        if hrr.HAS_NUMPY:
            self.fts_weight = _FTS_WEIGHT
            self.jaccard_weight = _JACCARD_WEIGHT
            self.hrr_weight = _HRR_WEIGHT
        else:
            self.fts_weight = 0.6
            self.jaccard_weight = 0.4
            self.hrr_weight = 0.0

    def search(
        self,
        query: str,
        category: str | None = None,
        min_trust: float = 0.3,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Hybrid search: FTS5 candidates → Jaccard/HRR rerank → trust weight."""
        candidates = self._fts_candidates(query, category, min_trust, limit * 3)
        if not candidates:
            return []

        query_tokens = _tokenize(query)
        query_vec: np.ndarray | None = None
        scored: list[dict[str, Any]] = []

        with self.store.lock:
            for fact in candidates:
                content_tokens = _tokenize(str(fact["content"]))
                tag_tokens = _tokenize(str(fact.get("tags") or ""))
                jaccard = _jaccard(query_tokens, content_tokens | tag_tokens)
                fts_score = float(fact.get("fts_rank", 0.0))

                if self.hrr_weight > 0 and fact.get("hrr_vector"):
                    fact_vec = hrr.bytes_to_phases(fact["hrr_vector"], dim=self.store.hrr_dim)
                    if query_vec is None:
                        query_vec = hrr.encode_text(query, self.store.hrr_dim)
                    assert query_vec is not None  # narrowed for mypy
                    hrr_sim = (hrr.similarity(query_vec, fact_vec) + 1.0) / 2.0
                else:
                    hrr_sim = 0.5  # neutral when HRR unavailable

                relevance = (
                    self.fts_weight * fts_score
                    + self.jaccard_weight * jaccard
                    + self.hrr_weight * hrr_sim
                )
                score = relevance * float(fact["trust_score"])

                if self.half_life > 0:
                    score *= self._temporal_decay(
                        fact.get("updated_at") or fact.get("created_at")
                    )

                fact["score"] = round(score, 6)
                scored.append(fact)

        scored.sort(key=lambda f: f["score"], reverse=True)
        results = scored[:limit]
        for fact in results:
            fact.pop("hrr_vector", None)  # not JSON-serializable
        self.store.bump_retrieval_counts([int(f["fact_id"]) for f in results])
        return results

    def probe(self, entity: str, category: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Entity recall via HRR algebra: facts where the entity plays a role.

        Falls back to keyword search when numpy is unavailable.
        """
        if not hrr.HAS_NUMPY:
            return self.search(entity, category=category, min_trust=0.0, limit=limit)

        ids = self.store.all_fact_ids_for_entities(entity)
        rows = [self.store.get_fact(i) for i in ids]
        facts = [f for f in rows if f is not None and f.get("hrr_vector")]
        if not facts:
            return self.search(entity, category=category, min_trust=0.0, limit=limit)

        role_entity = hrr.encode_atom("__hrr_role_entity__", self.store.hrr_dim)
        role_content = hrr.encode_atom("__hrr_role_content__", self.store.hrr_dim)
        entity_vec = hrr.encode_atom(entity.lower(), self.store.hrr_dim)
        probe_key = hrr.bind(entity_vec, role_entity)

        scored: list[dict[str, Any]] = []
        with self.store.lock:
            for fact in facts:
                fact_vec = hrr.bytes_to_phases(fact["hrr_vector"], dim=self.store.hrr_dim)
                residual = hrr.unbind(fact_vec, probe_key)
                content_vec = hrr.bind(
                    hrr.encode_text(str(fact["content"]), self.store.hrr_dim), role_content
                )
                sim = hrr.similarity(residual, content_vec)
                fact["score"] = round((sim + 1.0) / 2.0 * float(fact["trust_score"]), 6)
                scored.append(fact)

        scored.sort(key=lambda f: f["score"], reverse=True)
        results = scored[:limit]
        for fact in results:
            fact.pop("hrr_vector", None)
        return results

    def related(self, entity: str, category: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Facts structurally connected to an entity through shared context."""
        if not hrr.HAS_NUMPY:
            return self.search(entity, category=category, min_trust=0.0, limit=limit)

        ids = self.store.all_fact_ids_for_entities(entity)
        rows = [self.store.get_fact(i) for i in ids]
        facts = [f for f in rows if f is not None and f.get("hrr_vector")]
        if not facts:
            return self.search(entity, category=category, min_trust=0.0, limit=limit)

        entity_vec = hrr.encode_atom(entity.lower(), self.store.hrr_dim)
        role_entity = hrr.encode_atom("__hrr_role_entity__", self.store.hrr_dim)
        role_content = hrr.encode_atom("__hrr_role_content__", self.store.hrr_dim)

        scored: list[dict[str, Any]] = []
        with self.store.lock:
            for fact in facts:
                fact_vec = hrr.bytes_to_phases(fact["hrr_vector"], dim=self.store.hrr_dim)
                residual = hrr.unbind(fact_vec, entity_vec)
                best_sim = max(
                    hrr.similarity(residual, role_entity),
                    hrr.similarity(residual, role_content),
                )
                fact["score"] = round((best_sim + 1.0) / 2.0 * float(fact["trust_score"]), 6)
                scored.append(fact)

        scored.sort(key=lambda f: f["score"], reverse=True)
        results = scored[:limit]
        for fact in results:
            fact.pop("hrr_vector", None)
        return results

    def reason(
        self, entities: list[str], category: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Compositional multi-entity query — a vector-space JOIN.

        Scores facts by how much ALL entities are structurally present
        (AND semantics via min), something no keyword index can do.
        """
        if not hrr.HAS_NUMPY or not entities:
            return self.search(
                " ".join(entities), category=category, min_trust=0.0, limit=limit
            )

        candidate_ids: set[int] = set()
        for entity in entities:
            candidate_ids.update(self.store.all_fact_ids_for_entities(entity))
        if not candidate_ids:
            return self.search(
                " ".join(entities), category=category, min_trust=0.0, limit=limit
            )

        rows = [self.store.get_fact(i) for i in sorted(candidate_ids)]
        facts = [
            f
            for f in rows
            if f is not None and f.get("hrr_vector")
            and (category is None or f.get("category") == category)
        ]
        if not facts:
            return self.search(
                " ".join(entities), category=category, min_trust=0.0, limit=limit
            )

        role_entity = hrr.encode_atom("__hrr_role_entity__", self.store.hrr_dim)
        role_content = hrr.encode_atom("__hrr_role_content__", self.store.hrr_dim)
        probe_keys = [
            hrr.bind(hrr.encode_atom(e.lower(), self.store.hrr_dim), role_entity)
            for e in entities
        ]

        scored: list[dict[str, Any]] = []
        with self.store.lock:
            for fact in facts:
                fact_vec = hrr.bytes_to_phases(fact["hrr_vector"], dim=self.store.hrr_dim)
                entity_scores = [
                    hrr.similarity(hrr.unbind(fact_vec, key), role_content)
                    for key in probe_keys
                ]
                # AND semantics: all entities must be structurally present.
                min_sim = min(entity_scores)
                fact["score"] = round((min_sim + 1.0) / 2.0 * float(fact["trust_score"]), 6)
                scored.append(fact)

        scored.sort(key=lambda f: f["score"], reverse=True)
        results = scored[:limit]
        for fact in results:
            fact.pop("hrr_vector", None)
        return results

    def contradict(
        self, category: str | None = None, threshold: float = 0.3, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Memory hygiene: find facts sharing entities but making different claims.

        High entity overlap + low content similarity = potential contradiction.
        """
        if not hrr.HAS_NUMPY:
            return []

        with self.store.lock:
            conn = self.store.conn
            where = "WHERE f.hrr_vector IS NOT NULL"
            params: list[object] = []
            if category:
                where += " AND f.category = ?"
                params.append(category)

            rows = conn.execute(
                f"""
                SELECT f.fact_id, f.entry_uid, f.content, f.category, f.tags,
                       f.trust_score, f.created_at, f.updated_at, f.hrr_vector
                FROM facts f
                {where}
                """,
                params,
            ).fetchall()

        if len(rows) < 2:
            return []

        # Guard against O(n²) blow-up on large stores.
        max_contradict_facts = 500
        if len(rows) > max_contradict_facts:
            rows = sorted(
                rows, key=lambda r: r["updated_at"] or r["created_at"], reverse=True
            )[:max_contradict_facts]

        fact_entities: dict[int, set[str]] = {}
        for row in rows:
            fact_entities[int(row["fact_id"])] = {
                name.lower()
                for name in self._entity_names_for(int(row["fact_id"]))
            }

        contradictions: list[dict[str, Any]] = []
        facts = [dict(r) for r in rows]

        with self.store.lock:
            for i in range(len(facts)):
                for j in range(i + 1, len(facts)):
                    f1, f2 = facts[i], facts[j]
                    ents1 = fact_entities.get(int(f1["fact_id"]), set())
                    ents2 = fact_entities.get(int(f2["fact_id"]), set())
                    if not ents1 or not ents2:
                        continue

                    overlap = (
                        len(ents1 & ents2) / len(ents1 | ents2) if (ents1 | ents2) else 0.0
                    )
                    if overlap < 0.3:
                        continue

                    v1 = hrr.bytes_to_phases(f1["hrr_vector"], dim=self.store.hrr_dim)
                    v2 = hrr.bytes_to_phases(f2["hrr_vector"], dim=self.store.hrr_dim)
                    content_sim = hrr.similarity(v1, v2)
                    contradiction_score = overlap * (1.0 - (content_sim + 1.0) / 2.0)

                    if contradiction_score >= threshold:
                        for f in (f1, f2):
                            f.pop("hrr_vector", None)
                        contradictions.append({
                            "fact_a": f1,
                            "fact_b": f2,
                            "entity_overlap": round(overlap, 3),
                            "content_similarity": round(content_sim, 3),
                            "contradiction_score": round(contradiction_score, 3),
                            "shared_entities": sorted(ents1 & ents2),
                        })

        contradictions.sort(key=lambda c: c["contradiction_score"], reverse=True)
        return contradictions[:limit]

    def _entity_names_for(self, fact_id: int) -> list[str]:
        with self.store.lock:
            rows = self.store.conn.execute(
                """
                SELECT e.name FROM entities e
                JOIN fact_entities fe ON fe.entity_id = e.entity_id
                WHERE fe.fact_id = ?
                """,
                (fact_id,),
            ).fetchall()
            return [str(r["name"]) for r in rows]

    def _fts_candidates(
        self, query: str, category: str | None, min_trust: float, limit: int
    ) -> list[dict[str, Any]]:
        """Raw FTS5 candidates with rank normalized to [0, 1]."""
        with self.store.lock:
            conn = self.store.conn
            params: list[object] = [_sanitize_fts_query(query)]
            where_clauses = ["facts_fts MATCH ?"]
            if category:
                where_clauses.append("f.category = ?")
                params.append(category)
            where_clauses.append("f.trust_score >= ?")
            params.append(min_trust)
            params.append(limit)

            where_sql = " AND ".join(where_clauses)
            try:
                rows = conn.execute(
                    f"""
                    SELECT f.*, facts_fts.rank AS fts_rank_raw
                    FROM facts_fts
                    JOIN facts f ON f.fact_id = facts_fts.rowid
                    WHERE {where_sql}
                    ORDER BY facts_fts.rank
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            except Exception:
                # Malformed MATCH queries must never crash recall.
                return []

        if not rows:
            return []

        raw_ranks = [abs(float(row["fts_rank_raw"])) for row in rows]
        max_rank = max(max(raw_ranks), 1e-6)

        results = []
        for row, raw_rank in zip(rows, raw_ranks, strict=False):
            fact = dict(row)
            fact.pop("fts_rank_raw", None)
            fact["fts_rank"] = raw_rank / max_rank
            results.append(fact)
        return results

    def _temporal_decay(self, timestamp_str: object | None) -> float:
        """Exponential decay 0.5^(age_days / half_life_days); 1.0 when disabled."""
        if not self.half_life or not timestamp_str:
            return 1.0
        try:
            ts = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - ts).total_seconds() / 86400
            if age_days < 0:
                return 1.0
            return math.pow(0.5, age_days / self.half_life)
        except (ValueError, TypeError):
            return 1.0
