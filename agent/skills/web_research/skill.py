"""Web research skill — Google Custom Search primary, DuckDuckGo fallback.

Auto-switches when Google's 100/day limit is hit.
"""

from __future__ import annotations

import re
import time
from urllib.parse import unquote

import httpx

from agent.core.executor import ToolResult, register_tool

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


def _clean_ddg_url(url: str) -> str:
    """Extract real URL from DuckDuckGo redirect wrapper."""
    if "duckduckgo.com/l/" in url:
        match = re.search(r"uddg=([^&]+)", url)
        if match:
            return unquote(match.group(1))
    return url


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
    """DuckDuckGo HTML search — free fallback with browser-like headers."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        # Try the HTML search page (more reliable than lite)
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=headers,
        )
        resp.raise_for_status()

        text = resp.text
        results = []

        # Check for CAPTCHA
        if "challenge" in text.lower() and "duck" in text.lower() and len(text) < 5000:
            # CAPTCHA detected, try lite version as fallback
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers=headers,
            )
            text = resp.text

        # Extract result links: <a rel="nofollow" href="...">title</a>
        links = re.findall(r'<a[^>]*rel="nofollow"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', text)
        # Extract snippets
        snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', text, re.DOTALL)

        for i, (url, title) in enumerate(links[:num_results]):
            snippet = snippets[i].strip() if i < len(snippets) else ""
            snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            title = re.sub(r"<[^>]+>", "", title).strip()
            url = _clean_ddg_url(url)
            if title and url:
                results.append(f"{title}\n{snippet}\n{url}")

        if not results:
            # Fallback: plain text extraction
            for line in text.split("\n"):
                line = re.sub(r"<[^>]+>", "", line).strip()
                if line and len(line) > 20 and "duckduckgo" not in line.lower():
                    results.append(line)
                if len(results) >= num_results * 2:
                    break

        return "\n\n".join(results[:num_results]) if results else "No results found."


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
