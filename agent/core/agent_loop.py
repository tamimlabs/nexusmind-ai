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

# Elastic budget — like opencode IDE: short tasks finish in 3-5 steps without
# waiting, long builds keep going. The loop starts at _MAX_STEPS and auto-
# extends by _EXTEND_CHUNK while it is making progress (pending todos or recent
# successes). Hard ceiling _MAX_STEPS_HARD prevents a truly stuck model from
# running forever.
_MAX_STEPS = 40
_MAX_STEPS_HARD = 120
_EXTEND_CHUNK = 20

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
5. Use EXACT canonical tool names: web_search, fetch_url, run_command, execute_code, read_file, write_file, list_directory, summarize_text, extract_data, parse_json, github_resolve_repo, github_get_repo, github_list_prs, github_get_pr, github_review_pr, github_merge_pr, github_close_pr, github_apply_decisions, todowrite.
6. Verify before declaring done. For file builds, list the directory or read key files to confirm they exist and are real. If the check shows a problem, add a fixing step instead of claiming success.
7. Respect MEMORY CONTEXT (user preferences / standing instructions) and LESSONS. Never fabricate branding, text, images, or content that is not in the goal or in prior step results. If the goal needs photos and none are provided, download real ones from the web into an assets folder or build the visuals with CSS/JS.
8. The loop executes exactly ONE action per reply and returns its result to you. Do not try to do several actions in one reply.
9. Use {{step_N_result}} style references NEVER — put concrete values directly into tool_args (the actual prior RESULT text is already in your context).
10. Purely informational questions are completed as soon as the answer is backed by retrieved results — a web_search or summarize step then DONE.
11. You maintain a live TODO checklist via the todowrite tool (shown to the user). It is seeded from the roadmap but it is YOUR plan: call todowrite with the FULL desired todos array (opencode semantics) whenever you want to create/update it — include content/title, status pending/in_progress/completed/cancelled, priority high/medium/low. You may also still use optional "todo_updates" in a step reply for small delta updates (legacy), but prefer todowrite for full overwrites. Keep it honest and complete for EVERY task type.
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


async def _call_decide(
    decide_fn: Callable[..., Any],
    state: dict[str, Any],
    on_event: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Invoke ``decide_fn`` with optional streaming sink.

    Inspects the target signature so existing test fakes that only accept
    ``(state)`` keep working, while :func:`decide_next_step` can receive the
    live ``on_event`` to emit ``token`` deltas.
    """
    import inspect as _inspect

    try:
        sig = _inspect.signature(decide_fn)
    except (ValueError, TypeError):
        return await decide_fn(state)
    if "on_event" in sig.parameters and on_event is not None:
        return await decide_fn(state, on_event=on_event)
    return await decide_fn(state)


async def decide_next_step(
    state: dict[str, Any],
    on_event: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """The default decision brain: one Gemini call returning the next action.

    When ``on_event`` is supplied, the LLM reply is streamed token-by-token
    via ``generate_content_stream`` and each delta is forwarded as
    ``on_event("token", delta)`` (with a ``(task_id, "token", ...)`` fallback
    for the loop's 4-arg sink). If streaming is unavailable or yields nothing,
    the call falls back to the buffered :func:`generate_content` path so
    existing tests remain green.

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

    def _emit_token(delta: str) -> None:
        if on_event is None or not delta:
            return
        # Preferred 2-arg form ``on_event("token", delta)`` (spec); fall back
        # to the loop's 4-arg sink ``on_event(task_id, "token", delta)``.
        try:
            on_event("token", delta)
            return
        except TypeError:
            pass
        except Exception:
            logger.debug("token sink failed (2-arg)", exc_info=True)
            return
        # 4-arg sink needs a task_id — pull from state if present.
        task_id = state.get("task_id") or state.get("id") or ""
        try:
            on_event(task_id, "token", delta, "")
        except Exception:
            logger.debug("token sink failed (4-arg)", exc_info=True)

    # B1: attempt token streaming first when a sink is present.
    if on_event is not None:
        try:
            from agent.core.gemini_client import generate_content_stream as _stream

            raw_streamed = ""
            async for _delta in _stream(
                model=settings.gemini_model,
                system=_CONTROLLER_SYSTEM_PROMPT,
                user=user,
                temperature=0.2,
                max_tokens=_DECISION_MAX_TOKENS,
            ):
                if _delta:
                    raw_streamed += _delta
                    _emit_token(_delta)
            if raw_streamed:
                raw = raw_streamed
                # Parse the streamed reply exactly like the buffered path.
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
            # Empty stream — fall through to buffered call
            logger.debug("generate_content_stream yielded no text; falling back to buffered")
        except (OutputTruncatedError, QuotaExhaustedError):
            raise
        except Exception:
            logger.debug("Streaming decide failed, falling back to buffered generate_content", exc_info=True)

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
    # Opencode-like elastic budget: start at max_steps, extend if making progress
    effective_max = max_steps
    loop_start = datetime.now(UTC)

    # D2: Compaction — summarize very long transcripts via Gemini to avoid context blow-up at hard cap
    _COMPACTION_THRESHOLD = 90

    for _round in range(_MAX_STEPS_HARD):
        # Reached the current elastic budget — decide whether to extend or stop
        if _round >= effective_max:
            pending = sum(1 for t in task.todos if t.status.value in ("pending", "in_progress"))
            recent_ok = sum(1 for s in task.steps[-3:] if s.status == StepStatus.SUCCESS)
            if pending > 0 and recent_ok > 0 and effective_max < _MAX_STEPS_HARD:
                effective_max = min(effective_max + _EXTEND_CHUNK, _MAX_STEPS_HARD)
                _emit(
                    "thinking",
                    f"Extending budget to {effective_max} steps — {pending} todo(s) left, recent progress {recent_ok}/3",
                    f"elapsed {(datetime.now(UTC)-loop_start).total_seconds():.0f}s",
                )
            else:
                return _abort(
                    f"step budget reached ({effective_max} steps). The task may be incomplete; "
                    "partial work and any errors are reported below."
                )
        # Periodic compaction for very long runs (opencode session/compaction parity)
        if _round > 0 and _round % _COMPACTION_THRESHOLD == 0 and len(task.steps) >= _COMPACTION_THRESHOLD:
            try:
                from agent.core.gemini_client import generate_content as _gc

                older = task.steps[: len(task.steps) - _TRANSCRIPT_MAX_ENTRIES]
                summary_prompt = "Summarize these completed steps in 8 bullet lines, keep file paths and errors:\n" + "\n".join(f"{s.order} [{s.tool_name}] {s.description[:80]}: {(s.result or s.error or '')[:120]}" for s in older[-30:])
                comp = await _gc(system="Summarize agent progress concisely.", user=summary_prompt, temperature=0.2, max_tokens=600)
                if comp and len(comp) > 40:
                    # Keep first 10 raw steps + summary instead of 25 window
                    _emit("thinking", "Compacted transcript", comp[:400])
            except Exception:
                logger.debug("compaction failed", exc_info=True)
        task.updated_at = datetime.now(UTC)
        snapshot_text = _snapshot_workspace()
        transcript_text = _build_transcript(task)
        todo_text = _todo_state(task)
        roadmap_text = (
            "\n".join(f"- [{s.tool_name}] {s.description}" for s in (roadmap or [])) or "_None._"
        )
        state = {
            "goal": goals,
            "task_id": task.id,
            "memory_context": memory_context or "",
            "lessons": lessons or [],
            "skill_context": skill_context or "",
            "roadmap": roadmap_text,
            "todos": todo_text,
            "snapshot": snapshot_text,
            "transcript": transcript_text,
            "feedback": "",
        }
        # Opencode-like visibility: stream what the brain is about to see
        _emit(
            "thinking",
            f"Planning step {_round+1}/{effective_max}…",
            f"todos: {_trim(todo_text.replace(chr(10), ' | '), 180)}\ntranscript tail: {_trim(transcript_text.split(chr(10))[-1] if transcript_text else '', 180)}",
        )

        decide_started = datetime.now(UTC)
        decision: dict[str, Any] | None = None
        for _attempt in range(1, _MAX_DECISION_PARSE_RETRIES + 1):
            try:
                _emit("thinking", f"Gemini deciding… attempt {_attempt}/{_MAX_DECISION_PARSE_RETRIES}", f"model brain thinking (round {_round+1})")
                decision = await _call_decide(decide_fn, state, on_event=on_event)
                raw_preview = _trim(str(decision.get("_raw") or decision)[:800], 500)
                _emit("thinking", "Decision received", raw_preview)
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
                _emit("error", "Decision parse issue", state["feedback"][:300])
                continue
            repaired, error = _validate_step_decision(decision)
            if repaired is not None:
                decision = repaired
                break
            state["feedback"] = error
            _emit("error", "Invalid step shape", error[:300])
        else:
            # Parse retries exhausted without a usable action.
            last_note = _trim(state.get("feedback") or "", 160)
            return _abort(
                "the model produced no valid action after "
                f"{_MAX_DECISION_PARSE_RETRIES} attempts. Last feedback: {last_note}"
            )
        decide_ms = int((datetime.now(UTC) - decide_started).total_seconds() * 1000)

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
        # Emit todo_update for legacy delta so dashboard checklist moves in realtime even if model doesn't call todowrite
        if decision.get("todo_updates"):
            _emit("todo_update", f"Checklist {sum(1 for t in task.todos if t.status==TodoStatus.COMPLETED)}/{len(task.todos)}", _todo_state(task)[:1200])
        # Don't mark IN_PROGRESS for todowrite itself — it resets the whole list and would lose the mark (seen as 0/6 gray)
        _is_todowrite = str(decision.get("tool_name") or "").lower() == "todowrite"
        active_todo = _next_open_todo(task)
        if active_todo is not None and not _is_todowrite:
            _set_todo_status(active_todo, TodoStatus.IN_PROGRESS)
            _emit("todo_update", f"Working: {active_todo.title[:80]}", _todo_state(task)[:1200])
        elif _is_todowrite:
            active_todo = None  # todowrite is meta, not real work — don't auto-complete it as a todo

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
            if step.tool_name in ("write_file", "write_directory", "execute_code", "run_command")
            else None
        )
        if blocked_root is not None:
            block_msg = _BLOCK_TEMPLATE.replace("{root}", blocked_root)
            step.status = StepStatus.SKIPPED
            step.error = block_msg
            context[f"step_{step.order}_result"] = block_msg
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
            f"[{step.tool_name}] decide {decide_ms}ms | budget {effective_max} | elapsed {int((datetime.now(UTC)-loop_start).total_seconds())}s",
        )
        tool_started = datetime.now(UTC)
        # B2: incremental tool I/O streaming — wire stdout chunks to live
        # ``tool_delta`` events via context["on_output"] (executor.py drains
        # stdout per 4 KiB and invokes this sink). The final ToolResult is
        # unchanged; we just emit live deltas in addition.
        def _on_tool_chunk(chunk: str) -> None:
            if not chunk:
                return
            # Keep message/detail bounded for the live store
            _emit("tool_delta", chunk[:800], chunk[:1200])

        call_context: dict[str, Any] = dict(context or {})
        call_context["on_output"] = _on_tool_chunk
        # Also stash in the canonical context so execute_step's forwarder sees it
        # even if the caller passed a different dict identity.
        _saved_on_output = (context or {}).get("on_output") if isinstance(context, dict) else None
        if isinstance(context, dict):
            context["on_output"] = _on_tool_chunk
        try:
            result = await execute_fn(step, call_context)
        finally:
            if isinstance(context, dict):
                if _saved_on_output is None:
                    context.pop("on_output", None)
                else:
                    context["on_output"] = _saved_on_output
        tool_ms = int((datetime.now(UTC) - tool_started).total_seconds() * 1000)
        context[f"step_{step.order}_result"] = result.output
        details.append(
            f"step {step.order} [{step.tool_name}]: {'ok' if result.success else 'FAILED'} {tool_ms}ms"
        )
        # Opencode-like: stream full tool I/O for dashboard to show without truncation
        _emit(
            "tool_output" if result.success else "error",
            f"Step {step.order} {'output' if result.success else 'failed'} ({tool_ms}ms)",
            _trim((result.output or result.error or "")[:2000], 1500) + f"\n— tool: {step.tool_name} | decide: {decide_ms}ms | tool: {tool_ms}ms",
        )

        if result.success:
            consecutive_failures = 0
            # Auto-complete the active todo on success and emit live so right-panel Checklist turns green immediately
            if active_todo is not None and active_todo.status == TodoStatus.IN_PROGRESS:
                _set_todo_status(active_todo, TodoStatus.COMPLETED)
                _emit("todo_update", f"Done: {active_todo.title[:80]}", _todo_state(task)[:1200])
                # Prevent double-complete in the generic block below
                active_todo = None
            if step.tool_name == "todowrite":
                # Opencode semantics: overwrite whole checklist
                try:
                    from agent.models import TodoPriority

                    raw_todos = (result.metadata or {}).get("todos") or []
                    if isinstance(raw_todos, list) and raw_todos:
                        new_todos: list[TaskTodo] = []
                        for i, td in enumerate(raw_todos[:30]):
                            title = str(td.get("title") or td.get("content") or "").strip()[:140]
                            if not title:
                                continue
                            s_raw = str(td.get("status") or "pending").lower()
                            p_raw = str(td.get("priority") or "medium").lower()
                            try:
                                status = TodoStatus(s_raw)
                            except ValueError:
                                status = TodoStatus.PENDING if s_raw not in ("cancelled",) else TodoStatus.CANCELLED
                            try:
                                prio = TodoPriority(p_raw)
                            except ValueError:
                                prio = TodoPriority.MEDIUM
                            new_todos.append(TaskTodo(task_id=task.id, title=title, status=status, priority=prio, order=i))
                        if new_todos:
                            task.todos.clear()
                            task.todos.extend(new_todos)
                            _emit("todo_update", f"Checklist {sum(1 for t in new_todos if t.status==TodoStatus.COMPLETED)}/{len(new_todos)}", "\n".join(f"[{'x' if t.status==TodoStatus.COMPLETED else ' '}] {t.title}" for t in new_todos[:8]))
                except Exception:
                    logger.debug("todowrite apply failed", exc_info=True)
                _emit("done", f"Step {step.order} checklist updated", _trim(result.output or "", 300))
                continue
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
        f"step budget reached ({effective_max} steps, hard cap {_MAX_STEPS_HARD}). The task may be incomplete; "
        "partial work and any errors are reported below."
    )
