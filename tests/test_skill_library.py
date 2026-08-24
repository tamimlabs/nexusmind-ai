"""Tests for the self-evolving skill library (Hermes adaptation).

Covers: validation gates, provenance (created_by), usage telemetry,
deterministic staleness lifecycle, archive/restore, lexical matching +
plan-context injection, dedup gate, and the audit ledger.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

import agent.core.skill_library as sl


def _skill_doc(
    name: str = "summarize-hn-digest",
    description: str = "Use when summarizing news threads: year-filtered search then digest.",
    body: str | None = None,
) -> str:
    if body is None:
        body = (
            "# HN Digest\n\n## When to Use\n- user asks for news digests\n\n"
            "## Procedure\n1. web_search with the current year\n"
            "2. summarize_text the top stories\n\n## Pitfalls\n- old threads pollute results\n"
        )
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        "version: 1.0.0\n"
        "---\n\n" + body
    )


@pytest.fixture()
def lib(tmp_path: Path) -> sl.SkillLibrary:
    return sl.SkillLibrary(skills_dir=tmp_path / "skills")


class TestValidationGates:
    def test_create_and_read_back(self, lib):
        name = lib.create(name="Summarize HN Digest!", content=_skill_doc(), actor="user")
        assert name == "summarize-hn-digest"  # slugified
        path, meta, body = lib._read(name)
        assert meta["name"] == name
        assert "web_search" in body
        assert path.parent == lib.dir / name

    def test_invalid_name_rejected(self, lib):
        with pytest.raises(sl.SkillError, match="invalid"):
            lib.create(name="-bad-name", content=_skill_doc("-bad-name"))

    def test_agent_description_budget_enforced(self, lib):
        long_desc = "Use when the user wants a HackerNews digest and you must filter by year."
        assert len(long_desc) > 60
        with pytest.raises(sl.SkillError, match="60"):
            lib.create(
                name="hn-digest",
                content=_skill_doc("hn-digest", long_desc),
                created_by="agent",
                actor="agent",
            )
        # User-created skills keep the larger 1024-char budget
        lib.create(name="hn-digest", content=_skill_doc("hn-digest", long_desc))
        assert lib.exists("hn-digest")

    def test_missing_description_rejected(self, lib):
        doc = _skill_doc().replace('description: "Use when', 'other: "Use when')
        with pytest.raises(sl.SkillError, match="description"):
            lib.create(name="x-skill", content=doc)

    def test_collision_rejected(self, lib):
        lib.create(name="dupe", content=_skill_doc("dupe"))
        with pytest.raises(sl.SkillError, match="already exists"):
            lib.create(name="dupe", content=_skill_doc("dupe"))

    def test_unclosed_frontmatter_rejected(self, lib):
        with pytest.raises(sl.SkillError):
            lib.create(name="broken", content="---\nname: broken\n# no close")


class TestTelemetryAndLifecycle:
    def test_record_use_bumps_and_revives_stale(self, lib):
        lib.create(name="my-flow", content=_skill_doc("my-flow"))
        lib.record_use("my-flow")
        record = lib.usage_of("my-flow")
        assert record["use_count"] == 1
        assert record["last_used_at"]

        # Force stale via backdated activity, then a new use revives it
        lib.apply_transitions(now=datetime.now(UTC) + timedelta(days=31))
        assert lib.usage_of("my-flow")["state"] == sl.STATE_STALE
        lib.record_use("my-flow")
        assert lib.usage_of("my-flow")["state"] == sl.STATE_ACTIVE

    def test_stale_then_archive_transitions(self, lib):
        lib.create(name="old-flow", content=_skill_doc("old-flow"))
        applied = dict(lib.apply_transitions(now=datetime.now(UTC) + timedelta(days=31)))
        assert applied.get("old-flow") == sl.STATE_STALE
        applied = dict(lib.apply_transitions(now=datetime.now(UTC) + timedelta(days=91)))
        assert applied.get("old-flow") == sl.STATE_ARCHIVED
        assert not lib.exists("old-flow")  # moved out of rotation
        archived = [s for s in lib.list_skills(include_archived=True) if s["name"] == "old-flow"]
        assert archived and archived[0]["archived"]

    def test_pinned_skills_exempt(self, lib):
        lib.create(name="pinned-flow", content=_skill_doc("pinned-flow"))
        lib._usage_record("pinned-flow")["pinned"] = True
        lib._save_usage()
        applied = lib.apply_transitions(now=datetime.now(UTC) + timedelta(days=200))
        assert not applied

    def test_archive_restore_roundtrip(self, lib):
        lib.create(name="roundtrip", content=_skill_doc("roundtrip"))
        assert lib.archive("roundtrip")
        assert lib.restore("roundtrip")
        assert lib.exists("roundtrip")
        assert lib.usage_of("roundtrip")["state"] == sl.STATE_ACTIVE


class TestMatchingAndInjection:
    @pytest.fixture(autouse=True)
    def _seed(self, lib):
        self.lib = lib
        lib.create(name="hn-digest", content=_skill_doc("hn-digest"))
        lib.create(
            name="csv-trends",
            content=_skill_doc(
                "csv-trends",
                "Use when analyzing CSV files for trends and anomalies.",
            ),
            actor="user",
        )

    def test_match_ranks_by_lexical_overlap(self):
        matches = self.lib.match("analyze this csv file for trends please", top_k=2)
        assert matches[0][0] == "csv-trends"
        assert matches[0][1] > 0

    def test_plan_context_includes_index_and_best_body(self):
        context, matched = self.lib.plan_context("summarize hackernews into a digest")
        assert context.startswith("<available-skills>")
        assert "- hn-digest:" in context
        assert "<skill-procedure" in context
        assert matched == ["hn-digest"]
        assert "web_search" in context  # full procedure body embedded

    def test_plan_context_empty_library(self, tmp_path):
        empty = sl.SkillLibrary(skills_dir=tmp_path / "none")
        context, matched = empty.plan_context("anything at all")
        assert context == "" and matched == []

    def test_find_similar_dedup_gate(self):
        similar = self.lib.find_similar("summarize hackernews digest of news threads")
        assert any(s["name"] == "hn-digest" for s in similar)


class TestLedger:
    def test_every_mutation_is_ledgered_with_hashes(self, lib):
        lib.create(
            name="audited",
            content=_skill_doc("audited", "Use when auditing flows end to end."),
            actor="agent",
            created_by="agent",
        )
        lib.patch("audited", "# New body only\nstep one")
        lib.archive("audited", actor="user")
        entries = lib.read_ledger()
        actions = [e["action"] for e in entries]  # newest first
        assert actions == ["archive", "patch", "create"]
        patch_entry = next(e for e in entries if e["action"] == "patch")
        assert patch_entry["before_sha256"] != patch_entry["after_sha256"]
        assert patch_entry["before_content"].startswith("---")
        create_entry = next(e for e in entries if e["action"] == "create")
        assert create_entry["actor"] == "agent"

    def test_patch_preserves_provenance_and_bumps_version(self, lib):
        lib.create(
            name="prov-check",
            content=_skill_doc("prov-check", "Use when checking provenance flows."),
            created_by="agent",
            origin_task="task-123",
            actor="agent",
        )
        lib.patch("prov-check", "Bare markdown body without frontmatter.")
        _, meta, body = lib._read("prov-check")
        assert meta["created_by"] == "agent"
        assert meta["origin_task"] == "task-123"
        assert meta["version"] == "1.0.1"
        assert "Bare markdown body" in body


class TestSynthesisContract:
    def test_fenced_response_is_cleanable(self):
        """The orchestrator strips code fences before calling create()."""
        raw = "```markdown\n" + _skill_doc("fenced-skill") + "\n```"
        cleaned = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("```")
        ).strip()
        meta, _body = sl.SkillLibrary.split_frontmatter(cleaned)
        assert meta["name"] == "fenced-skill"

    def test_usage_sidecar_is_valid_json_on_disk(self, lib):
        lib.create(name="disk-check", content=_skill_doc("disk-check"))
        lib.record_use("disk-check")
        data = json.loads(lib.usage_path.read_text(encoding="utf-8"))
        assert data["disk-check"]["use_count"] == 1
