"""Webhook watcher - receives external events via an HTTP endpoint.

Doesn't poll: events arrive through a registered FastAPI route and
are queued until the watcher loop drains them.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from typing import Any

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class WebhookWatcher(BaseWatcher):
    """Receive custom webhook events instead of polling an external API.

    Pre-authorized: the owner explicitly configured the event-to-goal
    mapping, which acts as the standing instruction — no memory gate.
    """

    MAX_QUEUE_SIZE = 100
    MAX_EVENTS_PER_CHECK = 50

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.webhook_path = config.get("webhook_path", "/webhook/custom")
        # Maps event type -> goal template, e.g.
        # {"github.push": "Review new push to {repo} by {pusher}"}
        # Templates support {placeholder} substitution from the payload,
        # plus a "default" key as fallback for unmapped event types.
        self.event_mapping: dict[str, str] = config.get("event_mapping", {})
        self._queue: deque[dict[str, Any]] = deque()

    def receive_event(self, event_type: str, payload: dict[str, Any]) -> str:
        """Queue an inbound webhook event. Returns its external_id."""
        external_id = str(payload.get("id") or f"wh_{uuid.uuid4().hex}")

        if len(self._queue) >= self.MAX_QUEUE_SIZE:
            dropped = self._queue.popleft()
            logger.warning(
                "Webhook queue full for %s, dropped oldest event %s",
                self.watcher_id,
                dropped.get("external_id", "unknown"),
            )

        self._queue.append(
            {
                "event_type": event_type,
                "external_id": external_id,
                "payload": payload,
            }
        )
        logger.info("Webhook %s received event '%s'", self.watcher_id, event_type)
        return external_id

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Drain queued webhook events."""
        events: list[dict[str, Any]] = []
        while self._queue and len(events) < self.MAX_EVENTS_PER_CHECK:
            events.append(self._queue.popleft())
        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a webhook event into an agent goal via the mapping."""
        payload = event.get("payload", {})
        event_type = event.get("event_type", "")

        template = self.event_mapping.get(event_type) or self.event_mapping.get("default")
        if not template:
            logger.warning(
                "No goal mapping for webhook event type '%s' in %s", event_type, self.watcher_id
            )
            return None

        try:
            return template.format(**payload)
        except (KeyError, IndexError, ValueError) as e:
            logger.warning("Failed to format goal template for event '%s': %s", event_type, e)
            return template

    def register_routes(self, app: Any) -> None:
        """Register this watcher's endpoint with a FastAPI app or router.

        External services POST their payloads to `webhook_path`; each
        POST is queued as an event. The event type is taken from the
        X-GitHub-Event / X-Webhook-Event header, falling back to a
        top-level `event_type` field in the JSON body.
        """
        from fastapi import Request  # noqa: TC002 - runtime dependency
        from fastapi.responses import JSONResponse

        async def handle_webhook(request: Request) -> JSONResponse:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {"data": payload}

            event_type = (
                request.headers.get("X-GitHub-Event")
                or request.headers.get("X-Webhook-Event")
                or str(payload.get("event_type") or "custom")
            )
            external_id = self.receive_event(event_type, payload)
            return JSONResponse({"status": "queued", "external_id": external_id})

        app.post(self.webhook_path)(handle_webhook)
        logger.info("Webhook endpoint registered: POST %s", self.webhook_path)
