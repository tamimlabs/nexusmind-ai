"""Base watcher class for event-driven task execution."""
from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class BaseWatcher(abc.ABC):
    """Abstract base class for event watchers.

    Watchers monitor external event sources and trigger agent tasks
    when events are detected. They are token-efficient: only call
    the LLM when there's actual work to do.
    """

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        self.watcher_id = watcher_id
        self.config = config
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_check: datetime | None = None
        self._events_processed: int = 0
        self._state: dict[str, Any] = {}

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
                self._last_check = datetime.now(timezone.utc)
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

                # Keep only last 1000 processed IDs
                if len(processed_ids) > 1000:
                    processed_ids = set(list(processed_ids)[-500:])

                self._state["processed_ids"] = list(processed_ids)
                self._state["last_check"] = self._last_check.isoformat()
                self._state["events_processed"] = self._events_processed

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Watcher %s error: %s", self.watcher_id, e)

            await asyncio.sleep(interval)

    async def _trigger_task(self, goal: str, event: dict[str, Any]) -> None:
        """Trigger an agent task from an event."""
        from agent.models import Task, TaskPriority
        from agent.orchestrator import orchestrator

        task = Task(
            goal=goal,
            context={
                "watcher_id": self.watcher_id,
                "event_type": event.get("event_type", "unknown"),
                "external_id": event.get("external_id", ""),
            },
            priority=TaskPriority.HIGH if event.get("priority") == "high" else TaskPriority.MEDIUM,
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
