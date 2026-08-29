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
