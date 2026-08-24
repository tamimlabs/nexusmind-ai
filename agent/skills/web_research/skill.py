"""Web research skill — Google Custom Search primary, duckduckgo-search library fallback."""

from __future__ import annotations

import asyncio
import re
import time

import httpx

from agent.core.executor import ToolResult, register_tool

# --- Google Custom Search Config ---
_google_search_key = ""
_google_cx = ""
_google_daily_count = 0
_google_daily_limit = 100
_google_day_start = 0


def _init_google():
    global _google_search_key, _google_cx
    import os
    try:
        from agent.config import settings
        _google_search_key = getattr(settings, 'google_search_api_key', '') or os.environ.get("GOOGLE_SEARCH_API_KEY", "")
        _google_cx = getattr(settings, 'google_search_cx', '') or os.environ.get("GOOGLE_SEARCH_CX", "")
    except Exception:
        _google_search_key = os.environ.get("GOOGLE_SEARCH_API_KEY", "")
        _google_cx = os.environ.get("GOOGLE_SEARCH_CX", "")


def _google_available() -> bool:
    global _google_daily_count, _google_day_start
    _init_google()

    if not _google_search_key or not _google_cx:
        return False

    now = time.time()
    day_boundary = int(now / 86400) * 86400
    if _google_day_start < day_boundary:
        _google_daily_count = 0
        _google_day_start = day_boundary

    return _google_daily_count < _google_daily_limit


def _google_used():
    global _google_daily_count
    _google_daily_count += 1


async def _search_google(query: str, num_results: int) -> str | None:
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

            if resp.status_code in (400, 403, 429) or "quotaExceeded" in resp.text:
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


async def _search_ddg_library(query: str, num_results: int) -> str:
    """Use duckduckgo-search library — handles anti-bot internally."""
    from duckduckgo_search import DDGS

    def _do_search():
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=num_results))
        return results

    results = await asyncio.to_thread(_do_search)

    if not results:
        return "No results found."

    lines = []
    for r in results:
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        lines.append(f"{title}\n{body}\n{href}")

    return "\n\n".join(lines)


@register_tool("web_search")
async def web_search(query: str, num_results: int = 5, **_) -> ToolResult:
    """Search the web. Uses Google Custom Search first, falls back to duckduckgo-search library."""
    google_result = await _search_google(query, num_results)
    if google_result:
        return ToolResult(
            success=True,
            output=google_result[:3000],
            metadata={"engine": "google", "daily_remaining": _google_daily_limit - _google_daily_count},
        )

    try:
        ddg_result = await _search_ddg_library(query, num_results)
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
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
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
