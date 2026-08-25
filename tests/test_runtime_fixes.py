"""Tests for runtime fixes: execute_code temp-file handling (WinError 32)
and planner diagnostics for empty/unparseable Gemini responses.
"""

from __future__ import annotations

import json
from typing import Any

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


class TestPlannerDiagnostics:
    @pytest.fixture()
    def gemini(self, monkeypatch):
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
