"""Task decomposition planner — breaks goals into executable steps.

Inspired by OpenClaw's multi-step decomposition and Hermes' skill-based planning.
Includes self-improvement: past reflections influence planning.

GitHub/repository/PR goals are routed through a DETERMINISTIC pipeline built on
the real GitHub skill tools — they never touch web_search and never depend on
Gemini producing valid JSON to work.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.config import settings
from agent.models import Task, TaskStep

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are NexusMind — an autonomous AI agent with REAL execution capabilities.

YOU ARE NOT A CHATBOT. You have direct access to tools that execute real actions:
- You can run shell commands and Python code
- You can read/write files
- You can call APIs and take real actions

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

- run_command: Execute ANY shell command. Args: {"command": "..."}
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
   Step 1: write_file with the content

4. CODING TASKS:
   Step 1: execute_code with the implementation

RULES:
1. EVERY step MUST have "tool_name" and "tool_args"
2. Use {{step_N_result}} to reference previous step outputs
3. NEVER search the web when you can just DO the action
4. Keep plans SHORT — 3-5 steps maximum
5. Return ONLY valid JSON array

OUTPUT FORMAT (JSON array):
[
  {"description": "Fetch data", "tool_name": "run_command", "tool_args": {"command": "..."}},
  {"description": "Analyze result", "tool_name": "execute_code", "tool_args": {"code": "..."}}
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

# A PR reference: "#123", "pr 123", "PR #123", "pull request 123", "prs 4 and 7".
_PR_NUMBER_PATTERN = re.compile(r"#(\d{1,6})\b|\b(?:pr|pull\s+requests?)\s*#?(\d{1,6})\b", re.IGNORECASE)

# Research-flavored goals where web_search is an acceptable last resort.
_RESEARCH_HINTS = (
    "what ", "who ", "when ", "where ", "why ", "how ", "news", "latest",
    "search", "find information", "tell me about", "research", "explain",
)


def _is_github_goal(goal: str) -> bool:
    """Return True if the goal clearly concerns repositories/PRs."""
    return bool(_GITHUB_GOAL_PATTERN.search(goal))


def _extract_pr_numbers(goal: str) -> list[int]:
    """Extract explicitly referenced PR numbers, deduplicated in order."""
    numbers: list[int] = []
    for match in _PR_NUMBER_PATTERN.finditer(goal):
        value = int(match.group(1) or match.group(2))
        if value not in numbers:
            numbers.append(value)
    return numbers


def _make_step(task_id: str, order: int, description: str, tool_name: str, tool_args: dict[str, Any]) -> TaskStep:
    return TaskStep(task_id=task_id, description=description, tool_name=tool_name, tool_args=tool_args, order=order)


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
    wants_action = bool(_ACTION_INTENT_PATTERN.search(goal))
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


async def plan_task(task: Task, available_skills: list[str] | None = None, lessons: list[str] | None = None) -> list[TaskStep]:
    """Decompose a task into ordered steps.

    GitHub/repository/PR goals bypass Gemini entirely and get a deterministic
    pipeline — this guarantees they never degrade into web searches.

    Args:
        task: The task to plan.
        available_skills: Optional list of available skill names.
        lessons: Past reflections/lessons learned from previous tasks.

    Returns:
        Ordered list of TaskStep objects.

    """
    if _is_github_goal(task.goal):
        return _github_pipeline(task)

    from agent.core.gemini_client import generate_content

    lessons_context = ""
    if lessons:
        lessons_context = "\n\nLESSONS FROM PAST TASKS:\n" + "\n".join(
            f"- {lesson}" for lesson in lessons[:5]
        )

    user_prompt = f"""Goal: {task.goal}
Context: {json.dumps(task.context) if task.context else 'None'}{lessons_context}

Create a RESILIENT plan that will produce useful output even if some steps fail.
Return ONLY the JSON array."""

    try:
        response = await generate_content(
            model=settings.gemini_model,
            system=PLANNER_SYSTEM_PROMPT,
            user=user_prompt,
        )

        steps_data = _parse_steps_json(response)
        if not steps_data:
            raise ValueError("Planner returned no parseable steps")

        steps: list[TaskStep] = []
        for i, step_data in enumerate(steps_data):
            tool_name = step_data.get("tool_name") or ""
            if not tool_name:
                raise ValueError("Plan contained a step without tool_name")

            step = TaskStep(
                task_id=task.id,
                description=step_data.get("description", f"Step {i + 1}"),
                tool_name=tool_name,
                tool_args=step_data.get("tool_args", {}),
                order=i,
            )
            steps.append(step)

        logger.info("Planned %d steps for task %s", len(steps), task.id)
        return steps

    except Exception:
        logger.exception("Planning failed for task %s", task.id)
        return await _fallback_plan(task)


async def _fallback_plan(task: Task) -> list[TaskStep]:
    """Recover from planning failure WITHOUT inventing web-search noise."""
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
        )

        import re as _re

        json_match = _re.search(r'\{[^{}]+\}', response)
        if json_match:
            tool_data = json.loads(json_match.group())
            tool_name = tool_data.get("tool_name", "")
            tool_args = tool_data.get("tool_args", {})
            description = tool_data.get("description", task.goal[:100])

            if tool_name == "run_command" and "command" not in tool_args:
                tool_args = {"command": task.goal}
            elif tool_name == "execute_code" and "code" not in tool_args:
                tool_args = {"code": f"# {task.goal}\nprint('Ready to execute')"}
            elif tool_name == "web_search" and "query" not in tool_args:
                tool_args = {"query": task.goal, "num_results": 5}
            elif tool_name == "write_file" and "path" not in tool_args:
                tool_args = {"path": "output/result.md", "content": "{{step_0_result}}"}
            elif tool_name in ("read_file", "list_directory") and "path" not in tool_args:
                tool_args = {"path": "output/"}

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
    honest diagnostic step instead of a random web search full of noise.
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

    logger.warning("Failed to parse steps JSON")
    return []
