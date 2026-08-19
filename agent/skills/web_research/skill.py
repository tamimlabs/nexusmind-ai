"""Web research skill — Google Custom Search primary, DuckDuckGo fallback.

Auto-switches when Google's 100/day limit is hit.
"""

from __future__ import annotations

import time
import httpx

from agent.core.executor import register_tool, ToolResult
from agent.config import settings

# --- Google Custom Search Config ---
_google_search_key = ""  # Set via env: GOOGLE_SEARCH_API_KEY
_google_cx = ""           # Set via env: GOOGLE_SEARCH_CX
_google_daily_count = 0
_google_daily_limit = 100
_google_day_start = 0


def _init_google():
    """Load Google search credentials from env."""
    global _google_search_key, _google_cx
    import os
    _google_search_key = _google_search_key or os.environ.get("GOOGLE_SEARCH_API_KEY", "")
    _google_cx = _google_cx or os.environ.get("GOOGLE_SEARCH_CX", "")


def _google_available() -> bool:
    """Check if Google search is configured and not exhausted."""
    global _google_daily_count, _google_day_start
    _init_google()

    if not _google_search_key or not _google_cx:
        return False

    # Reset counter at midnight
    now = time.time()
    day_boundary = int(now / 86400) * 86400
    if _google_day_start < day_boundary:
        _google_daily_count = 0
        _google_day_start = day_boundary

    return _google_daily_count < _google_daily_limit


def _google_used():
    global _google_daily_count
    _google_daily_count += 1


# --- Google Custom Search ---

async def _search_google(query: str, num_results: int) -> str | None:
    """Try Google Custom Search. Returns None if unavailable or failed."""
    if not _google_available():
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": _google_search_key,
                    "cx": _google_cx,
                    "q": query,
                    "num": min(num_results, 10),
                },
            )

            if resp.status_code == 429 or "quotaExceeded" in resp.text:
                # Limit hit — switch to DuckDuckGo permanently for today
                global _google_daily_count
                _google_daily_count = _google_daily_limit
                return None

            resp.raise_for_status()
            data = resp.json()
            _google_used()

            items = data.get("items", [])
            if not items:
                return None

            lines = []
            for item in items:
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                link = item.get("link", "")
                lines.append(f"{title}\n{snippet}\n{link}")

            return "\n\n".join(lines)

    except Exception:
        return None


# --- DuckDuckGo Fallback ---

async def _search_duckduckgo(query: str, num_results: int) -> str:
    """DuckDuckGo Lite — unlimited free fallback."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://lite.duckduckgo.com/lite/",
            params={"q": query},
            headers={"User-Agent": "NexusMind/1.0"},
        )
        resp.raise_for_status()

        text = resp.text
        results = []
        for line in text.split("\n"):
            line = line.strip()
            if line and len(line) > 20 and not line.startswith("<"):
                results.append(line)
            if len(results) >= num_results * 3:
                break

        return "\n".join(results[:num_results * 3]) if results else "No results found."


# --- Main Tool ---

@register_tool("web_search")
async def web_search(query: str, num_results: int = 5, **_) -> ToolResult:
    """Search the web. Uses Google Custom Search first, falls back to DuckDuckGo automatically."""
    # Try Google first
    google_result = await _search_google(query, num_results)
    if google_result:
        return ToolResult(
            success=True,
            output=google_result[:3000],
            metadata={"engine": "google", "daily_remaining": _google_daily_limit - _google_daily_count},
        )

    # Fallback to DuckDuckGo
    try:
        ddg_result = await _search_duckduckgo(query, num_results)
        return ToolResult(
            success=True,
            output=ddg_result[:3000],
            metadata={"engine": "duckduckgo"},
        )
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


@register_tool("fetch_url")
async def fetch_url(url: str, max_chars: int = 5000, **_) -> ToolResult:
    """Fetch and extract text content from a URL."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "NexusMind/1.0"})
            resp.raise_for_status()

            import re
            content = resp.text
            content = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
            content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
            content = re.sub(r"<[^>]+>", " ", content)
            content = re.sub(r"\s+", " ", content).strip()

            return ToolResult(
                success=True,
                output=content[:max_chars],
                metadata={"content_type": resp.headers.get("content-type", ""), "status": resp.status_code},
            )
    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))
