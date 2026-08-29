"""Tests for the adaptive step-by-step agent loop (agent/core/agent_loop.py).

The loop must behave like a careful IDE agent, not a one-shot script:
one decision -> one execution -> result fed back -> next decision, with
self-correction, verification, and a generous budget for large projects.
"""

from pathlib import Path

import pytest

from agent.core.agent_loop import _build_transcript, _validate_step_decision, run_adaptive_loop
from agent.models import StepStatus, Task, TaskStep, ToolResult


def _task(goal: str = "Build a portfolio website into projects/testsite/ step by step.") -> Task:
    return Task(goal=goal)


async def _execute_ok(step, context):
    step.status = StepStatus.SUCCESS
    step.result = f"RESULT({step.description})"
    return ToolResult(success=True, output=step.result)


class TestStepByStepFeedback:
    @pytest.mark.asyncio
    async def test_one_decision_one_execution_in_order(self):
        """Every decision is followed by exactly one execution, in order."""
        plan = [
            {
                "kind": "step",
                "description": "A",
                "tool_name": "execute_code",
                "tool_args": {"code": "print(1)"},
            },
            {
                "kind": "step",
                "description": "B",
                "tool_name": "list_directory",
                "tool_args": {"path": "projects/testsite"},
            },
            {"done": True, "result": "done summary"},
        ]
        cursor = {"i": 0}

        async def decide(state):
            i = cursor["i"]
            cursor["i"] = i + 1
            return plan[i]

        executed_steps = []

        async def execute(step, context):
            executed_steps.append(step.description)
            return await _execute_ok(step, context)

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=execute)

        assert outcome.done is True
        assert outcome.summary == "done summary"
        assert len(task.steps) == 2
        assert executed_steps == ["A", "B"]
        assert [s.tool_name for s in task.steps] == ["execute_code", "list_directory"]
        assert all(s.status == StepStatus.SUCCESS for s in task.steps)

    @pytest.mark.asyncio
    async def test_result_is_fed_back_before_next_decision(self):
        """The transcript seen by decision #2 contains decision #1's RESULTS."""
        transcripts: list[str] = []

        async def decide(state):
            transcripts.append(state["transcript"])
            if "RESULT(A)" in state["transcript"]:
                return {"done": True, "result": "saw the result"}
            return {
                "kind": "step",
                "description": "A",
                "tool_name": "write_file",
                "tool_args": {"path": "x.txt", "content": "hi"},
            }

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=_execute_ok)

        assert outcome.done is True
        assert outcome.summary == "saw the result"
        # decision #1 saw "none yet", decision #2 saw the executed RESULT of A
        assert "None yet" in transcripts[0]
        assert "write_file" in transcripts[1]
        assert "RESULT(A)" in transcripts[1]

    @pytest.mark.asyncio
    async def test_context_includes_step_results_both_forms(self):
        """Both 0- and 1-indexed result keys land in context for {{step_N_result}}."""
        context: dict = {}
        decisions = [
            {
                "kind": "step",
                "description": "A",
                "tool_name": "write_file",
                "tool_args": {"path": "x.txt", "content": "hi"},
            },
            {"done": True, "result": "ok"},
        ]
        i = {"n": 0}

        async def decide(state):
            out = decisions[i["n"]]
            i["n"] += 1
            return out

        async def execute(step, context):
            return await _execute_ok(step, context)

        task = _task()
        await run_adaptive_loop(task, context, decide_fn=decide, execute_fn=execute)
        assert "step_0_result" in context
        assert "step_1_result" in context
        assert context["step_0_result"] == "RESULT(A)"
        assert context["step_0_result"] == context["step_1_result"]


class TestSelfCorrection:
    @pytest.mark.asyncio
    async def test_failure_error_seen_and_corrected(self):
        """After a failed step, the next decision sees the ERROR and can retry."""
        transcripts: list[str] = []

        async def decide(state):
            transcripts.append(state["transcript"])
            if (
                "ModuleNotFoundError" in state["transcript"]
                and "run_command" in state["transcript"]
            ):
                return {"done": True, "result": "fixed and installed"}
            if "ModuleNotFoundError" in state["transcript"]:
                return {
                    "kind": "step",
                    "description": "install first",
                    "tool_name": "run_command",
                    "tool_args": {"command": "pip install requests"},
                }
            return {
                "kind": "step",
                "description": "use requests",
                "tool_name": "execute_code",
                "tool_args": {"code": "import requests"},
            }

        async def execute(step, context):
            if step.tool_name == "execute_code" and "import requests" in (step.tool_args or {}).get(
                "code", ""
            ):
                step.status = StepStatus.FAILED
                step.error = "ModuleNotFoundError: No module named 'requests'"
                return ToolResult(success=False, output="", error=step.error)
            return await _execute_ok(step, context)

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=execute)

        assert outcome.done is True
        tool_names = [s.tool_name for s in task.steps]
        assert tool_names == ["execute_code", "run_command"]  # corrected the route
        assert "ModuleNotFoundError" in transcripts[1]
        assert task.steps[0].status == StepStatus.FAILED
        assert task.steps[1].status == StepStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_three_consecutive_failures_stop_loop(self):
        async def decide(state):
            return {
                "kind": "step",
                "description": "keep failing",
                "tool_name": "run_command",
                "tool_args": {"command": "false"},
            }

        async def execute(step, context):
            step.status = StepStatus.FAILED
            step.error = "boom"
            return ToolResult(success=False, output="", error="boom")

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=execute)

        assert outcome.done is False
        assert len(task.steps) >= 3
        assert "consecutive" in outcome.aborted_reason

    @pytest.mark.asyncio
    async def test_one_failure_then_success_does_not_abort(self):
        calls = {"n": 0}

        async def decide(state):
            calls["n"] += 1
            if calls["n"] >= 3:
                return {"done": True, "result": "recovered after a retry"}
            return {
                "kind": "step",
                "description": "try",
                "tool_name": "execute_code",
                "tool_args": {"code": "x=1"},
            }

        async def execute(step, context):
            if calls["n"] == 1:
                step.status = StepStatus.FAILED
                step.error = "first attempt broke"
                return ToolResult(success=False, output="", error="first attempt broke")
            return await _execute_ok(step, context)

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=execute)

        assert outcome.done is True  # the single failure did not trip the abort guard
        assert outcome.aborted_reason == ""
        assert len(task.steps) == 2
        assert task.steps[0].status == StepStatus.FAILED
        assert task.steps[1].status == StepStatus.SUCCESS


class TestVerificationAndLongRuns:
    @pytest.mark.asyncio
    async def test_large_project_many_steps(self):
        """A 12-step build runs start to finish, one step at a time."""
        steps = [
            {
                "kind": "step",
                "description": f"step {i}",
                "tool_name": "write_file",
                "tool_args": {"path": f"projects/testsite/file{i}.txt", "content": f"content {i}"},
            }
            for i in range(12)
        ]
        steps.append({"done": True, "result": "built everything"})
        i = {"n": 0}

        async def decide(state):
            out = steps[i["n"]]
            i["n"] += 1
            return out

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=_execute_ok)

        assert outcome.done is True
        assert len(task.steps) == 12
        assert [s.order for s in task.steps] == list(range(12))
        assert all(s.status == StepStatus.SUCCESS for s in task.steps)

    @pytest.mark.asyncio
    async def test_verify_before_done_pattern(self):
        """The loop executes a verification step (list_directory) before DONE."""
        decisions = [
            {
                "kind": "step",
                "description": "write index",
                "tool_name": "write_file",
                "tool_args": {"path": "projects/testsite/index.html", "content": "<h1>hi</h1>"},
            },
            {
                "kind": "step",
                "description": "verify files",
                "tool_name": "list_directory",
                "tool_args": {"path": "projects/testsite"},
            },
            {
                "kind": "step",
                "description": "read back",
                "tool_name": "read_file",
                "tool_args": {"path": "projects/testsite/index.html"},
            },
            {"done": True, "result": "verified: index.html exists"},
        ]
        i = {"n": 0}
        tool_order: list[str] = []

        async def decide(state):
            out = decisions[i["n"]]
            i["n"] += 1
            return out

        async def execute(step, context):
            tool_order.append(step.tool_name)
            return await _execute_ok(step, context)

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=execute)

        assert outcome.summary.startswith("verified")
        assert tool_order == ["write_file", "list_directory", "read_file"]


class TestGuards:
    @pytest.mark.asyncio
    async def test_done_immediately(self):
        task = _task()

        async def decide(state):
            return {"done": True, "result": "nothing needed"}

        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=_execute_ok)
        assert outcome.done is True
        assert outcome.summary == "nothing needed"
        assert task.steps == []

    @pytest.mark.asyncio
    async def test_step_budget_stops_loop(self):
        async def decide(state):
            return {
                "kind": "step",
                "description": "keep working",
                "tool_name": "execute_code",
                "tool_args": {"code": "x=1"},
            }

        task = _task()
        outcome = await run_adaptive_loop(
            task, {}, decide_fn=decide, execute_fn=_execute_ok, max_steps=5
        )
        assert outcome.done is False
        assert len(task.steps) == 5
        assert "budget" in outcome.aborted_reason

    @pytest.mark.asyncio
    async def test_malformed_decision_is_fed_back_and_repaired(self):
        """A truly invalid decision gets a SYSTEM NOTE feedback, then recovers."""
        fed_back: list[str] = []

        async def decide(state):
            if state["feedback"]:
                fed_back.append(state["feedback"])
                return {"done": True, "result": "recovered"}
            return {"description": "write a file", "tool_name": "no_such_tool_x", "tool_args": {}}

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=_execute_ok)

        assert outcome.done is True
        assert outcome.summary == "recovered"
        assert fed_back and "SYSTEM NOTE" in fed_back[0]
        # nothing executed: the only valid decisions were the bad one then DONE
        assert task.steps == []

    @pytest.mark.asyncio
    async def test_alias_tool_repaired_in_loop(self):
        """A misspelled tool alias is repaired deterministically, not rejected."""
        calls = {"n": 0}

        async def decide(state):
            calls["n"] += 1
            if calls["n"] >= 2:
                return {"done": True, "result": "wrote the file"}
            return {
                "kind": "step",
                "description": "write a file",
                "tool_name": "create_file",
                "tool_args": {"path": "a.txt", "content": "x"},
            }

        task = _task()
        outcome = await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=_execute_ok)

        assert outcome.done is True
        assert task.steps[0].tool_name == "write_file"  # alias repaired to canonical


class TestValidation:
    def test_validate_step_decision_repairs_alias(self):
        repaired, err = _validate_step_decision(
            {
                "description": "write",
                "tool_name": "create_file",
                "tool_args": {"path": "p", "content": "c"},
            }
        )
        assert err == ""
        assert repaired["tool_name"] == "write_file"

    def test_validate_rejects_invalid_tool(self):
        _, err = _validate_step_decision(
            {"description": "x", "tool_name": "not_a_tool", "tool_args": {}}
        )
        assert "SYSTEM NOTE" in err
        assert "not_a_tool" in err

    def test_validate_rejects_non_dict_args(self):
        _, err = _validate_step_decision(
            {"description": "x", "tool_name": "write_file", "tool_args": "oops"}
        )
        assert err

    def test_validate_rejects_oversized_content(self):
        from agent.core.agent_loop import _CONTENT_MAX_CHARS

        _, err = _validate_step_decision(
            {
                "description": "x",
                "tool_name": "write_file",
                "tool_args": {"path": "p", "content": "z" * 1_000_000},
            }
        )
        assert str(_CONTENT_MAX_CHARS) in err

    def test_transcript_is_bounded(self):
        task = _task()
        for i in range(60):
            task.steps.append(
                TaskStep(
                    order=i,
                    description=f"step {i}",
                    tool_name="write_file",
                    status=StepStatus.SUCCESS,
                    result="y" * 300,
                )
            )
        text = _build_transcript(task)
        assert len(text) <= 20000
        assert "step 59" in text  # newest kept


class TestNoFabrication:
    @pytest.mark.asyncio
    async def test_no_templates_anywhere(self):
        """The loop only ever runs steps the decision brain returned — nothing is
        hardcoded, no stock imagery, no fabricated plan injected by the loop."""
        import agent.core.agent_loop as loop

        source = Path(loop.__file__).read_text(encoding="utf-8")
        for banned in ("unsplash", "images.pexels.com", "images.unsplash.com"):
            assert banned not in source
        assert "decide_next_step" in source
        # The controller template never contains file content, only instructions.
        assert "<html" not in source


class TestTodoLifecycle:
    """The agent keeps a live checklist: seeded after planning, managed by the
    model (add/complete/skip), and mapped 1:1 onto the steps it actually takes."""

    def _roadmap(self, titles):
        steps = []
        for i, title in enumerate(titles):
            steps.append(
                TaskStep(
                    task_id="t",
                    description=title,
                    tool_name="write_file",
                    tool_args={},
                    order=i,
                )
            )
        return steps

    @pytest.mark.asyncio
    async def test_roadmap_seeds_checklist_after_planning(self):
        """Todos are created from the suggested roadmap before any step runs."""
        task = _task()

        async def decide(state):
            return {"done": True, "result": "Nothing needed."}

        await run_adaptive_loop(
            task,
            {},
            roadmap=self._roadmap(["Scaffold folder", "Write index.html", "Verify build"]),
            decide_fn=decide,
            execute_fn=_execute_ok,
        )
        assert len(task.todos) == 3
        assert [t.title for t in task.todos] == [
            "Scaffold folder",
            "Write index.html",
            "Verify build",
        ]
        assert all(t.status.value == "pending" for t in task.todos)

    @pytest.mark.asyncio
    async def test_step_completes_its_checkbox(self):
        """The todo a step is working toward flips pending -> in_progress ->
        completed when the step succeeds; DONE closes any IN_PROGRESS."""
        task = _task()
        executed: list[str] = []

        async def execute(step, context):
            executed.append(step.description)
            return ToolResult(success=True, output="ok")

        async def decide(state):
            if len(executed) == 0:
                return {
                    "description": "Scaffold folder",
                    "tool_name": "list_directory",
                    "tool_args": {},
                }
            if len(executed) == 1:
                return {
                    "description": "Write index.html",
                    "tool_name": "write_file",
                    "tool_args": {},
                }
            return {"done": True, "result": "All good."}

        await run_adaptive_loop(
            task,
            {},
            roadmap=self._roadmap(["Scaffold folder", "Write index.html"]),
            decide_fn=decide,
            execute_fn=execute,
        )
        statuses = {t.title: t.status.value for t in task.todos}
        assert statuses["Scaffold folder"] == "completed"
        assert statuses["Write index.html"] == "completed"

    @pytest.mark.asyncio
    async def test_model_can_add_complete_and_skip_todos(self):
        """todo_updates from a decision are applied to the live checklist."""
        task = _task()
        todos_calls: list[str] = []

        async def decide(state):
            todos_calls.append(state["todos"])
            if len(todos_calls) == 1:
                return {
                    "description": "Create folder",
                    "tool_name": "list_directory",
                    "tool_args": {},
                    "todo_updates": [
                        {"kind": "add", "title": "Write README"},
                        {"kind": "complete", "title": "Create folder"},
                        {"kind": "skip", "title": "Scaffold folder"},
                    ],
                }
            return {"done": True, "result": "done"}

        await run_adaptive_loop(
            task,
            {},
            roadmap=self._roadmap(["Scaffold folder", "Write index.html"]),
            decide_fn=decide,
            execute_fn=_execute_ok,
        )
        # The live checklist is surfaced to the model; the LAST call already
        # reflects the add/complete/skip applied after the first decision.
        assert "Write README" in todos_calls[1]
        titles = {t.title: t.status.value for t in task.todos}
        assert titles["Scaffold folder"] == "skipped"
        assert titles["Write index.html"] == "completed"
        assert titles["Write README"] == "pending"

    @pytest.mark.asyncio
    async def test_failed_step_reverts_todo_and_abort_skips_open_items(self):
        """Failed steps do not claim their checkbox; an early stop marks any
        in_progress item as skipped so the checklist stays honest."""
        task = _task()

        async def execute(step, context):
            return ToolResult(success=False, output="", error="sandbox blocked")

        async def decide(state):
            if len(task.steps) >= 3:
                return {"done": True, "result": "Gave up."}
            return {"description": "Run dangerous op", "tool_name": "run_command", "tool_args": {}}

        outcome = await run_adaptive_loop(
            task,
            {},
            roadmap=self._roadmap(["Scaffold folder"]),
            decide_fn=decide,
            execute_fn=execute,
            max_steps=4,
        )
        assert outcome.done is False
        assert task.todos[0].status.value in ("skipped", "pending")

    @pytest.mark.asyncio
    async def test_live_events_stream_during_work(self):
        """on_event fires step_running/done/error as steps run, so the dashboard
        sees the detailed process WHILE the task executes, not after it ends."""
        events: list[tuple[str, str]] = []

        def on_event(task_id, event_type, message, detail=""):
            events.append((event_type, message))

        calls = {"n": 0}

        async def decide(state):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "description": "Scaffold folder",
                    "tool_name": "list_directory",
                    "tool_args": {},
                }
            return {"done": True, "result": "Built and verified."}

        task = _task()
        await run_adaptive_loop(
            task,
            {},
            roadmap=self._roadmap(["Scaffold folder"]),
            decide_fn=decide,
            execute_fn=_execute_ok,
            on_event=on_event,
        )
        types = [e for e, _ in events]
        assert "step_running" in types
        assert types.count("done") >= 1
        assert any("Task complete" in m for _, m in events)

    @pytest.mark.asyncio
    async def test_emit_exceptions_do_not_break_the_loop(self):
        """A broken live-event sink must never crash execution."""

        def bad_sink(*args, **kwargs):
            raise RuntimeError("sink down")

        async def decide(state):
            return {"done": True, "result": "ok"}

        task = _task()
        outcome = await run_adaptive_loop(
            task,
            {},
            decide_fn=decide,
            execute_fn=_execute_ok,
            on_event=bad_sink,
        )
        assert outcome.done is True


class TestProjectCollisionGuard:
    """The agent must never silently replace an existing same-named project:
    writes into a pre-existing folder are blocked unless the agent first read
    one of its files (real update intent)."""

    @pytest.fixture()
    def existing_project(self, tmp_path, monkeypatch):
        import agent.config as cfg

        root = tmp_path / "wr"
        (root / "projects" / "old-site").mkdir(parents=True)
        (root / "projects" / "old-site" / "index.html").write_text("ORIGINAL", encoding="utf-8")
        monkeypatch.setattr(cfg, "_PROJECT_ROOT", root)
        return root

    @pytest.mark.asyncio
    async def test_write_into_preexisting_project_is_blocked(self, existing_project):
        """A blind write to an existing project is skipped without executing;
        the original content stays untouched and the model sees the reason."""
        executed: list[str] = []

        async def execute(step, context):
            executed.append(step.tool_name)
            return ToolResult(success=True, output="wrote")

        async def decide(state):
            if len(task.steps) < 2:
                return {
                    "description": "Overwrite the old project files",
                    "tool_name": "write_file",
                    "tool_args": {"path": "projects/old-site/index.html", "content": "NEW CONTENT"},
                }
            return {"done": True, "result": "Finished."}

        task = _task()
        await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=execute, max_steps=4)

        assert len(task.steps) >= 1
        assert task.steps[0].status == StepStatus.SKIPPED
        assert "BLOCKED" in (task.steps[0].error or "")
        assert executed == []  # the guard never ran the write
        file = existing_project / "projects" / "old-site" / "index.html"
        assert file.read_text(encoding="utf-8") == "ORIGINAL"

    @pytest.mark.asyncio
    async def test_read_first_then_write_is_allowed(self, existing_project):
        """Updating an existing project is fine once the agent read its files."""
        calls = {"n": 0}
        executed: list[str] = []

        async def decide(state):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "description": "Read existing index.html",
                    "tool_name": "read_file",
                    "tool_args": {"path": "projects/old-site/index.html"},
                }
            if calls["n"] == 2:
                return {
                    "description": "Update index.html in place",
                    "tool_name": "write_file",
                    "tool_args": {"path": "projects/old-site/index.html", "content": "NEW CONTENT"},
                }
            return {"done": True, "result": "Updated."}

        async def execute(step, context):
            executed.append(step.tool_name)
            path = (step.tool_args or {}).get("path")
            if step.tool_name == "write_file" and path:
                target = existing_project / path.replace("\\", "/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text((step.tool_args or {}).get("content", ""), encoding="utf-8")
            return ToolResult(success=True, output="ok")

        task = _task()
        outcome = await run_adaptive_loop(
            task, {}, decide_fn=decide, execute_fn=execute, max_steps=5
        )

        assert outcome.done is True
        assert executed == ["read_file", "write_file"]
        file = existing_project / "projects" / "old-site" / "index.html"
        assert file.read_text(encoding="utf-8") == "NEW CONTENT"

    @pytest.mark.asyncio
    async def test_fresh_project_folder_on_same_name_uses_new_name(self, existing_project):
        """The guard feedback should push the model to a NEW name; a write to a
        non-existent folder is allowed and executes normally."""
        executed: list[str] = []

        async def decide(state):
            if len(task.steps) < 1:
                return {
                    "description": "Create the new coffee-site project",
                    "tool_name": "execute_code",
                    "tool_args": {
                        "code": "from pathlib import Path\nPath('projects/coffee-site-2').mkdir(exist_ok=True)"
                    },
                }
            return {"done": True, "result": "Done."}

        async def execute(step, context):
            executed.append(step.tool_name)
            return ToolResult(success=True, output="created")

        task = _task()
        await run_adaptive_loop(task, {}, decide_fn=decide, execute_fn=execute, max_steps=4)
        assert [s.tool_name for s in task.steps] == ["execute_code"]
        assert executed == ["execute_code"]
