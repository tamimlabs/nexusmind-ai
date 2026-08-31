"""Discord watcher - monitors guild channels for new messages.

Token-efficient: only calls Gemini when new events are detected.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)

DISCORD_API_URL = "https://discord.com/api/v10"


class DiscordWatcher(BaseWatcher):
    """Watch Discord channels for new messages."""

    INSTRUCTION_KEYWORDS = ("discord", "channel", "mention", "reply", "respond", "message")

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.token = config.get("token", "")  # Bot token
        self.guild_id = config.get("guild_id", "")
        self.channel_ids = config.get("channel_ids", [])  # List of channel IDs
        self._last_message_id: dict[str, str] = dict(
            self._state.get("discord_last_id", {})
        )  # channel -> last seen message snowflake
        self._channel_names: dict[str, str] = {}  # channel -> name cache

    def _get_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.token}"}

    async def _load_channel_names(self, client: httpx.AsyncClient) -> None:
        """Fetch and cache channel names from the guild."""
        if self._channel_names or not self.guild_id:
            return
        try:
            resp = await client.get(
                f"{DISCORD_API_URL}/guilds/{self.guild_id}/channels",
                headers=self._get_headers(),
            )
            if resp.status_code == 200:
                self._channel_names = {c["id"]: c["name"] for c in resp.json()}
        except Exception as e:
            logger.warning("Discord channel list fetch failed: %s", e)

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check Discord API for new messages in watched channels."""
        # Early validation — don't poll unauthenticated (no bypass, no Illegal header exceptions)
        if not self.token:
            logger.debug(
                "Discord watcher %s skipped: not configured (missing token)", self.watcher_id
            )
            return []
        if not self.channel_ids:
            logger.debug("Discord watcher %s skipped: no channels configured", self.watcher_id)
            return []
        # Sync cursor from persisted state (handles restore after __init__)
        if self._state.get("discord_last_id"):
            self._last_message_id = dict(self._state["discord_last_id"])
        events = []

        async with httpx.AsyncClient(timeout=30) as client:
            await self._load_channel_names(client)

            for channel_id in self.channel_ids:
                try:
                    resp = await client.get(
                        f"{DISCORD_API_URL}/channels/{channel_id}/messages",
                        params={"limit": 5},
                        headers=self._get_headers(),
                    )
                    if resp.status_code != 200:
                        logger.warning(
                            "Discord message check failed for %s: HTTP %s",
                            channel_id,
                            resp.status_code,
                        )
                        continue

                    try:
                        last_id = int(self._last_message_id.get(channel_id, "0"))
                    except (ValueError, TypeError):
                        last_id = 0

                    def _safe_int(val: Any) -> int:
                        try:
                            return int(str(val))
                        except (ValueError, TypeError):
                            return 0

                    new_messages = [
                        msg
                        for msg in reversed(resp.json())
                        if not msg.get("author", {}).get("bot")  # Skip bots to avoid loops
                        and _safe_int(msg.get("id", "0"))
                        > last_id  # Snowflake IDs sort chronologically
                    ]
                    if new_messages:
                        last_msg_id = new_messages[-1].get("id")
                        if last_msg_id is not None:
                            self._last_message_id[channel_id] = str(last_msg_id)
                            self._state["discord_last_id"] = dict(self._last_message_id)

                    channel_name = self._channel_names.get(channel_id, channel_id)
                    for msg in new_messages:
                        msg_id = str(msg.get("id", ""))
                        if not msg_id:
                            continue
                        events.append(
                            {
                                "event_type": "discord.message.new",
                                "external_id": f"{channel_id}_{msg_id}",
                                "payload": {
                                    "channel_id": channel_id,
                                    "channel_name": channel_name,
                                    "message_id": msg_id,
                                    "author": msg.get("author", {}).get("username", "unknown"),
                                    "content": (msg.get("content") or "")[:500],
                                    "url": f"https://discord.com/channels/{self.guild_id}/{channel_id}/{msg_id}",
                                },
                            }
                        )
                except Exception as e:
                    logger.warning("Discord check failed for %s: %s", channel_id, e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a Discord message event into an agent goal."""
        payload = event["payload"]

        if event["event_type"] == "discord.message.new":
            instruction = self.standing_instruction()
            if instruction is None:
                await self.notify_unhandled_event(
                    f"Discord message in '{payload['channel_name']}' "
                    f"from {payload['author']}: '{payload['content']}'",
                    event,
                )
                return None

            return self.gated_goal(
                instruction,
                f"the new message in channel '{payload['channel_name']}' from "
                f"{payload['author']}: '{payload['content']}'. "
                f"Analyze and respond if relevant.",
            )

        return None
