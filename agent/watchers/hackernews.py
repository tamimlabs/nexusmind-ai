"""Hacker News watcher - monitors the HN front page and comments.

Token-efficient: only calls Gemini when new events are detected.
No auth required.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)

HN_API_BASE = "https://hacker-news.firebaseio.com/v0"


class HackerNewsWatcher(BaseWatcher):
    """Watch Hacker News for new front-page stories and comments."""

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.watch_stories = config.get("watch_stories", True)
        self.watch_comments = config.get("watch_comments", False)
        self.top_n = int(config.get("top_n", 10))

    async def _fetch_item(self, client: httpx.AsyncClient, item_id: int) -> dict[str, Any] | None:
        """Fetch a single HN item (story or comment)."""
        resp = await client.get(f"{HN_API_BASE}/item/{item_id}.json")
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
        return None

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check the HN API for new stories and comments."""
        events: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=30) as client:
            # Check front-page stories
            if self.watch_stories:
                try:
                    resp = await client.get(f"{HN_API_BASE}/topstories.json")
                    if resp.status_code == 200:
                        story_ids = resp.json()[: self.top_n]
                        for story_id in story_ids:
                            story = await self._fetch_item(client, story_id)
                            if not story:
                                continue
                            events.append({
                                "event_type": "hn.story.new",
                                "external_id": f"hn_story_{story['id']}",
                                "payload": {
                                    "id": story["id"],
                                    "title": story.get("title") or "",
                                    "url": story.get("url")
                                    or f"https://news.ycombinator.com/item?id={story['id']}",
                                    "score": story.get("score", 0),
                                    "author": story.get("by") or "",
                                    "comment_count": len(story.get("kids") or []),
                                },
                            })
                except Exception as e:
                    logger.warning("HN story check failed: %s", e)

            # Check latest comments
            if self.watch_comments:
                try:
                    resp = await client.get(f"{HN_API_BASE}/newcomments.json")
                    if resp.status_code == 200:
                        comment_ids = resp.json()[: self.top_n]
                        for comment_id in comment_ids:
                            comment = await self._fetch_item(client, comment_id)
                            if not comment:
                                continue
                            parent_title = ""
                            parent_id = comment.get("parent")
                            if parent_id:
                                parent = await self._fetch_item(client, parent_id)
                                parent_title = (parent or {}).get("title") or ""
                            events.append({
                                "event_type": "hn.comment.new",
                                "external_id": f"hn_comment_{comment['id']}",
                                "payload": {
                                    "id": comment["id"],
                                    "author": comment.get("by") or "",
                                    "text": (comment.get("text") or "")[:500],
                                    "parent_id": parent_id or 0,
                                    "story_title": parent_title,
                                    "url": f"https://news.ycombinator.com/item?id={comment['id']}",
                                },
                            })
                except Exception as e:
                    logger.warning("HN comment check failed: %s", e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert an HN event into an agent goal."""
        payload = event["payload"]

        if event["event_type"] == "hn.story.new":
            return (
                f"New HN story: '{payload['title']}' ({payload['url']}). "
                f"Summarize key points and discuss."
            )
        elif event["event_type"] == "hn.comment.new":
            return (
                f"New HN comment by '{payload['author']}' on '{payload['story_title']}': "
                f"'{payload['text']}'. Analyze the discussion and provide insights."
            )

        return None
