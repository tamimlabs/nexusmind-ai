"""Reddit watcher - monitors subreddits for new posts.

Token-efficient: only calls Gemini when new events are detected.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class RedditWatcher(BaseWatcher):
    """Watch subreddits for new posts via the Reddit JSON API."""

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.subreddits: list[str] = config.get("subreddits", [])
        self.sort = config.get("sort", "new")  # new/hot/top
        self.limit = min(int(config.get("limit", 5)), 100)

    def _get_headers(self) -> dict[str, str]:
        return {
            "User-Agent": "NexusMind-AI/1.0 (event watcher; +https://github.com/nexusmind-ai)",
            "Accept": "application/json",
        }

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check the Reddit JSON API for new posts in each subreddit."""
        events = []

        async with httpx.AsyncClient(timeout=30) as client:
            for subreddit in self.subreddits:
                try:
                    resp = await client.get(
                        f"https://www.reddit.com/r/{subreddit}/{self.sort}.json",
                        params={
                            "limit": self.limit,
                            **({"t": "day"} if self.sort == "top" else {}),
                        },
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        posts = resp.json()["data"]["children"]
                        for child in posts:
                            post = child["data"]
                            # Skip stickied posts (mod announcements)
                            if post.get("stickied"):
                                continue
                            events.append({
                                "event_type": "reddit.post.new",
                                "external_id": f"reddit_{post['id']}",
                                "payload": {
                                    "id": post["id"],
                                    "subreddit": post.get("subreddit", subreddit),
                                    "title": post.get("title", ""),
                                    "selftext": (post.get("selftext") or "")[:500],
                                    "author": post.get("author", ""),
                                    "num_comments": post.get("num_comments", 0),
                                    "score": post.get("score", 0),
                                    "url": f"https://www.reddit.com{post.get('permalink', '')}",
                                },
                            })
                    else:
                        logger.warning(
                            "Reddit r/%s check failed (%s): %s",
                            subreddit,
                            resp.status_code,
                            resp.text[:200],
                        )
                except Exception as e:
                    logger.warning("Reddit r/%s check failed: %s", subreddit, e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a Reddit event into an agent goal."""
        payload = event["payload"]

        if event["event_type"] == "reddit.post.new":
            return (
                f"New post in r/{payload['subreddit']}: '{payload['title']}'. "
                f"Analyze discussion and summarize."
            )

        return None
