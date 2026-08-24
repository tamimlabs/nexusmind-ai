"""GitHub watcher - monitors repos for new PRs and issues.

Token-efficient: only calls Gemini when new events are detected.

Safety policy — MEMORY-GATED ACTIONS:
The watcher NEVER acts on its own initiative. When a PR arrives it first
checks agent memory for a standing instruction from the owner (e.g. "when
a pr arrives, test and merge or decline with comment"). Found -> the agent
applies that instruction. Not found -> NO action, owner is notified.
Set auto_merge=True to bypass the memory gate (explicit opt-in).
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)

# Notify the owner about unhandled PRs AT MOST once per window (anti-spam)
_NO_INSTRUCTION_NOTIFY_WINDOW_SECONDS = 6 * 3600
_last_no_instruction_notify: dict[str, float] = {}


class GitHubWatcher(BaseWatcher):
    """Watch a GitHub repo for new PRs and issues."""

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.repo = config.get("repo", "")  # "owner/repo"
        self.watch_prs = config.get("watch_prs", True)
        self.watch_issues = config.get("watch_issues", True)
        self.token = config.get("token", "")  # GitHub token (optional)
        # Kept for backward compatibility; memory instructions now gate actions.
        self.auto_merge = config.get("auto_merge", False)
        self.auto_comment = config.get("auto_comment", False)

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

    # Keywords that mark a stored instruction as relevant to PR events
    _PR_INSTRUCTION_KEYWORDS = (
        "pr", "pull request", "pullrequest", "github", "repo", "repository",
        "merge", "marge", "reject", "decline", "deslind", "review", "test",
    )

    def _standing_instruction_for_prs(self) -> str | None:
        """Return the most recent user instruction about PRs, if one exists.

        This is the permission layer: the watcher acts ONLY on PRs when the
        owner has previously stated a direction (stored in memory).
        """
        from agent.core.memory import memory_store

        entries = memory_store.get_by_category("instruction")
        for entry in reversed(entries):  # most recent wins
            content = entry.content.lower()
            if any(kw in content for kw in self._PR_INSTRUCTION_KEYWORDS):
                return entry.content
        return None

    def _should_notify_no_instruction(self) -> bool:
        """Anti-spam: allow at most one 'PR arrived, no orders' message per window."""
        key = f"{self.watcher_id}:{self.repo}"
        now = time.monotonic()
        last = _last_no_instruction_notify.get(key)
        if last is not None and (now - last) < _NO_INSTRUCTION_NOTIFY_WINDOW_SECONDS:
            return False
        _last_no_instruction_notify[key] = now
        return True

    async def _notify_no_instruction(self, number: int, title: str) -> None:
        """Quietly inform the owner a PR arrived but no standing orders exist.

        Rate-limited to one message per window per watcher — never spam.
        """
        try:
            from agent.telegram import is_configured, send_message

            if is_configured() and self._should_notify_no_instruction():
                await send_message(
                    f"👀 <b>New PR detected</b>\n"
                    f"<b>PR #{number}:</b> {title[:150]}\n\n"
                    f"No standing instruction on file — taking NO action.\n"
                    f"Tell me what to do (e.g. 'when a pr arrives, review and "
                    f"merge or decline with comment') and I'll handle future PRs.\n"
                    f"(You'll get this notice at most once every 6 hours)"
                )
            else:
                logger.info(
                    "Watcher %s: unhandled PR #%d (notice suppressed by rate limit "
                    "or Telegram not configured)",
                    self.watcher_id, number,
                )
        except Exception:
            logger.debug("No-instruction notification skipped")

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a GitHub event into an agent goal — ONLY with user direction.

        Memory-gated policy:
        - No stored instruction about PRs → take NO action (return None).
        - Instruction exists → build a goal that applies it to this PR.
        """
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "github.pr.opened":
            number = payload["number"]
            title = payload["title"]
            author = payload["author"]

            instruction = self._standing_instruction_for_prs()
            if instruction is None:
                logger.info(
                    "Watcher %s: PR #%d arrived but no standing instruction in memory — skipping",
                    self.watcher_id, number,
                )
                await self._notify_no_instruction(number, title)
                return None

            goal = (
                f'Standing instruction from my owner: "{instruction}"\n'
                f"Apply it to pull request #{number} in {self.repo}. "
                f"PR #{number}: '{title}' by @{author}"
            )
            return goal

        elif event_type == "github.issue.opened":
            number = payload["number"]
            title = payload["title"]

            instruction = self._standing_instruction_for_prs()
            if instruction is None:
                await self._notify_no_instruction(number, f"Issue: {title}")
                return None

            return (
                f'Standing instruction from my owner: "{instruction}"\n'
                f"Apply it to issue #{number} in {self.repo}. "
                f"Issue #{number}: '{title}'"
            )

        return None
