"""Cron watcher - triggers a configured goal on a fixed schedule.

No external API: simply emits the configured goal each interval.
Supports simple cron expressions for the minute field:
  "* / N * * * *" -> every N minutes
  "* * * * *"     -> every minute
  "M * * * *"     -> hourly at minute M
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class CronWatcher(BaseWatcher):
    """Trigger a recurring goal on a simple interval-based schedule.

    Pre-authorized: the goal text was explicitly configured by the owner,
    which itself acts as the standing instruction — no memory gate needed.
    """

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.cron_expression = config.get("cron_expression", "*/5 * * * *")
        self.goal_text = config.get("goal", "")
        # Derive the polling interval from the cron expression so the
        # base watcher loop fires at the requested cadence.
        self.config.setdefault(
            "interval_seconds", self._parse_interval_minutes(self.cron_expression) * 60
        )

    @staticmethod
    def _parse_interval_minutes(expression: str) -> int:
        """Parse the minute field of a simple cron expression into minutes."""
        parts = expression.split()
        minute_field = parts[0] if parts else "*/5"
        try:
            if minute_field.startswith("*/"):
                return max(1, int(minute_field[2:]))
            if minute_field == "*":
                return 1
            return 60
        except ValueError:
            logger.warning("Unrecognized cron minute field '%s', defaulting to 5 min", minute_field)
            return 5

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Emit a trigger event on every check (one per interval)."""
        now = datetime.now(UTC)
        return [
            {
                "event_type": "cron.trigger",
                # Unique per-interval ID so the base dedup doesn't suppress repeats;
                # microsecond + short uuid avoids collision when interval <60s
                "external_id": f"cron_{self.watcher_id}_{now.strftime('%Y%m%dT%H%M%S_%f')}_{uuid.uuid4().hex[:4]}",
                "payload": {
                    "expression": self.cron_expression,
                    "triggered_at": now.isoformat(),
                },
            }
        ]

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Return the configured goal string."""
        if not self.goal_text:
            logger.warning("Cron watcher %s has no goal configured", self.watcher_id)
            return None
        return self.goal_text
