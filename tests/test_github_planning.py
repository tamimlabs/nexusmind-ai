"""Tests for GitHub task routing — the agent must NEVER web-search these."""

import asyncio

import pytest

from agent.core.planner import plan_task
from agent.models import Task, TaskStep, ToolResult


@pytest.fixture(autouse=True)
def _ensure_skills_loaded():
    import agent  # noqa: F401 — triggers skill/tool registration


class TestGithubGoalDetection:
    def test_detects_common_phrasings(self):
        from agent.core.planner import _is_github_goal

        goals = [
            "see my repository and the pr and if needed merge or reject",
            "check my repo",
            "merge PR #5",
            "review my pull requests",
            "look at github issues",
            "list all prs in tamimlabs/nexusmind-ai",
            "reject the open pullrequest",
        ]
        for goal in goals:
            assert _is_github_goal(goal), f"Should detect: {goal}"

    def test_non_github_goals_not_matched(self):
        from agent.core.planner import _is_github_goal

        for goal in ["write me a poem", "what is quantum computing", "summarize this article"]:
            assert not _is_github_goal(goal), f"Should NOT match: {goal}"


class TestGithubPipeline:
    def _plan(self, goal: str):
        from agent.core.planner import plan_task

        return asyncio.run(plan_task(Task(goal=goal)))

    def test_user_scenario_full_pipeline(self):
        """The exact scenario from the bug report."""
        steps = self._plan("see my repository and the pr and if needed merge or reject")
        tools = [s.tool_name for s in steps]
        assert "web_search" not in tools
        assert tools[0] == "github_resolve_repo"
        assert "github_list_prs" in tools
        assert "github_review_pr" in tools
        assert tools[-1] == "github_apply_decisions"

    def test_specific_pr_number(self):
        steps = self._plan("review pr #42 in my repo and merge if it looks good")
        review = next(s for s in steps if s.tool_name == "github_review_pr")
        assert review.tool_args["pr_number"] == 42
        assert any(s.tool_name == "github_apply_decisions" for s in steps)

    def test_multiple_pr_numbers(self):
        steps = self._plan("merge pr 3 and pr #7 if they are safe")
        review = next(s for s in steps if s.tool_name == "github_review_pr")
        assert review.tool_args["pr_list"] == '[{"number": 3}, {"number": 7}]'

    def test_read_only_goal_does_not_mutate(self):
        steps = self._plan("show me the open pull requests in my repository")
        tools = [s.tool_name for s in steps]
        assert "web_search" not in tools
        assert "github_apply_decisions" not in tools
        assert tools[-1] == "summarize_text"

    def test_steps_are_ordered_and_use_templates(self):
        steps = self._plan("handle the prs in my repo")
        orders = [s.order for s in steps]
        assert orders == list(range(len(steps)))
        review = next(s for s in steps if s.tool_name == "github_review_pr")
        assert review.tool_args["repo"] == "{{step_0_result}}"
        decisions = next(s for s in steps if s.tool_name == "github_apply_decisions")
        assert "{{step_" in decisions.tool_args["decisions"]

    def test_explicit_repo_in_goal_is_forwarded(self):
        steps = self._plan("review octocat/hello-world prs and merge what passes")
        resolve = steps[0]
        assert resolve.tool_name == "github_resolve_repo"
        assert "octocat/hello-world" in resolve.tool_args["goal_text"]


class TestFallbackSafety:
    def test_parse_steps_json_returns_empty_not_websearch(self):
        from agent.core.planner import _parse_steps_json

        assert _parse_steps_json("this is not json at all") == []
        assert _parse_steps_json('{"random": 1}') == []

    def test_last_resort_action_goal_gets_diagnostic_not_websearch(self):
        from agent.core.planner import _last_resort_step

        step = _last_resort_step(Task(goal="deploy the service to production"))
        assert step.tool_name != "web_search"
        assert step.tool_name == "execute_code"

    def test_last_resort_research_goal_may_still_search(self):
        from agent.core.planner import _last_resort_step

        step = _last_resort_step(Task(goal="what is the latest news about AI"))
        assert step.tool_name == "web_search"

    def test_planner_fallback_never_websearches_actions(self, monkeypatch):
        """Even when both Gemini calls fail, action goals must not web-search."""
        from agent.core import gemini_client
        from agent.core.planner import plan_task

        async def explode(**_):
            raise RuntimeError("gemini down")

        monkeypatch.setattr(gemini_client, "generate_content", explode)
        steps = asyncio.run(plan_task(Task(goal="restart the database server")))
        assert len(steps) == 1
        assert steps[0].tool_name != "web_search"


class TestSelfCorrectionGuard:
    async def test_failed_action_tool_cannot_switch_to_web_search(self, monkeypatch):
        from agent.core import executor as ex

        @ex.register_tool("failing_action_tool_test")
        async def failing(**_) -> ToolResult:
            return ToolResult(success=False, output="", error="connection refused")

        async def fake_self_correct(error, tool_name, original_args):
            return {"switch_to": "web_search", "query": "how to restart server"}

        monkeypatch.setattr(ex, "self_correct", fake_self_correct)

        step = TaskStep(
            description="run something real",
            tool_name="failing_action_tool_test",
            tool_args={},
            order=0,
        )
        result = await ex.execute_step(step, context={})
        assert not result.success
        # The tool must NOT have been swapped to web_search
        assert step.tool_name == "failing_action_tool_test"


class TestMemoryGatedWatcher:
    """Watcher acts ONLY when a standing instruction exists in memory."""

    def _watcher(self):
        from agent.watchers.github import GitHubWatcher

        return GitHubWatcher("test_watcher", {"repo": "tamimlabs/nexusmind-ai"})

    def _event(self, number=5, title="feat: new thing"):
        return {
            "event_type": "github.pr.opened",
            "payload": {"number": number, "title": title, "author": "dev"},
        }

    async def test_no_instruction_means_no_action(self, monkeypatch):
        from agent.core import memory as memory_mod

        monkeypatch.setattr(memory_mod.memory_store, "get_by_category", lambda cat: [])
        goal = await self._watcher().process_event(self._event())
        assert goal is None  # nothing triggered at all

    async def test_instruction_triggers_goal_with_pr_target(self, monkeypatch):
        from agent.core import memory as memory_mod
        from agent.models import MemoryEntry

        instruction = MemoryEntry(
            content="when you get a pr by watcher then test and marge or deslind it with comment",
            category="instruction",
        )
        monkeypatch.setattr(
            memory_mod.memory_store,
            "get_by_category",
            lambda cat: [instruction] if cat == "instruction" else [],
        )
        goal = await self._watcher().process_event(self._event())
        assert goal is not None
        assert "#5" in goal and "tamimlabs/nexusmind-ai" in goal
        # The stored direction is carried into the goal verbatim
        assert "marge or deslind" in goal

    async def test_unrelated_instruction_is_ignored(self, monkeypatch):
        from agent.core import memory as memory_mod
        from agent.models import MemoryEntry

        unrelated = MemoryEntry(content="always write python", category="instruction")
        monkeypatch.setattr(
            memory_mod.memory_store,
            "get_by_category",
            lambda cat: [unrelated] if cat == "instruction" else [],
        )
        goal = await self._watcher().process_event(self._event())
        assert goal is None

    async def test_stored_direction_routes_to_action_pipeline(self, monkeypatch):
        """'test and marge or deslind with comment' -> merge/reject pipeline."""
        from agent.core import memory as memory_mod
        from agent.models import MemoryEntry

        instruction = MemoryEntry(
            content="when you get a pr by watcher then test and marge or deslind it with comment",
            category="instruction",
        )
        monkeypatch.setattr(
            memory_mod.memory_store,
            "get_by_category",
            lambda cat: [instruction] if cat == "instruction" else [],
        )
        goal = await self._watcher().process_event(self._event())
        steps = await plan_task(Task(goal=goal))
        tools = [s.tool_name for s in steps]
        assert "web_search" not in tools
        assert tools[0] == "github_resolve_repo"
        assert "github_apply_decisions" in tools
        review = next(s for s in steps if s.tool_name == "github_review_pr")
        assert review.tool_args.get("pr_number") == 5


class TestStandingInstructionStorage:
    """Typing a durable instruction stores it instead of executing it."""

    async def test_when_phrase_saved_not_executed(self, monkeypatch):
        from agent.models import TaskStatus
        from agent.orchestrator import Orchestrator

        saved: list[str] = []

        class FakeMemory:
            def save_instruction(self, text):
                saved.append(text)
                return True

        orch = Orchestrator()
        orch.memory = FakeMemory()

        task = await orch.handle_task(Task(goal="when you get a pr then test and merge it"))
        assert task.status == TaskStatus.COMPLETED
        assert "standing instruction" in (task.result or "").lower()
        assert saved == ["when you get a pr then test and merge it"]
        assert task.steps == []  # nothing was planned/executed

    async def test_direct_command_not_misdetected(self):
        from agent.orchestrator import _is_standing_instruction

        assert not _is_standing_instruction("merge pr #7 if ok")
        assert not _is_standing_instruction("review pull request 3")
        assert _is_standing_instruction("whenever a pr opens, review it and merge if clean")


class TestNotificationRateLimit:
    """'No instruction' Telegram notices are rate-limited (anti-spam), all watchers."""

    async def test_second_notice_within_window_is_suppressed(self, monkeypatch):
        import agent.watchers.base as base_mod
        from agent.core import memory as memory_mod
        from agent.watchers.rss import RSSWatcher

        sent: list[str] = []

        async def fake_send(msg):
            sent.append(msg)

        monkeypatch.setattr(memory_mod.memory_store, "get_by_category", lambda cat: [])
        monkeypatch.setattr("agent.telegram.is_configured", lambda: True)
        monkeypatch.setattr("agent.telegram.send_message", fake_send)
        monkeypatch.setattr(base_mod, "_last_no_instruction_notify", {})

        w = RSSWatcher("w1", {"feed_url": "http://example.com/rss"})
        await w.notify_unhandled_event("first event")
        await w.notify_unhandled_event("second event")  # same window -> suppressed

        assert len(sent) == 1

    async def test_notice_allowed_again_after_window(self, monkeypatch):
        import agent.watchers.base as base_mod
        from agent.core import memory as memory_mod
        from agent.watchers.rss import RSSWatcher

        sent: list[str] = []

        async def fake_send(msg):
            sent.append(msg)

        monkeypatch.setattr(memory_mod.memory_store, "get_by_category", lambda cat: [])
        monkeypatch.setattr("agent.telegram.is_configured", lambda: True)
        monkeypatch.setattr("agent.telegram.send_message", fake_send)
        monkeypatch.setattr(base_mod, "_last_no_instruction_notify", {})

        t = [0.0]
        monkeypatch.setattr(base_mod.time, "monotonic", lambda: t[0])

        w = RSSWatcher("w1", {"feed_url": "http://example.com/rss"})
        await w.notify_unhandled_event("a")
        t[0] += 6 * 3600 + 1  # window elapsed
        await w.notify_unhandled_event("b")

        assert len(sent) == 2

    async def test_rate_limit_is_per_watcher(self, monkeypatch):
        import agent.watchers.base as base_mod
        from agent.core import memory as memory_mod
        from agent.watchers.reddit import RedditWatcher

        sent: list[str] = []

        async def fake_send(msg):
            sent.append(msg)

        monkeypatch.setattr(memory_mod.memory_store, "get_by_category", lambda cat: [])
        monkeypatch.setattr("agent.telegram.is_configured", lambda: True)
        monkeypatch.setattr("agent.telegram.send_message", fake_send)
        monkeypatch.setattr(base_mod, "_last_no_instruction_notify", {})

        w1 = RedditWatcher("w1", {})
        w2 = RedditWatcher("w2", {})
        await w1.notify_unhandled_event("w1 event")
        await w2.notify_unhandled_event("w2 event")  # different watcher -> allowed

        assert len(sent) == 2


class TestAllWatchersMemoryGated:
    """The memory gate applies uniformly to every auto-triggering watcher."""

    def _instruction(self):
        from agent.models import MemoryEntry

        return MemoryEntry(
            content="whenever something new shows up, summarize it for me", category="instruction"
        )

    async def test_rss_watcher_gate(self, monkeypatch):
        from agent.core import memory as memory_mod
        from agent.watchers.rss import RSSWatcher

        event = {
            "event_type": "rss.new_item",
            "payload": {"title": "Big news", "link": "http://x/1", "description": ""},
        }

        # No instruction -> silent
        monkeypatch.setattr(memory_mod.memory_store, "get_by_category", lambda cat: [])
        assert await RSSWatcher("rss1", {"feed_url": "http://x"}).process_event(event) is None

        # With instruction -> gated goal carrying the owner's words + event details
        monkeypatch.setattr(
            memory_mod.memory_store, "get_by_category", lambda cat: [self._instruction()]
        )
        goal = await RSSWatcher("rss1", {"feed_url": "http://x"}).process_event(event)
        assert goal and "summarize it for me" in goal and "Big news" in goal

    async def test_jira_watcher_gate(self, monkeypatch):
        from agent.core import memory as memory_mod
        from agent.models import MemoryEntry
        from agent.watchers.jira import JiraWatcher

        event = {
            "event_type": "jira.issue.new",
            "payload": {
                "key": "PROJ-9",
                "title": "Login broken",
                "status": "new",
                "comment_count": 0,
            },
        }
        monkeypatch.setattr(memory_mod.memory_store, "get_by_category", lambda cat: [])
        assert await JiraWatcher("jira1", {}).process_event(event) is None

        # Domain-specific instruction is required — generic ones don't unlock Jira
        jira_instruction = MemoryEntry(
            content="when a new jira issue appears, analyze it and suggest a fix",
            category="instruction",
        )
        monkeypatch.setattr(
            memory_mod.memory_store,
            "get_by_category",
            lambda cat: [jira_instruction] if cat == "instruction" else [],
        )
        goal = await JiraWatcher("jira1", {}).process_event(event)
        assert goal and "PROJ-9" in goal

    async def test_cron_watcher_is_pre_authorized(self):
        """Owner-configured cron goals run without a stored instruction."""
        from agent.watchers.cron import CronWatcher

        w = CronWatcher("cron1", {"goal": "Check deployment health"})
        assert w.INSTRUCTION_KEYWORDS == ()  # no gate keywords by design
        goal = await w.process_event({"event_type": "cron.trigger", "payload": {}})
        assert goal == "Check deployment health"

    def test_every_auto_watcher_declares_keywords(self):
        from agent.watchers.discord import DiscordWatcher
        from agent.watchers.email_watcher import EmailWatcher
        from agent.watchers.gitlab import GitLabWatcher
        from agent.watchers.hackernews import HackerNewsWatcher
        from agent.watchers.reddit import RedditWatcher
        from agent.watchers.slack import SlackWatcher

        for cls in (
            DiscordWatcher,
            EmailWatcher,
            GitLabWatcher,
            HackerNewsWatcher,
            RedditWatcher,
            SlackWatcher,
        ):
            assert cls.INSTRUCTION_KEYWORDS, f"{cls.__name__} missing INSTRUCTION_KEYWORDS"


class TestLogicAuditRegressions:
    """Regressions for logic flaws found in the full-suite audit."""

    async def test_question_goals_stay_read_only(self):
        """'explain how git handles merges' must NOT build an apply pipeline."""
        steps = await plan_task(Task(goal="explain how git handles merges in my repo"))
        tools = [s.tool_name for s in steps]
        assert "github_apply_decisions" not in tools
        assert tools[-1] == "summarize_text"

    def test_mid_sentence_trigger_words_still_execute(self, monkeypatch):
        """'merge pr #7 always using squash' is a command, not a policy."""
        from agent.orchestrator import _is_standing_instruction

        assert not _is_standing_instruction("merge pr #7 always using squash commits")
        assert not _is_standing_instruction("review the pr and by default branch checks too")
        # Genuine directives still detected
        assert _is_standing_instruction("always merge clean prs with squash")
        assert _is_standing_instruction("if a new pr arrives, review and merge if clean")

    def test_instructions_survive_memory_churn(self, monkeypatch, tmp_path):
        """Task outcomes flooding memory must NEVER evict standing instructions."""
        import agent.core.memory as mem_mod
        from agent.models import MemoryEntry

        monkeypatch.setattr(mem_mod, "_DB_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(mem_mod, "_MAX_ENTRIES", 50)
        store = mem_mod.MemoryStore()

        store.save_instruction("when a pr arrives, review and merge if clean")
        for i in range(120):  # flood well past the capped limit
            store.add(
                MemoryEntry(
                    content=f"unique outcome number {i} for eviction test", category="task_outcome"
                )
            )

        surviving = [e.content for e in store.get_by_category("instruction")]
        assert any("merge if clean" in c for c in surviving)
        # Eviction actually happened — episodic entries were trimmed to the cap
        assert store.size <= 50


class TestGithubSkillUnits:
    def test_parse_repo_url_https(self):
        from agent.skills.github.skill import _parse_repo_url

        assert (
            _parse_repo_url("https://github.com/tamimlabs/nexusmind-ai.git")
            == "tamimlabs/nexusmind-ai"
        )
        assert (
            _parse_repo_url("https://github.com/tamimlabs/nexusmind-ai/")
            == "tamimlabs/nexusmind-ai"
        )

    def test_parse_repo_url_ssh(self):
        from agent.skills.github.skill import _parse_repo_url

        assert (
            _parse_repo_url("git@github.com:tamimlabs/nexusmind-ai.git") == "tamimlabs/nexusmind-ai"
        )

    async def test_resolve_repo_from_explicit_text(self):
        from agent.skills.github.skill import github_resolve_repo

        result = await github_resolve_repo(goal_text="check prs in octocat/hello-world please")
        assert result.success
        assert result.output == "octocat/hello-world"

    async def test_resolve_repo_fails_without_any_source(self, monkeypatch):
        from agent.skills.github import skill as gh

        async def no_git(*_a, **_k):
            raise FileNotFoundError("not a git repo")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_git)
        monkeypatch.setattr(gh, "_default_repo", lambda: "")
        result = await gh.github_resolve_repo(goal_text="my repository please")
        assert not result.success
        assert "GITHUB_DEFAULT_REPO" in (result.error or "")

    async def test_heuristic_verdict_rejects_conflicts(self):
        from agent.skills.github.skill import _heuristic_verdict

        verdict = _heuristic_verdict({"mergeable_state": "dirty", "files": []})
        assert verdict["decision"] == "reject"

        verdict = _heuristic_verdict({"draft": True, "files": []})
        assert verdict["decision"] == "skip"

    async def test_apply_decisions_skips_uncertain(self):
        from agent.skills.github.skill import github_apply_decisions

        decisions = '[{"number": 1, "decision": "reject", "confidence": 0.2, "reason": "unsure"}]'
        result = await github_apply_decisions(repo="x/y", decisions=decisions)
        assert result.success
        assert "skipped rejection" in result.output

    async def test_apply_decisions_dry_run(self):
        from agent.skills.github.skill import github_apply_decisions

        decisions = '[{"number": 2, "decision": "merge", "confidence": 0.9, "reason": "clean"}]'
        result = await github_apply_decisions(repo="x/y", decisions=decisions, dry_run=True)
        assert result.success
        assert "would merge" in result.output
