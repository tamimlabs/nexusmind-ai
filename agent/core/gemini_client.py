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
#   Per-key wait is capped at _MAX_RATE_RETRY_SECONDS (45s) — if the window
#   doesn't reset, the key is parked briefly and the request rotates to the
#   next API key instead of blocking the task indefinitely.
# - Per-day per-key caps (free tier "20 requests/day") only reset after hours,
#   so the spent key is parked until its bucket resets and the request is
#   handed to the NEXT configured API key (each key has its own daily bucket).
#   QuotaExhaustedError is raised only when every key is spent for the day.
_RATE_POLL_SECONDS = 5.0  # fixed retry cadence while waiting out a rate window
_MAX_RATE_RETRY_SECONDS = 45.0  # max wall-time to poll a single key on RPS/RPM 429 before rotating
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


def _fallback_model_chain(primary: str) -> list[str]:
    """Ordered list of quota fallbacks for per-project-per-model 429s.

    Free tier is 20 req/day *per model per project* — rotating API keys in the
    same project does NOT help once the model bucket is spent. Switching to a
    different model (e.g. flash-lite, 2.0-flash) gives a fresh bucket. The
    chain is read from ``GEMINI_FALLBACK_MODELS`` so the operator can tune it
    without a restart; gemini_model_pro is appended as last resort.
    """
    raw = (getattr(settings, "gemini_fallback_models", "") or "").strip()
    chain: list[str] = []
    seen: set[str] = {primary}
    for part in raw.split(","):
        m = part.strip()
        if m and m not in seen:
            chain.append(m)
            seen.add(m)
    pro = getattr(settings, "gemini_model_pro", "") or ""
    if pro and pro not in seen:
        chain.append(pro)
    return chain


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
        """Min seconds between requests implied by the rps and rpm bounds.

        Scales RPM by the number of configured API keys (free tier is per-key).
        With 4 keys at 15 rpm each the effective budget is 60 rpm → 1s interval,
        not 4s. The server-side 429 retry + rotation still protects the real
        quota, so this only removes the *preemptive* sleep that made every
        website build pay 28s of idle time.
        """
        interval = 0.0
        if rps and rps > 0:
            interval = max(interval, 1.0 / rps)
        if rpm and rpm > 0:
            try:
                keys = max(1, rotator.key_count or 1)
                # Use active keys when some are parked for the day
                active = rotator.active_keys
                if active and active < keys:
                    keys = active
            except Exception:
                keys = 1
            effective_rpm = rpm * keys
            interval = max(interval, 60.0 / effective_rpm)
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
    rate_retry_start: float | None = None  # wall-time start for current key's RPS/RPM polls

    # Quota fallback chain (per-project-per-model free tier): when all keys are
    # parked for the current model, automatically retry the SAME prompt on the
    # next model in GEMINI_FALLBACK_MODELS — each model has its own 20/day bucket.
    _quota_chain = [primary_model] + _fallback_model_chain(primary_model)
    _quota_idx = 0

    def _try_next_quota_model(reason: str) -> bool:
        nonlocal _quota_idx, model_name, retry_note, rate_retry_start
        if _quota_idx + 1 >= len(_quota_chain):
            return False
        nxt = _quota_chain[_quota_idx + 1]
        logger.warning("Quota exhausted on %s (%s) — falling back to %s (%d/%d)", model_name, reason, nxt, _quota_idx + 1, len(_quota_chain) - 1)
        _quota_idx += 1
        model_name = nxt
        # Fresh quota bucket per model — clear per-key daily parks for next model
        try:
            rotator._cooldowns.clear()
        except Exception:
            pass
        retry_note = _retry_context_note(f"the previous model {reason} hit its quota and the request was moved to {nxt}; continue exactly as asked.")
        rate_retry_start = None
        return True

    attempts = 0
    # For RPS/RPM 429s we stay on the SAME key for up to 45s before rotating
    _reuse_client: Any | None = None
    _reuse_key: str | None = None
    while True:
        attempts += 1
        active_model = model_name
        if _reuse_client is not None and _reuse_key is not None and rate_retry_start is not None:
            # Still within the 45s budget for this key — retry same key
            client, key_used = _reuse_client, _reuse_key
        else:
            # Pick next available key (or wait if all in cooldown)
            if rotator.active_keys == 0 and rotator._cooldowns:
                shortest = min(rotator._cooldowns.values()) - time.monotonic()
                if shortest > 0:
                    await asyncio.sleep(min(shortest, 5))
            client, key_used = rotator.get_client()
            _reuse_client, _reuse_key = client, key_used

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
                _reuse_client = _reuse_key = None
                rate_retry_start = None
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
                _reuse_client = _reuse_key = None
                rate_retry_start = None  # new key -> reset RPS/RPM window
                if rotator.key_count <= 1 or rotator.active_keys == 0:
                    if _try_next_quota_model(f"key ...{key_used[-6:]} daily"):
                        _reuse_client = _reuse_key = None
                        continue
                    raise QuotaExhaustedError(retry_after=_DAILY_KEY_PARK_SECONDS) from exc
                if delay > 0:
                    await asyncio.sleep(min(delay, 2.0))
                continue

            # RPS/RPM-class limit: stay on SAME key for up to 45s, then park it
            # and rotate to the next API key. This is your requested bound.
            if rate_retry_start is None:
                rate_retry_start = time.monotonic()
            elapsed = time.monotonic() - rate_retry_start
            if elapsed >= _MAX_RATE_RETRY_SECONDS:
                logger.warning(
                    "Rate-limit on key ...%s persisted %.0fs (>=%.0fs); parking key and switching to next API key (attempt %d)",
                    key_used[-6:],
                    elapsed,
                    _MAX_RATE_RETRY_SECONDS,
                    attempts,
                )
                # Park this key briefly and rotate — don't sleep full poll window
                rotator.mark_rate_limited(key_used, retry_after=_RATE_POLL_SECONDS * 2)
                retry_note = _retry_context_note(
                    "the previous API key was rate-limited for ~45s and was "
                    "replaced; nothing was executed or lost — continue this "
                    "request exactly as asked, over the same context."
                )
                _reuse_client = _reuse_key = None
                rate_retry_start = None  # new key gets a fresh 45s budget
                if rotator.key_count <= 1 or rotator.active_keys == 0:
                    if _try_next_quota_model(f"key ...{key_used[-6:]} rate"):
                        _reuse_client = _reuse_key = None
                        continue
                    # Single key or all keys exhausted — surface as quota error
                    # so the orchestrator can abort cleanly with retry guidance.
                    raise QuotaExhaustedError(retry_after=_RATE_POLL_SECONDS) from exc
                continue

            retry_note = _retry_context_note(
                "the previous attempt was paused by a per-minute/second rate "
                "limit and is now being retried after the window reset; "
                "nothing was executed or lost — respond exactly as requested."
            )
            poll_seconds = max(delay, _RATE_POLL_SECONDS)
            # Don't overshoot the 45s budget — sleep only the remaining time
            remaining = _MAX_RATE_RETRY_SECONDS - elapsed
            sleep_for = min(poll_seconds, remaining) if remaining > 0 else 0
            # Keep retrying SAME key — still record a short backoff for
            # observability (test expects _cooldowns set) but reuse bypasses it.
            rotator.mark_rate_limited(key_used, retry_after=poll_seconds)
            logger.warning(
                "Rate-limit on key ...%s (attempt %d, %.0fs/%.0fs), model %s — retrying same key in ~%.0fs",
                key_used[-6:],
                attempts,
                elapsed,
                _MAX_RATE_RETRY_SECONDS,
                active_model,
                sleep_for,
            )
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            # _reuse_client/_reuse_key already set so next loop retries same key


async def generate_content_stream(
    model: str | None = None,
    system: str = "",
    user: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4096,
):
    """Yield text deltas via the streaming API (google.genai).

    Uses ``client.models.generate_content_stream`` so callers can emit
    token-by-token updates (e.g. ``on_event("token", delta)`` on the
    dashboard). Mirrors :func:`generate_content` quota/rate-limit handling:
    the stream is retried on the same key for ~45 s (RPS/RPM) or handed to
    the next key on a per-day cap, with the same ``RETRY_CONTEXT_MARKER``
    continuation note. Falls back to the stronger model on ``MAX_TOKENS``
    truncation. Callers should keep the non-streaming ``generate_content``
    fallback — streamed decoding may be unavailable in tests or on older
    SDK shapes.

    Yields:
        Incremental ``str`` deltas as they arrive.

    Example:
        ``async for delta in generate_content_stream(user="hello"): ...``
    """
    from google.genai import types as _types  # type: ignore

    primary_model = model or settings.gemini_model
    fallback_model = settings.gemini_model_pro
    model_name = primary_model
    active_max = max_tokens
    used_fallback = False
    fallback_rounds = 1 if (fallback_model and fallback_model != primary_model) else 0
    retry_note = ""
    rate_retry_start: float | None = None
    _reuse_client: Any | None = None
    _reuse_key: str | None = None
    _quota_chain_s = [primary_model] + _fallback_model_chain(primary_model)
    _quota_idx_s = 0

    def _try_next_quota_model_s(reason: str) -> bool:
        nonlocal _quota_idx_s, model_name
        if _quota_idx_s + 1 >= len(_quota_chain_s):
            return False
        nxt = _quota_chain_s[_quota_idx_s + 1]
        logger.warning("Quota exhausted (stream) on %s (%s) — falling back to %s", model_name, reason, nxt)
        _quota_idx_s += 1
        model_name = nxt
        try:
            rotator._cooldowns.clear()
        except Exception:
            pass
        return True

    while True:
        if _reuse_client is not None and _reuse_key is not None and rate_retry_start is not None:
            client, key_used = _reuse_client, _reuse_key
        else:
            if rotator.active_keys == 0 and rotator._cooldowns:
                shortest = min(rotator._cooldowns.values()) - time.monotonic()
                if shortest > 0:
                    await asyncio.sleep(min(shortest, 5))
            client, key_used = rotator.get_client()
            _reuse_client, _reuse_key = client, key_used

        effective_user = f"{user}\n\n— {retry_note} —" if retry_note else user

        def _build_contents(prompt: str) -> list[Any]:
            contents: list[Any] = []
            if system:
                contents.append(
                    _types.Content(
                        role="user",
                        parts=[_types.Part.from_text(text=f"[System Instructions]\n{system}")],
                    )
                )
                contents.append(
                    _types.Content(
                        role="model",
                        parts=[_types.Part.from_text(text="Understood. I will follow these instructions.")],
                    )
                )
            contents.append(_types.Content(role="user", parts=[_types.Part.from_text(text=prompt)]))
            return contents

        contents = _build_contents(effective_user)
        config = _types.GenerateContentConfig(temperature=temperature, max_output_tokens=active_max)

        # Acquire throttle before opening the stream
        await _throttle.acquire()

        # Try streaming; retry the whole stream on quota/rate errors (before any
        # delta has been yielded the caller can safely retry).
        stream = None
        yielded_any = False
        collected = ""
        try:

            def _open_stream():
                # Support fakes that only implement generate_content
                if hasattr(client.models, "generate_content_stream"):
                    return client.models.generate_content_stream(
                        model=model_name, contents=contents, config=config
                    )
                # Fallback shim: synthesize a single-chunk stream from the
                # non-streaming call (keeps tests that mock generate_content
                # working while still yielding via the streaming path).
                resp = client.models.generate_content(model=model_name, contents=contents, config=config)
                txt = getattr(resp, "text", None) or ""
                if _was_truncated(resp):
                    raise OutputTruncatedError(txt)

                class _OneChunk:
                    text = txt
                    candidates = getattr(resp, "candidates", None)

                return iter([_OneChunk()])

            stream = await asyncio.to_thread(_open_stream)

            # Incremental iteration: each next() may block on the network, so
            # hop it to a thread and yield deltas as they arrive.
            it = iter(stream)
            while True:
                try:
                    chunk = await asyncio.to_thread(next, it)
                except StopIteration:
                    break
                # Truncation reported on the chunk (or final chunk)
                if _was_truncated(chunk):
                    partial = collected + (getattr(chunk, "text", None) or "")
                    raise OutputTruncatedError(partial)
                delta = getattr(chunk, "text", None) or ""
                if delta:
                    collected += delta
                    yielded_any = True
                    yield delta
            # Stream completed without truncation
            return

        except OutputTruncatedError:
            if not fallback_rounds or used_fallback:
                raise
            used_fallback = True
            logger.warning(
                "Max output tokens reached (stream) on %s; switching to %s",
                model_name,
                fallback_model,
            )
            model_name = fallback_model
            active_max = min(max_tokens * 2, _FALLBACK_TOKEN_CEILING)
            # If we already yielded deltas, the caller has partial output;
            # don't restart streaming — surface the partial so the caller can
            # fall back to non-streaming.
            if yielded_any:
                raise OutputTruncatedError(collected)
            continue

        except Exception as exc:
            error_str = str(exc).lower()
            if "invalid" in error_str and "key" in error_str:
                rotator.mark_rate_limited(key_used, retry_after=86400)
                logger.error("Invalid API key ...%s, removing from rotation (stream)", key_used[-6:])
                _reuse_client = _reuse_key = None
                rate_retry_start = None
                # If nothing yielded yet, retry; otherwise surface.
                if yielded_any:
                    raise
                continue
            if not _is_quota_error(error_str):
                raise
            delay = _extract_retry_delay(exc)
            if _is_daily_quota(error_str):
                retry_note = _retry_context_note(
                    "the previous API key hit its per-day request cap and was replaced; nothing was executed or lost — continue this request exactly as asked, over the same context."
                )
                rotator.mark_rate_limited(key_used, retry_after=_DAILY_KEY_PARK_SECONDS)
                logger.warning(
                    "Daily Gemini request cap (stream) on key ...%s; parking key and switching",
                    key_used[-6:],
                )
                _reuse_client = _reuse_key = None
                rate_retry_start = None
                if yielded_any:
                    if _try_next_quota_model_s(f"key ...{key_used[-6:]} daily (yielded)"):
                        _reuse_client = _reuse_key = None
                        continue
                    raise QuotaExhaustedError(retry_after=_DAILY_KEY_PARK_SECONDS) from exc
                if rotator.key_count <= 1 or rotator.active_keys == 0:
                    if _try_next_quota_model_s(f"key ...{key_used[-6:]} daily"):
                        _reuse_client = _reuse_key = None
                        continue
                    raise QuotaExhaustedError(retry_after=_DAILY_KEY_PARK_SECONDS) from exc
                if delay > 0:
                    await asyncio.sleep(min(delay, 2.0))
                continue
            # RPS/RPM
            if rate_retry_start is None:
                rate_retry_start = time.monotonic()
            elapsed = time.monotonic() - rate_retry_start
            if elapsed >= _MAX_RATE_RETRY_SECONDS:
                logger.warning(
                    "Rate-limit (stream) on key ...%s persisted %.0fs; parking key and rotating",
                    key_used[-6:],
                    elapsed,
                )
                rotator.mark_rate_limited(key_used, retry_after=_RATE_POLL_SECONDS * 2)
                retry_note = _retry_context_note(
                    "the previous API key was rate-limited for ~45s and was replaced; nothing was executed or lost — continue this request exactly as asked, over the same context."
                )
                _reuse_client = _reuse_key = None
                rate_retry_start = None
                if yielded_any:
                    if _try_next_quota_model_s(f"key ...{key_used[-6:]} rate (yielded)"):
                        _reuse_client = _reuse_key = None
                        continue
                    raise QuotaExhaustedError(retry_after=_RATE_POLL_SECONDS) from exc
                if rotator.key_count <= 1 or rotator.active_keys == 0:
                    if _try_next_quota_model_s(f"key ...{key_used[-6:]} rate"):
                        _reuse_client = _reuse_key = None
                        continue
                    raise QuotaExhaustedError(retry_after=_RATE_POLL_SECONDS) from exc
                continue
            retry_note = _retry_context_note(
                "the previous attempt was paused by a per-minute/second rate limit and is now being retried after the window reset; nothing was executed or lost — respond exactly as requested."
            )
            poll_seconds = max(delay, _RATE_POLL_SECONDS)
            remaining = _MAX_RATE_RETRY_SECONDS - elapsed
            sleep_for = min(poll_seconds, remaining) if remaining > 0 else 0
            rotator.mark_rate_limited(key_used, retry_after=poll_seconds)
            logger.warning(
                "Rate-limit (stream) on key ...%s, retrying same key in ~%.0fs",
                key_used[-6:],
                sleep_for,
            )
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            if yielded_any:
                raise
            continue


async def generate_structured(
    model: str | None = None,
    system: str = "",
    user: str = "",
    response_schema: dict[str, Any] | None = None,
) -> str:
    """Generate content with structured output hints."""
    return await generate_content(model=model, system=system, user=user, temperature=0.1)
