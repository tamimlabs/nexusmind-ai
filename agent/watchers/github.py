"""GitHub watcher - monitors repos for new PRs and issues.

Token-efficient: only calls Gemini when new events are detected.
Now supports auto-merge and auto-reject based on review.
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
        self.auto_merge = config.get("auto_merge", True)  # Auto-merge safe PRs
        self.auto_comment = config.get("auto_comment", True)  # Post review comments

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
                                    "diff_url": pr.get("diff_url", ""),
                                    "head_sha": pr.get("head", {}).get("sha", ""),
                                    "base_branch": pr.get("base", {}).get("ref", "main"),
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
        """Convert a GitHub event into an agent goal with actions."""
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "github.pr.opened":
            goal = (
                f"Use run_command with curl to fetch PR details from {payload['diff_url']}.\n"
                f"Then use execute_code to analyze the code changes for quality and security.\n"
                f"Finally use run_command with curl to post a review comment on the PR.\n"
                f"Decide whether to merge or request changes based on your memory and instructions.\n"
                f"PR #{payload['number']}: '{payload['title']}' by @{payload['author']}"
            )
            return goal

        elif event_type == "github.issue.opened":
            return (
                f"Use run_command with curl to fetch issue details from the API.\n"
                f"Analyze the issue and post a helpful comment.\n"
                f"Issue #{payload['number']}: '{payload['title']}'"
            )

        return None
