"""GitLab watcher - monitors repos for new merge requests and issues."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class GitLabWatcher(BaseWatcher):
    """Watch a GitLab project for new MRs and issues."""

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.project_id = config.get("project_id", "")
        self.base_url = config.get("base_url", "https://gitlab.com")
        self.token = config.get("token", "")
        self.watch_mrs = config.get("watch_mrs", True)
        self.watch_issues = config.get("watch_issues", True)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        return headers

    async def check_for_events(self) -> list[dict[str, Any]]:
        events = []
        async with httpx.AsyncClient(timeout=30) as client:
            if self.watch_mrs:
                try:
                    resp = await client.get(
                        f"{self.base_url}/api/v4/projects/{self.project_id}/merge_requests",
                        params={"state": "opened", "order_by": "created_at", "sort": "desc", "per_page": 5},
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        for mr in resp.json():
                            events.append({
                                "event_type": "gitlab.mr.opened",
                                "external_id": f"mr_{mr['iid']}",
                                "payload": {
                                    "number": mr["iid"],
                                    "title": mr["title"],
                                    "body": (mr.get("description") or "")[:500],
                                    "author": mr["author"]["username"],
                                    "url": mr["web_url"],
                                    "action": "opened",
                                },
                            })
                except Exception as e:
                    logger.warning("GitLab MR check failed: %s", e)

            if self.watch_issues:
                try:
                    resp = await client.get(
                        f"{self.base_url}/api/v4/projects/{self.project_id}/issues",
                        params={"state": "opened", "order_by": "created_at", "sort": "desc", "per_page": 5},
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        for issue in resp.json():
                            events.append({
                                "event_type": "gitlab.issue.opened",
                                "external_id": f"issue_{issue['iid']}",
                                "payload": {
                                    "number": issue["iid"],
                                    "title": issue["title"],
                                    "body": (issue.get("description") or "")[:500],
                                    "author": issue["author"]["username"],
                                    "url": issue["web_url"],
                                    "action": "opened",
                                },
                            })
                except Exception as e:
                    logger.warning("GitLab issue check failed: %s", e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "gitlab.mr.opened":
            return (
                f"Review GitLab MR !{payload['number']}: "
                f"'{payload['title']}'. Analyze changes, check for issues, "
                f"and provide a summary with recommendations."
            )
        elif event_type == "gitlab.issue.opened":
            return (
                f"Analyze GitLab issue #{payload['number']}: "
                f"'{payload['title']}'. Understand the problem, research solutions, "
                f"and provide a helpful response."
            )
        return None
