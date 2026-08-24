"""RSS/Atom feed watcher - monitors any RSS or Atom feed for new items."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class RSSWatcher(BaseWatcher):
    """Watch an RSS/Atom feed for new items."""

    INSTRUCTION_KEYWORDS = ("rss", "feed", "article", "news", "summar")

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.feed_url = config.get("feed_url", "")
        self.max_items = config.get("max_items", 10)

    async def check_for_events(self) -> list[dict[str, Any]]:
        events = []
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    self.feed_url,
                    headers={"User-Agent": "NexusMind/1.0"},
                )
                resp.raise_for_status()
                content = resp.text

            root = ET.fromstring(content)

            # Detect RSS vs Atom
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = []

            # RSS 2.0
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                guid = item.findtext("guid", link)
                description = item.findtext("description", "")
                items.append({
                    "title": title,
                    "link": link,
                    "guid": guid or link,
                    "description": (description or "")[:500],
                })

            # Atom
            if not items:
                for entry in root.findall(".//atom:entry", ns):
                    title = entry.findtext("atom:title", "", ns)
                    link_el = entry.find("atom:link", ns)
                    link = link_el.get("href", "") if link_el is not None else ""
                    guid = entry.findtext("atom:id", "", ns) or link
                    summary = entry.findtext("atom:summary", "", ns) or entry.findtext("atom:content", "", ns)
                    items.append({
                        "title": title,
                        "link": link,
                        "guid": guid,
                        "description": (summary or "")[:500],
                    })

            for item in items[:self.max_items]:
                events.append({
                    "event_type": "rss.new_item",
                    "external_id": item["guid"],
                    "payload": {
                        "title": item["title"],
                        "link": item["link"],
                        "description": item["description"],
                        "feed_url": self.feed_url,
                    },
                })

        except Exception as e:
            logger.warning("RSS feed check failed for %s: %s", self.feed_url, e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        payload = event["payload"]

        instruction = self.standing_instruction()
        if instruction is None:
            await self.notify_unhandled_event(
                f"New RSS article: '{payload['title']}' ({payload['link']})"
            )
            return None

        return self.gated_goal(
            instruction,
            f"the new article '{payload['title']}' ({payload['link']}). "
            f"Fetch the content, summarize key points, and provide insights.",
        )
