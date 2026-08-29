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
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from agent.core.executor import execute_step
from agent.core.gemini_client import QuotaExhaustedError
from agent.core.planner import canonical_tool_names, repair_tool_name
from agent.models import StepStatus, Task, TaskStep, TaskTodo, TodoStatus

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
10. Purely informational questions are completed as soon as the answer is backed by retrieved results — a web_search or summarize step then DONE.
11. You maintain a live TODO checklist (shown to the user). It is seeded from the suggested roadmap but it is YOUR plan, not an obligation: keep it honest, up to date, and complete. A step reply may carry an OPTIONAL "todo_updates" array (max 5 entries) with items {"kind": "add"|"complete"|"skip", "title": "..."}. Titles must match an existing todo closely (case-insensitive substring). Use it to add new follow-up work you discover, mark items done that your action completes, or drop items that turned out unnecessary. The checklist applies to EVERY task type — research, file builds, GitHub ops, anything — so the user always sees where you are.
12. Keep each step small and single-purpose; for big goals prefer many small steps over one giant step (folder creation, then one file per step, then verification)."""

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

CURRENT TODO LIST (your live checklist — keep it honest and up to date; close items with todo_updates as you complete them)
{todos}

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


def _compressed_history(task: Task, budget: int) -> str:
    """One-line-per-step recap of steps too old for the raw transcript window.

    Instead of dropping them (which loses "where the task stands" on long
    runs), the oldest steps are compressed into short status lines the model
    still reads — the recent ones stay verbatim below.
    """
    if len(task.steps) <= _TRANSCRIPT_MAX_ENTRIES:
        return ""
    older = task.steps[:-_TRANSCRIPT_MAX_ENTRIES]
    lines: list[str] = []
    total = 0
    for s in older:
        if s.status == StepStatus.SUCCESS:
            mark = "ok"
        elif s.status == StepStatus.SKIPPED:
            mark = "skipped"
        elif s.status == StepStatus.FAILED:
            mark = "FAILED"
        else:
            mark = "ran"
        line = f"{s.order} [{s.tool_name}] {mark}: {_trim(s.description, 60)}"
        total += len(line) + 1
        if total > budget and lines:
            lines.append(f"{len(older) - len(lines)} earlier step(s) — details collapsed")
            break
        lines.append(line)
    return "\n".join(lines)


def _build_transcript(task: Task) -> str:
    """Compact, bounded transcript of executed steps (newest last).

    Steps older than the raw window are NOT silently dropped: the oldest ones
    appear as a one-line-per-step compressed recap ("EARLIER PROGRESS"), and
    the most recent steps stay verbatim with their RESULT/ERROR — so the model
    keeps working knowledge of everything done on very long runs.
    """
    if not task.steps:
        return "_None yet — this is the first action._"

    recap = _compressed_history(task, _TRANSCRIPT_TOTAL_MAX_CHARS // 3)
    recap_block = (
        f"EARLIER PROGRESS (collapsed to save space — recent steps below are verbatim):\n{recap}"
        if recap
        else ""
    )

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
    recent_block = "\n".join(kept)
    return "\n".join(p for p in (recap_block, recent_block) if p)


def _todo_state(task: Task) -> str:
    """Compact checklist view shown to the model and on the dashboard."""
    if not task.todos:
        return "_Empty — nothing planned yet._"
    lines = []
    for t in sorted(task.todos, key=lambda x: x.order):
        mark = {
            TodoStatus.PENDING: "[ ]",
            TodoStatus.IN_PROGRESS: "[~]",
            TodoStatus.COMPLETED: "[x]",
            TodoStatus.SKIPPED: "[-]",
        }.get(t.status, "[ ]")
        lines.append(f"{mark} ({t.status.value}) {_trim(t.title, 100)}")
    return "\n".join(lines)


def _seed_todos(task: Task, roadmap: list[TaskStep] | None) -> None:
    """Seed the live checklist from the suggested roadmap (best-effort)."""
    if task.todos or not roadmap:
        return
    for i, s in enumerate(roadmap):
        task.todos.append(
            TaskTodo(
                task_id=task.id,
                title=s.description.strip() or s.tool_name or f"Step {i}",
                status=TodoStatus.PENDING,
                order=i,
            )
        )


def _find_todo(task: Task, title: str) -> TaskTodo | None:
    """Match a title against an open (pending/in_progress) todo, substrings ok."""
    needle = (title or "").strip().lower()
    if not needle:
        return None
    for t in sorted(task.todos, key=lambda x: x.order):
        if t.status not in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS):
            continue
        hay = t.title.lower()
        if needle in hay or hay in needle:
            return t
    return None


def _set_todo_status(todo: TaskTodo, status: TodoStatus) -> None:
    todo.status = status
    todo.updated_at = datetime.now(UTC)


def _apply_todo_updates(task: Task, decision: dict[str, Any]) -> None:
    """Apply the model's optional todo_updates to the live checklist."""
    raw = decision.get("todo_updates")
    if not isinstance(raw, list):
        return
    for entry in raw[:5]:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        if kind == "add":
            if len(task.todos) >= 30:
                break
            max_order = max((t.order for t in task.todos), default=-1)
            task.todos.append(
                TaskTodo(
                    task_id=task.id,
                    title=_trim(title.strip(), 140),
                    status=TodoStatus.PENDING,
                    order=max_order + 1,
                )
            )
            continue
        if kind in ("complete", "skip"):
            todo = _find_todo(task, title)
            if todo is not None:
                _set_todo_status(
                    todo, TodoStatus.COMPLETED if kind == "complete" else TodoStatus.SKIPPED
                )


def _next_open_todo(task: Task) -> TaskTodo | None:
    """Lowest-order open todo (pending or already in_progress) for the next step."""
    for t in sorted(task.todos, key=lambda x: x.order):
        if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS):
            return t
    return None


# ── Project collision guard ───────────────────────────────────────
# The agent must NEVER silently replace an existing project of the same
# name. These helpers let the loop deterministically block writes into a
# project folder that existed BEFORE this task started, unless the agent
# has already READ one of its files (proof of intent to update in place).

_WRITE_ROOTS = ("projects", "output")
_ROOT_RE = re.compile(r"\b(projects|output)/([A-Za-z0-9._@\- ]+?)(?=/|['\")`,;\s]|$)")


def _step_target_roots(step: TaskStep) -> set[str]:
    """Project roots (e.g. ``projects/my-site``) a step would touch."""
    roots: set[str] = set()
    pool: list[str] = [str(step.description or "")]
    for value in (step.tool_args or {}).values():
        if isinstance(value, str):
            pool.append(value)
        elif isinstance(value, (list, tuple)):
            pool.extend(x for x in value if isinstance(x, str))
    for text in pool:
        norm = text.replace("\\", "/")
        for root in _ROOT_RE.findall(norm):
            name = root[1].strip(" '\"")
            if name:
                roots.add(f"{root[0]}/{name}")
    return roots


def _preexisting_project_roots() -> set[str]:
    """Non-empty project folders that existed before this task began."""
    from agent.config import _PROJECT_ROOT

    existing: set[str] = set()
    for root in _WRITE_ROOTS:
        base = _PROJECT_ROOT / root
        if not base.exists():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                if any(p.is_file() for p in child.rglob("*")):
                    existing.add(f"{root}/{child.name}")
            except OSError:
                pass
    return existing


_BLOCK_TEMPLATE = (
    'BLOCKED: "{root}" is an EXISTING project with content — do NOT overwrite or '
    "replace it. If the GOAL is to UPDATE it, first read its files (read_file / "
    "list_directory), then write. Otherwise choose a NEW project name (e.g. append "
    '"-2" or "v2") so the existing project stays untouched.'
)


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
        "todo_updates": decision.get("todo_updates"),
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
    from agent.core.gemini_client import (
        OutputTruncatedError,
        QuotaExhaustedError,
        generate_content,
    )

    user = _DECISION_USER_TEMPLATE.format(
        goal=state.get("goal", ""),
        memory_context=state.get("memory_context") or "_None recalled._",
        lessons="\n".join(f"- {lesson}" for lesson in (state.get("lessons") or [])[:5])
        or "_None._",
        skill_context=state.get("skill_context") or "_None._",
        roadmap=state.get("roadmap") or "_None._",
        todos=state.get("todos") or "_Empty — nothing planned yet._",
        snapshot=state.get("snapshot") or _snapshot_workspace(),
        transcript=state.get("transcript") or "_None yet._",
        feedback=(state.get("feedback") or "") + "\n" if state.get("feedback") else "",
    )
    try:
        raw = await generate_content(
            model=settings.gemini_model,
            system=_CONTROLLER_SYSTEM_PROMPT,
            user=user,
            temperature=0.2,
            max_tokens=_DECISION_MAX_TOKENS,
        )
    except OutputTruncatedError as exc:
        # Even after the automatic pro-model retry in generate_content, the
        # reply got cut off. Feed a repair note back so the loop CONTINUES
        # with a shorter reply instead of dying mid-task.
        return {
            "_error": (
                "SYSTEM NOTE: your previous reply was cut off (max output tokens "
                "reached, even on the fallback model). Reply again but MUCH SHORTER "
                "— any large inline file content must be moved into a small "
                "execute_code step that WRITES the file programmatically."
            ),
            "_raw": getattr(exc, "partial", ""),
        }
    except QuotaExhaustedError:
        # Propagate so run_adaptive_loop can abort the task with guidance.
        raise
    obj = _extract_json_object(raw) or {}
    obj = dict(obj)
    obj["_raw"] = raw
    if obj.get("done"):
        return {
            "done": True,
            "result": str(obj.get("result") or ""),
            "todo_updates": obj.get("todo_updates"),
            "_raw": raw,
        }
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
    on_event: Callable[..., Any] | None = None,
) -> AdaptiveOutcome:
    """Run the step-by-step loop against ``task`` (mutated in place).

    Each iteration decides ONE action, executes it, and records the real result
    into ``task.steps`` and ``context`` (both ``step_N_result`` forms, matching
    the executor's template keys). Loop stops when the model says DONE, when a
    route fails ``_MAX_CONSECUTIVE_FAILURES`` in a row, when decision parsing
    never produced a valid action, or when the step budget is exhausted.

    ``on_event(task_id, event_type, message, detail="")`` — if given, is called
    live so callers can stream the detailed process (steps + checkbox states)
    to the dashboard while the loop runs, not only after it finishes.
    """
    goals = task.goal or ""
    if context is None:
        context = {}
    consecutive_failures = 0
    details: list[str] = []

    def _emit(event_type: str, message: str, detail: str = "") -> None:
        if on_event is not None:
            try:
                on_event(task.id, event_type, message, detail)
            except Exception:
                logger.debug("live event sink failed", exc_info=True)

    def _abort(reason: str) -> AdaptiveOutcome:
        logger.warning("Adaptive loop stopped for task %s: %s", task.id, reason)
        for t in task.todos:
            if t.status is TodoStatus.IN_PROGRESS:
                _set_todo_status(t, TodoStatus.SKIPPED)
        task.updated_at = datetime.now(UTC)
        _emit("error", "Stopped early", reason)
        return AdaptiveOutcome(
            done=False,
            summary="",
            aborted_reason=reason,
            iterations=len(task.steps),
            steps_ran=len(task.steps),
        )

    _seed_todos(task, roadmap)
    preexisting = _preexisting_project_roots()
    read_roots: set[str] = set()

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
            "todos": _todo_state(task),
            "snapshot": _snapshot_workspace(),
            "transcript": _build_transcript(task),
            "feedback": "",
        }

        decision: dict[str, Any] | None = None
        for _attempt in range(1, _MAX_DECISION_PARSE_RETRIES + 1):
            try:
                decision = await _call_decide(decide_fn, state)
            except QuotaExhaustedError as exc:
                # Every key AND the fallback model are rate-limited/quota-gone
                # (free-tier daily limit is the usual suspect). Halt cleanly
                # with actionable guidance — never a raw API crash.
                return _abort(
                    "Gemini quota/rate limit reached for all models and keys "
                    f"(retry in ~{exc.retry_after:.0f}s). This is usually the "
                    "free-tier daily request cap — retry later, add more "
                    "GEMINI_API_KEYs, or upgrade to a paid tier."
                )
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
            _apply_todo_updates(task, decision or {})
            for t in task.todos:
                if t.status is TodoStatus.IN_PROGRESS:
                    _set_todo_status(t, TodoStatus.COMPLETED)
            task.updated_at = datetime.now(UTC)
            logger.info("Adaptive loop DONE for task %s after %d step(s)", task.id, len(task.steps))
            _emit(
                "done",
                "Task complete",
                _trim(summary, 300) or f"{len(task.steps)} step(s) executed.",
            )
            return AdaptiveOutcome(
                done=True, summary=summary, iterations=len(task.steps), steps_ran=len(task.steps)
            )

        _apply_todo_updates(task, decision)
        active_todo = _next_open_todo(task)
        if active_todo is not None:
            _set_todo_status(active_todo, TodoStatus.IN_PROGRESS)

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

        # Collision guard: never overwrite a project that existed before this
        # task, unless the agent already read one of its files (real update
        # intent). Reads are always allowed — they are how intent is proven.
        # Blocked steps are marked skipped and fed back to the model.
        targets = _step_target_roots(step)
        blocked_root = (
            next(
                (r for r in targets if r in preexisting and r not in read_roots),
                None,
            )
            if step.tool_name in ("write_file", "write_directory")
            else None
        )
        if blocked_root is not None:
            block_msg = _BLOCK_TEMPLATE.replace("{root}", blocked_root)
            step.status = StepStatus.SKIPPED
            step.error = block_msg
            context[f"step_{step.order}_result"] = block_msg
            context[f"step_{step.order + 1}_result"] = block_msg
            details.append(
                f"step {step.order} [{step.tool_name}]: BLOCKED (existing {blocked_root})"
            )
            _emit(
                "error",
                f"Step {step.order} blocked",
                f"{blocked_root} already exists — read its files first or pick a new name.",
            )
            continue

        _emit(
            "step_running",
            f"Step {step.order}: {_trim(step.description, 90)}",
            f"[{step.tool_name}]",
        )
        result = await execute_fn(step, context or {})
        context[f"step_{step.order}_result"] = result.output
        context[f"step_{step.order + 1}_result"] = result.output
        details.append(
            f"step {step.order} [{step.tool_name}]: {'ok' if result.success else 'FAILED'}"
        )

        if result.success:
            consecutive_failures = 0
            if step.tool_name in ("read_file", "list_directory"):
                read_roots.update(_step_target_roots(step))
            if active_todo is not None:
                _set_todo_status(active_todo, TodoStatus.COMPLETED)
            _emit("done", f"Step {step.order} complete", _trim(result.output or result.error, 300))
            continue
        consecutive_failures += 1
        if active_todo is not None and active_todo.status is TodoStatus.IN_PROGRESS:
            _set_todo_status(active_todo, TodoStatus.PENDING)
        _emit("error", f"Step {step.order} failed", _trim(result.error or "unknown error", 300))
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
