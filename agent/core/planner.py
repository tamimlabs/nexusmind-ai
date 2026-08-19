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

Rules:
- Each step must be specific and actionable
- Order steps logically (dependencies first)
- Use available tools when possible: web_search, read_file, write_file, execute_code, parse_json, summarize_text, extract_data
- Estimate which tool each step requires
- Return ONLY valid JSON array of steps

Output format (JSON):
[
  {"description": "step description", "tool_name": "tool_name_or_null", "tool_args": {"key": "value"}},
  ...
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

    skills_context = ""
    if available_skills:
        skills_context = f"\nAvailable skills: {', '.join(available_skills)}"

    lessons_context = ""
    if lessons:
        lessons_context = "\n\nLESSONS FROM PAST TASKS:\n" + "\n".join(f"- {l}" for l in lessons[:5])

    user_prompt = f"""Goal: {task.goal}
Context: {json.dumps(task.context) if task.context else 'None'}{skills_context}{lessons_context}

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
            step = TaskStep(
                task_id=task.id,
                description=step_data.get("description", f"Step {i + 1}"),
                tool_name=step_data.get("tool_name"),
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
                description=f"Execute goal directly: {task.goal}",
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
    return [{"description": text[:500]}]
