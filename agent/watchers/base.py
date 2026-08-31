"""Base watcher class for event-driven task execution."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Notify the owner about unhandled events AT MOST once per window (anti-spam)
_NO_INSTRUCTION_NOTIFY_WINDOW_SECONDS = 6 * 3600
_last_no_instruction_notify: dict[str, float] = {}


class BaseWatcher(abc.ABC):
    """Abstract base class for event watchers.

    Watchers monitor external event sources and trigger agent tasks
    when events are detected. They are token-efficient: only call
    the LLM when there's actual work to do.

    Autonomy policy — MEMORY-GATED ACTIONS:
    Watchers that auto-generate goals MUST NOT act on their own initiative.
    Before triggering a task, they check agent memory for a standing
    instruction from the owner relevant to their domain. Found -> the goal
    embeds that instruction. Not found -> NO action; the owner gets a
    rate-limited notification instead.
    Watchers whose goal text is explicitly configured by the owner (cron,
    webhook) are pre-authorized and skip this gate.
    """

    # Subclasses with auto-generated goals override this with keywords that
    # mark a stored instruction as relevant to their domain, e.g.
    # ("pr", "merge", "review"). Empty tuple = gate can never pass.
    INSTRUCTION_KEYWORDS: tuple[str, ...] = ()

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        self.watcher_id = watcher_id
        self.config = config
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_check: datetime | None = None
        self._events_processed: int = 0
        self._state: dict[str, Any] = {}

    def standing_instruction(self) -> str | None:
        """Most recent user instruction relevant to this watcher, if any.

        This is the permission layer: the watcher acts ONLY on events when
        the owner previously stated a direction (stored in memory).
        """
        if not self.INSTRUCTION_KEYWORDS:
            return None
        from agent.core.memory import memory_store

        entries = memory_store.get_by_category("instruction")
        for entry in reversed(entries):  # most recent wins
            content = entry.content.lower()
            if any(kw in content for kw in self.INSTRUCTION_KEYWORDS):
                return entry.content
        return None

    def gated_goal(self, instruction: str, event_description: str) -> str:
        """Build a goal that applies the owner's standing instruction to an event."""
        return (
            f'Standing instruction from my owner: "{instruction}"\n'
            f"Apply it to this event: {event_description}"
        )

    def _should_notify_no_instruction(self) -> bool:
        """Anti-spam: at most one 'event arrived, no orders' message per window."""
        now = time.monotonic()
        last = _last_no_instruction_notify.get(self.watcher_id)
        if last is not None and (now - last) < _NO_INSTRUCTION_NOTIFY_WINDOW_SECONDS:
            return False
        _last_no_instruction_notify[self.watcher_id] = now
        return True

    async def notify_unhandled_event(
        self, summary: str, event: dict[str, Any] | None = None
    ) -> None:
        """Inform owner an event arrived but no standing orders exist.

        Always creates a visible Task Panel entry (even when Telegram is muted
        by rate-limit), so nothing is silent. Telegram is still rate-limited.
        """
        # 1) Always create visible task for dashboard — never silent
        with contextlib.suppress(Exception):
            await self._register_unhandled_task(summary, event)

        # 2) Telegram notification (rate-limited)
        try:
            from agent.telegram import is_configured, send_message

            if is_configured() and self._should_notify_no_instruction():
                await send_message(
                    f"👀 <b>Event detected</b>\n{summary[:400]}\n\n"
                    f"No standing instruction on file — taking NO action.\n"
                    f"Tell me what to do for these events and I'll handle "
                    f"them automatically.\n"
                    f"(You'll get this notice at most once every 6 hours)"
                )
            else:
                logger.info(
                    "Watcher %s: unhandled event suppressed by rate limit "
                    "or Telegram not configured — task panel entry still created",
                    self.watcher_id,
                )
        except Exception:
            logger.debug("No-instruction notification skipped")

    async def _register_unhandled_task(
        self, summary: str, event: dict[str, Any] | None = None
    ) -> None:
        """Create a visible Task Panel entry for an unhandled watcher event."""
        # Try API live store first (dashboard polling), fallback to doing nothing
        try:
            from api.main import _register_watcher_unhandled as _reg

            await _reg(
                watcher_id=self.watcher_id,
                watcher_type=self.config.get("type", "unknown"),
                summary=summary,
                event=event,
            )
        except Exception:
            # api.main may not be loaded in tests / CLI — keep quiet
            logger.debug("Could not register unhandled task in live store", exc_info=True)
            # Fallback: at least log so it's searchable
            logger.warning("Unhandled event [%s]: %s", self.watcher_id, summary[:300])

    @abc.abstractmethod
    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check the event source for new events.

        Returns:
            List of event dicts. Each should have at least:
            - event_type: str
            - payload: dict
            - external_id: str (for deduplication)
        """
        ...

    @abc.abstractmethod
    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Process a single event and return a goal for the agent.

        Returns:
            Goal string for the agent, or None to skip this event.
        """
        ...

    async def start(self) -> None:
        """Start the watcher loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Watcher %s started", self.watcher_id)

    async def stop(self) -> None:
        """Stop the watcher loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        logger.info("Watcher %s stopped", self.watcher_id)

    async def _loop(self) -> None:
        """Main watcher loop - check, process, sleep, repeat."""
        interval = self.config.get("interval_seconds", 300)
        processed_ids = set(self._state.get("processed_ids", []))

        while self._running:
            try:
                self._last_check = datetime.now(UTC)
                events = await self.check_for_events()

                for event in events:
                    ext_id = event.get("external_id", "")
                    if ext_id in processed_ids:
                        continue

                    goal = await self.process_event(event)
                    if goal:
                        # Trigger agent task
                        await self._trigger_task(goal, event)
                        processed_ids.add(ext_id)
                        self._events_processed += 1

                # Keep only last 1000 processed IDs - deterministic ordered truncation
                if len(processed_ids) > 1000:
                    ordered = self._state.get("processed_ids", [])
                    if ordered:
                        # ordered is insertion-ordered from previous iterations
                        # supplement with any new ids not yet in ordered (sorted for determinism)
                        new_ids = sorted(processed_ids - set(ordered))
                        combined = list(ordered) + new_ids
                        trimmed = combined[-500:]
                        processed_ids = set(trimmed)
                    else:
                        processed_ids = set(sorted(processed_ids)[-500:])

                self._state["processed_ids"] = list(processed_ids)
                self._state["last_check"] = self._last_check.isoformat()
                self._state["events_processed"] = self._events_processed

                # Persist live state so dedup survives a restart (works with
                # both the Firestore and file backends). Lazy import avoids a
                # circular module dependency.
                with contextlib.suppress(Exception):
                    from agent.watchers import manager as _wm

                    _wm._save_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Watcher %s error: %s", self.watcher_id, e)

            await asyncio.sleep(interval)

    async def _trigger_task(self, goal: str, event: dict[str, Any]) -> None:
        """Trigger an agent task from an event."""
        from agent.models import Task, TaskPriority
        from agent.orchestrator import orchestrator

        # Priority mapping: github PR merge/reject must be HIGH/CRITICAL and sequential
        raw_priority = event.get("priority", "")
        if event.get("event_type", "").startswith("github.pr.") or event.get(
            "event_type", ""
        ).startswith("gitlab.mr."):
            # PR/MR events are always HIGH — they need local verification one-by-one
            priority = TaskPriority.CRITICAL if raw_priority == "high" else TaskPriority.HIGH
        elif raw_priority == "high":
            priority = TaskPriority.HIGH
        elif raw_priority == "critical":
            priority = TaskPriority.CRITICAL
        else:
            priority = TaskPriority.MEDIUM

        task = Task(
            goal=goal,
            context={
                "watcher_id": self.watcher_id,
                "event_type": event.get("event_type", "unknown"),
                "external_id": event.get("external_id", ""),
            },
            priority=priority,
        )

        logger.info("Watcher %s triggering task: %s", self.watcher_id, goal[:100])
        await orchestrator.handle_task(task)

    def get_status(self) -> dict[str, Any]:
        return {
            "watcher_id": self.watcher_id,
            "running": self._running,
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "events_processed": self._events_processed,
            "state": self._state,
        }
