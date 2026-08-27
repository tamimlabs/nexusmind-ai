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
from agent.core.skill_library import skill_library as _skill_library
from agent.models import StepStatus, Task, TaskStatus

logger = logging.getLogger(__name__)

# Trivial tasks that don't need memory storage
_TRIVIAL_PATTERNS = [
    "what is", "what are", "who is", "tell me about",
    "hello", "hi", "hey", "thanks", "ok", "yes", "no",
]

# Phrases that mark a goal as a STANDING INSTRUCTION (future policy) rather
# than an immediate command. Stored in memory; the watcher enforces them later.
_INSTRUCTION_TRIGGERS = (
    "when you", "whenever", "every time", "each time", "from now on",
    "in future", "in the future", "always ", "by default", "next time",
    "if a new ", "if any new ", "going forward",
)


def _is_standing_instruction(goal: str) -> bool:
    """True if the goal reads like a durable instruction, not a one-off command.

    Anchored to the START of the goal so one-off commands that merely contain
    a trigger word mid-sentence (e.g. "merge pr #7 always using squash")
    are still executed immediately.
    """
    g = goal.lower().lstrip()
    return g.startswith(_INSTRUCTION_TRIGGERS)


def _is_trivial(task: Task) -> bool:
    """Check if a task is trivial and doesn't warrant memory storage."""
    goal_lower = task.goal.lower().strip()
    # Short goals with no tools used are trivial
    if len(task.steps) <= 1 and len(goal_lower) < 50:
        for pattern in _TRIVIAL_PATTERNS:
            if goal_lower.startswith(pattern):
                return True
    return False


def _clean_lessons(raw: str) -> list[str]:
    """Sanitize reflection output into clean, self-contained lesson lines.

    Guards against the two failure modes seen in production:
    - prompt echoes ("You just completed a task... Goal:") being saved whole
    - max-token truncations storing half a sentence as a "lesson"
    """
    if not raw:
        return []
    echo_markers = (
        "you just completed",
        "goal:",
        "output 0-2 lessons",
        "rules:",
        "examples of good",
        "examples of bad",
        "result:",
        "steps taken:",
    )
    lessons: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line or "nothing_to_save" in line.lower():
            continue
        low = line.lower()
        if any(marker in low for marker in echo_markers):
            continue
        words = line.split()
        # Template asks for single sentences under 20 words; allow slack but
        # reject fragments (too short) and run-ons/truncations (too long).
        if not 4 <= len(words) <= 30:
            continue
        # A sentence must END like one — mid-thought truncations don't.
        if line[-1] not in ".!?":
            continue
        lessons.append(line)
        if len(lessons) == 2:
            break
    return lessons


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
        self.skills = _skill_library
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

        # Standing instructions ("whenever a PR arrives, ...") are stored as
        # policy for future watcher events — not executed now. Watcher- and
        # webhook-triggered tasks carry event context and skip this check.
        if _is_standing_instruction(task.goal) and "event_type" not in task.context:
            self.memory.save_instruction(task.goal)
            task.status = TaskStatus.COMPLETED
            task.result = (
                f"Saved as standing instruction (not executed now):\n{task.goal}\n\n"
                "The agent will follow this automatically when matching events "
                "arrive via watchers."
            )
            task.updated_at = datetime.now(UTC)
            logger.info("Stored standing instruction from task %s", task.id)

            from agent.telegram import is_configured, notify_task_completed

            if is_configured():
                await notify_task_completed(task.id, task.goal, "Standing instruction saved")
            return task

        # Notify via Telegram
        from agent.telegram import is_configured, notify_task_started
        if is_configured():
            await notify_task_started(task.id, task.goal)

        try:
            # Prefetch relevant memory for this turn (Hermes pattern): a fenced
            # <memory-context> block with trust-ranked, hybrid-retrieved facts.
            # Trivial prompts are gated inside prefetch() and return "".
            memory_context = self.memory.prefetch(task.goal)
            if memory_context:
                logger.info("Recalled %d chars of memory context for planning", len(memory_context))

            # Check memory for similar past tasks (for context, not always needed)
            similar = self.memory.search(task.goal, top_k=3, category="task_outcome")
            if similar:
                logger.info("Found %d similar past tasks in memory", len(similar))

            # Extract lessons from past reflections (self-improvement)
            lessons = self._extract_lessons(task.goal)
            if lessons:
                logger.info("Applying %d past lessons to planning", len(lessons))

            # Procedural skill index + best-matched procedure body (Hermes
            # description-as-router pattern). matched skills get use-credit
            # when the task completes successfully.
            skill_context, matched_skills = self.skills.plan_context(task.goal)
            if skill_context:
                logger.info(
                    "Injected skill context (%d matched procedure(s))", len(matched_skills)
                )

            # Get available skills
            available_skills = [
                e.metadata.get("skill_name", "")
                for e in self.memory.get_by_category("skill")
            ]

            # Plan with lessons + recalled memory + procedural skills
            task.steps = await plan_task(
                task,
                available_skills or None,
                lessons or None,
                memory_context or None,
                skill_context or None,
            )
            task.status = TaskStatus.EXECUTING

            # Execute steps
            context: dict[str, Any] = {"task_id": task.id, "task_goal": task.goal}
            for step in task.steps:
                logger.info("Executing step %d: %s", step.order, step.description[:80])
                result = await execute_step(step, context)
                # Store with BOTH 0-indexed and 1-indexed keys so templates always resolve
                context[f"step_{step.order}_result"] = result.output
                context[f"step_{step.order + 1}_result"] = result.output
                if not result.success:
                    logger.warning("Step %d failed: %s — skipping and continuing", step.order, result.error)
                    context[f"step_{step.order}_result"] = f"[Step skipped: {result.error}]"
                    context[f"step_{step.order + 1}_result"] = f"[Step skipped: {result.error}]"
                    continue

            task.status = TaskStatus.COMPLETED
            # Use the most meaningful result (prefer summarize/extract over write_file)
            if task.steps:
                best_result = None
                for s in reversed(task.steps):
                    if s.status == StepStatus.SUCCESS and s.result:
                        if not any(kw in (s.tool_name or "") for kw in ["write_file", "read_file", "list_directory"]):
                            best_result = s
                            break
                        if best_result is None:
                            best_result = s
                task.result = (best_result.result if best_result else task.steps[-1].result) or "Task completed"
                # Always append saved file locations so user knows where output went
                saved = []
                for s in task.steps:
                    if s.status == StepStatus.SUCCESS and s.result and s.tool_name in ("write_file", "execute_code", "run_command"):
                        # write_file: "Written X chars to output/..." , execute_code scaffold: "Project scaffold written to ..."
                        m = s.result.strip()
                        if "Written" in m and " to " in m:
                            # extract path after " to "
                            try:
                                path = m.split(" to ", 1)[1].splitlines()[0].strip().strip("'\"")
                                saved.append(path)
                            except Exception:
                                pass
                        elif "Project scaffold written to" in m:
                            try:
                                path = m.split("Project scaffold written to", 1)[1].splitlines()[0].strip().strip("'\"")
                                saved.append(path)
                            except Exception:
                                pass
                        elif "written to" in m.lower() and ("output/" in m or "projects/" in m):
                            saved.append(m.splitlines()[0][:120].strip())
                if saved:
                    uniq = []
                    for p in saved:
                        if p not in uniq:
                            uniq.append(p)
                    task.result = task.result.rstrip() + "\n\n📁 Saved to:\n" + "\n".join(f"- {p}" for p in uniq)
            else:
                task.result = context.get("step_0_result", "Task completed")
            task.updated_at = datetime.now(UTC)

            # Only store outcome if the task was non-trivial and had multiple steps
            if not _is_trivial(task) and len(task.steps) > 1:
                self.memory.save_task_outcome(task.goal, task.result[:200], success=True)

            # Auto-extract durable preferences/decisions from this interaction
            # (Hermes session-harvest pattern, applied per-task instead).
            extracted = self.memory.extract_and_store(task.goal)
            if extracted:
                logger.info("Auto-extracted %d durable fact(s) from task goal", extracted)

            # Credit any injected procedural skills that were actually followed,
            # then try to grow the library from this task (self-evolution).
            for name in matched_skills:
                self.skills.record_use(name)
                logger.info("Skill '%s' used by task %s", name, task.id)
            await self._maybe_create_skill(task)

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

    async def _maybe_create_skill(self, task: Task) -> None:
        """Auto-create a reusable skill from a solved multi-step task.

        Hermes' trigger heuristics (skill_manager_tool schema): complex task
        succeeded, non-trivial workflow discovered. We gate deterministically
        (>=2 successful steps of >=2 DISTINCT tools), then let Gemini write the
        SKILL.md, then enforce the same hard validation gates as manual creates.
        Failures are logged and swallowed — never break task completion.
        """
        if _is_trivial(task) or len(task.steps) < 2:
            return
        successful = [s for s in task.steps if s.status == StepStatus.SUCCESS]
        distinct_tools = {s.tool_name for s in successful if s.tool_name}
        if len(successful) < 2 or len(distinct_tools) < 2:
            logger.debug("Task %s too routine for skill creation", task.id)
            return

        # Dedup gate: skip if an existing skill already covers this procedure.
        probe = f"{task.goal} {(task.result or '')[:200]}"
        similar = self.skills.find_similar(probe)
        if similar:
            logger.info("Similar skill exists (%s); skipping auto-creation", similar[0]["name"])
            return

        try:
            from agent.core.gemini_client import generate_content

            steps_summary = "\n".join(
                f"- [{s.tool_name}] {s.description}: {(s.result or '')[:120]}"
                for s in successful[:6]
            )
            synthesis_prompt = f"""A task was just completed successfully. Decide whether it contains a
REUSABLE PROCEDURE worth saving as a skill for future similar tasks.

Goal: {task.goal}

Steps executed:
{steps_summary}

Final result: {(task.result or '')[:400]}

RULES:
- Skills must encode a REPEATABLE WORKFLOW (trigger + steps + pitfalls), NOT task-specific facts.
- If this was routine/one-off with nothing reusable, output exactly: NOTHING_TO_SAVE
- description MUST be <= 60 chars: 'Use when <trigger>. <behavior>.' One sentence.
- name MUST be lowercase-kebab-case slug.

Output ONLY the SKILL.md file:

---
name: <kebab-slug>
description: "<=60 chars, trigger first"
version: 1.0.0
---

# <Title>

## When to Use
<bullet triggers>

## Procedure
<numbered steps referencing real tools: web_search, fetch_url, run_command,
execute_code, read_file, write_file, github_*>

## Pitfalls
<what went wrong or could go wrong>"""

            response = await generate_content(
                system=(
                    "You are a self-evolving AI agent that distills solved tasks "
                    "into reusable markdown skills. Be extremely selective."
                ),
                user=synthesis_prompt,
                temperature=0.3,
                max_tokens=900,
            )
            if not response or "NOTHING_TO_SAVE" in response.upper():
                logger.debug("No skill synthesized from task %s", task.id)
                return

            content = response.strip()
            if content.startswith("```"):
                content = "\n".join(
                    line for line in content.splitlines() if not line.strip().startswith("```")
                ).strip()

            meta, _body = self.skills.split_frontmatter(content)
            name = self.skills.create(
                name=str(meta.get("name") or task.goal),
                content=content,
                actor="agent",
                created_by="agent",
                origin_task=task.id,
            )
            logger.info("Auto-created skill '%s' from task %s", name, task.id)
        except Exception:
            logger.warning("Skill auto-creation failed for task %s (non-critical)", task.id, exc_info=True)

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

            # Sanitize: prompt echoes and mid-sentence truncations previously
            # leaked into LESSONS context and contaminated future planning.
            cleaned = _clean_lessons(reflection)
            if not cleaned:
                logger.debug("No usable lessons from task %s", task.id)
                return

            # Check for novelty against existing reflections
            existing = [e.content for e in self.memory.get_by_category("reflection")]
            joined = "\n".join(cleaned)
            if _is_novel_reflection(joined, existing):
                self.memory.save_reflection(joined)
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
