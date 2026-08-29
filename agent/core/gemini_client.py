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


class OutputTruncatedError(RuntimeError):
    """The model hit its ``max_output_tokens`` limit mid-reply.

    Raise when the API reports the response was cut off, so callers can retry
    on a stronger model or feed a truncation note back to the model and
    continue the task instead of silently dropping the partial reply.
    """

    def __init__(self, partial: str = "") -> None:
        super().__init__("model reply was truncated (max output tokens reached)")
        self.partial = partial or ""


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

        now = time.time()
        attempts = 0
        while attempts < len(self._keys):
            key = self._keys[self._current_index % len(self._keys)]
            self._current_index += 1

            # Check if key is in cooldown
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
        self._cooldowns[key] = time.time() + retry_after
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
        now = time.time()
        return sum(1 for k in self._keys if now >= self._cooldowns.get(k, 0))


rotator = KeyRotator()


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
    """
    from google.genai import types

    primary_model = model or settings.gemini_model
    fallback_model = settings.gemini_model_pro
    model_name = primary_model
    active_max = max_tokens
    used_fallback = False
    last_error: Exception | None = None
    fallback_rounds = 1 if (fallback_model and fallback_model != primary_model) else 0

    for attempt in range(rotator.key_count + fallback_rounds):
        client, key_used = rotator.get_client()

        # If all keys in cooldown, async wait before trying
        if rotator.active_keys == 0:
            shortest = min(rotator._cooldowns.values()) - time.time()
            if shortest > 0:
                await asyncio.sleep(min(shortest, 5))

        def _call(client=client, model=model_name, max_out=active_max) -> str:
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
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user)]))

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
            last_error = exc
            error_str = str(exc).lower()

            if (
                "rate" in error_str
                or "429" in error_str
                or "quota" in error_str
                or "503" in error_str
                or "unavailable" in error_str
            ):
                rotator.mark_rate_limited(key_used, retry_after=15 + attempt * 15)
                logger.warning(
                    "Rate limited/unavailable on key ...%s (attempt %d), rotating",
                    key_used[-6:],
                    attempt + 1,
                )
                continue
            elif "invalid" in error_str and "key" in error_str:
                rotator.mark_rate_limited(key_used, retry_after=86400)
                logger.error("Invalid API key ...%s, removing from rotation", key_used[-6:])
                continue
            else:
                raise

    raise RuntimeError(f"Gemini generation failed after {rotator.key_count} attempts: {last_error}")


async def generate_structured(
    model: str | None = None,
    system: str = "",
    user: str = "",
    response_schema: dict[str, Any] | None = None,
) -> str:
    """Generate content with structured output hints."""
    return await generate_content(model=model, system=system, user=user, temperature=0.1)
