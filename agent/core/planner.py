"""Task decomposition planner — breaks goals into executable steps.

Inspired by OpenClaw's multi-step decomposition and Hermes' skill-based planning.
Now includes self-improvement: past reflections influence future planning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.config import settings
from agent.models import Task, TaskStep

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are an expert task planner for an autonomous AI agent.
Given a user goal, decompose it into a sequence of concrete, executable steps.

AVAILABLE TOOLS (you MUST use these exact names):
- web_search: Search the web. Args: {"query": "search terms", "num_results": 5}
- fetch_url: Fetch content from a URL. Args: {"url": "https://..."}
- read_file: Read a file. Args: {"path": "file_path"}
- write_file: Write to a file. Args: {"path": "file_path", "content": "text"}
- list_directory: List files in a directory. Args: {"path": "dir_path"}
- execute_code: Run Python code (REQUIRES APPROVAL). Args: {"code": "python code"}
- run_command: Run shell command (REQUIRES APPROVAL). Args: {"command": "shell command"}
- parse_json: Extract data from JSON. Args: {"json_text": "json string", "keys": ["key1"]}
- summarize_text: Summarize long text. Args: {"text": "text to summarize"}
- extract_data: Extract structured data from text. Args: {"text": "input text", "pattern": "what to extract"}

RULES:
1. EVERY step MUST have a "tool_name" from the list above — no exceptions
2. EVERY step MUST have "tool_args" matching that tool's expected arguments
3. Order steps logically (dependencies first)
4. Use the RIGHT tool for the job — web_search for searching, fetch_url for URLs, etc.
5. For research tasks: use web_search first, then fetch_url on results
6. For file tasks: use read_file/write_file
7. For code tasks: use execute_code
8. Return ONLY valid JSON array

OUTPUT FORMAT (JSON array):
[
  {"description": "Search for recent Python news", "tool_name": "web_search", "tool_args": {"query": "Python news 2026", "num_results": 5}},
  {"description": "Fetch the top article", "tool_name": "fetch_url", "tool_args": {"url": "https://example.com/article"}},
  {"description": "Summarize findings", "tool_name": "summarize_text", "tool_args": {"text": "{{step_1_result}}"}}
]
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

Decompose this into executable steps. Return ONLY the JSON array."""

    try:
        response = await generate_content(
            model=settings.gemini_model,
            system=PLANNER_SYSTEM_PROMPT,
            user=user_prompt,
        )

        steps_data = _parse_steps_json(response)
        steps: list[TaskStep] = []
        for i, step_data in enumerate(steps_data):
            # Ensure tool_name is always set
            tool_name = step_data.get("tool_name")
            if not tool_name:
                tool_name = "web_search"  # Default fallback

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
        return [
            TaskStep(
                task_id=task.id,
                description=f"Search the web for: {task.goal}",
                tool_name="web_search",
                tool_args={"query": task.goal},
                order=0,
            )
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
