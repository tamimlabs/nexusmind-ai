"""Tests for runtime fixes: execute_code temp-file handling (WinError 32),
planner diagnostics, truncate-tolerant plan salvage, robust fallback JSON
parsing, and the deterministic creative-goal pipeline.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

import agent.core.executor as ex
import agent.core.planner as planner_mod
from agent.models import Task


@pytest.fixture()
def isolated_scratch(monkeypatch, tmp_path):
    """Point _scratch_dir at a hermetic temp location."""
    monkeypatch.setenv("NEXUSMIND_TEMP", str(tmp_path))
    return tmp_path


class TestExecuteCode:
    @pytest.mark.asyncio
    async def test_success(self, isolated_scratch):
        result = await ex.execute_code("print('hello world')")
        assert result.success is True
        assert "hello world" in result.output

    @pytest.mark.asyncio
    async def test_failure_returns_stderr(self, isolated_scratch):
        result = await ex.execute_code("raise ValueError('boom')")
        assert result.success is False
        assert "boom" in (result.error or "")

    @pytest.mark.asyncio
    async def test_unsupported_language_no_disk_io(self, isolated_scratch):
        result = await ex.execute_code("console.log(1)", language="javascript")
        assert result.success is False
        assert "Unsupported" in (result.error or "")

    @pytest.mark.asyncio
    async def test_runs_in_d_temp_not_user_temp(self, isolated_scratch, monkeypatch):
        captured: list[str] = []

        real_exec = ex.asyncio.create_subprocess_exec

        async def spy(*args: Any, **kwargs: Any):
            captured.append(str(args[1]))
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(ex.asyncio, "create_subprocess_exec", spy)
        await ex.execute_code("print('x')")
        assert captured and str(isolated_scratch) in captured[0]

    @pytest.mark.asyncio
    async def test_script_file_cleaned_up(self, isolated_scratch):
        await ex.execute_code("print('cleanup check')")
        leftovers = list(isolated_scratch.glob("nexusmind_*.py"))
        assert leftovers == []

    @pytest.mark.asyncio
    async def test_timeout_kills_and_reports(self, isolated_scratch, monkeypatch):
        monkeypatch.setattr(ex, "_EXEC_TIMEOUT", 2)
        result = await ex.execute_code("import time; time.sleep(30)")
        assert result.success is False
        assert "timed out after 2s" in (result.error or "")


@pytest.fixture()
def gemini(monkeypatch):
    """Scriptable fake of gemini_client.generate_content (module-scoped)."""
    calls: list[dict[str, Any]] = []
    responses: list[str] = []

    async def fake_generate(**kwargs: Any) -> str:
        calls.append(kwargs)
        if responses:
            return responses.pop(0)
        raise IndexError("no scripted response")  # exercises fallback paths

    monkeypatch.setattr(
        "agent.core.gemini_client.generate_content", fake_generate
    )
    return calls, responses


class TestPlannerDiagnostics:
    @pytest.mark.asyncio
    async def test_empty_response_logged_and_falls_back(self, gemini, caplog):
        _, responses = gemini
        responses[:] = [""]  # safety block / quota symptom
        with caplog.at_level("WARNING"):
            steps = await planner_mod.plan_task(Task(goal="research quantum computing"))
        assert "EMPTY response" in caplog.text
        # fallback also got empty -> last-resort research step, no crash
        assert len(steps) == 1

    @pytest.mark.asyncio
    async def test_prose_response_snippet_is_logged(self, gemini, caplog):
        _, responses = gemini
        responses[:] = [
            "I'm sorry, but I cannot help with that request.",
            json.dumps({"tool_name": "web_search",
                        "tool_args": {"query": "quantum"}}),
        ]
        with caplog.at_level("WARNING"):
            steps = await planner_mod.plan_task(Task(goal="research quantum computing"))
        assert "Unparseable planner response" in caplog.text
        assert len(steps) >= 1  # recovered via tool-selector or last resort


class TestTruncatedPlanRecovery:
    """max_output_tokens truncation must not kill otherwise-valid plans."""

    GOOD_STEPS: ClassVar[list[dict[str, Any]]] = [
        {"description": "Find news",
         "tool_name": "web_search",
         "tool_args": {"query": "quantum computing"}},
        {"description": "Summarize findings",
         "tool_name": "summarize_text",
         "tool_args": {"text": "{{step_0_result}}"}},
    ]

    def test_salvage_recovers_completed_steps(self):
        truncated = json.dumps(self.GOOD_STEPS)[:-1] + ',\n  {"description": "Wri'
        out = planner_mod._parse_steps_json(truncated)
        assert [s["tool_name"] for s in out] == ["web_search", "summarize_text"]

    @pytest.mark.asyncio
    async def test_truncated_plan_used_without_correction_round(self, gemini):
        calls, responses = gemini
        responses[:] = [json.dumps(self.GOOD_STEPS)[:-1] + ', {"tool_name": "wri']
        steps = await planner_mod.plan_task(
            Task(goal="research quantum computing breakthroughs")
        )
        assert len(calls) == 1  # salvaged -> NO corrective re-plan round
        assert [s.tool_name for s in steps] == ["web_search", "summarize_text"]

    @pytest.mark.asyncio
    async def test_planner_budget_raised(self, gemini):
        calls, responses = gemini
        responses[:] = [
            json.dumps([{"description": "a", "tool_name": "web_search",
                         "tool_args": {"query": "x"}}])
        ]
        await planner_mod.plan_task(Task(goal="research fusion energy"))
        assert calls[0]["max_tokens"] >= 8192


class TestFallbackSelectorParsing:
    @pytest.mark.asyncio
    async def test_nested_tool_args_parsed(self, gemini):
        """Brace-span parsing must survive NESTED tool_args (old regex broke)."""
        _, responses = gemini
        responses[:] = [
            "",  # main plan: safety-blocked empty
            '```json\n{"tool_name": "execute_code", "description": "gen",'
            ' "tool_args": {"code": "print(1)"}}\n```',
        ]
        steps = await planner_mod.plan_task(Task(goal="research dark matter"))
        assert len(steps) == 1
        assert steps[0].tool_name == "execute_code"
        assert steps[0].tool_args["code"] == "print(1)"


class TestCreativePipeline:
    def test_creative_goal_detected(self):
        assert planner_mod._is_creative_goal("redesign the youtube homepage")
        assert planner_mod._is_creative_goal("build a landing page for our startup")

    def test_questions_and_non_build_goals_excluded(self):
        assert not planner_mod._is_creative_goal("how do I create a website?")
        assert not planner_mod._is_creative_goal("what did we create for homepage docs?")
        assert not planner_mod._is_creative_goal("merge pr 5 into main")

    @pytest.mark.asyncio
    async def test_dead_api_still_ships_artifact_zero_llm_calls(self, gemini):
        """Planner totally down + rate limits -> deterministic HTML artifact."""
        calls, _ = gemini  # every call raises IndexError
        steps = await planner_mod.plan_task(
            Task(goal="redesign the youtube homepage that can shock the youtube")
        )
        assert len(calls) == 1  # one failed plan attempt, then ZERO further LLM use
        assert len(steps) == 1
        assert steps[0].tool_name == "execute_code"
        code = steps[0].tool_args["code"]
        assert "<!DOCTYPE html>" in code
        assert ".html" in steps[0].description
