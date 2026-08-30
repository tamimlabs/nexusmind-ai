"""Self-evolving procedural skill library — adapted from Hermes Agent.

Hermes patterns ported here (tools/skill_manager_tool.py, tools/skill_usage.py,
agent/curator.py):

- Skills are markdown packages (``<name>/SKILL.md``) with YAML frontmatter,
  not code. The agent itself authors them from solved tasks.
- Description-as-router: every description is a <=60-char trigger-first sentence
  ("Use when X. Does Y.") rendered into a compact system-prompt index; matching
  is done over that index, never by embeddings.
- Usage telemetry (.usage.json sidecar) records use/patch counts. Counters are
  observability, not quality scores — they drive only the deterministic
  staleness lifecycle (active -> stale @30d idle -> archived @90d idle).
- Provenance gate: skills authored by the auto-synthesis loop carry
  ``created_by: agent``; future curation may only touch those.
- Append-only audit ledger (.ledger.jsonl) with sha256 before/after hashes for
  every mutation — enterprise audit trail with cheap undo semantics.

Differences from Hermes (deliberate):
- Matching/injection is lexical (token overlap) instead of pure LLM choice: the
  planner always sees the index, and the single best-matching procedure body is
  injected automatically. No lazy ``skill_view`` round-trip needed at our scale.
- No background curator LLM fork; consolidation is out of scope.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import shutil
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Module-level defaults (patchable in tests, mirroring agent.core.memory).
SKILLS_DIR = pathlib.Path("data") / "skills"

# ── Validation gates (Hermes values) ────────────────────────────────────────
MAX_NAME_LENGTH = 64
VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_DESCRIPTION_LENGTH = 1024
DESC_PROMPT_LIMIT = 60  # trigger-first budget; the index truncates at 57+"..."
MAX_SKILL_CONTENT_CHARS = 100_000
MAX_LEDGER_BEFORE_CHARS = 20_000

# ── Lifecycle thresholds (curator defaults) ─────────────────────────────────
STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90
ARCHIVE_DIRNAME = ".archive"
USAGE_FILENAME = ".usage.json"
LEDGER_FILENAME = ".ledger.jsonl"

STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"

_MATCH_THRESHOLD = 0.12  # min Jaccard to auto-inject a procedure body
_SIMILAR_DEFAULT_THRESHOLD = 0.3  # dedup gate (stemmed tokens)
_ARCHIVE_TS_SUFFIX = re.compile(r"-\d{8}-\d{6}$")


def _requires_tools(meta: dict[str, Any]) -> list[str]:
    """Extract ``metadata.conditions.requires_tools`` from SKILL.md frontmatter
    (Hermes tool-binding pattern)."""
    conditions = (meta.get("metadata") or {}).get("conditions") or {}
    raw = conditions.get("requires_tools") or conditions.get("requires_toolsets") or []
    if isinstance(raw, list):
        return [str(t) for t in raw if str(t).strip()]
    if raw:
        return [str(raw)]
    return []


def _needs_missing_tools(skill: dict[str, Any], available_tools: set[str]) -> bool:
    """True when the skill declares required tools the session lacks."""
    required = skill.get("requires_tools") or []
    if not required:
        return False
    return not set(required).issubset(available_tools)


class SkillError(ValueError):
    """Raised when a skill operation violates validation gates."""


def slugify(text: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """Convert free text into a valid skill name (kebab-case slug)."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", text.lower().strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    return slug[:max_length].rstrip("-.") or "skill"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stem(word: str) -> str:
    """Very light suffix stripping so inflections collapse to one token."""
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    if len(word) >= 5 and word.endswith("e"):
        word = word[:-1]
    if len(word) >= 4 and word.endswith("s"):
        word = word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    """Tokenization for lexical matching (light-stemmed, len>=2)."""
    return {_stem(w) for w in re.findall(r"[a-z0-9]{2,}", text.lower())}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SkillLibrary:
    """Filesystem-backed procedural skill store with telemetry and lifecycle."""

    def __init__(self, skills_dir: str | pathlib.Path | None = None) -> None:
        self.dir = pathlib.Path(skills_dir) if skills_dir else SKILLS_DIR
        self.archive_dir = self.dir / ARCHIVE_DIRNAME
        self.usage_path = self.dir / USAGE_FILENAME
        self.ledger_path = self.dir / LEDGER_FILENAME
        self._lock = threading.RLock()
        self._usage_cache: dict[str, dict[str, Any]] | None = None
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Parsing / validation
    # ------------------------------------------------------------------

    @staticmethod
    def split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        """Split SKILL.md text into (frontmatter dict, body). BOM-tolerant."""
        text = content.lstrip("\ufeff").lstrip()
        if not text.startswith("---"):
            raise SkillError("SKILL.md must start with a '---' frontmatter block")
        end = text.find("\n---", 3)
        if end == -1:
            raise SkillError("Frontmatter block is not closed with '---'")
        raw = text[3:end].strip()
        body = text[end + 4 :].lstrip("\n")
        try:
            meta: Any = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise SkillError(f"Invalid frontmatter YAML: {exc}") from None
        if not isinstance(meta, dict):
            raise SkillError("Frontmatter must be a YAML mapping")
        return meta, body

    @classmethod
    def validate_content(cls, content: str, *, new: bool, created_by: str) -> dict[str, Any]:
        """Enforce Hermes' hard gates. Returns parsed frontmatter."""
        if len(content) > MAX_SKILL_CONTENT_CHARS:
            raise SkillError(
                f"SKILL.md exceeds {MAX_SKILL_CONTENT_CHARS} char limit ({len(content)})"
            )
        meta, body = cls.split_frontmatter(content)

        name = str(meta.get("name") or "").strip()
        if not name:
            raise SkillError("Frontmatter requires a 'name' field")
        if len(name) > MAX_NAME_LENGTH or not VALID_NAME_RE.fullmatch(name):
            raise SkillError(
                f"name '{name}' invalid: must match {VALID_NAME_RE.pattern} "
                f"and be <= {MAX_NAME_LENGTH} chars"
            )

        description = str(meta.get("description") or "").strip()
        if not description:
            raise SkillError("Frontmatter requires a 'description' field")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise SkillError(f"description exceeds {MAX_DESCRIPTION_LENGTH} chars")
        if new and created_by == "agent" and len(description) > DESC_PROMPT_LIMIT:
            raise SkillError(
                f"description is {len(description)} chars; agent-created skills "
                f"must be <= {DESC_PROMPT_LIMIT}: one sentence, trigger first, "
                "'Use when <trigger>. <behavior>.'"
            )
        if not body.strip():
            raise SkillError("Skill body must not be empty")
        return meta

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _skill_path(self, name: str) -> pathlib.Path:
        """Locate an ACTIVE skill's SKILL.md anywhere under the root."""
        direct = self.dir / name / "SKILL.md"
        if direct.exists():
            return direct
        candidates = [
            p
            for p in sorted(self.dir.glob("*/*/SKILL.md"))
            if p.parent.name == name and not self._in_hidden_dir(p)
        ]
        return candidates[0] if candidates else direct

    @staticmethod
    def _in_hidden_dir(path: pathlib.Path) -> bool:
        """True if any path segment below the skills root starts with '.'."""
        return any(part.startswith(".") for part in path.parts[:-2])

    def exists(self, name: str) -> bool:
        return self._skill_path(name).exists()

    def _read(self, name: str) -> tuple[pathlib.Path, dict[str, Any], str]:
        path = self._skill_path(name)
        if not path.exists():
            raise KeyError(f"Skill not found: {name}")
        content = path.read_text(encoding="utf-8")
        meta, body = self.split_frontmatter(content)
        return path, meta, body

    def _category_of(self, path: pathlib.Path) -> str:
        parent = path.parent.parent
        if parent == self.dir or parent.name.startswith("."):
            return ""
        return parent.name

    @staticmethod
    def _render_header(
        meta: dict[str, Any], *, created_by: str, origin_task: str | None, created_at: str
    ) -> str:
        header = (
            "---\n"
            f"name: {meta['name']}\n"
            f'description: "{meta["description"]}"\n'
            f"version: {meta.get('version', '1.0.0')}\n"
            f"author: {meta.get('author', 'user')}\n"
            f"created_by: {created_by}\n"
            f"origin_task: {origin_task or 'null'}\n"
            f"created_at: {created_at}\n"
        )
        env = meta.get("metadata") or {}
        if isinstance(env, dict) and env:
            block = (
                yaml.safe_dump(env, default_flow_style=False, sort_keys=False).strip().splitlines()
            )
            header += "metadata:\n" + "\n".join(f"  {line}" for line in block) + "\n"
        return header + "---\n\n"

    # ------------------------------------------------------------------
    # Mutations (each gated by validation + ledger)
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        content: str,
        actor: str = "user",
        created_by: str = "user",
        origin_task: str | None = None,
    ) -> str:
        """Write a new SKILL.md. Returns the canonical skill name.

        Gates: name regex/length, description presence (+60-char budget for
        agent-created), size cap, collision check across the whole tree.
        """
        name = slugify(name)
        meta = self.validate_content(content, new=True, created_by=created_by)
        fm_name = str(meta.get("name") or "")
        if fm_name and slugify(fm_name) != name:
            name = slugify(fm_name)

        normalized = dict(meta)
        normalized["name"] = name
        normalized["description"] = str(meta.get("description", "")).strip()
        _, body = self.split_frontmatter(content)
        final_content = (
            self._render_header(
                normalized,
                created_by=created_by,
                origin_task=origin_task,
                created_at=_now_iso(),
            )
            + body
        )

        target_dir = self.dir / name
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "SKILL.md"
        now = _now_iso()
        with self._lock:
            if path.exists():
                raise SkillError(f"Skill '{name}' already exists")
            path.write_text(final_content, encoding="utf-8", newline="\n")
            record = self._usage_record(name)
            record.update(
                {
                    "created_by": created_by,
                    "state": STATE_ACTIVE,
                    "pinned": False,
                    "use_count": 0,
                    "patch_count": 0,
                    "created_at": now,
                    "last_used_at": None,
                    "last_patched_at": None,
                }
            )
            self._save_usage()
        self._append_ledger(
            {
                "action": "create",
                "name": name,
                "actor": actor,
                "after_sha256": _sha256(final_content),
            }
        )
        logger.info("Skill created (%s/%s): %s", created_by, actor, name)
        self._emit_lifecycle("create", name, created_by=created_by)
        return name

    def patch(self, name: str, new_content: str, actor: str = "user") -> None:
        """Replace a skill's body (frontmatter preserved), ledgering the diff.

        Accepts either a full SKILL.md document or a bare markdown body.
        """
        path, old_meta, _ = self._read(name)
        try:
            new_meta, body = self.split_frontmatter(new_content)
            self.validate_content(
                new_content,
                new=False,
                created_by=str(old_meta.get("created_by") or "user"),
            )
            description = str(new_meta.get("description") or old_meta.get("description"))
        except SkillError:
            body = new_content.strip()
            description = str(old_meta.get("description"))

        normalized = dict(old_meta)
        normalized["name"] = str(old_meta.get("name") or name)
        normalized["description"] = description
        version = str(old_meta.get("version") or "1.0.0")
        try:
            major, minor, rev = (int(part) for part in version.split("."))
            version = f"{major}.{minor}.{rev + 1}"
        except ValueError:
            pass
        normalized["version"] = version

        final_content = (
            self._render_header(
                normalized,
                created_by=str(old_meta.get("created_by") or "user"),
                origin_task=str(old_meta["origin_task"]) if old_meta.get("origin_task") else None,
                created_at=str(old_meta.get("created_at") or _now_iso()),
            )
            + body
        )

        before = path.read_text(encoding="utf-8")
        with self._lock:
            path.write_text(final_content, encoding="utf-8", newline="\n")
            record = self._usage_record(name)
            record["patch_count"] = int(record.get("patch_count", 0)) + 1
            record["patch_generation"] = int(record.get("patch_generation", 0)) + 1
            record["last_patched_at"] = _now_iso()
            self._save_usage()
        self._append_ledger(
            {
                "action": "patch",
                "name": name,
                "actor": actor,
                "before_sha256": _sha256(before),
                "after_sha256": _sha256(final_content),
                "before_content": before[:MAX_LEDGER_BEFORE_CHARS],
            }
        )
        self._emit_lifecycle("patch", name, patch_generation=record["patch_generation"])

    def archive(self, name: str, actor: str = "user") -> bool:
        """Move a skill into .archive/ (recoverable soft delete)."""
        path = self._skill_path(name)
        if not path.exists():
            return False
        with self._lock:
            dest = self.archive_dir / name
            if dest.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                dest = self.archive_dir / f"{name}-{stamp}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.parent.rename(dest)
            record = self._usage_record(name)
            record["state"] = STATE_ARCHIVED
            self._save_usage()
        self._append_ledger({"action": "archive", "name": name, "actor": actor})
        logger.info("Skill archived: %s -> %s", name, dest.name)
        self._emit_lifecycle("archive", name, actor=actor)
        return True

    def restore(self, name: str, actor: str = "user") -> bool:
        """Restore the newest archived copy of a skill back into rotation."""
        candidates = sorted(
            p for p in self.archive_dir.glob(f"{name}*") if (p / "SKILL.md").exists()
        )
        if not candidates:
            return False
        src = candidates[-1]
        dest = self.dir / _ARCHIVE_TS_SUFFIX.sub("", src.name)
        if dest.exists():
            raise SkillError(f"Cannot restore '{name}': active skill already exists")
        with self._lock:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
            record = self._usage_record(name)
            record["state"] = STATE_ACTIVE
            self._save_usage()
        self._append_ledger({"action": "restore", "name": name, "actor": actor})
        self._emit_lifecycle("restore", name, actor=actor)
        return True

    def delete(self, name: str, actor: str = "user") -> bool:
        """Hard delete (use archive() when recoverability matters)."""
        path = self._skill_path(name)
        if not path.exists():
            return False
        before = path.read_text(encoding="utf-8")
        with self._lock:
            shutil.rmtree(path.parent)
            self._usage().pop(name, None)
            self._save_usage()
        self._append_ledger(
            {
                "action": "delete",
                "name": name,
                "actor": actor,
                "before_sha256": _sha256(before),
                "before_content": before[:MAX_LEDGER_BEFORE_CHARS],
            }
        )
        self._emit_lifecycle("delete", name, actor=actor)
        return True

    # ------------------------------------------------------------------
    # Telemetry + lifecycle
    # ------------------------------------------------------------------

    def _usage(self) -> dict[str, dict[str, Any]]:
        if self._usage_cache is None:
            loaded: Any = {}
            if self.usage_path.exists():
                try:
                    loaded = json.loads(self.usage_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logger.warning("Corrupt usage sidecar, starting fresh")
            self._usage_cache = loaded if isinstance(loaded, dict) else {}
        return self._usage_cache

    def _usage_record(self, name: str) -> dict[str, Any]:
        return self._usage().setdefault(
            name,
            {
                "created_by": None,
                "state": STATE_ACTIVE,
                "pinned": False,
                "use_count": 0,
                "patch_count": 0,
                "patch_generation": 0,
                "last_reused_patch_generation": 0,
                "created_at": _now_iso(),
                "last_used_at": None,
                "last_patched_at": None,
            },
        )

    def _save_usage(self) -> None:
        cache = self._usage_cache or {}
        tmp = self.usage_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.usage_path)

    def set_hook(self, callback: Any) -> None:
        """Register a live ``on_skill_lifecycle`` hook (Hermes pattern).

        The callback receives ``(action, name)`` plus extra keyword args
        (e.g. ``use_count``, ``reused_after_patch``). Errors are swallowed so
        telemetry can never break skill operations.
        """
        self._hook = callback

    def _emit_lifecycle(self, action: str, name: str, **extra: Any) -> None:
        hook = getattr(self, "_hook", None)
        if hook is None:
            return
        try:
            hook(action, name, **extra)
        except Exception:
            logger.debug("Skill lifecycle hook failed", exc_info=True)

    def record_use(self, name: str) -> None:
        """Bump use_count on successful reuse, tracking reuse-after-patch."""
        with self._lock:
            record = self._usage_record(name)
            record["use_count"] = int(record.get("use_count", 0)) + 1
            record["last_used_at"] = _now_iso()
            generation = int(record.get("patch_generation", 0))
            reused = int(record.get("last_reused_patch_generation", 0))
            if generation > reused:
                record["last_reused_patch_generation"] = generation
            if record.get("state") == STATE_STALE:
                record["state"] = STATE_ACTIVE
            self._save_usage()
        self._emit_lifecycle(
            "use",
            name,
            use_count=record["use_count"],
            patch_generation=generation,
            reused_after_patch=generation > 0
            and generation == record["last_reused_patch_generation"],
        )

    def usage_of(self, name: str) -> dict[str, Any]:
        return dict(self._usage_record(name))

    @staticmethod
    def _activity_anchor(record: dict[str, Any]) -> datetime:
        stamps = [record.get(k) for k in ("last_used_at", "last_patched_at")]
        stamps = [s for s in stamps if s] or [record.get("created_at")]
        for value in stamps:
            if value:
                try:
                    return datetime.fromisoformat(str(value))
                except ValueError:
                    continue
        return datetime.now(UTC)

    def apply_transitions(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Deterministic state machine: active -> stale (@30d) -> archived (@90d).

        Pinned skills are exempt. Returns the transitions applied.
        """
        now = now or datetime.now(UTC)
        stale_cutoff = now - timedelta(days=STALE_AFTER_DAYS)
        archive_cutoff = now - timedelta(days=ARCHIVE_AFTER_DAYS)
        applied: list[tuple[str, str]] = []
        with self._lock:
            for name, record in self._usage().items():
                if record.get("pinned") or record.get("state") == STATE_ARCHIVED:
                    continue
                anchor = self._activity_anchor(record)
                if record["state"] == STATE_ACTIVE and anchor <= stale_cutoff:
                    record["state"] = STATE_STALE
                    applied.append((name, STATE_STALE))
                elif record["state"] == STATE_STALE and anchor <= archive_cutoff:
                    if self.archive(name, actor="curator"):
                        applied.append((name, STATE_ARCHIVED))
            if applied:
                self._save_usage()
        return applied

    # ------------------------------------------------------------------
    # Discovery / matching / prompt rendering
    # ------------------------------------------------------------------

    def list_skills(self, include_archived: bool = False) -> list[dict[str, Any]]:
        """All skills with merged frontmatter + usage metadata."""
        results: list[dict[str, Any]] = []
        roots = [self.dir] + ([self.archive_dir] if include_archived else [])
        seen: set[str] = set()
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("**/SKILL.md")):
                if root == self.dir and self._in_hidden_dir(path):
                    continue  # skip .archive/ and other dot-dirs
                name = path.parent.name
                if name in seen:
                    continue
                seen.add(name)
                try:
                    meta, body = self.split_frontmatter(path.read_text(encoding="utf-8"))
                except SkillError:
                    continue
                entry: dict[str, Any] = {
                    "name": name,
                    "description": str(meta.get("description", "")),
                    "version": str(meta.get("version", "1.0.0")),
                    "author": str(meta.get("author", "")),
                    "created_by": meta.get("created_by", ""),
                    "origin_task": meta.get("origin_task"),
                    "category": self._category_of(path),
                    "archived": root == self.archive_dir,
                    "chars": len(body),
                    "path": str(path),
                    "requires_tools": _requires_tools(meta),
                }
                entry.update(self._usage_record(name))
                results.append(entry)
        return results

    def find_similar(
        self, text: str, threshold: float = _SIMILAR_DEFAULT_THRESHOLD
    ) -> list[dict[str, Any]]:
        """Skills whose name+description overlap heavily with text (dedup gate)."""
        probe = _tokens(text)
        similar: list[dict[str, Any]] = []
        for skill in self.list_skills():
            signature = _tokens(skill["name"] + " " + skill["description"])
            score = _jaccard(probe, signature)
            if score >= threshold:
                similar.append({"name": skill["name"], "score": round(score, 3)})
        return sorted(similar, key=lambda item: -float(item["score"]))

    def match(
        self,
        goal: str,
        top_k: int = 3,
        available_tools: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Rank skills against a goal by lexical overlap (index-router signal).

        ``available_tools`` gates out skills whose ``requires_tools`` are not
        all available in the current session (Hermes conditional activation).
        """
        probe = _tokens(goal)
        scored: list[tuple[str, float]] = []
        for skill in self.list_skills():
            if skill.get("archived"):
                continue
            if available_tools is not None and _needs_missing_tools(skill, available_tools):
                continue
            signature = _tokens(skill["name"].replace("-", " ") + " " + skill["description"])
            scored.append((str(skill["name"]), _jaccard(probe, signature)))
        scored.sort(key=lambda pair: -pair[1])
        return [(n, round(s, 3)) for n, s in scored[:top_k]]

    def render_index(self, available_tools: set[str] | None = None) -> str:
        """Compact ``<available-skills>`` catalog for the planner prompt.

        Skills whose recorded ``requires_tools`` are unavailable are hidden.
        """
        categories: dict[str, list[dict[str, Any]]] = {}
        for skill in self.list_skills():
            if skill.get("archived"):
                continue
            if available_tools is not None and _needs_missing_tools(skill, available_tools):
                continue
            categories.setdefault(skill["category"] or "general", []).append(skill)
        lines: list[str] = []
        for category in sorted(categories):
            lines.append(f"  {category}:")
            for skill in categories[category]:
                raw_desc = str(skill["description"])
                desc = raw_desc[:57] + "..." if len(raw_desc) > 57 else raw_desc
                lines.append(f"    - {skill['name']}: {desc}")
        return "\n".join(lines)

    def plan_context(
        self,
        goal: str,
        available_tools: set[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Build the fenced skill-context block for planning.

        Returns ``(context_text, matched_names)`` where matched_names are the
        skills whose FULL procedure body was embedded (for use-tracking later).
        Skills requiring tools the session lacks are filtered out
        (``available_tools``).
        """
        index = self.render_index(available_tools=available_tools)
        if not index.strip():
            return "", []
        parts = [
            "<available-skills>",
            "Before planning, scan this skill index. If ANY skill is even "
            "partially relevant to the goal, follow its embedded procedure.",
            "",
            index,
            "",
        ]
        matched: list[str] = []
        for name, score in self.match(goal, top_k=1, available_tools=available_tools):
            if score < _MATCH_THRESHOLD:
                continue
            try:
                _, meta, body = self._read(name)
            except KeyError:
                continue
            parts += [
                f'<skill-procedure name="{name}" relevance="{score}">',
                f"Description: {meta.get('description', '')}",
                "",
                body.strip(),
                "</skill-procedure>",
                "",
            ]
            matched.append(name)
        parts.append("</available-skills>")
        return "\n".join(parts), matched

    # ------------------------------------------------------------------
    # Audit ledger
    # ------------------------------------------------------------------

    def _append_ledger(self, entry: dict[str, Any]) -> None:
        record = {"ts": _now_iso(), **entry}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_ledger(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest-first view of the audit ledger."""
        if not self.ledger_path.exists():
            return []
        lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines[-limit:]):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


skill_library = SkillLibrary()
