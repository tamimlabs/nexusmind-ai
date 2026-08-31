"""Tests for deterministic routing (Hermes/OpenClaw adaptations).

Covers: the zero-cost command gate, the planner's tool-name repair ladder,
corrective re-plan with catalog feedback, and dynamic prompt/tool consistency.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import agent.core.command_gate as gate
import agent.core.planner as planner_mod
from agent.core.planner import repair_tool_name
from agent.models import Task

_VALID = [
    "web_search",
    "fetch_url",
    "run_command",
    "execute_code",
    "read_file",
    "write_file",
    "list_directory",
    "summarize_text",
    "extract_data",
    "parse_json",
    "github_merge_pr",
]


class TestLooksLikeCommand:
    def test_simple_command(self):
        assert gate.looks_like_command("/help")

    def test_command_with_args(self):
        assert gate.looks_like_command("/memory pr reviews")

    def test_absolute_path_is_not_command(self):
        # Hermes heuristic: first token containing "/" is a path, not a command
        assert not gate.looks_like_command("/Users/tony/notes.md can you fix this?")

    def test_plain_text(self):
        assert not gate.looks_like_command("research AI news and summarize")

    def test_empty(self):
        assert not gate.looks_like_command("")

    def test_nested_slash(self):
        assert not gate.looks_like_command("/a/b stuff")


class TestRepairLadder:
    def test_exact(self):
        assert repair_tool_name("web_search", _VALID) == "web_search"

    def test_case_and_separator_normalization(self):
        assert repair_tool_name("Web Search", _VALID) == "web_search"
        assert repair_tool_name("  WEB-SEARCH ", _VALID) == "web_search"

    def test_tool_suffix_strip(self):
        assert repair_tool_name("run_command_tool", _VALID) == "run_command"

    def test_alias_map(self):
        assert repair_tool_name("search", _VALID) == "web_search"
        assert repair_tool_name("bash", _VALID) == "run_command"
        assert repair_tool_name("cat", _VALID) == "read_file"
        assert repair_tool_name("summary", _VALID) == "summarize_text"

    def test_fuzzy_match(self):
        assert repair_tool_name("websearch", _VALID) == "web_search"
        assert repair_tool_name("githubmerge", _VALID) == "github_merge_pr"

    def test_garbage_returns_none(self):
        assert repair_tool_name("zzzqqq", _VALID) is None

    def test_empty_returns_none(self):
        assert repair_tool_name("", _VALID) is None


class TestPlanValidation:
    @pytest.fixture()
    def gemini(self, monkeypatch):
        """Fake generate_content; plan_task imports it lazily per call."""
        calls: list[dict[str, Any]] = []
        responses: list[str] = []

        async def fake_generate(**kwargs: Any) -> str:
            calls.append(kwargs)
            return responses.pop(0)

        monkeypatch.setattr("agent.core.gemini_client.generate_content", fake_generate)
        return calls, responses

    @pytest.mark.asyncio
    async def test_invalid_tools_trigger_corrective_replan(self, gemini):
        calls, responses = gemini
        responses[:] = [
            json.dumps(
                [
                    {
                        "description": "Search",
                        "tool_name": "WebSearch",
                        "tool_args": {"query": "x"},
                    },
                    {"description": "Deploy", "tool_name": "deploy_to_kubernetes", "tool_args": {}},
                ]
            ),
            json.dumps(
                [
                    {
                        "description": "Search",
                        "tool_name": "web_search",
                        "tool_args": {"query": "x"},
                    },
                    {
                        "description": "Summarize",
                        "tool_name": "summarize_text",
                        "tool_args": {"text": "{{step_0_result}}"},
                    },
                ]
            ),
        ]
        steps = await planner_mod.plan_task(Task(goal="research something"))
        assert len(calls) == 2  # original + exactly one corrective round
        assert "DO NOT exist" in calls[1]["user"]
        assert "deploy_to_kubernetes" in calls[1]["user"]
        assert [s.tool_name for s in steps] == ["web_search", "summarize_text"]

    @pytest.mark.asyncio
    async def test_still_invalid_after_retry_are_dropped(self, gemini):
        _, responses = gemini
        responses[:] = [
            json.dumps([{"description": "a", "tool_name": "zzzqqq"}]),
            json.dumps(
                [
                    {
                        "description": "ok",
                        "tool_name": "read_file",
                        "tool_args": {"path": "output/x.md"},
                    },
                    {"description": "bad", "tool_name": "warp_drive_engage"},
                ]
            ),
        ]
        steps = await planner_mod.plan_task(Task(goal="read the config file"))
        assert [s.tool_name for s in steps] == ["read_file"]

    @pytest.mark.asyncio
    async def test_all_invalid_falls_back(self, gemini):
        _, responses = gemini
        responses[:] = [
            json.dumps(
                [
                    {"description": "a", "tool_name": "zzzqqq"},
                    {"description": "b", "tool_name": "qqqzzz"},
                ]
            ),
            json.dumps([{"description": "c", "tool_name": "xxxwww"}]),
        ]
        steps = await planner_mod.plan_task(Task(goal="explain how git works"))
        assert len(steps) == 1
        assert steps[0].tool_name == "web_search"  # research-hint fallback

    @pytest.mark.asyncio
    async def test_valid_plan_needs_single_call(self, gemini):
        calls, responses = gemini
        responses[:] = [
            json.dumps(
                [
                    {"description": "s", "tool_name": "Web Search", "tool_args": {"query": "x"}},
                ]
            ),
        ]
        steps = await planner_mod.plan_task(Task(goal="find news about x"))
        assert len(calls) == 1  # repaired locally, no corrective round needed
        assert steps[0].tool_name == "web_search"


class TestCommandGate:
    @pytest.mark.asyncio
    async def test_help_lists_commands(self):
        out = await gate.handle_command("/help")
        assert out and "/memory" in out and "/skills" in out

    @pytest.mark.asyncio
    async def test_start_aliases_help(self):
        assert await gate.handle_command("/start") == await gate.handle_command("/help")

    @pytest.mark.asyncio
    async def test_tools_lists_live_registry(self):
        out = await gate.handle_command("/tools")
        assert "web_search" in out

    @pytest.mark.asyncio
    async def test_status_reports_model(self):
        out = await gate.handle_command("/status")
        assert "Agent Online" in out

    @pytest.mark.asyncio
    async def test_unknown_slash_falls_through(self):
        assert await gate.handle_command("/frobnicate now") is None

    @pytest.mark.asyncio
    async def test_non_command_falls_through(self):
        assert await gate.handle_command("research latest AI news") is None

    @pytest.mark.asyncio
    async def test_path_is_not_a_command(self):
        assert await gate.handle_command("/Users/tony/notes.md fix typo") is None

    @pytest.mark.asyncio
    async def test_tasks_uses_registered_provider(self):
        gate.register_provider(
            "recent_tasks",
            lambda: [
                {"status": "completed", "goal": "long goal here"},
                {"status": "failed", "goal": "short one"},
            ],
        )
        try:
            out = await gate.handle_command("/tasks 2")
        finally:
            gate._providers.pop("recent_tasks", None)
        assert "[completed]" in out and "[failed]" in out

    @pytest.mark.asyncio
    async def test_memory_isolated_from_real_db(self, monkeypatch):
        import agent.core.memory as mem_mod

        monkeypatch.setattr(mem_mod.memory_store, "search", lambda q, top_k=5: [])
        out = await gate.handle_command("/memory anything at all")
        assert "No matching memories." in out

    @pytest.mark.asyncio
    async def test_skills_uses_injected_library(self, monkeypatch):
        import agent.core.skill_library as sl_mod

        class FakeLib:
            def apply_transitions(self):
                pass

            def list_skills(self):
                return [{"name": "demo-flow", "description": "Use when demoing.", "use_count": 3}]

        monkeypatch.setattr(sl_mod, "skill_library", FakeLib())
        out = await gate.handle_command("/skills demo")
        assert "demo-flow" in out and "(3 uses)" in out


class TestPromptCatalogConsistency:
    def test_catalog_lists_canonical_names_with_docs(self):
        section = planner_mod._tool_catalog_section()
        assert "- web_search:" in section
        for name in planner_mod.canonical_tool_names():
            assert f"- {name}" in section


class TestCommandAPI:
    def test_endpoint_handles_known_commands(self):
        from fastapi.testclient import TestClient

        from api.main import app

        client = TestClient(app)
        res = client.post("/api/command", json={"text": "/status"})
        assert res.status_code == 200
        body = res.json()
        assert body["handled"] is True and "Agent Online" in body["response"]

    def test_endpoint_falls_through_for_natural_language(self):
        from fastapi.testclient import TestClient

        from api.main import app

        client = TestClient(app)
        res = client.post("/api/command", json={"text": "build me a rocket"})
        assert res.status_code == 200
        assert res.json() == {"handled": False, "response": None}
