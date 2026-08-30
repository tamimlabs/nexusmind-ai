"""Cloud Pub/Sub event-driven task routing.

Publishes task events and subscribes to external triggers (GitHub webhooks,
Stripe events, etc.) to route them to the agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent.config import settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_publisher = None
_subscriber = None
_background_tasks: set[asyncio.Future[Any]] = set()


def _get_publisher():
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def _get_subscriber():
    global _subscriber
    if _subscriber is None:
        from google.cloud import pubsub_v1
        _subscriber = pubsub_v1.SubscriberClient()
    return _subscriber


# ── Publishing ────────────────────────────────────────────────────

def publish_task_event(
    task_id: str,
    goal: str,
    status: str,
    priority: str = "medium",
    context: dict[str, Any] | None = None,
) -> str:
    """Publish a task event to Pub/Sub.

    Returns:
        The published message ID.

    """
    publisher = _get_publisher()
    topic_path = publisher.topic_path(settings.google_cloud_project, settings.pubsub_topic_tasks)

    data = {
        "task_id": task_id,
        "goal": goal,
        "status": status,
        "priority": priority,
        "context": context or {},
        "timestamp": datetime.now(UTC).isoformat(),
    }

    future = publisher.publish(
        topic_path,
        data=json.dumps(data).encode("utf-8"),
        task_id=task_id,
        status=status,
    )
    message_id = future.result()
    logger.info("Published task event: %s (message: %s)", task_id, message_id)
    return message_id


def publish_event(event_type: str, payload: dict[str, Any]) -> str:
    """Publish a generic event (webhook trigger, etc.)."""
    publisher = _get_publisher()
    topic_path = publisher.topic_path(settings.google_cloud_project, settings.pubsub_topic_events)

    data = {
        "event_type": event_type,
        "payload": payload,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    future = publisher.publish(
        topic_path,
        data=json.dumps(data).encode("utf-8"),
        event_type=event_type,
    )
    return future.result()


# ── Subscribing ───────────────────────────────────────────────────

def subscribe_to_tasks(callback: Callable[[dict[str, Any]], Awaitable[None]]) -> Any:
    """Subscribe to task events and process them.

    Args:
        callback: Async function to call with each parsed event.

    Returns:
        Streaming pull future (blocking).

    """
    subscriber = _get_subscriber()
    subscription_path = subscriber.subscription_path(
        settings.google_cloud_project,
        settings.pubsub_subscription_tasks,
    )

    def _message_handler(message) -> None:
        try:
            data = json.loads(message.data.decode("utf-8"))
            logger.info("Received task event: %s", data.get("task_id", "unknown"))
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                task = loop.create_task(callback(data))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            else:
                # No running loop (e.g. called from sync context) — run directly
                try:
                    nloop = asyncio.get_event_loop()
                    nloop.run_until_complete(callback(data))
                except RuntimeError:
                    asyncio.run(callback(data))
            message.ack()
        except Exception:
            logger.exception("Failed to process message")
            message.nack()

    future = subscriber.subscribe(subscription_path, callback=_message_handler)
    logger.info("Subscribed to %s", subscription_path)
    return future


# ── Event Types (for webhook triggers) ───────────────────────────

EVENT_GITHUB_COMMIT = "github.commit"
EVENT_GITHUB_PR = "github.pull_request"
EVENT_STRIPE_INVOICE = "stripe.invoice"
EVENT_TICKET_CREATED = "ticket.created"
EVENT_EMAIL_RECEIVED = "email.received"
EVENT_CALENDAR_EVENT = "calendar.event"
EVENT_CUSTOM = "custom"
