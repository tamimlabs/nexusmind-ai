"""Email skill — send email via SMTP with draft fallback.

Tools:
- send_email: send via SMTP (TLS port 587). If SMTP not configured or send
  fails, saves a draft to output/email_drafts/ and returns actionable guidance.

Config (env / .env / settings):
- EMAIL_SMTP_SERVER (preferred) or EMAIL_IMAP_SERVER (fallback as SMTP host)
- EMAIL_ADDRESS (sender)
- EMAIL_PASSWORD or EMAIL_IMAP_PASSWORD (app password)
- EMAIL_SMTP_PORT (optional, default 587)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.core.executor import register_tool
from agent.models import ToolResult

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DRAFT_DIR = _PROJECT_ROOT / "output" / "email_drafts"


def _parse_dotenv() -> dict[str, str]:
    """Parse project-root .env into dict (no env side-effects)."""
    env: dict[str, str] = {}
    for candidate in (_PROJECT_ROOT / ".env", Path(".env")):
        if candidate.exists():
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip("'\"")
            except Exception:
                logger.debug("Could not parse %s", candidate)
            break
    return env


def _resolve(keys: list[str], dotenv: dict[str, str]) -> str:
    """Return first non-empty value for keys from settings → os.environ → .env."""
    # 1. settings (if fields exist)
    try:
        from agent.config import settings

        for k in keys:
            # settings uses lower-case field names
            attr = k.lower()
            val = getattr(settings, attr, None)
            if val:
                return str(val).strip().strip("'\"")
            # also try without EMAIL_ prefix variations
    except Exception:
        pass
    # 2. os.environ
    for k in keys:
        v = os.environ.get(k)
        if v:
            return str(v).strip().strip("'\"")
    # 3. .env file
    for k in keys:
        v = dotenv.get(k)
        if v:
            return str(v).strip()
    return ""


def _email_config() -> dict[str, str]:
    """Resolve SMTP config from settings/env/.env."""
    dotenv = _parse_dotenv()
    smtp_server = _resolve(
        ["EMAIL_SMTP_SERVER", "SMTP_SERVER", "EMAIL_IMAP_SERVER", "EMAIL_HOST"],
        dotenv,
    )
    address = _resolve(["EMAIL_ADDRESS", "EMAIL", "SMTP_ADDRESS"], dotenv)
    password = _resolve(
        ["EMAIL_PASSWORD", "EMAIL_IMAP_PASSWORD", "IMAP_PASSWORD", "SMTP_PASSWORD"],
        dotenv,
    )
    port = _resolve(["EMAIL_SMTP_PORT", "SMTP_PORT"], dotenv) or "587"
    return {"smtp_server": smtp_server, "address": address, "password": password, "port": port}


def _save_draft(to: str, subject: str, body: str, cc: str = "") -> str:
    """Persist email as draft under output/email_drafts/. Returns path str or ''."""
    try:
        _DRAFT_DIR.mkdir(parents=True, exist_ok=True)
        safe_to = re.sub(r"[^a-zA-Z0-9._@-]", "_", to)[:60] or "unknown"
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_{safe_to}.eml"
        path = _DRAFT_DIR / fname
        header = [
            f"To: {to}",
            f"Cc: {cc}" if cc else None,
            f"Subject: {subject}",
            f"Date: {datetime.now(UTC).isoformat()}",
            "",
            body,
        ]
        path.write_text("\n".join(h for h in header if h is not None), encoding="utf-8")
        return str(path)
    except Exception as exc:
        logger.debug("Failed to save email draft: %s", exc)
        return ""


def _send_sync(
    smtp_server: str,
    port: int,
    address: str,
    password: str,
    to: str,
    subject: str,
    body: str,
    cc: str,
) -> None:
    """Blocking SMTP send (run in thread)."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(body)

    recipients = [r.strip() for r in ([to] + cc.split(",")) if r.strip()] if cc else [to]

    with smtplib.SMTP(smtp_server, port, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(address, password)
        server.send_message(msg, from_addr=address, to_addrs=recipients)


@register_tool("send_email", high_risk=True)
async def send_email(to: str, subject: str, body: str, cc: str = "", **_: Any) -> ToolResult:
    """Send an email via SMTP (TLS).

    Args:
        to: recipient address.
        subject: email subject.
        body: plain-text body.
        cc: optional CC address(es), comma-separated.
    """
    if not to or not to.strip():
        return ToolResult(success=False, output="", error="send_email: 'to' is required")
    if not subject:
        subject = "(no subject)"
    if body is None:
        body = ""

    cfg = _email_config()
    smtp_server = cfg["smtp_server"]
    address = cfg["address"]
    password = cfg["password"]
    try:
        port = int(cfg["port"])
    except ValueError:
        port = 587

    # Fallback when SMTP not configured — save draft and guide user
    if not smtp_server or not address or not password:
        draft = _save_draft(to, subject, body, cc)
        missing = [
            k
            for k, v in [
                ("EMAIL_SMTP_SERVER/EMAIL_IMAP_SERVER", smtp_server),
                ("EMAIL_ADDRESS", address),
                ("EMAIL_PASSWORD", password),
            ]
            if not v
        ]
        hint = (
            f"SMTP not configured (missing: {', '.join(missing)}). "
            "Set EMAIL_SMTP_SERVER (or EMAIL_IMAP_SERVER), EMAIL_ADDRESS and EMAIL_PASSWORD "
            "in .env or environment (use an app password for Gmail). "
            "For Gmail: EMAIL_SMTP_SERVER=smtp.gmail.com, EMAIL_ADDRESS=you@gmail.com, EMAIL_PASSWORD=<app-password>."
        )
        if draft:
            hint += f" Draft saved to {draft}."
        return ToolResult(success=False, output="", error=hint)

    try:
        await asyncio.to_thread(
            _send_sync, smtp_server, port, address, password, to.strip(), subject, body, cc
        )
        return ToolResult(
            success=True, output=f"Email sent to {to} via {smtp_server}:{port} from {address}"
        )
    except Exception as exc:
        draft = _save_draft(to, subject, body, cc)
        err = str(exc)[:600]
        hint = f"SMTP send failed via {smtp_server}:{port} as {address}: {err}. Draft"
        hint += f" saved to {draft}." if draft else " (draft save also failed)."
        hint += " Check EMAIL_SMTP_SERVER/EMAIL_ADDRESS/EMAIL_PASSWORD and that TLS port 587 is reachable."
        logger.warning("send_email failed: %s", err)
        return ToolResult(success=False, output="", error=hint)
