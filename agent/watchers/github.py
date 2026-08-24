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
            # Build goal with merge/reject instructions
            goal = (
                f"Review GitHub PR #{payload['number']} in {self.repo}: "
                f"'{payload['title']}' by @{payload['author']}. "
                f"URL: {payload['url']}\n\n"
                f"Steps:\n"
                f"1. Fetch the PR diff and read all code changes\n"
                f"2. Analyze code quality, security, and best practices\n"
                f"3. If the code is SAFE and HIGH QUALITY:\n"
                f"   - Post a comment: '✅ Auto-approved by NexusMind AI. Code looks good.'\n"
                f"   - Merge the PR using GitHub API\n"
                f"4. If the code has ISSUES:\n"
                f"   - Post a comment explaining what's wrong\n"
                f"   - Do NOT merge\n"
                f"5. If the code is DANGEROUS (security risks, hardcoded secrets, malicious code):\n"
                f"   - Post a comment: '🚫 Rejected by NexusMind AI. Reason: [explain]'\n"
                f"   - Request changes\n\n"
                f"Use execute_code tool to call GitHub API. Your GitHub token is configured."
            )
            return goal

        elif event_type == "github.issue.opened":
            return (
                f"Analyze GitHub issue #{payload['number']} in {self.repo}: "
                f"'{payload['title']}'. Understand the problem, research solutions, "
                f"and provide a helpful response as a comment."
            )

        return None
