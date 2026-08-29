"""Adaptive step-by-step agent loop.

The task master loop works like a programmer in an IDE instead of a
one-shot script: at every iteration it

1. LOOKS at the goal, recalled memory, and every executed step's real result
   (including errors),
2. DECIDES the single next action (or that the goal is satisfied),
3. EXECUTES that one action,
4. Records the outcome, then goes back to 1.

Self-correction is structural: a failed step lands in the transcript as an
ERROR and the next decision fixes the approach (adjust args, switch tool,
verify, retry). Builds are verified by the model itself (list_directory /
read_file) before it declares DONE, and the loop keeps running for as long as
it takes — bounded only by a generous step budget — so multi-file, multi-tool
projects complete step by step instead of being skipped because an initial
one-shot plan was malformed.

No deterministic template/scaffold lives here: every action is authored by the
model from the exact goal text + memory + live results.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from agent.core.executor import execute_step
from agent.core.planner import canonical_tool_names, repair_tool_name
from agent.models import Task, TaskStep

logger = logging.getLogger(__name__)

# A single task may legitimately need many steps for large projects. The loop
# is allowed to keep working for as long as it takes up to this many actions.
_MAX_STEPS = 40

# If a path fails repeatedly, stop feeding a broken route — the model already
# got the errors and could not or did not fix them.
_MAX_CONSECUTIVE_FAILURES = 3

# Decision replies carry the step payload inline (write_file content etc.).
# This budget fits full file bodies while staying safely under truncation.
_DECISION_MAX_TOKENS = 16384

# How many times a malformed decision reply is fed back for repair.
_MAX_DECISION_PARSE_RETRIES = 3

# A single step's inline payload must stay small so the reply stays valid JSON.
_CONTENT_MAX_CHARS = 20000

# Transcript is bounded so long runs stay within the model's context.
_TRANSCRIPT_MAX_ENTRIES = 25
_TRANSCRIPT_ENTRY_MAX_CHARS = 220
_TRANSCRIPT_TOTAL_MAX_CHARS = 12000

# Deterministic workspace snapshot roots (never the whole disk).
_SNAPSHOT_ROOTS = ("projects", "output")
_SNAPSHOT_MAX_LINES = 100
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".idea"}


@dataclass
class AdaptiveOutcome:
    """What the adaptive loop produced for its caller (the orchestrator)."""

    done: bool = False
    summary: str = ""  # the model's DONE summary (may be empty)
    aborted_reason: str = ""  # why the loop stopped early, if it did
    iterations: int = 0
    steps_ran: int = 0


_CONTROLLER_SYSTEM_PROMPT = """You are the action brain of NexusMind, an autonomous agent. You work ONE STEP AT A TIME, like a programmer in an IDE: look at where things stand, pick the single next action, act, then look again. You keep going — correcting, verifying, refining — until the goal is genuinely satisfied.

RULES
1. Every reply is EXACTLY ONE JSON object. No markdown fences, no prose around it.
2. You reply with either:
   - one tool step: {"description": "...", "tool_name": "<tool>", "tool_args": {...}}
   - or, when the goal is fully met (and verified where possible): {"done": true, "result": "<final summary: where files were saved / what the answer is / what changed>"}
3. NEVER invent results that tools did not produce. If a step failed, read its ERROR in "STEPS EXECUTED SO FAR" and fix the approach — adjust args, switch tool, or finish with the partial result. Never pretend success.
4. Multi-file websites / web apps are built as SEPARATE steps: first a tiny execute_code that only creates the folder(s) with pathlib, then ONE write_file step per file (e.g. index.html, css/styles.css, js/main.js, README.md). Keep every step's inline content under 20000 characters so the reply stays valid JSON — never dump a whole site into one step. For very large generated files, use a small execute_code that WRITES the file programmatically.
5. Use EXACT canonical tool names: web_search, fetch_url, run_command, execute_code, read_file, write_file, list_directory, summarize_text, extract_data, parse_json, github_resolve_repo, github_get_repo, github_list_prs, github_get_pr, github_review_pr, github_merge_pr, github_close_pr, github_apply_decisions.
6. Verify before declaring done. For file builds, list the directory or read key files to confirm they exist and are real. If the check shows a problem, add a fixing step instead of claiming success.
7. Respect MEMORY CONTEXT (user preferences / standing instructions) and LESSONS. Never fabricate branding, text, images, or content that is not in the goal or in prior step results. If the goal needs photos and none are provided, download real ones from the web into an assets folder or build the visuals with CSS/JS.
8. The loop executes exactly ONE action per reply and returns its result to you. Do not try to do several actions in one reply.
9. Use {{step_N_result}} style references NEVER — put concrete values directly into tool_args (the actual prior RESULT text is already in your context).
10. Purely informational questions are completed as soon as the answer is backed by retrieved results — a web_search or summarize step then DONE."""

_DECISION_USER_TEMPLATE = """GOAL
{goal}

MEMORY CONTEXT
{memory_context}

LESSONS FROM PAST TASKS
{lessons}

SKILL GUIDANCE
{skill_context}

SUGGESTED ROADMAP (a rough initial plan — the GOAL and the live results below override it; deviate freely when reality demands)
{roadmap}

WORKSPACE NOW
{snapshot}

STEPS EXECUTED SO FAR
{transcript}
{feedback}
Reply with exactly one JSON object now."""


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ").replace("\r", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _snapshot_workspace() -> str:
    """Cheap deterministic view of projects/output dirs (2 levels deep)."""
    from agent.config import _PROJECT_ROOT

    lines: list[str] = []
    for root in _SNAPSHOT_ROOTS:
        base = _PROJECT_ROOT / root
        if not base.exists():
            continue
        try:
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name in _SKIP_DIRS:
                    continue
                if child.is_dir():
                    lines.append(f"- {root}/{child.name}/")
                    try:
                        for sub in sorted(child.iterdir()):
                            if sub.is_dir() and sub.name in _SKIP_DIRS:
                                continue
                            if sub.is_dir():
                                lines.append(f"  - {root}/{child.name}/{sub.name}/")
                            else:
                                lines.append(
                                    f"  - {root}/{child.name}/{sub.name} ({sub.stat().st_size} B)"
                                )
                    except OSError:
                        pass
                else:
                    lines.append(f"- {root}/{child.name} ({child.stat().st_size} B)")
            if len(lines) >= _SNAPSHOT_MAX_LINES:
                lines = lines[:_SNAPSHOT_MAX_LINES]
                lines.append("…(truncated)")
                break
        except OSError:
            logger.debug("Workspace snapshot failed for %s", root, exc_info=True)
    return "\n".join(lines) if lines else "_No projects/ or output/ yet._"


def _build_transcript(task: Task) -> str:
    """Compact, bounded transcript of executed steps (newest last)."""
    if not task.steps:
        return "_None yet — this is the first action._"
    entries: list[str] = []
    for s in task.steps[-_TRANSCRIPT_MAX_ENTRIES:]:
        line = f"[{s.status.value}] {s.order} {s.tool_name}: {_trim(s.description, 80)}"
        if s.result:
            line += "\n    RESULT: " + _trim(s.result, _TRANSCRIPT_ENTRY_MAX_CHARS)
        elif s.error:
            line += "\n    ERROR: " + _trim(s.error, _TRANSCRIPT_ENTRY_MAX_CHARS)
        entries.append(line)
    total = 0
    kept: list[str] = []
    for line in reversed(entries):
        total += len(line) + 1
        if total > _TRANSCRIPT_TOTAL_MAX_CHARS and kept:
            kept.append("…(earlier details omitted)")
            break
        kept.append(line)
    kept.reverse()
    return "\n".join(kept)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first balanced JSON object from a model reply."""
    decoder = json.JSONDecoder()
    # Models occasionally wrap the JSON in ```json ... ``` fences — strip them
    # first so the object start is easy to find.
    if "```" in text:
        stripped = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
        if stripped:
            text = stripped
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for pos in starts[:12]:
        try:
            obj, _ = decoder.raw_decode(text[pos:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _validate_step_decision(decision: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Validate a candidate step decision: returns (repaired, error_reason)."""
    if decision.get("done"):
        return decision, ""
    description = decision.get("description", "")
    if not isinstance(description, str) or not description.strip():
        return (
            None,
            'SYSTEM NOTE: your reply was not a valid step — it needs a non-empty "description".',
        )
    tool_name = decision.get("tool_name")
    if not isinstance(tool_name, str):
        return None, 'SYSTEM NOTE: your reply was not a valid step — it needs a "tool_name" key.'
    repaired = repair_tool_name(tool_name)
    if repaired is None:
        valid = ", ".join(canonical_tool_names())
        return (
            None,
            f'SYSTEM NOTE: tool "{tool_name}" is not in the registry. Valid tools: {valid}.',
        )
    tool_args = decision.get("tool_args")
    if not isinstance(tool_args, dict) or tool_args is None:
        return (
            None,
            'SYSTEM NOTE: your reply was not a valid step — "tool_args" must be a JSON object.',
        )
    for key in ("content", "code", "text", "body"):
        value = tool_args.get(key)
        if isinstance(value, str) and len(value) > _CONTENT_MAX_CHARS:
            return None, (
                f'SYSTEM NOTE: "{key}" is {len(value)} characters; keep a single step under '
                f"{_CONTENT_MAX_CHARS} (valid JSON limit). Split this file into smaller parts or "
                "generate it with a small execute_code that writes the file programmatically."
            )
    return {
        "kind": "step",
        "description": description.strip(),
        "tool_name": repaired,
        "tool_args": tool_args,
    }, ""


async def _call_decide(decide_fn: Callable[..., Any], state: dict[str, Any]) -> dict[str, Any]:
    return await decide_fn(state)


async def decide_next_step(state: dict[str, Any]) -> dict[str, Any]:
    """The default decision brain: one Gemini call returning the next action.

    The returned dict is either ``{"done": True, "result": "...", "_raw": ...}``
    or a step decision (see ``_validate_step_decision``) with ``_raw`` carrying
    the original text for diagnostics.
    """
    from agent.config import settings
    from agent.core.gemini_client import generate_content

    user = _DECISION_USER_TEMPLATE.format(
        goal=state.get("goal", ""),
        memory_context=state.get("memory_context") or "_None recalled._",
        lessons="\n".join(f"- {lesson}" for lesson in (state.get("lessons") or [])[:5])
        or "_None._",
        skill_context=state.get("skill_context") or "_None._",
        roadmap=state.get("roadmap") or "_None._",
        snapshot=state.get("snapshot") or _snapshot_workspace(),
        transcript=state.get("transcript") or "_None yet._",
        feedback=(state.get("feedback") or "") + "\n" if state.get("feedback") else "",
    )
    raw = await generate_content(
        model=settings.gemini_model,
        system=_CONTROLLER_SYSTEM_PROMPT,
        user=user,
        temperature=0.2,
        max_tokens=_DECISION_MAX_TOKENS,
    )
    obj = _extract_json_object(raw) or {}
    obj = dict(obj)
    obj["_raw"] = raw
    if obj.get("done"):
        return {"done": True, "result": str(obj.get("result") or ""), "_raw": raw}
    repaired, error = _validate_step_decision(obj)
    if repaired is None:
        return {"_error": error, "_raw": raw}
    repaired["_raw"] = raw
    return repaired


async def run_adaptive_loop(
    task: Task,
    context: dict[str, Any],
    memory_context: str = "",
    lessons: list[str] | None = None,
    skill_context: str = "",
    roadmap: list[TaskStep] | None = None,
    execute_fn: Callable[..., Any] = execute_step,
    decide_fn: Callable[..., Any] = decide_next_step,
    max_steps: int = _MAX_STEPS,
) -> AdaptiveOutcome:
    """Run the step-by-step loop against ``task`` (mutated in place).

    Each iteration decides ONE action, executes it, and records the real result
    into ``task.steps`` and ``context`` (both ``step_N_result`` forms, matching
    the executor's template keys). Loop stops when the model says DONE, when a
    route fails ``_MAX_CONSECUTIVE_FAILURES`` in a row, when decision parsing
    never produced a valid action, or when the step budget is exhausted.
    """
    goals = task.goal or ""
    if context is None:
        context = {}
    consecutive_failures = 0
    details: list[str] = []

    def _abort(reason: str) -> AdaptiveOutcome:
        logger.warning("Adaptive loop stopped for task %s: %s", task.id, reason)
        task.updated_at = datetime.now(UTC)
        return AdaptiveOutcome(
            done=False,
            summary="",
            aborted_reason=reason,
            iterations=len(task.steps),
            steps_ran=len(task.steps),
        )

    for _round in range(max_steps):
        task.updated_at = datetime.now(UTC)
        roadmap_text = (
            "\n".join(f"- [{s.tool_name}] {s.description}" for s in (roadmap or [])) or "_None._"
        )
        state = {
            "goal": goals,
            "memory_context": memory_context or "",
            "lessons": lessons or [],
            "skill_context": skill_context or "",
            "roadmap": roadmap_text,
            "snapshot": _snapshot_workspace(),
            "transcript": _build_transcript(task),
            "feedback": "",
        }

        decision: dict[str, Any] | None = None
        for _attempt in range(1, _MAX_DECISION_PARSE_RETRIES + 1):
            decision = await _call_decide(decide_fn, state)
            if decision is None:
                decision = {"_error": "SYSTEM NOTE: the decision brain returned nothing."}
            if decision.get("done"):
                break
            if decision.get("_error"):
                state["feedback"] = str(decision["_error"])
                continue
            repaired, error = _validate_step_decision(decision)
            if repaired is not None:
                decision = repaired
                break
            state["feedback"] = error
        else:
            # Parse retries exhausted without a usable action.
            last_note = _trim(state.get("feedback") or "", 160)
            return _abort(
                "the model produced no valid action after "
                f"{_MAX_DECISION_PARSE_RETRIES} attempts. Last feedback: {last_note}"
            )

        if decision is None or decision.get("done"):
            summary = str((decision or {}).get("result") or "").strip()
            task.updated_at = datetime.now(UTC)
            logger.info("Adaptive loop DONE for task %s after %d step(s)", task.id, len(task.steps))
            return AdaptiveOutcome(
                done=True, summary=summary, iterations=len(task.steps), steps_ran=len(task.steps)
            )

        step = TaskStep(
            task_id=task.id,
            description=str(decision.get("description", "")),
            tool_name=decision.get("tool_name"),
            tool_args=dict(decision.get("tool_args") or {}),
            order=len(task.steps),
        )
        task.steps.append(step)
        task.updated_at = datetime.now(UTC)
        logger.info("Step %d: %s", step.order, step.description[:80])
        result = await execute_fn(step, context or {})
        context[f"step_{step.order}_result"] = result.output
        context[f"step_{step.order + 1}_result"] = result.output
        details.append(
            f"step {step.order} [{step.tool_name}]: {'ok' if result.success else 'FAILED'}"
        )

        if result.success:
            consecutive_failures = 0
            continue
        consecutive_failures += 1
        if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            latest = _trim(result.error or "unknown error", 200)
            return _abort(
                f"{_MAX_CONSECUTIVE_FAILURES} consecutive steps failed (last: {latest}). "
                "Repeated failures on the same route were not self-corrected."
            )

    return _abort(
        f"step budget reached ({max_steps} steps). The task may be incomplete; "
        "partial work and any errors are reported below."
    )
