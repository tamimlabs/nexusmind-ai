"""Email watcher - monitors an IMAP inbox for new emails.

Token-efficient: only calls Gemini when new emails are detected.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import imaplib
import logging
from email.header import decode_header
from email.utils import parseaddr
from typing import Any

from agent.watchers.base import BaseWatcher

logger = logging.getLogger(__name__)


class EmailWatcher(BaseWatcher):
    """Watch an IMAP inbox for new (or unread) emails."""

    INSTRUCTION_KEYWORDS = ("email", "mail", "inbox", "reply", "respond")

    MAX_FETCH_PER_CHECK = 20

    def __init__(self, watcher_id: str, config: dict[str, Any]):
        super().__init__(watcher_id, config)
        import os as _os

        def _resolve_env(*keys: str) -> str:
            for k in keys:
                v = _os.environ.get(k) or config.get(k) or config.get(k.lower())
                if v:
                    return str(v).strip().strip("'\"")
            # Also check .env file directly
            try:
                from pathlib import Path as _P

                envf = _P(__file__).resolve().parents[2] / ".env"
                if envf.exists():
                    data = envf.read_text(encoding="utf-8")
                    for k in keys:
                        for line in data.splitlines():
                            if line.strip().startswith(f"{k}="):
                                return line.partition("=")[2].strip().strip("'\"")
            except Exception:
                pass
            return ""

        self.imap_server = (
            config.get("imap_server")
            or config.get("imapServer")
            or _resolve_env("EMAIL_IMAP_SERVER")
            or "imap.gmail.com"
        )
        self.email_address = config.get("email", "") or _resolve_env("EMAIL_ADDRESS", "EMAIL")
        self.password = config.get("password", "") or _resolve_env(
            "EMAIL_IMAP_PASSWORD", "EMAIL_PASSWORD", "IMAP_PASSWORD"
        )  # App password (not account password)
        self.folder = config.get("folder", "INBOX")
        self.watch_unread = config.get("watch_unread", True)
        self._seen_message_ids: set[str] = set(self._state.get("email_seen_ids", []))

    @staticmethod
    def _decode_subject(raw_subject: str | None) -> str:
        """Decode RFC 2047 encoded email subjects."""
        if not raw_subject:
            return ""
        parts: list[str] = []
        for chunk, charset in decode_header(raw_subject):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return "".join(parts)

    def _get_body_preview(self, msg: email.message.Message) -> str:
        """Extract a plain-text preview of the email body."""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition") or "")
                if content_type == "text/plain" and "attachment" not in disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body = payload.decode(charset, errors="replace")
                        break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                body = payload.decode(charset, errors="replace")
        return " ".join(body.split())[:500]

    def _fetch_emails_sync(self) -> list[dict[str, Any]]:
        """Fetch recent emails via IMAP (blocking - run in a thread)."""
        fetched: list[dict[str, Any]] = []
        mail = imaplib.IMAP4_SSL(self.imap_server)
        try:
            mail.login(self.email_address, self.password)
            mail.select(self.folder)

            criteria = "(UNSEEN)" if self.watch_unread else "(ALL)"
            status, data = mail.search(None, criteria)
            if status != "OK" or not data or not data[0]:
                return fetched

            sequence_nums = data[0].split()[-self.MAX_FETCH_PER_CHECK :]
            for seq_num in sequence_nums:
                status, msg_data = mail.fetch(seq_num, "(RFC822)")
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue
                raw_bytes = msg_data[0][1]
                if not raw_bytes:
                    continue
                msg = email.message_from_bytes(raw_bytes)
                sender_name, sender_addr = parseaddr(str(msg.get("From") or ""))
                fetched.append(
                    {
                        "message_id": str(
                            msg.get("Message-ID") or seq_num.decode(errors="replace")
                        ),
                        "sender_name": sender_name,
                        "sender_addr": sender_addr,
                        "subject": self._decode_subject(msg.get("Subject")),
                        "date": str(msg.get("Date") or ""),
                        "body_preview": self._get_body_preview(msg),
                    }
                )
        finally:
            with contextlib.suppress(Exception):
                mail.logout()
        return fetched

    async def check_for_events(self) -> list[dict[str, Any]]:
        """Check the IMAP inbox for new emails."""
        # Early validation — don't try IMAP login unauthenticated
        if not self.email_address or not self.password:
            logger.debug(
                "Email watcher %s skipped: not configured (missing email/password)", self.watcher_id
            )
            return []
        # Sync dedup set from persisted state (handles restore after __init__)
        if self._state.get("email_seen_ids"):
            self._seen_message_ids = set(self._state["email_seen_ids"])
        try:
            fetched = await asyncio.to_thread(self._fetch_emails_sync)
        except Exception as e:
            logger.warning("Email check failed: %s", e)
            return []

        events: list[dict[str, Any]] = []
        first_run = not self._seen_message_ids
        for info in fetched:
            message_id = info["message_id"]
            self._seen_message_ids.add(message_id)
            # On first run, seed seen IDs without emitting the whole inbox
            if first_run:
                continue
            sender = info["sender_name"] or info["sender_addr"]
            events.append(
                {
                    "event_type": "email.received",
                    "external_id": message_id,
                    "payload": {
                        "sender": sender,
                        "sender_addr": info["sender_addr"],
                        "subject": info["subject"],
                        "date": info["date"],
                        "body_preview": info["body_preview"],
                    },
                }
            )

        if len(self._seen_message_ids) > 1000:
            self._seen_message_ids = set(sorted(self._seen_message_ids)[-500:])

        # Persist seen IDs for dedup across restarts
        self._state["email_seen_ids"] = sorted(self._seen_message_ids)[-500:]

        return events

    async def process_event(self, event: dict[str, Any]) -> str | None:
        """Convert an email event into an agent goal."""
        payload = event["payload"]

        if event["event_type"] == "email.received":
            instruction = self.standing_instruction()
            if instruction is None:
                await self.notify_unhandled_event(
                    f"New email from '{payload['sender']}': '{payload['subject']}'",
                    event,
                )
                return None

            return self.gated_goal(
                instruction,
                f"the new email from '{payload['sender']}': "
                f"'{payload['subject']}'. Analyze and draft response.",
            )

        return None
