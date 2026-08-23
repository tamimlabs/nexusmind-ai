"""Jira watcher - monitors projects for new and updated issues.

Token-efficient: only calls Gemini when new events are detected.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class JiraWatcher(BaseWatcher):
    """Watch a Jira project for new and updated issues."""

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.domain = config.get("domain", "")  # e.g. "company.atlassian.net"
        self.email = config.get("email", "")
        self.token = config.get("token", "")  # Jira API token
        self.project_key = config.get("project_key", "")
        self.watch_new = config.get("watch_new", True)
        self.watch_updates = config.get("watch_updates", True)
        # Track issue update timestamps: issue key -> last seen "updated" value
        self._updated_seen: dict[str, str] = {}

    def _get_headers(self) -> dict[str, str]:
        credentials = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        return {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        }

    def _issue_url(self, issue_key: str) -> str:
        return f"https://{self.domain}/browse/{issue_key}"

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check Jira REST API v2 for new and updated issues."""
        events = []

        async with httpx.AsyncClient(timeout=30) as client:
            # Check new issues
            if self.watch_new:
                try:
                    resp = await client.get(
                        f"https://{self.domain}/rest/api/2/search",
                        params={
                            "jql": f"project = {self.project_key} ORDER BY created DESC",
                            "maxResults": 5,
                            "fields": "summary,status,description,reporter,created,updated",
                        },
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        for issue in resp.json().get("issues", []):
                            fields = issue.get("fields", {})
                            key = issue["key"]
                            events.append({
                                "event_type": "jira.issue.new",
                                "external_id": f"jira_issue_{key}",
                                "payload": {
                                    "key": key,
                                    "title": fields.get("summary", ""),
                                    "description": (fields.get("description") or "")[:500],
                                    "status": fields.get("status", {}).get("name", ""),
                                    "reporter": fields.get("reporter", {}).get("displayName", ""),
                                    "url": self._issue_url(key),
                                    "action": "new",
                                },
                            })
                    else:
                        logger.warning(
                            "Jira new-issue search failed (%s): %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                except Exception as e:
                    logger.warning("Jira new-issue check failed: %s", e)

            # Check updated issues (status changes, comments)
            if self.watch_updates:
                try:
                    resp = await client.get(
                        f"https://{self.domain}/rest/api/2/search",
                        params={
                            "jql": f"project = {self.project_key} ORDER BY updated DESC",
                            "maxResults": 5,
                            "fields": "summary,status,comment,updated",
                        },
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        for issue in resp.json().get("issues", []):
                            fields = issue.get("fields", {})
                            key = issue["key"]
                            updated = fields.get("updated", "")
                            last_seen = self._updated_seen.get(key)
                            if last_seen is not None and updated <= last_seen:
                                continue  # no change since last check
                            self._updated_seen[key] = updated
                            events.append({
                                "event_type": "jira.issue.updated",
                                "external_id": f"jira_issue_{key}_{updated}",
                                "payload": {
                                    "key": key,
                                    "title": fields.get("summary", ""),
                                    "status": fields.get("status", {}).get("name", ""),
                                    "comment_count": fields.get("comment", {}).get("total", 0),
                                    "updated": updated,
                                    "url": self._issue_url(key),
                                    "action": "updated",
                                },
                            })
                    else:
                        logger.warning(
                            "Jira update search failed (%s): %s",
                            resp.status_code,
                            resp.text[:200],
                        )
                except Exception as e:
                    logger.warning("Jira update check failed: %s", e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a Jira event into an agent goal."""
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "jira.issue.new":
            return (
                f"New Jira issue {payload['key']}: '{payload['title']}'. "
                f"Analyze the issue and provide a solution."
            )
        elif event_type == "jira.issue.updated":
            return (
                f"Jira issue {payload['key']} was updated "
                f"(status: {payload['status']}, comments: {payload['comment_count']}): "
                f"'{payload['title']}'. Review the update and provide a helpful response."
            )

        return None
