"""Orchestrator — the main agent loop.

Combines OpenClaw's reasoning engine pattern with Hermes' self-improving loop.
Flow: receive task -> check memory -> plan -> execute -> reflect -> store.

Self-improvement: past reflections are extracted as lessons and fed into future planning.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.config import settings
from agent.core.executor import execute_step, list_tools
from agent.core.memory import memory_store
from agent.core.planner import plan_task
from agent.models import Task, TaskStatus

logger = logging.getLogger(__name__)


class Orchestrator:
    """Main agent loop — receives tasks, plans, executes, learns."""

    def __init__(self) -> None:
        self.memory = memory_store
        self._running = False

    def _extract_lessons(self, goal: str) -> list[str]:
        """Search memory for relevant past reflections and extract actionable lessons."""
        reflections = self.memory.search(goal, top_k=5, category="reflection")
        lessons = []
        for r in reflections:
            text = r.content.strip()
            if text and len(text) > 10:
                lessons.append(text)
        return lessons

    async def handle_task(self, task: Task) -> Task:
        """Process a task from start to finish."""
        logger.info("Handling task [%s]: %s", task.id, task.goal[:100])
        task.status = TaskStatus.PLANNING
        task.updated_at = datetime.now(timezone.utc)

        try:
            # Check memory for similar past tasks
            similar = self.memory.search(task.goal, top_k=3, category="task_outcome")
            if similar:
                logger.info("Found %d similar past tasks in memory", len(similar))

            # Extract lessons from past reflections (self-improvement)
            lessons = self._extract_lessons(task.goal)
            if lessons:
                logger.info("Applying %d past lessons to planning", len(lessons))

            # Get available skills
            available_skills = [
                e.metadata.get("skill_name", "")
                for e in self.memory.get_by_category("skill")
            ]

            # Plan with lessons (self-improvement feeds into planning)
            task.steps = await plan_task(task, available_skills or None, lessons or None)
            task.status = TaskStatus.EXECUTING

            # Execute steps
            context: dict[str, Any] = {"task_id": task.id, "task_goal": task.goal}
            for step in task.steps:
                logger.info("Executing step %d: %s", step.order, step.description[:80])
                result = await execute_step(step, context)
                context[f"step_{step.order}_result"] = result.output
                if not result.success:
                    task.status = TaskStatus.FAILED
                    task.error = result.error or "Step failed"
                    self.memory.save_task_outcome(task.goal, task.error, success=False)
                    await self._self_reflect(task)
                    return task

            task.status = TaskStatus.COMPLETED
            task.result = context.get("step_0_result", "Task completed")
            task.updated_at = datetime.now(timezone.utc)
            self.memory.save_task_outcome(task.goal, task.result, success=True)
            await self._self_reflect(task)

            logger.info("Task [%s] completed successfully", task.id)
            return task

        except Exception as exc:
            logger.exception("Task [%s] failed with exception", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            self.memory.save_task_outcome(task.goal, str(exc), success=False)
            return task

    async def handle_goal(self, goal: str, context: dict[str, Any] | None = None) -> Task:
        """Create and handle a task from a goal string."""
        task = Task(goal=goal, context=context or {})
        return await self.handle_task(task)

    async def _self_reflect(self, task: Task) -> None:
        """Post-task reflection — adapted from Hermes' self-improvement loop.

        After each task, the agent reflects on what happened and extracts
        actionable lessons that influence future planning.
        """
        from agent.core.gemini_client import generate_content

        success = task.status == TaskStatus.COMPLETED
        reflection_prompt = f"""You just completed a task. Reflect and extract actionable lessons.

Goal: {task.goal}
Success: {success}
Result: {(task.result or task.error or 'N/A')[:500]}
Steps taken: {len(task.steps)}

Write 1-3 concise lessons learned. Each lesson should be:
- A specific, actionable insight (not vague)
- Something that would help plan a similar task better next time
- Example: "When searching for recent news, always specify the year in the query"

If nothing notable, say "No specific lessons learned."

Keep response under 150 words."""

        try:
            reflection = await generate_content(
                system="You are a self-improving AI agent. Extract concrete lessons. Be concise.",
                user=reflection_prompt,
                temperature=0.3,
                max_tokens=200,
            )
            self.memory.save_reflection(reflection)
            logger.debug("Self-reflection saved for task %s", task.id)
        except Exception:
            logger.debug("Self-reflection failed (non-critical)")

    def get_status(self) -> dict[str, Any]:
        """Return current orchestrator status."""
        return {
            "memory_size": self.memory.size,
            "available_tools": list_tools(),
            "running": self._running,
        }


orchestrator = Orchestrator()
