"""Orchestrator — the main agent loop.

Combines OpenClaw's reasoning engine pattern with Hermes' self-improving loop.
Flow: receive task -> check memory -> plan -> execute -> reflect -> store.
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

    async def handle_task(self, task: Task) -> Task:
        """Process a task from start to finish."""
        logger.info("Handling task [%s]: %s", task.id, task.goal[:100])
        task.status = TaskStatus.PLANNING
        task.updated_at = datetime.now(timezone.utc)

        try:
            similar = self.memory.search(task.goal, top_k=3, category="task_outcome")
            if similar:
                logger.info("Found %d similar past tasks in memory", len(similar))

            available_skills = [
                e.metadata.get("skill_name", "")
                for e in self.memory.get_by_category("skill")
            ]
            task.steps = await plan_task(task, available_skills or None)
            task.status = TaskStatus.EXECUTING

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

        After each task, the agent reflects on what happened and optionally
        generates a reusable skill if the task involved a novel pattern.
        """
        from agent.core.gemini_client import generate_content

        success = task.status == TaskStatus.COMPLETED
        reflection_prompt = f"""You just completed a task. Reflect briefly.

Goal: {task.goal}
Success: {success}
Result: {(task.result or task.error or 'N/A')[:500]}
Steps taken: {len(task.steps)}

Answer in 2-3 sentences:
1. What worked well or what failed?
2. Is this a repeatable pattern worth saving as a skill?
3. If yes, give a 1-line skill name and description."""

        try:
            reflection = await generate_content(
                system="You are a self-improving AI agent. Be concise.",
                user=reflection_prompt,
                temperature=0.4,
                max_tokens=300,
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
