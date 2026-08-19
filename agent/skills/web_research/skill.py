"""Web research skill — search and extract information from the web."""

from __future__ import annotations

import httpx

from agent.core.executor import register_tool, ToolResult


@register_tool("web_search")
async def web_search(query: str, num_results: int = 5, **_) -> ToolResult:
    """Search the web using DuckDuckGo Lite."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers={"User-Agent": "NexusMind/1.0"},
            )
            resp.raise_for_status()

            # Simple extraction from lite page
            text = resp.text
            # Extract links and snippets (basic parsing)
            results = []
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                if line and len(line) > 20 and not line.startswith("<"):
                    results.append(line)
                if len(results) >= num_results * 3:
                    break

            output = "\n".join(results[:num_results * 3]) if results else "No results found."
            return ToolResult(success=True, output=output[:3000])

    except Exception as exc:
        return ToolResult(success=False, output="", error=str(exc))


@register_tool("fetch_url")
async def fetch_url(url: str, max_chars: int = 5000, **_) -> ToolResult:
    """Fetch and extract text content from a URL."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "NexusMind/1.0"},
            )
            resp.raise_for_status()

            content = resp.text
            # Strip HTML tags (basic)
            import re
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
