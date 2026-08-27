"""GitHub watcher - monitors repos for new PRs and issues.

Token-efficient: only calls Gemini when new events are detected.

Safety policy — MEMORY-GATED ACTIONS (inherited from BaseWatcher):
The watcher NEVER acts on its own initiative. When a PR arrives it first
checks agent memory for a standing instruction from the owner (e.g. "when
a pr arrives, test and merge or decline with comment"). Found -> the agent
applies that instruction. Not found -> NO action, owner is notified.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


def _resolve_github_token() -> str:
    """Resolve a GitHub token from global config when the watcher has none.

    Precedence: Vault/Secret environment variable injected by the platform
    (settings.github_token already merges .env + os.environ), then a direct
    .env parse as a last resort. This lets one Cloud Run Secret (GITHUB_TOKEN)
    power watchers created without an embedded token.
    """
    try:
        from agent.config import settings

        if getattr(settings, "github_token", ""):
            return settings.github_token
    except Exception:
        pass

    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GITHUB_TOKEN="):
                return line.partition("=")[2].strip().strip("'\"")
    return ""


def _resolve_default_repo() -> str:
    """Fallback repo from global config (GITHUB_DEFAULT_REPO), if any.

    Keeps the watcher generic: no repository is hardcoded. Per-watcher
    ``config["repo"]`` always wins; the configured default only fills in
    when a watcher is created without one.
    """
    try:
        from agent.config import settings

        return getattr(settings, "github_default_repo", "") or ""
    except Exception:
        return ""


class GitHubWatcher(BaseWatcher):
    """Watch a GitHub repo for new PRs and issues."""

    # Keywords that mark a stored instruction as relevant to GitHub events
    # NOTE: "marge" and "deslind" are intentional fuzzy/typo variants kept for
    # backward compatibility; "merge" is the correct keyword.
    INSTRUCTION_KEYWORDS = (
        "pr", "pull request", "pullrequest", "github", "repo", "repository",
        "merge", "marge", "reject", "decline", "deslind", "review", "test", "approve",
    )

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        self.repo = config.get("repo") or _resolve_default_repo()  # "owner/repo"
        self.watch_prs = config.get("watch_prs", True)
        self.watch_issues = config.get("watch_issues", True)
        self.token = config.get("token") or _resolve_github_token()
        # Kept for backward compatibility; memory instructions now gate actions.
        self.auto_merge = config.get("auto_merge", False)
        self.auto_comment = config.get("auto_comment", False)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github.v3+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
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
                        params={"state": "open", "sort": "created", "direction": "desc", "per_page": 100},
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        for pr in resp.json():
                            events.append({
                                "event_type": "github.pr.opened",
                                "external_id": f"pr_{pr['number']}",
                                "priority": "high",
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
                    else:
                        logger.warning("GitHub PR check failed: %s %s", resp.status_code, resp.text[:200])
                except Exception as e:
                    logger.warning("GitHub PR check failed: %s", e)

            # Check Issues
            if self.watch_issues:
                try:
                    resp = await client.get(
                        f"https://api.github.com/repos/{self.repo}/issues",
                        params={"state": "open", "sort": "created", "direction": "desc", "per_page": 100},
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
                    else:
                        logger.warning("GitHub issue check failed: %s %s", resp.status_code, resp.text[:200])
                except Exception as e:
                    logger.warning("GitHub issue check failed: %s", e)

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert a GitHub event into an agent goal — ONLY with user direction.

        Memory-gated policy (inherited from BaseWatcher):
        - No stored instruction about PRs → take NO action (return None).
        - Instruction exists → build a goal that applies it to this PR.
        """
        payload = event["payload"]
        event_type = event["event_type"]

        if event_type == "github.pr.opened":
            number = payload["number"]
            title = payload["title"]
            author = payload["author"]

            instruction = self.standing_instruction()
            if instruction is None:
                logger.info(
                    "Watcher %s: PR #%d arrived but no standing instruction in memory — skipping",
                    self.watcher_id, number,
                )
                await self.notify_unhandled_event(f"New PR #{number} in {self.repo}: '{title}'", event)
                return None

            return self.gated_goal(
                instruction,
                f"pull request #{number} in {self.repo}. "
                f"PR #{number}: '{title}' by @{author}",
            )

        elif event_type == "github.issue.opened":
            number = payload["number"]
            title = payload["title"]

            instruction = self.standing_instruction()
            if instruction is None:
                await self.notify_unhandled_event(
                    f"New issue #{number} in {self.repo}: '{title}'", event
                )
                return None

            return self.gated_goal(
                instruction,
                f"issue #{number} in {self.repo}. Issue #{number}: '{title}'",
            )

        return None
