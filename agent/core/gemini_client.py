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
) -> str:
    """Generate text content using Gemini with automatic key rotation and retry."""
    from google.genai import types

    model_name = model or settings.gemini_model
    last_error = None

    for attempt in range(rotator.key_count):
        client, key_used = rotator.get_client()

        # If all keys in cooldown, async wait before trying
        if rotator.active_keys == 0:
            shortest = min(rotator._cooldowns.values()) - time.time()
            if shortest > 0:
                await asyncio.sleep(min(shortest, 5))

        def _call(client=client) -> str:
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
                        parts=[types.Part.from_text(text="Understood. I will follow these instructions.")],
                    )
                )
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user)]))

            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
            return response.text or ""

        try:
            result = await asyncio.to_thread(_call)
            return result

        except Exception as exc:
            last_error = exc
            error_str = str(exc).lower()

            if "rate" in error_str or "429" in error_str or "quota" in error_str or "503" in error_str or "unavailable" in error_str:
                rotator.mark_rate_limited(key_used, retry_after=15 + attempt * 15)
                logger.warning("Rate limited/unavailable on key ...%s (attempt %d), rotating", key_used[-6:], attempt + 1)
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
