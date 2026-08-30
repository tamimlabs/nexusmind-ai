"""Data processing skill — parse, transform, and analyze data."""

from __future__ import annotations

import json

from agent.core.executor import ToolResult, register_tool


@register_tool("parse_json")
async def parse_json(data: str, **_) -> ToolResult:
    """Parse a JSON string and return formatted output."""
    try:
        parsed = json.loads(data)
        formatted = json.dumps(parsed, indent=2, default=str)
        return ToolResult(
            success=True,
            output=formatted[:10000],
            metadata={"type": type(parsed).__name__},
        )
    except json.JSONDecodeError as exc:
        return ToolResult(success=False, output="", error=f"Invalid JSON: {exc}")


@register_tool("summarize_text")
async def summarize_text(text: str, max_length: int = 500, **_) -> ToolResult:
    """Summarize text using Gemini AI for intelligent extraction."""
    if not text or len(text.strip()) < 50:
        return ToolResult(success=True, output=text or "No content to summarize.")

    from agent.core.gemini_client import generate_content

    try:
        response = await generate_content(
            system=f"""You are an expert summarizer. Create a clear, well-structured summary.
Rules:
- Maximum {max_length} words
- Include key facts, trends, and insights
- Use bullet points for clarity
- Remove HTML entities, broken URLs, and noise
- Write in clean, readable English""",
            user=f"Summarize this content:\n\n{text[:8000]}",
            temperature=0.3,
            max_tokens=4000,
        )
        return ToolResult(success=True, output=response[:8000])
    except Exception:
        # Fallback: extract key sentences
        sentences = [s.strip() for s in text.replace("\n", " ").split(".") if len(s.strip()) > 20]
        if not sentences:
            return ToolResult(success=True, output="No content to summarize.")
        summary_parts = []
        current_length = 0
        for sentence in sentences:
            if current_length + len(sentence) > max_length:
                break
            summary_parts.append(sentence)
            current_length += len(sentence)
        summary = ". ".join(summary_parts) + "." if summary_parts else sentences[0][:max_length]
        return ToolResult(success=True, output=summary)


@register_tool("extract_data")
async def extract_data(text: str, keys: str = "", **_) -> ToolResult:
    """Extract structured data from text using simple patterns."""
    import re

    results: dict[str, list[str]] = {}

    # Emails
    emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    if emails:
        results["emails"] = list(set(emails))

    # URLs
    urls = re.findall(r"https?://[^\s<>\"']+", text)
    if urls:
        results["urls"] = list(set(urls))

    # Phone numbers (stricter: require leading + or (XXX) or 10+ digits avoiding dates)
    phones = re.findall(r"(?:\+\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}", text)
    # Filter: keep only those with at least 10 digits and not matching date-like patterns
    filtered = []
    for p in phones:
        digits = re.sub(r"\D", "", p)
        if len(digits) >= 10 and len(digits) <= 15:
            # Exclude if looks like date (e.g., 12/05/2024 already captured separately)
            if not re.match(r"^\d{1,2}[/-]\d{1,2}[/-]", p.strip()):
                filtered.append(p.strip())
    if filtered:
        results["phones"] = list(set(filtered))

    # Dates (basic)
    dates = re.findall(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", text)
    if dates:
        results["dates"] = list(set(dates))

    if not results:
        return ToolResult(success=True, output="No structured data found.")

    output = json.dumps(results, indent=2)
    return ToolResult(success=True, output=output, metadata={"fields_found": list(results.keys())})
