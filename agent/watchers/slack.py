"""Slack watcher - monitors channels for new messages.

Token-efficient: only calls Gemini when new events are detected.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)

SLACK_API_URL = "https://slack.com/api"


class SlackWatcher(BaseWatcher):
    """Watch Slack channels for new messages."""

    INSTRUCTION_KEYWORDS = ("slack", "channel", "mention", "reply", "respond", "message")

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.token = config.get("token", "")  # Bot token (xoxb-...)
        self.channels = config.get("channels", [])  # List of channel IDs
        self.watch_mentions = config.get("watch_mentions", True)
        self._last_ts: dict[str, str] = {}  # channel -> last seen message ts
        self._channel_names: dict[str, str] = {}  # channel -> name cache

    def _get_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _get_channel_name(self, client: httpx.AsyncClient, channel_id: str) -> str:
        """Resolve and cache a human-readable channel name."""
        if channel_id not in self._channel_names:
            try:
                resp = await client.get(
                    f"{SLACK_API_URL}/conversations.info",
                    params={"channel": channel_id},
                    headers=self._get_headers(),
                )
                data = resp.json() if resp.status_code == 200 else {}
                self._channel_names[channel_id] = (
                    data.get("channel", {}).get("name") or channel_id
                )
            except Exception as e:
                logger.warning("Slack channel name lookup failed: %s", e)
                self._channel_names[channel_id] = channel_id
        return self._channel_names[channel_id]

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check Slack API for new messages in watched channels."""
        events = []

        async with httpx.AsyncClient(timeout=30) as client:
            for channel_id in self.channels:
                try:
                    resp = await client.get(
                        f"{SLACK_API_URL}/conversations.history",
                        params={"channel": channel_id, "limit": 5},
                        headers=self._get_headers(),
                    )
                    data = resp.json()
                    if resp.status_code != 200 or not data.get("ok"):
                        logger.warning(
                            "Slack history check failed for %s: %s",
                            channel_id,
                            data.get("error", resp.status_code),
                        )
                        continue

                    last_ts = self._last_ts.get(channel_id, "")
                    new_messages = [
                        msg
                        for msg in reversed(data.get("messages", []))  # Oldest first
                        if not msg.get("bot_id")  # Skip bots to avoid loops
                        and msg.get("ts", "") > last_ts
                    ]
                    if new_messages:
                        self._last_ts[channel_id] = new_messages[-1]["ts"]

                    channel_name = await self._get_channel_name(client, channel_id)
                    for msg in new_messages:
                        text = (msg.get("text") or "")[:500]
                        is_mention = "<@" in text
                        event: dict[str, Any] = {
                            "event_type": "slack.message.new",
                            "external_id": f"{channel_id}_{msg['ts']}",
                            "payload": {
                                "channel": channel_id,
                                "channel_name": channel_name,
                                "user": msg.get("user", msg.get("username", "unknown")),
                                "text": text,
                                "ts": msg.get("ts"),
                                "is_mention": is_mention,
                            },
                        }
                        if self.watch_mentions and is_mention:
                            event["priority"] = "high"
                        events.append(event)
                except Exception as e:
                    logger.warning("Slack check failed for %s: %s", channel_id, e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a Slack message event into an agent goal."""
        payload = event["payload"]

        if event["event_type"] == "slack.message.new":
            instruction = self.standing_instruction()
            if instruction is None:
                await self.notify_unhandled_event(
                    f"Slack message in #{payload['channel_name']} "
                    f"from {payload['user']}: '{payload['text']}'"
                )
                return None

            mention_note = (
                " This is a direct mention - prioritize a response."
                if payload.get("is_mention")
                else ""
            )
            return self.gated_goal(
                instruction,
                f"the new message in #{payload['channel_name']} "
                f"from {payload['user']}: '{payload['text']}'. "
                f"Analyze and respond if relevant.{mention_note}",
            )

        return None
