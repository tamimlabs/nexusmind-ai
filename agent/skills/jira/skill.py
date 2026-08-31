"""Jira skill — comment and transition issues via the Jira Cloud REST API.

Tools:
- jira_comment_issue: add a comment to an issue
- jira_transition_issue: transition an issue to a new status

Auth uses Jira email + API token (basic auth) resolved from
settings.jira_domain / jira_email / jira_token, env vars
JIRA_DOMAIN / JIRA_EMAIL / JIRA_TOKEN, or the project .env file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from agent.core.executor import register_tool
from agent.models import ToolResult

logger = logging.getLogger(__name__)


def _parse_env_file_values(keys: tuple[str, ...]) -> dict[str, str]:
    """Parse requested keys from the project .env file without mutating os.environ."""
    values: dict[str, str] = {}
    try:
        from agent.config import _ENV_FILE, _PROJECT_ROOT  # type: ignore

        candidates = [
            _ENV_FILE,
            _PROJECT_ROOT / ".env",
            Path(__file__).resolve().parents[3] / ".env",
        ]
    except Exception:
        candidates = [Path(__file__).resolve().parents[3] / ".env", Path(".env")]
    for env_file in candidates:
        try:
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k in keys and k not in values:
                        values[k] = v.strip().strip("'\"")
                if len(values) == len(keys):
                    break
        except Exception:
            logger.debug("Could not parse env file %s for Jira config", env_file)
    return values


def _jira_config() -> tuple[str, str, str] | None:
    """Return (domain, email, token) or None if any piece is missing.

    Resolution order for each field:
      1. agent.config.settings (jira_domain / jira_email / jira_token)
      2. os.environ (JIRA_DOMAIN / JIRA_EMAIL / JIRA_TOKEN)
      3. project .env file (same keys)
    """
    domain = ""
    email = ""
    token = ""

    # 1. settings
    try:
        from agent.config import settings  # type: ignore

        domain = getattr(settings, "jira_domain", "") or ""
        email = getattr(settings, "jira_email", "") or ""
        token = getattr(settings, "jira_token", "") or ""
    except Exception:
        pass

    # 2. environment
    if not domain:
        domain = os.environ.get("JIRA_DOMAIN", "").strip()
    if not email:
        email = os.environ.get("JIRA_EMAIL", "").strip()
    if not token:
        token = os.environ.get("JIRA_TOKEN", "").strip()

    # 3. .env file fallback
    if not domain or not email or not token:
        env_vals = _parse_env_file_values(("JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_TOKEN"))
        if not domain:
            domain = env_vals.get("JIRA_DOMAIN", "")
        if not email:
            email = env_vals.get("JIRA_EMAIL", "")
        if not token:
            token = env_vals.get("JIRA_TOKEN", "")

    domain = domain.strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    email = email.strip()
    token = token.strip()

    if not domain or not email or not token:
        return None
    return domain, email, token


def _jira_url(domain: str, path: str) -> str:
    return f"https://{domain}{path}"


def _adf_body(text: str) -> dict[str, Any]:
    """Wrap plain text in Atlassian Document Format for /rest/api/3 comment."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


@register_tool("jira_comment_issue", high_risk=True)
async def jira_comment_issue(issue_key: str, body: str, **_: Any) -> ToolResult:
    """Add a comment to a Jira issue.

    Args:
        issue_key: Issue key e.g. PROJ-123.
        body: Comment text (plain text, wrapped to ADF for API v3).
    """
    cfg = _jira_config()
    if cfg is None:
        return ToolResult(
            success=False,
            output="",
            error="Jira not configured — set JIRA_DOMAIN, JIRA_EMAIL, JIRA_TOKEN in .env or environment (or settings.jira_domain/jira_email/jira_token)",
        )
    domain, email, token = cfg
    if not issue_key or not issue_key.strip():
        return ToolResult(success=False, output="", error="issue_key is required (e.g. PROJ-123)")
    if not body:
        return ToolResult(success=False, output="", error="body is required")

    issue_key = issue_key.strip()
    url = _jira_url(domain, f"/rest/api/3/issue/{issue_key}/comment")

    # Jira Cloud REST API v3 expects ADF; send ADF body with fallback-compatible key
    payload: dict[str, Any] = {"body": _adf_body(body)}

    try:
        async with httpx.AsyncClient(timeout=30, auth=(email, token)) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                comment_id = str(data.get("id", "")) if isinstance(data, dict) else ""
                suffix = f" (id {comment_id})" if comment_id else ""
                return ToolResult(success=True, output=f"Comment added to {issue_key}{suffix}")
            try:
                detail: Any = resp.json()
            except Exception:
                detail = resp.text
            msg = detail.get("message", "") if isinstance(detail, dict) else str(detail)
            # Truncate noisy HTML error pages
            msg = str(msg)[:600] if msg else resp.text[:600]
            return ToolResult(
                success=False,
                output="",
                error=f"Jira comment failed [{resp.status_code}]: {msg}",
            )
    except Exception as exc:
        logger.exception("jira_comment_issue failed for %s", issue_key)
        return ToolResult(success=False, output="", error=str(exc)[:600])


@register_tool("jira_transition_issue", high_risk=True)
async def jira_transition_issue(issue_key: str, transition_id: str, **_: Any) -> ToolResult:
    """Transition a Jira issue to a new status.

    Args:
        issue_key: Issue key e.g. PROJ-123.
        transition_id: Transition id (as returned by /rest/api/3/issue/{key}/transitions).
    """
    cfg = _jira_config()
    if cfg is None:
        return ToolResult(
            success=False,
            output="",
            error="Jira not configured — set JIRA_DOMAIN, JIRA_EMAIL, JIRA_TOKEN in .env or environment (or settings.jira_domain/jira_email/jira_token)",
        )
    domain, email, token = cfg
    if not issue_key or not issue_key.strip():
        return ToolResult(success=False, output="", error="issue_key is required (e.g. PROJ-123)")
    if not transition_id or not str(transition_id).strip():
        return ToolResult(success=False, output="", error="transition_id is required")

    issue_key = issue_key.strip()
    tid = str(transition_id).strip()
    url = _jira_url(domain, f"/rest/api/3/issue/{issue_key}/transitions")
    payload: dict[str, Any] = {"transition": {"id": tid}}

    try:
        async with httpx.AsyncClient(timeout=30, auth=(email, token)) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if resp.status_code in (200, 201, 204):
                return ToolResult(
                    success=True, output=f"Transitioned {issue_key} with transition {tid}"
                )
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            msg = detail.get("message", "") if isinstance(detail, dict) else str(detail)
            # Surface validation errors from Jira (e.g. invalid transition)
            if isinstance(detail, dict) and detail.get("errorMessages"):
                msg = "; ".join(str(m) for m in detail["errorMessages"])[:600]
            elif not msg:
                msg = (resp.text or "")[:600]
            return ToolResult(
                success=False,
                output="",
                error=f"Jira transition failed [{resp.status_code}]: {str(msg)[:600]}",
            )
    except Exception as exc:
        logger.exception("jira_transition_issue failed for %s", issue_key)
        return ToolResult(success=False, output="", error=str(exc)[:600])
