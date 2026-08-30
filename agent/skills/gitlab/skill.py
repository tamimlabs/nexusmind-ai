"""GitLab skill — MR operations via the GitLab REST API (v4).

Simplified mirror of ``agent/skills/github/skill.py``.

Tools:
- gitlab_list_mrs: list merge requests for a project
- gitlab_get_mr: fetch a single merge request
- gitlab_merge_mr: merge a merge request (high-risk)

Auth via PRIVATE-TOKEN header. Credentials from GITLAB_TOKEN /
GITLAB_BASE_URL (settings → env → .env file), default base
https://gitlab.com.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from agent.core.executor import register_tool
from agent.models import ToolResult

logger = logging.getLogger(__name__)


def _config() -> tuple[str, str]:
    """Return (base_url, token) from settings/env/.env.

    Precedence: ``agent.config.settings`` attrs → ``os.environ`` →
    project ``.env`` file → default base ``https://gitlab.com``.
    Accepts both ``GITLAB_BASE_URL`` and legacy ``GITLAB_URL`` setting names.
    """
    base_url = ""
    token = ""

    try:
        from agent.config import settings

        # settings may not declare gitlab fields (extra=ignore) — use getattr
        base_url = (
            getattr(settings, "gitlab_base_url", "")
            or getattr(settings, "gitlab_url", "")
            or getattr(settings, "GITLAB_BASE_URL", "")
            or ""
        )
        token = getattr(settings, "gitlab_token", "") or getattr(settings, "GITLAB_TOKEN", "") or ""
    except Exception:
        pass

    import os

    if not token:
        token = os.environ.get("GITLAB_TOKEN", "")
    if not base_url:
        base_url = os.environ.get("GITLAB_BASE_URL", "") or os.environ.get("GITLAB_URL", "")

    # Fallback: parse project .env directly (covers env_file not yet loaded)
    if not token or not base_url:
        try:
            from agent.config import _PROJECT_ROOT

            env_file = Path(_PROJECT_ROOT) / ".env"
        except Exception:
            env_file = Path(__file__).resolve().parents[3] / ".env"
        try:
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, _, value = stripped.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if not token and key == "GITLAB_TOKEN":
                        token = value
                    elif not base_url and key in ("GITLAB_BASE_URL", "GITLAB_URL"):
                        base_url = value
        except Exception:
            logger.debug("Could not parse project .env for GitLab config", exc_info=True)

    if not base_url:
        base_url = "https://gitlab.com"
    base_url = base_url.rstrip("/")
    return base_url, token


def _headers(token: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token:
        headers["PRIVATE-TOKEN"] = token
    return headers


def _error_result(status: int, data: Any, action: str) -> ToolResult:
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message", data.get("error_description", data.get("error", ""))))
        if not message:
            message = json.dumps(data)[:300]
    elif isinstance(data, str):
        message = data[:300]
    hint = ""
    if status in (401, 403):
        hint = " (Check GITLAB_TOKEN has api scope and access to the project)"
    elif status == 404:
        hint = " (Not found — check project_id and GITLAB_BASE_URL; private projects need a valid GITLAB_TOKEN)"
    return ToolResult(success=False, output="", error=f"GitLab API {action} failed [{status}]: {message}{hint}")


@register_tool("gitlab_list_mrs")
async def gitlab_list_mrs(project_id: str, state: str = "opened", **_: Any) -> ToolResult:
    """List merge requests for a GitLab project.

    Args:
        project_id: Numeric ID or URL-encoded path (e.g. ``123`` or ``group%2Fproject``).
        state: MR state filter (opened, closed, merged, all). Defaults to opened.
    """
    base_url, token = _config()
    if not token:
        logger.warning("GitLab call WITHOUT token — private projects will 401/404. Set GITLAB_TOKEN in .env")
    url = f"{base_url}/api/v4/projects/{project_id}/merge_requests"
    params = {"state": state}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=_headers(token), params=params)
        try:
            data: Any = resp.json()
        except Exception:
            data = resp.text
        if resp.status_code != 200:
            return _error_result(resp.status_code, data, "list_mrs")
        if not isinstance(data, list):
            return ToolResult(success=False, output="", error="Unexpected GitLab API response")
        # Compact summary matching github_list_prs shape
        mrs = [
            {
                "iid": mr.get("iid"),
                "id": mr.get("id"),
                "title": mr.get("title"),
                "author": (mr.get("author") or {}).get("username", "unknown"),
                "state": mr.get("state"),
                "source_branch": mr.get("source_branch"),
                "target_branch": mr.get("target_branch"),
                "created_at": mr.get("created_at", ""),
            }
            for mr in data
        ]
        return ToolResult(success=True, output=json.dumps(mrs), metadata={"count": len(mrs), "project_id": str(project_id)})


@register_tool("gitlab_get_mr")
async def gitlab_get_mr(project_id: str, mr_iid: int, **_: Any) -> ToolResult:
    """Fetch a single merge request by IID."""
    base_url, token = _config()
    url = f"{base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers=_headers(token))
        try:
            data: Any = resp.json()
        except Exception:
            data = resp.text
        if resp.status_code != 200:
            return _error_result(resp.status_code, data, "get_mr")
        assert isinstance(data, dict)
        summary = {
            "iid": data.get("iid"),
            "id": data.get("id"),
            "title": data.get("title"),
            "description": (data.get("description") or "")[:500],
            "state": data.get("state"),
            "author": (data.get("author") or {}).get("username"),
            "source_branch": data.get("source_branch"),
            "target_branch": data.get("target_branch"),
            "merge_status": data.get("merge_status"),
            "has_conflicts": data.get("has_conflicts"),
            "web_url": data.get("web_url"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        }
        return ToolResult(success=True, output=json.dumps(summary, indent=2))


@register_tool("gitlab_merge_mr", high_risk=True)
async def gitlab_merge_mr(project_id: str, mr_iid: int, **_: Any) -> ToolResult:
    """Merge a merge request (PUT /projects/:id/merge_requests/:iid/merge)."""
    base_url, token = _config()
    url = f"{base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/merge"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.put(url, headers=_headers(token))
        try:
            data: Any = resp.json()
        except Exception:
            data = resp.text
        if resp.status_code in (200, 201):
            sha = ""
            if isinstance(data, dict):
                sha = str(data.get("sha", "") or data.get("merge_commit_sha", ""))
            suffix = f" (sha {sha[:7]})" if sha else ""
            return ToolResult(success=True, output=f"Merged MR !{mr_iid} in project {project_id}{suffix}")
        if resp.status_code == 405:
            return ToolResult(success=False, output="", error=f"MR !{mr_iid} is not mergeable (405 Method Not Allowed — check conflicts/pipeline)")
        if resp.status_code == 401:
            return _error_result(resp.status_code, data, "merge_mr")
        return _error_result(resp.status_code, data, "merge_mr")
