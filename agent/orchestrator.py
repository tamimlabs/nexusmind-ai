"""Orchestrator — the main agent loop.

Combines OpenClaw's reasoning engine pattern with Hermes' self-improving loop.
Flow: receive task -> check memory -> plan -> execute -> reflect -> store.

Self-improvement: past reflections are extracted as lessons and fed into future planning.

Memory policy (inspired by Hermes Agent):
- Only store MEANINGFUL outcomes, not routine tasks
- Only reflect when there's something genuinely NEW to learn
- Deduplicate: don't store if similar lesson already exists
- Hard limit: keep memory lean, force consolidation when full
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agent.core.executor import execute_step, list_tools, set_task_context
from agent.core.memory import memory_store
from agent.core.planner import plan_task
from agent.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# Trivial tasks that don't need memory storage
_TRIVIAL_PATTERNS = [
    "what is", "what are", "who is", "tell me about",
    "hello", "hi", "hey", "thanks", "ok", "yes", "no",
]


def _is_trivial(task: Task) -> bool:
    """Check if a task is trivial and doesn't warrant memory storage."""
    goal_lower = task.goal.lower().strip()
    # Short goals with no tools used are trivial
    if len(task.steps) <= 1 and len(goal_lower) < 50:
        for pattern in _TRIVIAL_PATTERNS:
            if goal_lower.startswith(pattern):
                return True
    return False


def _is_novel_reflection(reflection: str, existing: list[str]) -> bool:
    """Check if a reflection contains genuinely new information."""
    if not reflection or len(reflection.strip()) < 20:
        return False
    # Check if similar content already exists
    reflection_lower = reflection.lower()
    for existing_ref in existing:
        # If significant overlap, it's not novel
        existing_words = set(existing_ref.lower().split())
        reflection_words = set(reflection_lower.split())
        if existing_words and reflection_words:
            overlap = len(existing_words & reflection_words) / max(len(existing_words), 1)
            if overlap > 0.6:
                return False
    return True


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
        task.updated_at = datetime.now(UTC)

        # Set task context for Telegram approval messages
        set_task_context(task.id, task.goal)

        # Notify via Telegram
        from agent.telegram import is_configured, notify_task_started
        if is_configured():
            await notify_task_started(task.id, task.goal)

        try:
            # Check memory for similar past tasks (for context, not always needed)
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
                    # Only store failures that are interesting (not trivial task failures)
                    if not _is_trivial(task):
                        self.memory.save_task_outcome(task.goal, task.error, success=False)
                    await self._self_reflect(task)
                    return task

            task.status = TaskStatus.COMPLETED
            # Use the last step's result as the final result
            if task.steps:
                last_step = task.steps[-1]
                task.result = last_step.result or "Task completed"
            else:
                task.result = context.get("step_0_result", "Task completed")
            task.updated_at = datetime.now(UTC)

            # Only store outcome if the task was non-trivial and had multiple steps
            if not _is_trivial(task) and len(task.steps) > 1:
                self.memory.save_task_outcome(task.goal, task.result[:200], success=True)

            await self._self_reflect(task)

            # Notify via Telegram
            from agent.telegram import is_configured, notify_task_completed
            if is_configured():
                await notify_task_completed(task.id, task.goal, task.result[:300])

            logger.info("Task [%s] completed successfully", task.id)
            return task

        except Exception as exc:
            logger.exception("Task [%s] failed with exception", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            if not _is_trivial(task):
                self.memory.save_task_outcome(task.goal, str(exc)[:200], success=False)

            # Notify via Telegram
            from agent.telegram import is_configured, notify_task_failed
            if is_configured():
                await notify_task_failed(task.id, task.goal, str(exc)[:300])

            return task

    async def handle_goal(self, goal: str, context: dict[str, Any] | None = None) -> Task:
        """Create and handle a task from a goal string."""
        task = Task(goal=goal, context=context or {})
        return await self.handle_task(task)

    async def _self_reflect(self, task: Task) -> None:
        """Post-task reflection — adapted from Hermes' self-improvement loop.

        Only stores reflection if it contains genuinely new, actionable information.
        Skips trivial tasks entirely.
        """
        # Skip reflection for trivial tasks
        if _is_trivial(task):
            return

        from agent.core.gemini_client import generate_content

        success = task.status == TaskStatus.COMPLETED
        reflection_prompt = f"""You just completed a task. Extract ONLY genuinely new, actionable lessons.

Goal: {task.goal}
Success: {success}
Result: {(task.result or task.error or 'N/A')[:500]}
Steps taken: {len(task.steps)}

RULES:
- Only output lessons that are SPECIFIC and REUSABLE for future similar tasks
- Do NOT save generic advice like "plan carefully" or "check inputs"
- Do NOT save task-specific details like filenames or URLs
- If this was a routine task with no new insights, say exactly: NOTHING_TO_SAVE
- Each lesson should be a single sentence, under 20 words

Examples of GOOD lessons:
- "When searching for news, always include the year in the query"
- "JSON extraction from HTML needs to handle escaped quotes"
- "The fetch_url tool strips HTML tags automatically, no need for manual cleanup"

Examples of BAD lessons (don't save these):
- "Task completed successfully" (not actionable)
- "Used web_search and summarize tools" (just describing what happened)
- "Always double-check file paths" (too generic)

Output 0-2 lessons, or NOTHING_TO_SAVE."""

        try:
            reflection = await generate_content(
                system="You are a self-improving AI agent. Only extract genuinely new lessons. Be extremely selective.",
                user=reflection_prompt,
                temperature=0.2,
                max_tokens=200,
            )

            # Don't save if the model says nothing is worth saving
            if not reflection or "NOTHING_TO_SAVE" in reflection.upper():
                logger.debug("No new lessons from task %s", task.id)
                return

            # Check for novelty against existing reflections
            existing = [e.content for e in self.memory.get_by_category("reflection")]
            if _is_novel_reflection(reflection, existing):
                self.memory.save_reflection(reflection.strip())
                logger.info("New lesson saved from task %s", task.id)
            else:
                logger.debug("Reflection not novel, skipping for task %s", task.id)

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
