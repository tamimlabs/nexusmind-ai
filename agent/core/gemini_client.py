"""Gemini client with multi-key rotation for higher rate limits.

Rotates between multiple API keys automatically. If a key hits rate limit,
switches to the next one. Supports unlimited keys via env var:
  GEMINI_API_KEY=key1,key2,key3
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agent.config import settings

logger = logging.getLogger(__name__)

# Upper bound for the fallback's output budget. Doubling the primary budget is
# enough for an agent to finish the step; the Pro model supports far more.
_FALLBACK_TOKEN_CEILING = 65536

# Quota/rate-limit survival:
# - RPS/RPM-class limits ("per minute", "per second") reset within seconds to a
#   minute, so the client WAITS and keeps polling every ~5 s until the window
#   frees up — a burst is never fatal and the task is never abandoned for it.
# - Per-day per-key caps (free tier "20 requests/day") only reset after hours,
#   so the spent key is parked until its bucket resets and the request is
#   handed to the NEXT configured API key (each key has its own daily bucket).
#   QuotaExhaustedError is raised only when every key is spent for the day.
_RATE_POLL_SECONDS = 5.0  # fixed retry cadence while waiting out a rate window
_DAILY_KEY_PARK_SECONDS = 86400  # key retired until the daily bucket resets

# Diagnostic markers in the 429 body used to tell the two apart.
_DAILY_QUOTA_MARKERS = (
    "per day",
    "per_day",
    "per-day",
    "daily",
    "day limit",
    "free_tier",
    "requests per day",
)
_RATE_QUOTA_MARKERS = (
    "per minute",
    "per second",
    "per 60 seconds",
    "requests per minute",
    "requests per second",
    "throughput",
    "too many requests",
)

# Marker surfaced to the model on a retried call so the key/model handling the
# retry understands it is a CONTINUATION, not a fresh start.
RETRY_CONTEXT_MARKER = "[PRIOR ATTEMPT CONTEXT]"


def _retry_context_note(reason: str) -> str:
    return f"{RETRY_CONTEXT_MARKER}: {reason}"


class OutputTruncatedError(RuntimeError):
    """The model hit its ``max_output_tokens`` limit mid-reply.

    Raise when the API reports the response was cut off, so callers can retry
    on a stronger model or feed a truncation note back to the model and
    continue the task instead of silently dropping the partial reply.
    """

    def __init__(self, partial: str = "") -> None:
        super().__init__("model reply was truncated (max output tokens reached)")
        self.partial = partial or ""


class QuotaExhaustedError(RuntimeError):
    """Every model/key hit a rate limit or quota (429 / RESOURCE_EXHAUSTED).

    ``retry_after`` says when a retry is likely to succeed. Callers (the
    orchestrator) can park the task for later instead of failing it.
    """

    def __init__(self, retry_after: float = 60.0) -> None:
        super().__init__(f"Gemini quota/rate limit exhausted; try again in ~{retry_after:.0f}s")
        self.retry_after = retry_after


def _is_quota_error(error_str: str) -> bool:
    return (
        "429" in error_str
        or "quota" in error_str
        or "resource_exhausted" in error_str
        or "rate" in error_str
        or "503" in error_str
        or "unavailable" in error_str
    )


def _is_daily_quota(error_str: str) -> bool:
    """True when the 429 is a per-day per-key cap (e.g. free-tier 20 req/day).

    Daily caps only reset after hours/days, so the spent key must be parked and
    a different API key used (each key has its own daily bucket). Anything not
    clearly daily is treated as a rate error worth waiting out.
    """
    return any(marker in error_str for marker in _DAILY_QUOTA_MARKERS)


def _extract_retry_delay(exc: Exception) -> float:
    """Pull the server's ``retryDelay`` (e.g. ``'2.6s'``) out of a 429 body."""
    import re as _re

    match = _re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
    return float(match.group(1)) if match else 0.0


def _was_truncated(response: Any) -> bool:
    """True when the API reports ``finish_reason == MAX_TOKENS``."""
    from google.genai import types

    try:
        return bool(
            response.candidates
            and response.candidates[0].finish_reason == types.FinishReason.MAX_TOKENS
        )
    except Exception:
        # Unknown SDK shape — never assume truncation on a complete-looking reply.
        return False


class KeyRotator:
    """Manages multiple Gemini API keys with round-robin rotation and rate-limit backoff."""

    def __init__(self) -> None:
        self._keys: list[str] = []
        self._clients: dict[str, Any] = {}
        self._current_index = 0
        self._cooldowns: dict[str, float] = {}  # key -> until_when (epoch)
        self._init_keys()

    def _init_keys(self) -> None:
        """Parse comma-separated keys from env."""
        raw = settings.gemini_api_key
        if not raw or raw == "your-gemini-api-key":
            logger.warning("No Gemini API keys configured. Set GEMINI_API_KEY in .env")
            return

        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        logger.info("Gemini key rotator initialized with %d keys", len(self._keys))

    def refresh(self) -> None:
        """Re-parse GEMINI_API_KEY after a settings reload.

        Keys saved via the dashboard at runtime must be usable immediately
        without a server restart. Removes clients/cooldowns for keys that were
        dropped so rotation state stays consistent with the live key set.
        """
        raw = (settings.gemini_api_key or "").strip()
        if not raw or raw == "your-gemini-api-key":
            self._keys = []
        else:
            self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        for key in list(self._clients):
            if key not in self._keys:
                self._clients.pop(key, None)
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if k in self._keys}
        logger.info("Gemini key rotator refreshed: %d key(s) active", len(self._keys))

    def _get_next_key(self) -> str:
        """Get the next available key (skips keys in cooldown)."""
        if not self._keys:
            raise RuntimeError("No Gemini API keys configured")

        now = time.monotonic()
        attempts = 0
        while attempts < len(self._keys):
            key = self._keys[self._current_index % len(self._keys)]
            self._current_index += 1

            # Check if key is in cooldown (monotonic to survive NTP jumps)
            cooldown_until = self._cooldowns.get(key, 0)
            if now >= cooldown_until:
                return key

            attempts += 1

        # All keys in cooldown — use the one with shortest cooldown
        best_key = min(self._keys, key=lambda k: self._cooldowns.get(k, 0))
        wait_time = self._cooldowns[best_key] - now
        if wait_time > 0:
            logger.warning("All keys in cooldown. Waiting %.1fs for next key", wait_time)
            # Note: this is called from sync context in _get_next_key
            # The caller should handle async waiting if needed
        return best_key

    def mark_rate_limited(self, key: str, retry_after: float = 60) -> None:
        """Mark a key as rate-limited with backoff duration."""
        self._cooldowns[key] = time.monotonic() + retry_after
        logger.warning("Key ...%s rate-limited, backing off %.0fs", key[-6:], retry_after)

    def get_client(self) -> tuple[Any, str]:
        """Get a Gemini client with the next available key.

        Returns:
            Tuple of (client, key_used).

        """
        from google import genai

        key = self._get_next_key()

        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key)

        return self._clients[key], key

    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def active_keys(self) -> int:
        now = time.monotonic()
        return sum(1 for k in self._keys if now >= self._cooldowns.get(k, 0))


rotator = KeyRotator()


class RateThrottle:
    """Client-side RPS/RPM gate so the agent self-limits to its Gemini tier.

    The tighter of the two bounds wins, so the free tier (1 req/s, 15 req/min)
    is never overstepped by the agent's own bursty loop — which would otherwise
    trigger server-side 429s mid-task. Reads the live ``settings`` values on
    every acquire, so switching tier from the dashboard takes effect on the
    very next request without a restart. A bound of ``0`` disables it.
    """

    def __init__(self) -> None:
        self._next_emit = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _interval(rps: float, rpm: int) -> float:
        """Min seconds between requests implied by the rps and rpm bounds."""
        interval = 0.0
        if rps and rps > 0:
            interval = max(interval, 1.0 / rps)
        if rpm and rpm > 0:
            interval = max(interval, 60.0 / rpm)
        return interval

    async def acquire(self) -> float:
        """Sleep until the next request is allowed, then claim the slot."""
        interval = self._interval(settings.gemini_rps, settings.gemini_rpm)
        if interval <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            wait = self._next_emit - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_emit = time.monotonic() + interval
            return interval


_throttle = RateThrottle()


async def generate_content(
    model: str | None = None,
    system: str = "",
    user: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    retry: bool = True,
    fallback_max_tokens: int | None = None,
) -> str:
    """Generate text content using Gemini with automatic key rotation and retry.

    If the primary model hits its output-token limit (truncated reply), the
    call transparently retries once on the stronger configured model
    (``settings.gemini_model_pro``) with a larger output budget — so long
    tasks keep going instead of stopping on an unfinished answer.
    ``OutputTruncatedError`` is raised only when no fallback model is set or
    the fallback is truncated too (the caller can then tell the model to reply
    shorter, which the agent loop does).

    Rate limiting is handled by classification of the 429 body:
    - RPS/RPM-class (per-minute/second): the request is WAITED out — the client
      polls again every ~5 s until the rate window resets. The task is never
      abandoned for a transient burst.
    - Per-day per-key cap: the spent key is parked until its daily bucket
      resets and the request is retried on the NEXT configured API key. Each
      retried call (rate or daily) carries a short context marker so the key
      that handles it knows it is a continuation, not a fresh start.
      ``QuotaExhaustedError`` is raised only when every key is spent today.
    """
    from google.genai import types

    primary_model = model or settings.gemini_model
    fallback_model = settings.gemini_model_pro
    model_name = primary_model
    active_max = max_tokens
    used_fallback = False
    fallback_rounds = 1 if (fallback_model and fallback_model != primary_model) else 0
    retry_note = ""  # appended to the prompt once a retry is needed (see below)

    attempts = 0
    while True:
        attempts += 1
        active_model = model_name
        # If all keys in cooldown, wait for the shortest cooldown before picking a key
        if rotator.active_keys == 0 and rotator._cooldowns:
            shortest = min(rotator._cooldowns.values()) - time.monotonic()
            if shortest > 0:
                await asyncio.sleep(min(shortest, 5))
        client, key_used = rotator.get_client()

        effective_user = f"{user}\n\n— {retry_note} —" if retry_note else user

        def _call(
            client=client, model=active_model, max_out=active_max, prompt=effective_user
        ) -> str:
            contents = []
            if system:
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=f"[System Instructions]\n{system}")],
                    )
                )
                contents.append(
                    types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(
                                text="Understood. I will follow these instructions."
                            )
                        ],
                    )
                )
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_out,
                ),
            )
            text = response.text or ""
            if _was_truncated(response):
                raise OutputTruncatedError(text)
            return text

        try:
            await _throttle.acquire()
            return await asyncio.to_thread(_call)

        except OutputTruncatedError:
            if not fallback_rounds or used_fallback:
                raise
            used_fallback = True
            logger.warning(
                "Max output tokens reached on %s; switching to %s to continue",
                model_name,
                fallback_model,
            )
            model_name = fallback_model
            active_max = fallback_max_tokens or min(max_tokens * 2, _FALLBACK_TOKEN_CEILING)
            continue

        except Exception as exc:
            error_str = str(exc).lower()
            if "invalid" in error_str and "key" in error_str:
                rotator.mark_rate_limited(key_used, retry_after=86400)
                logger.error("Invalid API key ...%s, removing from rotation", key_used[-6:])
                continue

            if not _is_quota_error(error_str):
                raise

            delay = _extract_retry_delay(exc)

            if _is_daily_quota(error_str):
                # Per-day per-key cap (free tier: 20 req/day): this key is spent
                # until tomorrow. Park it, hand the same request (now with a
                # context note) to the NEXT key, which has its own daily bucket.
                retry_note = _retry_context_note(
                    "the previous API key hit its per-day request cap and was "
                    "replaced; nothing was executed or lost — continue this "
                    "request exactly as asked, over the same context."
                )
                rotator.mark_rate_limited(key_used, retry_after=_DAILY_KEY_PARK_SECONDS)
                logger.warning(
                    "Daily Gemini request cap on key ...%s (attempt %d); parking "
                    "key and switching to the next API key",
                    key_used[-6:],
                    attempts,
                )
                if rotator.key_count <= 1 or rotator.active_keys == 0:
                    raise QuotaExhaustedError(retry_after=_DAILY_KEY_PARK_SECONDS) from exc
                if delay > 0:
                    await asyncio.sleep(min(delay, 2.0))
                continue

            # RPS/RPM-class limit: WAIT it out, then keep polling every ~5 s
            # until the per-minute/second window resets. Never give up on a
            # transient burst — the caller's model sees a note that this is a
            # continuation of the paused request.
            retry_note = _retry_context_note(
                "the previous attempt was paused by a per-minute/second rate "
                "limit and is now being retried after the window reset; "
                "nothing was executed or lost — respond exactly as requested."
            )
            poll_seconds = max(delay, _RATE_POLL_SECONDS)
            rotator.mark_rate_limited(key_used, retry_after=poll_seconds)
            logger.warning(
                "Rate-limit on key ...%s (attempt %d), model %s — polling again in ~%.0fs",
                key_used[-6:],
                attempts,
                active_model,
                poll_seconds,
            )
            await asyncio.sleep(poll_seconds)


async def generate_structured(
    model: str | None = None,
    system: str = "",
    user: str = "",
    response_schema: dict[str, Any] | None = None,
) -> str:
    """Generate content with structured output hints."""
    return await generate_content(model=model, system=system, user=user, temperature=0.1)
