"""Regression tests for the credential-safety & failure-honesty fixes:

1. .env is resolved to the PROJECT ROOT everywhere, not the process CWD —
   credentials saved from the dashboard land where Settings actually reads them
   (the "new machine / different launch directory" bug).
2. Settings reload mutates the shared singleton IN PLACE so modules holding
   ``from agent.config import settings`` references stay fresh, and the Gemini
   key rotator picks up keys added at runtime without a restart.
3. A task whose steps ALL fail is reported as FAILED (not silently
   "completed") with actionable credential-missing guidance.
4. Telegram HTML parse crashes are avoided: dynamic text is escaped and
   messages retry as plain text when entities can't be parsed.
"""

from __future__ import annotations

from typing import Any

import pytest

import agent.config as config_mod
import agent.orchestrator as orch
import agent.telegram as tg
from agent.core import gemini_client
from agent.core.memory import memory_store
from agent.models import StepStatus, Task, TaskStep, ToolResult


class TestEnvFileResolution:
    def test_env_file_points_at_project_root(self):
        assert config_mod._ENV_FILE == config_mod._PROJECT_ROOT / ".env"
        # Settings reads the very file credentials_routes writes to.
        assert config_mod.settings.gemini_api_key  # loaded from project .env


class TestSettingsReload:
    def test_reload_mutates_singleton_in_place(self, monkeypatch):
        before = config_mod.settings
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        config_mod.reload_settings()
        assert config_mod.settings is before  # same object, all refs stay fresh
        assert config_mod.settings.gemini_model == "gemini-3.5-flash-lite"

    def test_reload_rotator_picks_up_new_keys(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "aaa,bbb")
        config_mod.reload_settings()
        assert gemini_client.rotator.key_count == 2
        assert set(gemini_client.rotator._keys) == {"aaa", "bbb"}

        monkeypatch.setenv("GEMINI_API_KEY", "zzz")
        config_mod.reload_settings()
        assert gemini_client.rotator.key_count == 1
        assert gemini_client.rotator._keys == ["zzz"]


class TestExecutorEnvSnapshot:
    def test_snapshot_reads_project_env_not_cwd(self, monkeypatch, tmp_path):
        fake_env = tmp_path / ".env"
        fake_env.write_text('GITHUB_TOKEN="ghp_fake"\nNUMBER=42\n# comment\ngarbage\n', encoding="utf-8")
        monkeypatch.setattr(config_mod, "_ENV_FILE", fake_env)

        import agent.core.executor as ex

        snap = ex._env_snapshot()
        assert snap["GITHUB_TOKEN"] == "ghp_fake"
        assert snap["NUMBER"] == "42"
        assert "# comment" not in snap


class TestCredentialHint:
    def test_hint_for_missing_api_key(self):
        err = "Received standard status code 403. GCLOUD: The API key is invalid."
        hint = orch._credential_hint(err)
        assert any(k in hint for k in ("GOOGLE_SEARCH_API_KEY", "GEMINI_API_KEY"))

    def test_hint_for_github_401(self):
        hint = orch._credential_hint("git push returned 401 Bad credentials (token expired)")
        assert "GITHUB_TOKEN" in hint

    def test_no_hint_for_unrelated_error(self):
        assert orch._credential_hint("Amount computed as $12.50") == ""


class TestAllFailedMarksFailed:
    async def _run_task(self, monkeypatch, steps: list[TaskStep], realize=(None, [])):
        async def fake_execute_step(step: TaskStep, context: dict[str, Any]) -> ToolResult:
            res = _STEP_RESULTS[step.order]
            if res.success:
                step.status = StepStatus.SUCCESS
                step.result = res.output
            else:
                step.status = StepStatus.FAILED
                step.error = res.error
            return res

        fake_plan = async_plan(realize[0] if realize[0] is not None else steps)
        monkeypatch.setattr(orch, "plan_task", fake_plan)
        monkeypatch.setattr(orch, "execute_step", fake_execute_step)
        monkeypatch.setattr(memory_store, "save_task_outcome", lambda *a, **k: None)
        monkeypatch.setattr(memory_store, "prefetch", lambda *a, **k: "")
        monkeypatch.setattr(memory_store, "search", lambda *a, **k: [])
        monkeypatch.setattr(memory_store, "extract_and_store", lambda *a, **k: 0)
        monkeypatch.setattr(orch._skill_library, "plan_context", lambda *a, **k: ("", []))
        monkeypatch.setattr(orch, "_is_trivial", lambda t: False)
        orch_inst = orch.Orchestrator()
        orch_inst._maybe_create_skill = async_noop
        orch_inst._self_reflect = async_noop
        orch_inst._gemini_should_store = async_false
        monkeypatch.setattr(config_mod.settings, "gemini_full_control", False)
        monkeypatch.setattr(config_mod.settings, "telegram_bot_token", "")
        monkeypatch.setattr(config_mod.settings, "telegram_chat_id", "")
        orch_inst = orch.Orchestrator()
        orch_inst.memory = memory_store
        return await orch_inst.handle_task(Task(goal="Build a market research report using premium data sources"))

    @pytest.mark.asyncio
    async def test_zero_successes_becomes_failed_with_hint(self, monkeypatch):
        global _STEP_RESULTS
        _STEP_RESULTS = {
            1: ToolResult(success=False, output="", error="The API key is invalid"),
            2: ToolResult(success=False, output="", error="Python 3.13 not available"),
        }
        steps = _make_steps(2)
        task = await self._run_task(monkeypatch, steps)
        assert task.status.value == "failed"
        assert task.result == ""
        assert "could not be completed" in task.error
        assert "API key is invalid" in task.error
        assert "Hint:" in task.error

    @pytest.mark.asyncio
    async def test_partial_failure_still_completed_but_transparent(self, monkeypatch):
        global _STEP_RESULTS
        _STEP_RESULTS = {
            1: ToolResult(success=True, output="Report drafted."),
            2: ToolResult(success=False, output="", error="GitHub API 403 forbidden"),
        }
        steps = _make_steps(2)
        task = await self._run_task(monkeypatch, steps)
        assert task.status.value == "completed"
        assert "of 2 step(s) failed" in task.result
        assert "GITHUB_TOKEN" in task.result


class TestTelegramEscaping:
    def test_esc_neutralizes_raw_html(self):
        assert tg._esc("<!DOCTYPE html>") == "&lt;!DOCTYPE html&gt;"
        assert "&amp;" in tg._esc("Ta & B")  # & never left raw

    @pytest.mark.asyncio
    async def test_send_message_falls_back_to_plain_text(self, monkeypatch):
        sent_payloads: list[dict[str, Any]] = []

        class FakeResp:
            def __init__(self, ok: bool, desc: str = ""):
                self._ok, self._desc = ok, desc

            def json(self) -> dict[str, Any]:
                if self._ok:
                    return {"ok": True, "result": {"message_id": 7}}
                return {"ok": False, "description": self._desc}

        class FakePoster:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url: str, **kwargs: Any):
                payload = dict(kwargs.get("data", {}))
                sent_payloads.append(payload)
                if "parse_mode" in payload:
                    return FakeResp(False, "Bad Request: can't parse entities: Unsupported start tag <!DOCTYPE>")
                assert "text" in payload
                return FakeResp(True)

        monkeypatch.setattr(config_mod.settings, "telegram_bot_token", "test:token")
        monkeypatch.setattr(config_mod.settings, "telegram_chat_id", "123")
        monkeypatch.setattr(tg.httpx, "AsyncClient", lambda timeout: FakePoster())

        result = await tg.send_message("Result: <!DOCTYPE html>")
        assert result is not None
        assert len(sent_payloads) == 2
        assert "parse_mode" in sent_payloads[0]
        assert "parse_mode" not in sent_payloads[1]
        assert sent_payloads[1]["text"] == "Result: <!DOCTYPE html>"


def _make_steps(n: int) -> list[TaskStep]:
    return [
        TaskStep(
            order=i + 1,
            description=f"Mock step {i + 1}",
            tool_name="web_search",
            tool_args={"query": "test"},
        )
        for i in range(n)
    ]


_STEP_RESULTS: dict[int, ToolResult] = {}


def async_plan(steps):
    async def _plan(*a, **k):
        return [s.model_copy(deep=True) for s in steps]

    return _plan


async def async_noop(*a, **k):
    return None


async def async_false(*a, **k):
    return False
