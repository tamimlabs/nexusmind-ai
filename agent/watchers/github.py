"""GitHub watcher - monitors repos for new PRs and issues.

Token-efficient: only calls Gemini when new events are detected.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class GitHubWatcher(BaseWatcher):
    """Watch a GitHub repo for new PRs and issues."""

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.repo = config.get("repo", "")  # "owner/repo"
        self.watch_prs = config.get("watch_prs", True)
        self.watch_issues = config.get("watch_issues", True)
        self.token = config.get("token", "")  # GitHub token (optional)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check GitHub API for new PRs and issues."""
        events = []

        async with httpx.AsyncClient(timeout=30) as client:
            # Check PRs
            if self.watch_prs:
                try:
                    resp = await client.get(
                        f"https://api.github.com/repos/{self.repo}/pulls",
                        params={"state": "open", "sort": "created", "direction": "desc", "per_page": 5},
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        for pr in resp.json():
                            events.append({
                                "event_type": "github.pr.opened",
                                "external_id": f"pr_{pr['number']}",
                                "payload": {
                                    "number": pr["number"],
                                    "title": pr["title"],
                                    "body": (pr.get("body") or "")[:500],
                                    "author": pr["user"]["login"],
                                    "url": pr["html_url"],
                                    "action": "opened",
                                },
                            })
                except Exception as e:
                    logger.warning("GitHub PR check failed: %s", e)

            # Check Issues
            if self.watch_issues:
                try:
                    resp = await client.get(
                        f"https://api.github.com/repos/{self.repo}/issues",
                        params={"state": "open", "sort": "created", "direction": "desc", "per_page": 5},
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        for issue in resp.json():
                            # Skip PRs (they appear in issues endpoint too)
                            if "pull_request" in issue:
                                continue
                            events.append({
                                "event_type": "github.issue.opened",
                                "external_id": f"issue_{issue['number']}",
                                "payload": {
                                    "number": issue["number"],
                                    "title": issue["title"],
                                    "body": (issue.get("body") or "")[:500],
                                    "author": issue["user"]["login"],
                                    "url": issue["html_url"],
                                    "action": "opened",
                                },
                            })
                except Exception as e:
                    logger.warning("GitHub issue check failed: %s", e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a GitHub event into an agent goal."""
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "github.pr.opened":
            return (
                f"Review GitHub PR #{payload['number']} in {self.repo}: "
                f"'{payload['title']}'. Analyze the changes, check for issues, "
                f"and provide a summary with recommendations."
            )
        elif event_type == "github.issue.opened":
            return (
                f"Analyze GitHub issue #{payload['number']} in {self.repo}: "
                f"'{payload['title']}'. Understand the problem, research solutions, "
                f"and provide a helpful response."
            )

        return None
