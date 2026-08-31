"""Task decomposition planner — breaks goals into executable steps.

Inspired by OpenClaw's multi-step decomposition and Hermes' skill-based planning.
Includes self-improvement: past reflections influence planning.

GitHub/repository/PR goals are routed through a DETERMINISTIC pipeline built on
the real GitHub skill tools — they never touch web_search and never depend on
Gemini producing valid JSON to work.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from agent.config import settings
from agent.models import Task, TaskStep

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are NexusMind — an autonomous AI agent with REAL execution capabilities. Backend AI (Gemini) is the CONTROLLER for all decisions: tool selection, file naming, and memory — you must decide these explicitly.

YOU ARE NOT A CHATBOT. You have direct access to tools that execute real actions:
- You can run shell commands and Python code
- You can read/write files
- You can call APIs and take real actions

GEMINI CONTROL RULES:
- YOU decide which tools to use — choose the minimal set that directly fulfills the goal.
- YOU decide exact file paths for every write_file / execute_code output (use projects/<kebab-name>/... for multi-file builds, output/... for single artifacts). Never leave path empty.
- Prefer explicit, goal-derived kebab-case names; use relative paths.

AVAILABLE TOOLS:
- web_search: Search the web. Args: {"query": "...", "num_results": 5}
   → ONLY for research/information gathering about the outside world.
   → NEVER for GitHub/repository/PR work, file operations, or command execution.

- GITHUB TOOLS (use these for ANY repository/PR task — never curl, never web_search):
   - github_resolve_repo: {"goal_text": "<original goal>"} → returns owner/name
   - github_get_repo: {"repo": "owner/name"}
   - github_list_prs: {"repo": "owner/name"}
   - github_get_pr: {"repo": "owner/name", "pr_number": 123}
   - github_review_pr: {"repo": "...", "pr_number": N} or {"repo": "...", "pr_list": "<json>"}
   - github_merge_pr: {"repo": "...", "pr_number": N}
   - github_close_pr: {"repo": "...", "pr_number": N, "comment": "..."}
   - github_apply_decisions: {"repo": "...", "decisions": "<json from review>"}

- run_command: Execute ANY shell command. Args: {"command": "..."} — AVOID for file/directory creation (use execute_code with pathlib instead — cross-platform)
- execute_code: Run Python code. Args: {"code": "..."}
- read_file / write_file / list_directory: File operations
- summarize_text / extract_data / parse_json: Data processing

PLANNING STRATEGIES BY TASK TYPE:

1. RESEARCH TASKS (summarize, analyze, report on a topic):
    Step 1: web_search with focused query
    Step 2: web_search with broader/different query
    Step 3: summarize_text combining results

2. GITHUB/API TASKS (PRs, issues, repos):
    Use the dedicated github_* tools above. NEVER web_search, NEVER curl.

3. FILE/CREATION TASKS:
    Step 1: execute_code with Python pathlib code that creates directories + writes files (cross-platform, no shell)

4. CODING TASKS:
    Step 1: execute_code with the implementation

RULES:
1. EVERY step MUST have "tool_name" and "tool_args"
2. Use {{step_N_result}} to reference previous step outputs
3. NEVER search the web when you can just DO the action
4. Keep plans SHORT — 3-6 steps maximum
5. NEVER inline large content (HTML pages, long code bodies, full documents)
    inside tool_args. If one step is huge, the plan gets cut off mid-JSON and
    only the earlier complete steps survive — so keep EVERY step small. Never
    embed an entire website inside execute_code's code string.
6. MULTI-FILE PROJECTS (websites / apps / full-stack builds):
     Step 1: a tiny execute_code that ONLY creates the folder(s) via pathlib
        (e.g. pathlib.Path('projects/portfolio').mkdir(parents=True, exist_ok=True)).
     Then: ONE write_file step PER FILE, in dependency order — index.html,
        css/styles.css, js/main.js, README.md — each file fully authored by you
        from the goal (NO templates, NO props or filler content). Files must link
        with RELATIVE paths. Do not merge files, and keep the design focused so
        every step stays small.
     NEVER use run_command mkdir/mkdir -p — use execute_code pathlib ... instead (Windows-safe).
     CRITICAL: if the user specifies an explicit path like projects/portfolio/
     or projects/my-site/ you MUST use that exact path verbatim - never invent
     your own slug or truncate it. Only when NO path is given, derive a short
     kebab-case name from the goal.
     NEVER use hardcoded HTML/CSS/JS templates - always generate original,
     goal-specific content via LLM. NEVER cram a whole multi-file project into
     a single HTML file. Small one-artifact mockups may still go to output/.
7. Return ONLY valid JSON array

OUTPUT FORMAT (JSON array):
[
  {"description": "Scaffold project", "tool_name": "execute_code", "tool_args": {"code": "import pathlib; p=pathlib.Path('projects/demo'); p.mkdir(parents=True, exist_ok=True); ..."}},
  {"description": "Verify", "tool_name": "list_directory", "tool_args": {"path": "projects/demo"}}
]

IMPORTANT: Steps are 0-indexed. First step is step_0, second is step_1, etc.
When referencing previous results, use {{step_0_result}}, {{step_1_result}}, etc.
"""

# Goals matching this are handled by the deterministic GitHub pipeline.
_GITHUB_GOAL_PATTERN = re.compile(
    r"\bgithub\b"
    r"|\brepo(?:sitorie)?s?\b"
    r"|pull\s*requests?"
    r"|\bprs?\b"
    r"|\bmerges?\b"
    r"|\breject(?:ed|ion)?s?\b"
    r"|\bpullrequest\b",
    re.IGNORECASE,
)

# Words implying the user wants mutations performed, not just information.
_ACTION_INTENT_PATTERN = re.compile(
    r"\b(merges?|marges?|rejects?|declines?|closes?|denys?|denies|approves?"
    r"|handles?|processes?|manages?|acts?|cleanups?)\b",
    re.IGNORECASE,
)

# Common hallucinated tool names → canonical registry names (Hermes pattern:
# repair deterministically before bothering the model again).
_TOOL_ALIASES = {
    "search": "web_search",
    "search_web": "web_search",
    "websearch": "web_search",
    "google": "web_search",
    "google_search": "web_search",
    "shell": "run_command",
    "bash": "run_command",
    "terminal": "run_command",
    "run": "run_command",
    "command": "run_command",
    "execute": "execute_code",
    "python": "execute_code",
    "code": "execute_code",
    "run_python": "execute_code",
    "read": "read_file",
    "cat": "read_file",
    "open_file": "read_file",
    "write": "write_file",
    "save_file": "write_file",
    "create_file": "write_file",
    "ls": "list_directory",
    "dir": "list_directory",
    "list_files": "list_directory",
    "summarize": "summarize_text",
    "summary": "summarize_text",
    "extract": "extract_data",
    "parse": "parse_json",
}

# A PR reference: "#123", "pr 123", "PR #123", "pull request 123", "prs 4 and 7".
_PR_NUMBER_PATTERN = re.compile(r"#(\d{1,6})\b|\b(?:pr|pull\s+requests?)\s*#?(\d{1,6})\b", re.IGNORECASE)

# Research-flavored goals where web_search is an acceptable last resort.
_RESEARCH_HINTS = (
    "what ", "who ", "when ", "where ", "why ", "how ", "news", "latest",
    "search", "find information", "tell me about", "research", "explain",
)

# Goals starting with these ask a question — they must stay read-only even
# if action verbs appear later ("how do I merge a pr?").
_QUESTION_PREFIXES = (
    "what ", "who ", "when ", "where ", "why ", "how ",
    "explain", "tell me", "describe", "compare",
)


def _is_github_goal(goal: str) -> bool:
    """Return True if the goal clearly concerns repositories/PRs — not just news about GitHub."""
    g = (goal or "").lower()
    # Nothing is hardcoded to a task type — we only route to the deterministic
    # GitHub pipeline when the *user's command* explicitly asks for a repo/PR
    # operation. Plain "news about github" or "search github news" stays research.
    if "github" in g and "news" in g:
        has_repo_signal = bool(
            re.search(r"\b\w+/\w+\b", goal)  # owner/name like tamimlabs/repo
            or _PR_NUMBER_PATTERN.search(goal)  # #123
            or re.search(r"\b(repo(sitorie)?s?|pull\s*request|prs?\b|branch|commit|merge|close|issue)\b", g)
        )
        if not has_repo_signal:
            return False
    # Also handle "trending/latest on github" without news word — similar
    if "github" in g and any(w in g for w in ("trending", "latest headlines", "latest update")):
        has_repo_signal = bool(
            re.search(r"\b\w+/\w+\b", goal)
            or _PR_NUMBER_PATTERN.search(goal)
            or re.search(r"\b(repo|pull\s*request|prs?\b|branch|commit|merge|close)\b", g)
        )
        if not has_repo_signal:
            return False
    return bool(_GITHUB_GOAL_PATTERN.search(goal))


# NOTE: build goals get NO deterministic template.
# Every website / app / landing page is authored by Gemini from the user's
# goal text and recalled memory context (see plan_task). A hardcoded scaffold
# previously made every 'build a website' task render the same fabricated
# site with stock images and invented branding - that was removed. When the
# planner truly cannot respond we fall back to plan salvage and, last resort,
# an honest diagnostic step that never fabricates content.




def _extract_pr_numbers(goal: str) -> list[int]:
    """Extract explicitly referenced PR numbers, deduplicated in order."""
    numbers: list[int] = []
    for match in _PR_NUMBER_PATTERN.finditer(goal):
        value = int(match.group(1) or match.group(2))
        if value not in numbers:
            numbers.append(value)
    return numbers


# Planner output budget. Ambitious multi-file goals produce large plans, and a
# cap lower than what the plan needs truncates the JSON mid-step (lost builds).
# Raised to give whole plans headroom; each step stays small so a rare
# truncation still salvages every earlier complete step.
_PLANNER_MAX_TOKENS = 16384


def _make_step(task_id: str, order: int, description: str, tool_name: str, tool_args: dict[str, Any]) -> TaskStep:
    return TaskStep(task_id=task_id, description=description, tool_name=tool_name, tool_args=tool_args, order=order)


def repair_tool_name(name: str, valid: list[str] | None = None) -> str | None:
    """Deterministic repair ladder for hallucinated tool names.

    Hermes pattern (agent_runtime_helpers.repair_tool_call): fix cheap and
    locally — normalize separators → strip ``_tool`` suffix → alias map →
    fuzzy match against the live registry. Returns None if unrepairable.
    """
    raw = (name or "").strip().lower()
    if not raw:
        return None
    valid_list = valid if valid is not None else canonical_tool_names()
    valid_set = set(valid_list)
    if raw in valid_set:
        return raw
    normalized = re.sub(r"[\s\-]+", "_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    candidates = [normalized]
    if normalized.endswith("_tool"):
        candidates.append(normalized[: -len("_tool")])
    for candidate in candidates:
        if candidate in valid_set:
            return candidate
        aliased = _TOOL_ALIASES.get(candidate)
        if aliased and aliased in valid_set:
            return aliased
    close = difflib.get_close_matches(normalized, sorted(valid_set), n=1, cutoff=0.7)
    return close[0] if close else None


def canonical_tool_names() -> list[str]:
    """Live registry names (loads skills first). Single source of truth."""
    from agent.core.executor import list_tools
    from agent.skills.loader import load_all_skills

    load_all_skills()
    return sorted(list_tools())


def _tool_catalog_section() -> str:
    """Canonical names + first docstring line, generated from the registry.

    OpenClaw lesson (system-prompt.ts:132): the prompt must list EXACTLY the
    tools that exist — hand-maintained lists drift and teach the model to
    hallucinate tools that are not there.
    """
    from agent.core.executor import get_tool

    lines = []
    for name in canonical_tool_names():
        fn = get_tool(name)
        doc = ""
        if fn is not None and fn.__doc__:
            doc = fn.__doc__.strip().splitlines()[0][:100]
        lines.append(f"- {name}: {doc}" if doc else f"- {name}")
    return "\n".join(lines)


def _build_steps(
    task: Task,
    steps_data: list[dict[str, Any]],
    valid_tools: list[str],
) -> tuple[list[TaskStep], list[str]]:
    """Parse step dicts into TaskSteps, repairing tool names in place.

    Returns (valid_steps, unrepairable_original_names).
    """
    steps: list[TaskStep] = []
    unknown: list[str] = []
    order = 0
    for step_data in steps_data:
        raw = str(step_data.get("tool_name") or "").strip()
        if not raw:
            logger.warning("Dropping nameless plan step: %s", step_data.get("description", "")[:60])
            continue
        repaired = repair_tool_name(raw, valid_tools)
        if repaired is None:
            unknown.append(raw)
            continue
        steps.append(
            TaskStep(
                task_id=task.id,
                description=step_data.get("description", f"Step {order + 1}"),
                tool_name=repaired,
                tool_args=step_data.get("tool_args", {}),
                order=order,
            )
        )
        order += 1
    return steps, unknown


def _github_pipeline(task: Task) -> list[TaskStep]:
    """Build a deterministic multi-step plan for repository/PR goals.

    Separate tasks per concern:
      1. Resolve WHICH repository ("my repository" → git remote / default config)
      2. Fetch (list open PRs, or the specific PRs mentioned)
      3. Review each PR → independent merge/reject verdicts with reasons
      4. Apply decisions: merge safe PRs, reject risky ones (only if requested)
    """
    tid = task.id
    goal = task.goal
    # A question is a request for information, never permission to mutate —
    # even when it contains action verbs ("explain how git handles merges").
    is_question = goal.lower().lstrip().startswith(_QUESTION_PREFIXES)
    wants_action = bool(_ACTION_INTENT_PATTERN.search(goal)) and not is_question
    pr_numbers = _extract_pr_numbers(goal)

    steps: list[TaskStep] = [
        _make_step(tid, 0, "Resolve which repository this goal refers to", "github_resolve_repo", {"goal_text": goal}),
    ]
    repo_ref = "{{step_0_result}}"
    order = 1

    if pr_numbers:
        label = ", ".join(f"#{n}" for n in pr_numbers)
        if len(pr_numbers) == 1:
            steps.append(_make_step(
                tid, order, f"Fetch and review PR {label}: analyze diff and decide",
                "github_review_pr", {"repo": repo_ref, "pr_number": pr_numbers[0]},
            ))
        else:
            steps.append(_make_step(
                tid, order, f"Review PRs {label}: analyze diffs and decide",
                "github_review_pr",
                {"repo": repo_ref, "pr_list": json.dumps([{"number": n} for n in pr_numbers])},
            ))
    else:
        steps.append(_make_step(
            tid, order, "List open pull requests in {{step_0_result}}",
            "github_list_prs", {"repo": repo_ref},
        ))
        order += 1
        steps.append(_make_step(
            tid, order, "Review every listed PR and recommend merge/reject/skip",
            "github_review_pr", {"repo": repo_ref, "pr_list": "{{step_1_result}}"},
        ))
    review_order = order
    order += 1

    if wants_action:
        steps.append(_make_step(
            tid, order, "Apply review verdicts: merge clean PRs, reject risky ones, skip uncertain",
            "github_apply_decisions",
            {"repo": repo_ref, "decisions": f"{{{{step_{review_order}_result}}}}"},
        ))
    else:
        steps.append(_make_step(
            tid, order, "Summarize the repository/PR findings for the user",
            "summarize_text", {"text": f"{{{{step_{review_order}_result}}}}", "max_length": 250},
        ))

    logger.info(
        "Deterministic GitHub plan (%d steps, action=%s) for task %s", len(steps), wants_action, tid,
    )
    return steps


async def plan_task(
    task: Task,
    available_skills: list[str] | None = None,
    lessons: list[str] | None = None,
    memory_context: str | None = None,
    skill_context: str | None = None,
    on_event: Any | None = None,
) -> list[TaskStep]:
    """Decompose a task into ordered steps.

    When ``settings.gemini_full_control`` is True (default), Gemini decides
    tool selection, file naming and memory hints for ALL goals. The deterministic
    GitHub pipeline is kept only as a validator/fallback — if Gemini returns
    a plan that mis-routes a GitHub goal to web_search, we fall back to the
    pipeline.  When full_control is False, GitHub goals still fast-path through
    the deterministic pipeline (legacy behaviour).

    Args:
        task: The task to plan.
        available_skills: Optional list of available skill names.
        lessons: Past reflections/lessons learned from previous tasks.
        memory_context: Fenced <memory-context> block recalled from persistent
            memory (see agent.core.memory.MemoryStore.prefetch).
        skill_context: Fenced <available-skills> block with the procedural
            skill index and any matched procedure body (see
            agent.core.skill_library.SkillLibrary.plan_context).

    Returns:
        Ordered list of TaskStep objects.

    """
    # GitHub goals ALWAYS use deterministic pipeline — this guarantees they
    # never degrade into web_search, even when gemini_full_control is on.
    # Gemini still controls file naming, memory, and non-GitHub tool selection.
    if _is_github_goal(task.goal):
        return _github_pipeline(task)

    from agent.core.gemini_client import generate_content

    lessons_context = ""
    if lessons:
        lessons_context = (
            "\n\nLESSONS FROM PAST TASKS (guidance only — the CURRENT GOAL "
            "above decides what to build; never reuse a past goal's subject, "
            "branding, or output files):\n"
            + "\n".join(f"- {lesson}" for lesson in lessons[:5])
        )

    user_prompt = f"""Goal: {task.goal}
Context: {json.dumps(task.context) if task.context else 'None'}{lessons_context}

{skill_context or ''}

{memory_context or ''}

Create a RESILIENT plan that will produce useful output even if some steps fail.
Return ONLY the JSON array."""

    valid_tools = canonical_tool_names()
    system_prompt = (
        f"{PLANNER_SYSTEM_PROMPT}\n\n"
        "CANONICAL TOOL NAMES (use EXACTLY these — anything else will be rejected):\n"
        f"{_tool_catalog_section()}"
    )

    def _emit_token(delta: str) -> None:
        if on_event is None or not delta:
            return
        # 4-arg sink (task_id, event_type, message, detail) preferred by orchestrator/live bus
        try:
            on_event(task.id, "token", delta, "")
            return
        except TypeError:
            pass
        except Exception:
            logger.debug("planner token sink failed (4-arg)", exc_info=True)
            return
        try:
            on_event("token", delta)
        except Exception:
            logger.debug("planner token sink failed (2-arg)", exc_info=True)

    async def _generate(feedback: str = "") -> str:
        suffix = f"\n\n{feedback}" if feedback else ""
        # Buffered-only (free-tier streaming is 5-20x slower and truncates).
        # One buffered response arrives in ~15-30s and the fast-path then
        # executes its concrete steps with ZERO per-step model calls.
        if on_event is not None:
            try:
                on_event(task.id, "thinking", "Compiling full step plan (buffered, faster than streaming)…", "")
            except TypeError:
                try:
                    on_event("thinking", "Compiling full step plan (buffered, faster than streaming)…", "")
                except Exception:
                    pass
            except Exception:
                pass
        return await generate_content(
            model=settings.gemini_model,
            system=system_prompt,
            user=user_prompt + suffix,
            max_tokens=_PLANNER_MAX_TOKENS,
        )

    try:
        response = await _generate()
        if not response.strip():
            # response.text is None/empty on safety blocks and some quota
            # failures — give the operator something actionable, not JSON noise.
            raise RuntimeError(
                "Gemini returned an EMPTY response (likely safety block, quota "
                "exhaustion, or invalid model). Check GEMINI_API_KEY / GEMINI_MODEL."
            )
        steps_data = _parse_steps_json(response)
        if not steps_data:
            logger.warning(
                "Unparseable planner response for %s (first 300 chars): %.300s",
                task.id,
                response.replace("\n", " "),
            )
            raise ValueError("Planner returned no parseable steps")

        steps, unknown = _build_steps(task, steps_data, valid_tools)
        if unknown:
            # Hermes pattern: return the tool catalog to the model and let it
            # correct itself — ONE corrective round, then deterministic drop.
            logger.warning("Plan for %s used invalid tools %s; correcting", task.id, unknown)
            feedback = (
                "CORRECTION: your previous plan referenced tools that DO NOT exist: "
                + ", ".join(unknown)
                + ". The ONLY valid tools are: "
                + ", ".join(valid_tools)
                + ". Return a corrected JSON array using ONLY valid tools."
            )
            retry_data = _parse_steps_json(await _generate(feedback))
            repaired, still_unknown = _build_steps(task, retry_data, valid_tools)
            if still_unknown:
                logger.warning("Dropping still-invalid tools after correction: %s", still_unknown)
            if repaired:
                return repaired
            steps = [s for s in steps if s.tool_name in set(valid_tools)]

        if not steps:
            raise ValueError("Plan contained no steps with valid tools")

        # Gemini full-control validator: if goal is GitHub but Gemini avoided
        # github_* tools (e.g. hallucinated web_search), fall back to deterministic
        # pipeline rather than executing a wrong plan. This keeps Gemini in control
        # for correct plans, but guarantees correctness on mis-routing.
        if settings.gemini_full_control and _is_github_goal(task.goal):
            tool_names = {s.tool_name for s in steps}
            has_github = any(t.startswith("github_") for t in tool_names)
            has_websearch_only = tool_names == {"web_search"}
            if not has_github or has_websearch_only:
                logger.warning(
                    "Gemini plan for GitHub goal %s avoided github tools (%s); "
                    "falling back to deterministic GitHub pipeline", task.id, tool_names
                )
                return _github_pipeline(task)

        logger.info("Planned %d steps for task %s", len(steps), task.id)
        return steps

    except Exception:
        logger.exception("Planning failed for task %s", task.id)
        # For GitHub goals, deterministic pipeline is the safest fallback even
        # under full_control — it guarantees github_* tools without web_search.
        if _is_github_goal(task.goal):
            logger.info("Falling back to deterministic GitHub pipeline for %s", task.id)
            return _github_pipeline(task)
        return await _fallback_plan(task)


async def _fallback_plan(task: Task) -> list[TaskStep]:
    """Recover from planning failure WITHOUT inventing content.

    No template/scaffold is emitted here: a build goal that cannot be planned
    still goes through the tool selector and, as a last resort, an honest
    diagnostic step — never a fabricated site.
    """
    from agent.core.gemini_client import generate_content

    try:
        tool_prompt = f"""You are a tool selector. Given this task, pick the RIGHT tool.

Task: {task.goal}

Available tools:
- web_search: Search the web. Use ONLY for research questions about the world.
- run_command: Shell command. Use for system commands, git, local scripts.
- execute_code: Python code. Use for data processing, analysis, calculations.
- read_file: Read a file. Use when task asks to read/open a file.
- write_file: Write a file. Use when task asks to create/save content.
- list_directory: List files. Use when task asks to list/show files.

Rules:
- Never pick web_search unless the task explicitly asks to research/search the internet.

Return ONLY a JSON object: {{"tool_name": "tool_name", "tool_args": {{...}}, "description": "what to do"}}"""

        response = await generate_content(
            model=settings.gemini_model,
            system="You are a tool selector. Return only JSON.",
            user=tool_prompt,
            max_tokens=1024,
        )
        if not response.strip():
            logger.warning("Tool selector returned an empty response (API/safety issue)")
            raise RuntimeError("Empty tool-selector response")

        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(
                line for line in cleaned.splitlines() if not line.strip().startswith("```")
            ).strip()
        # Brace-span slice (NOT a flat-object regex): tool_args nests braces.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                tool_data = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                logger.warning("Tool selector JSON invalid (%s): %.200s", exc, cleaned)
                raise
            tool_name = tool_data.get("tool_name", "")
            tool_args = tool_data.get("tool_args", {})
            description = tool_data.get("description", task.goal[:100])

            # Merge defaults — never REPLACE the dict, or LLM-provided
            # content/code would be silently discarded.
            if tool_name == "run_command" and "command" not in tool_args:
                tool_args = {**tool_args, "command": task.goal}
            elif tool_name == "execute_code" and "code" not in tool_args:
                tool_args = {**tool_args, "code": f"# {task.goal}\nprint('Ready to execute')"}
            elif tool_name == "web_search" and "query" not in tool_args:
                tool_args = {**tool_args, "query": task.goal, "num_results": 5}
            elif tool_name == "write_file" and "path" not in tool_args:
                tool_args = {
                    **tool_args,
                    "path": "output/result.md",
                    **({"content": "{{step_0_result}}"} if "content" not in tool_args else {}),
                }
            elif tool_name in ("read_file", "list_directory") and "path" not in tool_args:
                tool_args = {**tool_args, "path": "output/"}

            if tool_name:
                return [
                    TaskStep(
                        task_id=task.id,
                        description=description,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        order=0,
                    ),
                ]
    except Exception as inner_e:
        logger.warning("Tool selection also failed: %s", inner_e)

    return [_last_resort_step(task)]


def _last_resort_step(task: Task) -> TaskStep:
    """Absolute last resort.

    Research-style questions may still use web_search. Everything else gets an
    honest diagnostic step — a fabricated site is never emitted.
    """
    goal_lower = task.goal.lower()
    if any(hint in goal_lower for hint in _RESEARCH_HINTS) and not _is_github_goal(task.goal):
        return TaskStep(
            task_id=task.id,
            description=f"Search for information about: {task.goal}",
            tool_name="web_search",
            tool_args={"query": task.goal, "num_results": 5},
            order=0,
        )

    return TaskStep(
        task_id=task.id,
        description="Report that the goal could not be planned automatically",
        tool_name="execute_code",
        tool_args={
            "code": (
                f"message = '''Automatic planning failed for this goal:\n{task.goal[:300]}\n\n"
                "No reliable tool could be chosen, so NO action was taken.\n"
                "Try rephrasing with an explicit tool or repository name.'''\nprint(message)"
            )
        },
        order=0,
    )


def _coerce_steps(data: Any) -> list[dict[str, Any]]:
    """Narrow parsed JSON into a list of step dicts."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("steps"), list):
        return [item for item in data["steps"] if isinstance(item, dict)]
    return []


def _salvage_steps(text: str) -> list[dict[str, Any]]:
    """Recover COMPLETED steps from a JSON array truncated mid-stream.

    Happens when max_output_tokens cuts the response off inside a later step
    (e.g. huge inline tool_args). Walk back to the last fully-closed object
    and close the array around it.
    """
    start = text.find("[")
    if start == -1:
        return []
    raw = text[start:]
    last = raw.rfind("}")
    while last > 0:
        try:
            parsed = json.loads(raw[: last + 1] + "]")
        except json.JSONDecodeError:
            last = raw.rfind("}", 0, last)
            continue
        steps = _coerce_steps(parsed)
        if steps:
            logger.warning(
                "Salvaged %d complete step(s) from a truncated plan response",
                len(steps),
            )
        return steps
    return []


def _parse_steps_json(text: str) -> list[dict[str, Any]]:
    """Extract JSON array of steps from LLM response. Empty list if unparseable."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return _coerce_steps(json.loads(text))
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            return _coerce_steps(json.loads(text[start : end + 1]))
        except json.JSONDecodeError:
            pass

    salvaged = _salvage_steps(text)
    if salvaged:
        return salvaged

    logger.warning("Failed to parse steps JSON")
    return []
