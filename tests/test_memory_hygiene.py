"""Regression tests for memory-contamination fixes.

A "make product landing page" goal once recalled an old landing-page
task_outcome transcript and copied it. Fixes covered here:
- prefetch() excludes raw task_outcome transcripts and frames recall as
  background-only
- _clean_lessons() rejects prompt echoes / truncated reflections before
  they are stored as lessons
- planner frames LESSONS context as guidance, not a template
"""

from __future__ import annotations

from typing import Any

import pytest

import agent.core.memory as mem_mod
import agent.core.planner as planner_mod
from agent.core.memory import MemoryStore
from agent.models import Task
from agent.orchestrator import _clean_lessons


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_mod, "_DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(mem_mod, "_LEGACY_JSON_PATH", tmp_path / "no-legacy.json")
    return MemoryStore()


class TestPrefetchExcludesTranscripts:
    def test_task_outcomes_never_injected(self, store):
        store.add(
            mem_mod.MemoryEntry(
                content=(
                    "Task: create a google landing page redesign\n"
                    "Result: Written 10806 chars to output\\index.html\nSuccess: True"
                ),
                category="task_outcome",
            )
        )
        block = store.prefetch("make a product landing page for my startup")
        assert block == "" or "output\\\\index.html" not in block
        assert "google landing page" not in block.replace("\\", "")

    def test_distilled_facts_still_recalled_with_background_framing(self, store):
        store.add(
            mem_mod.MemoryEntry(
                content="User prefers dark themed dashboards with red accents.",
                category="general",
            )
        )
        block = store.prefetch("build dark themed dashboards")
        assert block != ""
        assert "BACKGROUND ONLY" in block
        assert "dark themed" in block


class TestCleanLessons:
    def test_prompt_echo_rejected(self):
        raw = (
            "You just completed a task. Extract ONLY genuinely new lessons.\n\n"
            "Goal: make a landing page\n"
            "Result: Written file successfully."
        )
        assert _clean_lessons(raw) == []

    def test_truncated_sentence_rejected(self):
        # No terminal punctuation -> mid-thought truncation
        assert _clean_lessons("For abstract or creative goals use imagery first") == []

    def test_valid_lesson_kept_and_bullets_stripped(self):
        raw = '- When generating HTML files, write them via execute_code to avoid arg limits.'
        out = _clean_lessons(raw)
        assert len(out) == 1
        assert out[0].startswith("When generating")

    def test_nothing_to_save(self):
        assert _clean_lessons("NOTHING_TO_SAVE") == []

    def test_capped_at_two_lines(self):
        raw = "\n".join(
            [
                f"Lesson number {i} teaches something reusable for later tasks."
                for i in range(5)
            ]
        )
        assert len(_clean_lessons(raw)) == 2

    def test_mixed_output_salvages_only_clean_lines(self):
        raw = (
            "You just completed a task. Goal: x\n"
            "- JSON extraction from HTML needs escaped-quote handling.\n"
            "For abstract or creative\n"
            "* Always include the year in news search queries.\n"
            "Task completed successfully."  # too generic AND no period... has none
        )
        out = _clean_lessons(raw)
        assert out == [
            "JSON extraction from HTML needs escaped-quote handling.",
            "Always include the year in news search queries.",
        ]


class TestPlannerLessonsFraming:
    @pytest.fixture()
    def captured(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def fake_generate(**kwargs: Any) -> str:
            seen.update(kwargs)
            import json as _json

            return _json.dumps(
                [{"description": "search", "tool_name": "web_search",
                  "tool_args": {"query": "x"}}]
            )

        monkeypatch.setattr("agent.core.gemini_client.generate_content", fake_generate)
        return seen

    @pytest.mark.asyncio
    async def test_lessons_labelled_guidance_only(self, captured):
        await planner_mod.plan_task(
            Task(goal="research fusion energy"),
            lessons=["Always include the year in news search queries."],
        )
        assert "guidance only" in captured["user"]
        assert "never reuse a past goal's subject" in captured["user"]
