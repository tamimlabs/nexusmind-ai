"""Task decomposition planner — breaks goals into executable steps.

Inspired by OpenClaw's multi-step decomposition and Hermes' skill-based planning.
Now includes self-improvement: past reflections influence planning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.config import settings
from agent.models import Task, TaskStep

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are NexusMind — an elite autonomous AI agent planner.
Your job: decompose user goals into executable steps that WILL succeed.

CRITICAL DESIGN PRINCIPLE: Every plan must be RESILIENT. Assume tools CAN fail.
Build plans that produce useful output even if some steps fail.

AVAILABLE TOOLS:
- web_search: Search the web. Args: {"query": "...", "num_results": 5}
  → Returns: titles, snippets, URLs. Snippets are USUALLY enough — no need to fetch full page.
  → PRO TIP: Use multiple queries with different angles for comprehensive results.

- fetch_url: Fetch full webpage content. Args: {"url": "https://..."}
  → RISKY: DNS failures, timeouts, blocks. ONLY use if you truly need full article text.
  → BETTER: Use web_search snippets directly — they're usually sufficient.

- summarize_text: Summarize text. Args: {"text": "text"}
- extract_data: Extract structured data. Args: {"text": "...", "pattern": "..."}
- parse_json: Parse JSON. Args: {"json_text": "...", "keys": [...]}
- read_file / write_file / list_directory: File operations
- execute_code: Python (REQUIRES APPROVAL). Args: {"code": "..."}
- run_command: Shell (REQUIRES APPROVAL). Args: {"command": "..."}

PLANNING STRATEGIES BY TASK TYPE:

1. RESEARCH TASKS (summarize, analyze, report on a topic):
   Step 1: web_search with focused query
   Step 2: web_search with broader/different query (for diversity)
   Step 3: summarize_text combining BOTH search results
   Step 4: write_file to save output
   → NEVER use fetch_url for research — snippets are enough.

2. FILE/CREATION TASKS (create, write, generate):
   Step 1: (optional) web_search if you need reference data
   Step 2: write_file with the content
   → Keep it simple. Don't over-plan.

3. ANALYSIS TASKS (compare, evaluate, compute):
   Step 1: Gather data via web_search or read_file
   Step 2: extract_data or summarize_text to process
   Step 3: write_file with results

4. CODING TASKS (write code, run computations):
   Step 1: Plan the logic
   Step 2: execute_code with the implementation
   → Minimize steps. Code tasks should be direct.

5. GITHUB/API TASKS (monitor repos, review PRs, manage issues):
   Step 1: run_command with curl to call GitHub API
   Step 2: execute_code to parse the response and analyze
   Step 3: run_command with curl to post comment or merge
   Step 4: write_file to save review log
   → NEVER use web_search for API calls — use run_command or execute_code directly.

RULES:
1. EVERY step MUST have "tool_name" and "tool_args"
2. Use {{step_N_result}} to reference previous step outputs
3. For research: use MULTIPLE web_search calls with different queries
4. NEVER default to fetch_url — it fails often (DNS, blocks, timeouts)
5. ALWAYS end with write_file to save meaningful output to output/ directory
6. Keep plans SHORT — 3-5 steps maximum. Efficiency > thoroughness.
7. Return ONLY valid JSON array

OUTPUT FORMAT (JSON array):
[
  {"description": "Search for recent Python news", "tool_name": "web_search", "tool_args": {"query": "Python news 2026", "num_results": 5}},
  {"description": "Summarize the search results into a report", "tool_name": "summarize_text", "tool_args": {"text": "{{step_0_result}}"}},
  {"description": "Save the summary to a file", "tool_name": "write_file", "tool_args": {"path": "output/summary.md", "content": "{{step_1_result}}"}}
]

IMPORTANT: Steps are 0-indexed. First step is step_0, second is step_1, etc.
When referencing previous results, use {{step_0_result}}, {{step_1_result}}, etc.
"""


async def plan_task(task: Task, available_skills: list[str] | None = None, lessons: list[str] | None = None) -> list[TaskStep]:
    """Decompose a task into ordered steps using Gemini.

    Args:
        task: The task to plan.
        available_skills: Optional list of available skill names.
        lessons: Past reflections/lessons learned from previous tasks.

    Returns:
        Ordered list of TaskStep objects.

    """
    from agent.core.gemini_client import generate_content

    lessons_context = ""
    if lessons:
        lessons_context = "\n\nLESSONS FROM PAST TASKS:\n" + "\n".join(f"- {l}" for l in lessons[:5])

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
        steps: list[TaskStep] = []
        for i, step_data in enumerate(steps_data):
            tool_name = step_data.get("tool_name")
            if not tool_name:
                tool_name = "web_search"

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
        # Resilient fallback: choose tool based on task content
        goal_lower = task.goal.lower()

        # GitHub/API task
        if any(w in goal_lower for w in ["github", "pr", "pull request", "repo", "api", "curl", "merge", "review code"]):
            return [
                TaskStep(
                    task_id=task.id,
                    description="Fetch data from GitHub API",
                    tool_name="run_command",
                    tool_args={"command": "source .env 2>/dev/null; curl -s -H \"Authorization: token $GITHUB_TOKEN\" https://api.github.com/repos/tamimlabs/nexusmind-ai/pulls"},
                    order=0,
                ),
                TaskStep(
                    task_id=task.id,
                    description="Analyze the API response and take action",
                    tool_name="execute_code",
                    tool_args={"code": "import json, urllib.request, os\ntoken = os.getenv('GITHUB_TOKEN', '')\nprint(f'GitHub token loaded: {bool(token)}')"},
                    order=1,
                ),
            ]

        # File read/list task
        if any(w in goal_lower for w in ["read file", "list directory", "show file", "open file"]):
            return [
                TaskStep(
                    task_id=task.id,
                    description="Read the requested file or directory",
                    tool_name="read_file" if "read" in goal_lower else "list_directory",
                    tool_args={"path": "output/"},
                    order=0,
                ),
            ]

        # Code execution task
        if any(w in goal_lower for w in ["run code", "execute", "calculate", "compute", "python", "script"]):
            return [
                TaskStep(
                    task_id=task.id,
                    description="Write and execute the Python code",
                    tool_name="execute_code",
                    tool_args={"code": "print('Ready to execute')"},
                    order=0,
                ),
            ]

        # Shell command task
        if any(w in goal_lower for w in ["run command", "shell", "bash", "terminal", "install"]):
            return [
                TaskStep(
                    task_id=task.id,
                    description="Execute the shell command",
                    tool_name="run_command",
                    tool_args={"command": "echo 'Ready to run command'"},
                    order=0,
                ),
            ]

        # Default: web search fallback
        return [
            TaskStep(
                task_id=task.id,
                description=f"Search for information about: {task.goal}",
                tool_name="web_search",
                tool_args={"query": task.goal, "num_results": 5},
                order=0,
            ),
            TaskStep(
                task_id=task.id,
                description="Create a comprehensive summary",
                tool_name="summarize_text",
                tool_args={"text": "{{step_0_result}}"},
                order=1,
            ),
            TaskStep(
                task_id=task.id,
                description="Save the summary to output file",
                tool_name="write_file",
                tool_args={"path": "output/summary.md", "content": "{{step_1_result}}"},
                order=2,
            ),
        ]


def _parse_steps_json(text: str) -> list[dict[str, Any]]:
    """Extract JSON array of steps from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "steps" in result:
            return result["steps"]
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse steps JSON, returning fallback")
    return [{"description": text[:500], "tool_name": "web_search", "tool_args": {"query": text[:100]}}]
