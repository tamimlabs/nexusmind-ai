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
   → curl commands are SAFE and auto-approved — use them freely for API calls.

RULES:
1. EVERY step MUST have "tool_name" and "tool_args"
2. Use {{step_N_result}} to reference previous step outputs
3. For research: use MULTIPLE web_search calls with different queries
4. NEVER default to fetch_url — it fails often (DNS, blocks, timeouts)
5. ALWAYS end with write_file to save meaningful output to output/ directory
6. Keep plans SHORT — 3-5 steps maximum. Efficiency > thoroughness.
7. Return ONLY valid JSON array
8. If goal mentions GitHub, PRs, merge, repository, or API — use run_command with curl, NEVER web_search

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
        # Ask Gemini to pick the right tool for this task
        try:
            tool_prompt = f"""You are a tool selector. Given this task, pick the RIGHT tool.

Task: {task.goal}

Available tools:
- web_search: Search the web. Use for research questions.
- run_command: Shell command. Use for API calls (curl), system commands, git, etc.
- execute_code: Python code. Use for data processing, analysis, calculations.
- read_file: Read a file. Use when task asks to read/open a file.
- write_file: Write a file. Use when task asks to create/save content.
- list_directory: List files. Use when task asks to list/show files.

Return ONLY a JSON object: {{"tool_name": "tool_name", "tool_args": {{...}}, "description": "what to do"}}"""

            response = await generate_content(
                model=settings.gemini_model,
                system="You are a tool selector. Return only JSON.",
                user=tool_prompt,
            )

            # Parse the response
            import re as _re
            json_match = _re.search(r'\{[^{}]+\}', response)
            if json_match:
                tool_data = json.loads(json_match.group())
                tool_name = tool_data.get("tool_name", "web_search")
                tool_args = tool_data.get("tool_args", {})
                description = tool_data.get("description", task.goal[:100])

                # Fix tool_args based on tool type
                if tool_name == "run_command":
                    if "command" not in tool_args:
                        tool_args = {"command": task.goal}
                elif tool_name == "execute_code":
                    if "code" not in tool_args:
                        tool_args = {"code": f"# {task.goal}\nprint('Ready to execute')"}
                elif tool_name == "web_search":
                    if "query" not in tool_args:
                        tool_args = {"query": task.goal, "num_results": 5}
                elif tool_name == "write_file":
                    if "path" not in tool_args:
                        tool_args = {"path": "output/result.md", "content": "{{step_0_result}}"}
                elif tool_name == "read_file":
                    if "path" not in tool_args:
                        tool_args = {"path": "output/"}
                elif tool_name == "list_directory":
                    if "path" not in tool_args:
                        tool_args = {"path": "output/"}

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

        # Last resort: web search
        return [
            TaskStep(
                task_id=task.id,
                description=f"Search for information about: {task.goal}",
                tool_name="web_search",
                tool_args={"query": task.goal, "num_results": 5},
                order=0,
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
