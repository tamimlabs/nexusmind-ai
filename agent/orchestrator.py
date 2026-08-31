"""Orchestrator — the main agent loop.

Combines OpenClaw's reasoning engine pattern with Hermes' self-improving loop.
Flow: receive task -> check memory -> plan -> execute -> reflect -> store.

Self-improvement: past reflections are extracted as lessons and fed into future planning.

Memory policy (inspired by Hermes Agent):
- Only store MEANINGFUL outcomes, not routine tasks
- Only reflect when there's something genuinely NEW to learn
- Deduplicate: don't store if similar lesson already exists
- Hard limit: keep memory lean, force consolidation when full
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent.core.agent_loop import AdaptiveOutcome, decide_next_step, run_adaptive_loop
from agent.core.executor import execute_step, list_tools, set_task_context, untrust_task
from agent.core.memory import memory_store
from agent.core.planner import plan_task
from agent.core.skill_library import skill_library as _skill_library
from agent.models import StepStatus, Task, TaskStatus, TaskStep

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Trivial tasks that don't need memory storage
_TRIVIAL_PATTERNS = [
    "what is",
    "what are",
    "who is",
    "tell me about",
    "hello",
    "hi",
    "hey",
    "thanks",
    "ok",
    "yes",
    "no",
]

# Phrases that mark a goal as a STANDING INSTRUCTION (future policy) rather
# than an immediate command. Stored in memory; the watcher enforces them later.
_INSTRUCTION_TRIGGERS = (
    "when you",
    "whenever",
    "every time",
    "each time",
    "from now on",
    "in future",
    "in the future",
    "always ",
    "by default",
    "next time",
    "if a new ",
    "if any new ",
    "going forward",
)


def _is_standing_instruction(goal: str) -> bool:
    """True if the goal reads like a durable instruction, not a one-off command.

    Anchored to the START of the goal so one-off commands that merely contain
    a trigger word mid-sentence (e.g. "merge pr #7 always using squash")
    are still executed immediately.
    """
    g = goal.lower().lstrip()
    return g.startswith(_INSTRUCTION_TRIGGERS)


def _is_trivial(task: Task) -> bool:
    """Check if a task is trivial and doesn't warrant memory storage."""
    goal_lower = task.goal.lower().strip()
    # Short goals with no tools used are trivial
    if len(task.steps) <= 1 and len(goal_lower) < 50:
        for pattern in _TRIVIAL_PATTERNS:
            if goal_lower.startswith(pattern):
                return True
    return False


# ── Phase A1: Complexity Router helpers ──────────────────────────

# Tier2: single-tool intent patterns (regex -> canonical tool).
# Order matters — more specific first; github single before generic web.
_TIER2_TOOL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # read_file — explicit "read file <path>" / open / cat
    (re.compile(r"\bread(_file)?\b|\bopen\s+file\b|\bcat\s+[^\s]+\b|\bshow\s+file\b", re.I), "read_file"),
    # write_file — "write file" / "create file" / "save to"
    (re.compile(r"\bwrite(_file)?\b|\bcreate\s+(?:a\s+)?file\b|\bsave\s+(?:to|as)\b", re.I), "write_file"),
    # list_directory — "list files/directory/folder" / ls / dir
    (re.compile(r"\blist(_directory)?\b|\blist\s+(?:files?|directory|folder)|\b\bls\b|\bdir\b", re.I), "list_directory"),
    # fetch_url — explicit fetch/get url or bare https://
    (re.compile(r"\bfetch(_url)?\b|https?://\S+", re.I), "fetch_url"),
    # github single-action — one github tool only (not multi-step pipeline)
    (re.compile(r"\bgithub_(?:get_repo|list_prs|get_pr|resolve_repo|review_pr|merge_pr|close_pr)\b|\b(?:list|get|show|merge|close)\s+(?:pr|pull\s*request|repo)\b", re.I), "github"),
    # web_search — last, broadest
    (re.compile(r"\bweb_search\b|\bsearch\s+(?:the\s+)?web\b|\bweb\s+search\b|\bgoogle\s+search\b", re.I), "web_search"),
]

# Fallback broad search hint for very short "search for X" queries.
_WEB_SEARCH_FALLBACK_RE = re.compile(r"^\s*search\s+for\s+.+", re.I)


def _is_tier1_goal(goal: str) -> bool:
    """A3 slicing check: <50 chars and starts with a trivial prefix."""
    gl = goal.lower().strip()
    if len(gl) >= 50:
        return False
    return any(gl.startswith(pat) for pat in _TRIVIAL_PATTERNS)


def _classify_tier(task: Task) -> int:
    """Return 1 (trivial), 2 (single-tool), or 3 (complex).

    Tier1: _is_trivial or _is_tier1_goal -> single Gemini, no tools.
    Tier2: regex-detect single tool intent -> one TaskStep via execute_step.
    Tier3: default — full memory/skill/roadmap + adaptive loop.
    """
    # Tier1 — lightest possible, no tools, minimal prompt
    if _is_trivial(task) or _is_tier1_goal(task.goal):
        return 1
    # Tier2 — single-tool shortcut. Guard: very long or multi-conjunction goals stay Tier3.
    goal = task.goal or ""
    if len(goal) > 280 or goal.lower().count(" and ") > 2:
        return 3
    # Multi-intent guard: if goal contains mentions of >=2 distinct tool families, keep Tier3
    # e.g. "research AI news and then write a report" mentions search + write -> complex.
    distinct_hits: set[str] = set()
    for pat, tool in _TIER2_TOOL_PATTERNS:
        if pat.search(goal):
            distinct_hits.add(tool)
    if _WEB_SEARCH_FALLBACK_RE.match(goal):
        distinct_hits.add("web_search")
    # Also treat "research" + "write" as two intents even when "research" is not an explicit web_search phrase
    gl = goal.lower()
    if len(distinct_hits) >= 2:
        return 3
    if "research" in gl and "write" in gl:
        return 3
    # Need 2+ conjunctions or sequencers like "then" with mixed verbs.
    if (" and then " in gl or " then " in gl) and distinct_hits:
        # if the detected single tool is not the whole goal, it's a multi-step pipeline
        if len(goal) > 60:
            return 3
    # Single-tool fast path
    if distinct_hits:
        return 2
    return 3


def _extract_first_path(goal: str) -> str | None:
    """Heuristic path extraction for Tier2 file tools."""
    # quoted projects/output path first
    m = re.search(r"[\"']((?:projects|output)[/\w.\-\\]+)[\"']", goal)
    if m:
        return m.group(1).strip().strip(",.;")
    m = re.search(r"\b((?:projects|output)/[^\s\"',;]+)", goal)
    if m:
        return m.group(1).strip().strip(",.;\"'")
    m = re.search(r"\b([\w\-./\\]+\.(?:txt|md|html|js|css|json|py|yaml|yml|csv))\b", goal, re.I)
    if m:
        return m.group(1)
    return None


def _extract_url(goal: str) -> str | None:
    m = re.search(r"https?://[^\s\"'<>]+", goal)
    return m.group(0).strip().strip(",.;") if m else None


def _extract_repo(goal: str) -> str | None:
    """owner/name slug for github single-tool tier2."""
    m = re.search(r"\b([\w.\-]+/[\w.\-]+)\b", goal)
    if m:
        cand = m.group(1)
        # avoid matching plain english like "search for"
        if "/" in cand and not cand.lower().startswith("http"):
            return cand
    return None


def _detect_simple_step(task: Task) -> TaskStep | None:
    """Map a Tier2 goal to a single TaskStep with heuristic args.

    Returns None if the goal is not a clean single-tool intent (caller falls back to Tier3).
    """
    goal = (task.goal or "").strip()
    if not goal:
        return None
    gl = goal.lower()

    # — Helpers to build step —
    def _step(tool: str, args: dict[str, Any], desc: str) -> TaskStep:
        return TaskStep(task_id=task.id, description=desc, tool_name=tool, tool_args=args, order=0)

    # github single-tool (one call only — not the deterministic multi-step pipeline)
    if re.search(r"\bgithub_(?:get_repo|list_prs|get_pr|resolve_repo|review_pr|merge_pr|close_pr)\b", goal, re.I):
        # explicit tool name mentioned — extract it
        m = re.search(r"\bgithub_(get_repo|list_prs|get_pr|resolve_repo|review_pr|merge_pr|close_pr)\b", goal, re.I)
        tool = f"github_{m.group(1).lower()}" if m else "github_get_repo"
        repo = _extract_repo(goal)
        args: dict[str, Any] = {"repo": repo} if repo else {"goal_text": goal}
        # pr number variants
        pm = re.search(r"#(\d{1,6})\b|\bpr\s*#?(\d{1,6})\b", goal, re.I)
        if pm and tool in ("github_get_pr", "github_review_pr", "github_merge_pr", "github_close_pr"):
            num = int(pm.group(1) or pm.group(2))
            args["pr_number"] = num
            if repo:
                args["repo"] = repo
            else:
                args.pop("repo", None)
                args["goal_text"] = goal
        return _step(tool, args, goal[:120])

    if re.search(r"\b(?:list|get|show|merge|close)\s+(?:pr|pull\s*request|repo)\b", gl) and "github" in gl:
        # short github natural-language single action — infer tool
        repo = _extract_repo(goal)
        prm = re.search(r"#(\d{1,6})\b|\bpr\s*#?(\d{1,6})\b", goal, re.I)
        if prm:
            num = int(prm.group(1) or prm.group(2))
            return _step("github_get_pr", {"repo": repo or "owner/repo", "pr_number": num}, goal[:120])
        if "list" in gl and "pr" in gl:
            return _step("github_list_prs", {"repo": repo or "owner/repo"}, goal[:120])
        if "repo" in gl:
            return _step("github_get_repo", {"repo": repo or "owner/repo"}, goal[:120])

    # read_file
    if re.search(r"\bread(_file)?\b|\bopen\s+file\b|\bcat\s+[^\s]+\b|\bshow\s+file\b", goal, re.I):
        path = _extract_first_path(goal) or "output/"
        return _step("read_file", {"path": path}, f"Read file {path}")

    # write_file — needs path + content
    if re.search(r"\bwrite(_file)?\b|\bcreate\s+(?:a\s+)?file\b|\bsave\s+(?:to|as)\b", goal, re.I):
        path = _extract_first_path(goal)
        # try to pull content after "content" / ":" / quoted block
        content: str | None = None
        cm = re.search(r"content\s*[:=]\s*[\"']?(.+?)[\"']?\s*$", goal, re.I | re.S)
        if cm:
            content = cm.group(1).strip()
        elif ":" in goal and len(goal.split(":", 1)[1].strip()) > 5:
            content = goal.split(":", 1)[1].strip().strip("\"'")
        if not path:
            # derive from goal slug
            slug = "-".join(re.findall(r"[a-z0-9]+", gl)[:5])[:30] or "output"
            path = f"output/{slug}.txt"
        if not content:
            content = f"Generated for task: {goal[:300]}"
        return _step("write_file", {"path": path, "content": content}, f"Write file {path}")

    # list_directory
    if re.search(r"\blist(_directory)?\b|\blist\s+(?:files?|directory|folder)", goal, re.I) or re.match(r"^\s*ls\b", gl) or re.match(r"^\s*dir\b", gl):
        path = _extract_first_path(goal)
        # directory not file — strip filename if extracted path looks like a file
        if path and re.search(r"\.\w{1,5}$", path) and "/" in path:
            path = path.rsplit("/", 1)[0] or "projects"
        path = path or "projects"
        return _step("list_directory", {"path": path}, f"List directory {path}")

    # fetch_url
    url = _extract_url(goal)
    if url and ("fetch" in gl or "url" in gl or url in goal):
        # only treat as fetch_url tier2 when fetch intent explicit or goal is basically a URL fetch
        if re.search(r"\bfetch", gl) or len(goal) < 300:
            return _step("fetch_url", {"url": url}, f"Fetch URL {url[:80]}")

    # web_search — explicit tool or "search for ..." / "google"
    if re.search(r"\bweb_search\b|\bsearch\s+(?:the\s+)?web\b|\bweb\s+search\b|\bgoogle\b", goal, re.I) or _WEB_SEARCH_FALLBACK_RE.match(goal):
        # strip leading search phrasing for query
        query = re.sub(r"^\s*(?:web_search|web search|search\s+for|search|google)\s*[:\-]?\s*", "", goal, flags=re.I).strip()
        query = query or goal
        return _step("web_search", {"query": query[:300], "num_results": 5}, f"Web search: {query[:80]}")

    return None


def _safe_emit(emit: Any | None, task_id: str, event_type: str, message: str, detail: str = "") -> None:
    if emit is None:
        return
    try:
        emit(task_id, event_type, message, detail)
    except Exception:
        logger.debug("emit failed for %s %s", task_id, event_type, exc_info=True)


async def _handle_tier1(task: Task, emit: Any | None = None) -> Task:
    """Tier1: single Gemini generate_content without tools, FAST.

    A3: prompt slicing — omit snapshot/lessons/skill_context entirely.
    Router must still emit live events (thinking/tool_output/done) so the
    dashboard shows realtime progress for every tier.
    """
    _safe_emit(emit, task.id, "thinking", "Tier1 trivial — direct answer (no tools)", task.goal[:160])
    task.status = TaskStatus.EXECUTING
    task.updated_at = datetime.now(UTC)
    try:
        from agent.config import settings as _s
        from agent.core.gemini_client import generate_content as _gen

        # Minimal prompt — goal only, no memory/lessons/skills/snapshot
        raw = await _gen(
            model=_s.gemini_model,
            system="You are NexusMind, a helpful assistant. Answer concisely and accurately.",
            user=task.goal,
            temperature=0.3,
            max_tokens=1024,
        )
        text = (raw or "").strip() or "Done."
        task.status = TaskStatus.COMPLETED
        task.result = text
        task.updated_at = datetime.now(UTC)
        _safe_emit(emit, task.id, "tool_output", "LLM answer", text[:1500])
        _safe_emit(emit, task.id, "done", "Task complete", text[:400])
        from agent.telegram import is_configured as _is_cfg, notify_task_completed as _notify_done

        if _is_cfg():
            await _notify_done(task.id, task.goal, text[:300])
        untrust_task(task.id)
        return task
    except Exception as exc:
        logger.exception("Tier1 failed for %s", task.id)
        task.status = TaskStatus.FAILED
        task.error = str(exc)
        task.updated_at = datetime.now(UTC)
        _safe_emit(emit, task.id, "error", "Tier1 failed", str(exc)[:400])
        untrust_task(task.id)
        return task


async def _handle_tier2(task: Task, step: TaskStep, emit: Any | None = None) -> Task:
    """Tier2: one TaskStep executed via execute_step directly, no adaptive loop."""
    _safe_emit(emit, task.id, "thinking", "Tier2 single-tool — direct execution", f"[{step.tool_name}] {step.description[:140]}")
    task.steps.append(step)
    task.status = TaskStatus.EXECUTING
    task.updated_at = datetime.now(UTC)
    _safe_emit(emit, task.id, "step_running", f"Step 0: {step.description[:90]}", f"[{step.tool_name}] direct")
    context: dict[str, Any] = {"task_id": task.id, "task_goal": task.goal}
    result = await execute_step(step, context)
    # Mirror executor's in-place step mutation for dashboard
    _safe_emit(
        emit,
        task.id,
        "tool_output" if result.success else "error",
        f"Step 0 {'output' if result.success else 'failed'}",
        (result.output or result.error or "")[:1500],
    )
    if result.success:
        task.status = TaskStatus.COMPLETED
        task.result = result.output or "Task completed"
        # surface saved file locations for write_file similar to Tier3
        if step.tool_name == "write_file" and step.tool_args.get("path"):
            task.result = task.result.rstrip() + f"\n\n📁 Saved to:\n- {step.tool_args['path']}"
        _safe_emit(emit, task.id, "done", "Task complete", task.result[:400])
        from agent.telegram import is_configured as _is_cfg2, notify_task_completed as _notify2

        if _is_cfg2():
            await _notify2(task.id, task.goal, task.result[:300])
    else:
        task.status = TaskStatus.FAILED
        task.error = result.error or "Step failed"
        task.result = ""
        hint = _credential_hint(task.error)
        detail = task.error + ("\n\n" + hint if hint else "")
        _safe_emit(emit, task.id, "error", "Task failed", detail[:600])
        from agent.telegram import is_configured as _is_cfg3, notify_task_failed as _notify_fail

        if _is_cfg3():
            await _notify_fail(task.id, task.goal, task.error[:300])
    task.updated_at = datetime.now(UTC)
    untrust_task(task.id)
    return task


def _clean_lessons(raw: str) -> list[str]:
    """Sanitize reflection output into clean, self-contained lesson lines.

    Guards against the two failure modes seen in production:
    - prompt echoes ("You just completed a task... Goal:") being saved whole
    - max-token truncations storing half a sentence as a "lesson"
    """
    if not raw:
        return []
    echo_markers = (
        "you just completed",
        "goal:",
        "output 0-2 lessons",
        "rules:",
        "examples of good",
        "examples of bad",
        "result:",
        "steps taken:",
    )
    lessons: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line or "nothing_to_save" in line.lower():
            continue
        low = line.lower()
        if any(marker in low for marker in echo_markers):
            continue
        words = line.split()
        # Template asks for single sentences under 20 words; allow slack but
        # reject fragments (too short) and run-ons/truncations (too long).
        if not 4 <= len(words) <= 30:
            continue
        # A sentence must END like one — mid-thought truncations don't.
        if line[-1] not in ".!?":
            continue
        lessons.append(line)
        if len(lessons) == 2:
            break
    return lessons


def _is_novel_reflection(reflection: str, existing: list[str]) -> bool:
    """Check if a reflection contains genuinely new information."""
    if not reflection or len(reflection.strip()) < 20:
        return False
    # Check if similar content already exists
    reflection_lower = reflection.lower()
    for existing_ref in existing:
        # If significant overlap, it's not novel
        existing_words = set(existing_ref.lower().split())
        reflection_words = set(reflection_lower.split())
        if existing_words and reflection_words:
            overlap = len(existing_words & reflection_words) / max(len(existing_words), 1)
            if overlap > 0.6:
                return False
    return True


async def _gemini_should_store(task: Task, is_failure: bool = False) -> bool:
    """Ask Gemini whether this task outcome is worth remembering.

    When Gemini is unavailable, falls back to deterministic heuristics.
    """
    try:
        from agent.core.gemini_client import generate_content

        steps_summary = "; ".join(f"{s.tool_name}:{s.status.value}" for s in task.steps[:6])
        prompt = (
            f"Task goal: {task.goal}\n"
            f"Success: {not is_failure and task.status.value == 'completed'}\n"
            f"Steps: {steps_summary or 'none'}\n"
            f"Result snippet: {(task.result or task.error or '')[:300]}\n\n"
            "Should this be saved to long-term memory? Consider:\n"
            "- Is there a reusable outcome, decision, or preference?\n"
            "- Is it trivial (greetings, one-word, simple lookup)?\n"
            "Reply with ONLY 'YES' or 'NO'."
        )
        raw = await generate_content(
            system="You are a memory policy controller. Answer only YES or NO.",
            user=prompt,
            temperature=0.1,
            max_tokens=8,
        )
        answer = raw.strip().lower()
        if "yes" in answer:
            return True
        if "no" in answer:
            return False
    except Exception:
        logger.debug("Gemini memory decision failed, using fallback", exc_info=True)
    # Deterministic fallback
    if _is_trivial(task):
        return False
    successful = [s for s in task.steps if s.status == StepStatus.SUCCESS and s.tool_name]
    return len(successful) >= 1  # looser than legacy 2x2 when Gemini decides fallback


_CREDENTIAL_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("github", "ghp_", "api.github.com", "bad credentials"), "GITHUB_TOKEN"),
    (("utterances", "issue comment", "pull_request", "pull request"), "GITHUB_TOKEN"),
    (
        (
            "customsearch",
            "googlesearch",
            "google search",
            "gcloud",
            "google",
            "quotaexceeded",
            "serp",
        ),
        "GOOGLE_SEARCH_API_KEY / GOOGLE_SEARCH_CX / GEMINI_API_KEY",
    ),
    (("slack", "app.slack.com"), "SLACK_BOT_TOKEN"),
    (("discord", "discord.com"), "DISCORD_BOT_TOKEN"),
    (("gitlab", "gitlab.com"), "GITLAB_TOKEN / GITLAB_BASE_URL"),
    (("jira", "atlassian"), "JIRA_DOMAIN / JIRA_EMAIL / JIRA_TOKEN"),
    (("reddit", "reddit.com"), "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"),
    (("imap", "email", "smtp"), "EMAIL_IMAP_SERVER / EMAIL_ADDRESS / EMAIL_IMAP_PASSWORD"),
    (("telegram", "bot token", "chat id"), "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"),
]

_CREDENTIAL_KEYWORD_RE = re.compile(
    r"\b401\b|\b403\b|\b429\b|api[_ -]?key|authentication failed|unauthorized|forbidden|invalid.*token|no token",
    re.IGNORECASE,
)


def _credential_hint(error_text: str) -> str:
    """Return actionable 'credential missing/invalid' guidance, or '' if N/A."""
    if not error_text:
        return ""
    deets = error_text.lower()
    keys: list[str] = []
    for needles, env_keys in _CREDENTIAL_HINTS:
        if any(n in deets for n in needles):
            keys.append(env_keys)
    if not keys and not _CREDENTIAL_KEYWORD_RE.search(error_text):
        return ""
    if not keys:
        keys = ["the failing service's API key"]
    keys = list(dict.fromkeys(keys))
    return (
        "Hint: this looks like a missing or invalid credential. Set "
        + " / ".join(keys)
        + " under Dashboard → Settings → Credentials (saved to the project .env) and retry the task."
    )


def _compose_failure_error(task: Task, extra: str = "") -> str:
    """Build an actionable error message when every planned step failed."""
    errors = [
        (s.error or s.description or f"step {s.order}").strip()
        for s in task.steps
        if s.status != StepStatus.SUCCESS and (s.error or "").strip()
    ]
    detail = "; ".join(dict.fromkeys(e.strip() for e in errors))[:800] or (
        "All planned steps failed with no recorded error message."
    )
    if extra and (extra[:200] not in detail):
        detail = extra + " " + detail
    hint = _credential_hint("\n".join(errors))
    message = (
        f"The task could not be completed — {len(task.steps)} planned step(s) ran but none succeeded.\n"
        + detail
    )
    if hint:
        message += "\n\n" + hint
    return message


class Orchestrator:
    """Main agent loop — receives tasks, plans, executes, learns."""

    def __init__(self) -> None:
        self.memory = memory_store
        self.skills = _skill_library
        self._running = False

    def _extract_lessons(self, goal: str) -> list[str]:
        """Search memory for relevant past reflections and extract actionable lessons."""
        reflections = self.memory.search(goal, top_k=5, category="reflection")
        lessons = []
        for r in reflections:
            text = r.content.strip()
            if text and len(text) > 10:
                lessons.append(text)
        return lessons

    async def handle_task(self, task: Task, emit: Callable[..., Any] | None = None) -> Task:
        """Process a task from start to finish."""
        logger.info("Handling task [%s]: %s", task.id, task.goal[:100])
        task.status = TaskStatus.PLANNING
        task.updated_at = datetime.now(UTC)

        # Set task context for Telegram approval messages
        set_task_context(task.id, task.goal)

        # Standing instructions ("whenever a PR arrives, ...") are stored as
        # policy for future watcher events — not executed now. Watcher- and
        # webhook-triggered tasks carry event context and skip this check.
        if _is_standing_instruction(task.goal) and "event_type" not in task.context:
            self.memory.save_instruction(task.goal)
            task.status = TaskStatus.COMPLETED
            task.result = (
                f"Saved as standing instruction (not executed now):\n{task.goal}\n\n"
                "The agent will follow this automatically when matching events "
                "arrive via watchers."
            )
            task.updated_at = datetime.now(UTC)
            logger.info("Stored standing instruction from task %s", task.id)

            from agent.telegram import is_configured, notify_task_completed

            if is_configured():
                await notify_task_completed(task.id, task.goal, "Standing instruction saved")
            return task

        # Notify via Telegram
        from agent.telegram import is_configured, notify_task_started

        if is_configured():
            await notify_task_started(task.id, task.goal)

        try:
            # ── Phase A1: Complexity Router (before heavy memory/skill/planning) ──
            # Tier1 trivial -> single Gemini, FAST. Tier2 single-tool -> one execute_step.
            # Tier3 complex (default) -> full flow with lazy roadmap.
            tier = _classify_tier(task)
            logger.info("Router classified task %s as Tier%d: %.60s", task.id, tier, task.goal)
            if tier == 1:
                return await _handle_tier1(task, emit=emit)
            if tier == 2:
                simple_step = _detect_simple_step(task)
                if simple_step is not None:
                    return await _handle_tier2(task, simple_step, emit=emit)
                # fallthrough to Tier3 if detection ambiguous
                logger.info("Tier2 detection ambiguous for %s; falling through to Tier3", task.id)
                tier = 3
            # Tier3 — full Hermes/OpenClaw flow (heavy memory/skill/planning only here)
            _safe_emit(emit, task.id, "thinking", "Tier3 complex — full planning & adaptive loop", f"goal: {task.goal[:120]}")

            # Prefetch relevant memory for this turn (Hermes pattern): a fenced
            # <memory-context> block with trust-ranked, hybrid-retrieved facts.
            # Trivial prompts are gated inside prefetch() and return "".
            _safe_emit(emit, task.id, "thinking", "Recalling memory…", task.goal[:120])
            memory_context = self.memory.prefetch(task.goal)
            if memory_context:
                logger.info("Recalled %d chars of memory context for planning", len(memory_context))

            # Check memory for similar past tasks (for context, not always needed)
            similar = self.memory.search(task.goal, top_k=3, category="task_outcome")
            if similar:
                logger.info("Found %d similar past tasks in memory", len(similar))

            # Extract lessons from past reflections (self-improvement)
            lessons = self._extract_lessons(task.goal)
            if lessons:
                logger.info("Applying %d past lessons to planning", len(lessons))

            # Procedural skill index + best-matched procedure body (Hermes
            # description-as-router pattern). matched skills get use-credit
            # when the task completes successfully.
            _safe_emit(emit, task.id, "thinking", "Matching skills…", task.goal[:120])
            skill_context, matched_skills = self.skills.plan_context(
                task.goal, available_tools=set(list_tools())
            )
            if skill_context:
                logger.info("Injected skill context (%d matched procedure(s))", len(matched_skills))

            # Get available skills
            available_skills = [
                e.metadata.get("skill_name", "") for e in self.memory.get_by_category("skill")
            ]

            # Best-effort initial roadmap — LAZY: only for Tier3.
            # Tier1/2 already returned; this Gemini call is skipped entirely for them.
            # Streaming: plan_task will forward token deltas via emit so the
            # dashboard's word-to-word bubble starts within 2-3s.
            _safe_emit(emit, task.id, "thinking", "Drafting roadmap with Gemini Flash… (streaming)", task.goal[:120])
            roadmap: list = []
            try:
                import asyncio as _aio

                try:
                    roadmap = await _aio.wait_for(
                        plan_task(
                            task,
                            available_skills or None,
                            lessons or None,
                            memory_context or None,
                            skill_context or None,
                            on_event=emit,
                        ),
                        timeout=12,
                    )
                    logger.info("Roadmap ready (%d suggested steps) for task %s", len(roadmap), task.id)
                except _aio.TimeoutError:
                    logger.warning("Roadmap planning timed out after 12s for %s; continuing without roadmap", task.id)
                    _safe_emit(emit, task.id, "thinking", "Roadmap slow — continuing step-by-step", "")
                    roadmap = []
            except Exception:
                logger.exception(
                    "Initial roadmap failed for %s; the adaptive loop will work step-by-step from scratch",
                    task.id,
                )

            task.status = TaskStatus.EXECUTING
            task.updated_at = datetime.now(UTC)

            # STEP-BY-STEP EXECUTION (adaptive): the model decides the next
            # action, we execute THAT ONE action, and its real result
            # (including errors) is fed back into the very next decision.
            # Steps are appended to task.steps live, so the dashboard shows
            # progress in real time, and the loop keeps working — correcting
            # and verifying — until the model is satisfied or a safety budget
            # forces a stop. No content is ever fabricated here: every action
            # comes from the model, goal and live results.
            context: dict[str, Any] = {"task_id": task.id, "task_goal": task.goal}
            # Opencode-like elastic per-task budget (caller can request longer runs)
            max_steps_override = None
            for k in ("max_steps", "max_steps_override"):
                if k in task.context:
                    try:
                        v = int(task.context[k])
                        if 1 <= v <= 200:
                            max_steps_override = v
                            break
                    except Exception:
                        pass
            if max_steps_override is None:
                # fall back to global config (default 40, hard cap 120)
                try:
                    from agent.config import settings as _s

                    cfg_steps = int(getattr(_s, "agent_max_steps", 40))
                    if 5 <= cfg_steps <= 200:
                        max_steps_override = cfg_steps
                except Exception:
                    pass
            loop_kwargs: dict[str, Any] = dict(
                memory_context=memory_context or "",
                lessons=lessons or [],
                skill_context=skill_context or "",
                roadmap=roadmap,
                execute_fn=execute_step,
                decide_fn=decide_next_step,
                on_event=emit,
            )
            if max_steps_override is not None:
                loop_kwargs["max_steps"] = max_steps_override
            outcome: AdaptiveOutcome = await run_adaptive_loop(task, context, **loop_kwargs)

            task.status = TaskStatus.COMPLETED
            task.updated_at = datetime.now(UTC)
            # If EVERY executed step failed (required credentials missing,
            # sandbox blocked, the model picked a wrong route and could not
            # self-correct), or the loop could not even start, the task did NOT
            # succeed. Previously this was silently reported as "completed" —
            # surface it as a FAILURE with actionable guidance instead.
            failed = [s for s in task.steps if s.status != StepStatus.SUCCESS]
            successful = [s for s in task.steps if s.status == StepStatus.SUCCESS]
            if (task.steps and not successful) or (
                not task.steps and outcome.aborted_reason and not outcome.summary
            ):
                task.status = TaskStatus.FAILED
                task.error = _compose_failure_error(task, extra=outcome.aborted_reason)
                task.result = ""
                if not _is_trivial(task):
                    self.memory.save_task_outcome(task.goal, task.error[:200], success=False)
                from agent.telegram import is_configured, notify_task_failed

                if is_configured():
                    await notify_task_failed(task.id, task.goal, task.error[:300])
                task.updated_at = datetime.now(UTC)
                logger.error("Task [%s] FAILED: %s", task.id, task.error[:200])
                untrust_task(task.id)
                return task

            # Use the most meaningful result (prefer the model's DONE summary
            # over raw tool output).
            best_result = None
            if task.steps:
                for s in reversed(task.steps):
                    if s.status == StepStatus.SUCCESS and s.result:
                        if not any(
                            kw in (s.tool_name or "")
                            for kw in ["write_file", "read_file", "list_directory"]
                        ):
                            best_result = s
                            break
                        if best_result is None:
                            best_result = s
            if outcome.summary:
                task.result = outcome.summary
            elif best_result:
                task.result = best_result.result
            elif task.steps:
                task.result = task.steps[-1].result or "Task completed"
            else:
                task.result = "Task completed"

            # Always append saved file locations so user knows where output went
            saved = []
            for s in task.steps:
                if (
                    s.status == StepStatus.SUCCESS
                    and s.result
                    and s.tool_name in ("write_file", "execute_code", "run_command")
                ):
                    # write_file: "Written X chars to output/..." , execute_code scaffold: "Project scaffold written to ..."
                    m = s.result.strip()
                    if "Written" in m and " to " in m:
                        # extract path after " to "
                        try:
                            path = m.split(" to ", 1)[1].splitlines()[0].strip().strip("'\"")
                            saved.append(path)
                        except Exception:
                            pass
                    elif "Project scaffold written to" in m:
                        try:
                            path = (
                                m.split("Project scaffold written to", 1)[1]
                                .splitlines()[0]
                                .strip()
                                .strip("'\"")
                            )
                            saved.append(path)
                        except Exception:
                            pass
                    elif "written to" in m.lower() and ("output/" in m or "projects/" in m):
                        saved.append(m.splitlines()[0][:120].strip())
            if saved:
                uniq = []
                for p in saved:
                    if p not in uniq:
                        uniq.append(p)
                task.result = (
                    task.result.rstrip() + "\n\n📁 Saved to:\n" + "\n".join(f"- {p}" for p in uniq)
                )
            # Partial failure: surface skipped steps instead of hiding them
            if failed:
                notes = []
                for s in failed[:5]:
                    label = (s.error or "unknown error").strip()[:120]
                    notes.append(f"- Step {s.order}: {label}")
                partial = (
                    f"\n\n⚠️ {len(failed)} of {len(task.steps)} step(s) failed and were skipped:\n"
                    + "\n".join(notes)
                )
                hint = _credential_hint("\n".join(s.error or "" for s in failed))
                if hint:
                    partial += "\n\n" + hint
                task.result = task.result.rstrip() + partial
            task.updated_at = datetime.now(UTC)

            # Memory policy: when gemini_full_control is True, Gemini decides
            # what to store, what filename to use, and what lessons to keep.
            # Deterministic gates remain as fallback when Gemini unavailable.
            from agent.config import settings as _settings

            if _settings.gemini_full_control and _settings.gemini_api_key:
                should_store = await _gemini_should_store(task)
                if should_store:
                    self.memory.save_task_outcome(task.goal, task.result[:200], success=True)
                    logger.info("Gemini decided to store task outcome for %s", task.id)
                # Gemini-driven extraction (preferences/decisions/memory hints)
                try:
                    extracted = await self.memory.gemini_extract_and_store(task.goal)
                except Exception:
                    logger.debug(
                        "Gemini memory extraction failed, falling back to regex", exc_info=True
                    )
                    extracted = self.memory.extract_and_store(task.goal)
                if extracted:
                    logger.info(
                        "Gemini auto-extracted %d durable fact(s) from task goal", extracted
                    )
            else:
                # Legacy deterministic policy
                if not _is_trivial(task) and len(task.steps) > 1:
                    successful = [
                        s for s in task.steps if s.status == StepStatus.SUCCESS and s.tool_name
                    ]
                    distinct = {s.tool_name for s in successful}
                    if len(successful) >= 2 and len(distinct) >= 2:
                        self.memory.save_task_outcome(task.goal, task.result[:200], success=True)
                extracted = self.memory.extract_and_store(task.goal)
                if extracted:
                    logger.info("Auto-extracted %d durable fact(s) from task goal", extracted)

            # Credit any injected procedural skills that were actually followed,
            # then try to grow the library from this task (self-evolution).
            for name in matched_skills:
                self.skills.record_use(name)
                logger.info("Skill '%s' used by task %s", name, task.id)
            await self._maybe_create_skill(task)

            await self._self_reflect(task)

            # Notify via Telegram
            from agent.telegram import is_configured, notify_task_completed

            if is_configured():
                await notify_task_completed(task.id, task.goal, task.result[:300])

            logger.info("Task [%s] completed successfully", task.id)
            untrust_task(task.id)
            return task

        except asyncio.CancelledError:
            logger.info("Task [%s] cancelled", task.id)
            task.status = TaskStatus.FAILED
            task.error = "Cancelled by user"
            untrust_task(task.id)
            raise
        except Exception as exc:
            logger.exception("Task [%s] failed with exception", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            untrust_task(task.id)
            from agent.config import settings as _fail_settings

            if _fail_settings.gemini_full_control and _fail_settings.gemini_api_key:
                try:
                    if await _gemini_should_store(task, is_failure=True):
                        self.memory.save_task_outcome(task.goal, str(exc)[:200], success=False)
                except Exception:
                    # fallback to deterministic gate
                    if not _is_trivial(task):
                        self.memory.save_task_outcome(task.goal, str(exc)[:200], success=False)
            else:
                if not _is_trivial(task):
                    self.memory.save_task_outcome(task.goal, str(exc)[:200], success=False)

            # Notify via Telegram
            from agent.telegram import is_configured, notify_task_failed

            if is_configured():
                await notify_task_failed(task.id, task.goal, str(exc)[:300])

            return task

    async def handle_goal(self, goal: str, context: dict[str, Any] | None = None) -> Task:
        """Create and handle a task from a goal string."""
        task = Task(goal=goal, context=context or {})
        return await self.handle_task(task)

    async def _maybe_create_skill(self, task: Task) -> None:
        """Auto-create a reusable skill from a solved multi-step task.

        Hermes' trigger heuristics (skill_manager_tool schema): complex task
        succeeded, non-trivial workflow discovered. We gate deterministically
        (>=2 successful steps of >=2 DISTINCT tools), then let Gemini write the
        SKILL.md, then enforce the same hard validation gates as manual creates.
        Failures are logged and swallowed — never break task completion.
        """
        if _is_trivial(task) or len(task.steps) < 2:
            return
        successful = [s for s in task.steps if s.status == StepStatus.SUCCESS]
        distinct_tools = {s.tool_name for s in successful if s.tool_name}
        if len(successful) < 2 or len(distinct_tools) < 2:
            logger.debug("Task %s too routine for skill creation", task.id)
            return

        # Dedup gate: skip if an existing skill already covers this procedure.
        probe = f"{task.goal} {(task.result or '')[:200]}"
        similar = self.skills.find_similar(probe)
        if similar:
            logger.info("Similar skill exists (%s); skipping auto-creation", similar[0]["name"])
            return

        try:
            from agent.core.gemini_client import generate_content

            steps_summary = "\n".join(
                f"- [{s.tool_name}] {s.description}: {(s.result or '')[:120]}"
                for s in successful[:6]
            )
            synthesis_prompt = f"""A task was just completed successfully. Decide whether it contains a
REUSABLE PROCEDURE worth saving as a skill for future similar tasks.

Goal: {task.goal}

Steps executed:
{steps_summary}

Final result: {(task.result or "")[:400]}

RULES:
- Skills must encode a REPEATABLE WORKFLOW (trigger + steps + pitfalls), NOT task-specific facts.
- If this was routine/one-off with nothing reusable, output exactly: NOTHING_TO_SAVE
- description MUST be <= 60 chars: 'Use when <trigger>. <behavior>.' One sentence.
- name MUST be lowercase-kebab-case slug.

Output ONLY the SKILL.md file:

---
name: <kebab-slug>
description: "<=60 chars, trigger first"
version: 1.0.0
---

# <Title>

## When to Use
<bullet triggers>

## Procedure
<numbered steps referencing real tools: web_search, fetch_url, run_command,
execute_code, read_file, write_file, github_*>

## Pitfalls
<what went wrong or could go wrong>"""

            response = await generate_content(
                system=(
                    "You are a self-evolving AI agent that distills solved tasks "
                    "into reusable markdown skills. Be extremely selective."
                ),
                user=synthesis_prompt,
                temperature=0.3,
                max_tokens=900,
            )
            if not response or "NOTHING_TO_SAVE" in response.upper():
                logger.debug("No skill synthesized from task %s", task.id)
                return

            content = response.strip()
            if content.startswith("```"):
                content = "\n".join(
                    line for line in content.splitlines() if not line.strip().startswith("```")
                ).strip()

            meta, _body = self.skills.split_frontmatter(content)
            name = self.skills.create(
                name=str(meta.get("name") or task.goal),
                content=content,
                actor="agent",
                created_by="agent",
                origin_task=task.id,
            )
            logger.info("Auto-created skill '%s' from task %s", name, task.id)
        except Exception:
            logger.warning(
                "Skill auto-creation failed for task %s (non-critical)", task.id, exc_info=True
            )

    async def _self_reflect(self, task: Task) -> None:
        """Post-task reflection — Gemini decides what is worth remembering.

        When ``gemini_full_control`` is on, Gemini judges novelty against
        existing lessons instead of the heuristic Jaccard overlap.
        """
        from agent.config import settings as _s

        if _s.gemini_full_control and _s.gemini_api_key:
            # Let Gemini decide triviality itself via the prompt; no early exit
            pass
        elif _is_trivial(task):
            return

        from agent.core.gemini_client import generate_content

        success = task.status == TaskStatus.COMPLETED
        reflection_prompt = f"""You just completed a task. Extract ONLY genuinely new, actionable lessons.

Goal: {task.goal}
Success: {success}
Result: {(task.result or task.error or "N/A")[:500]}
Steps taken: {len(task.steps)}

RULES:
- Only output lessons that are SPECIFIC and REUSABLE for future similar tasks
- Do NOT save generic advice like "plan carefully" or "check inputs"
- Do NOT save task-specific details like filenames or URLs
- If this was a routine task with no new insights, say exactly: NOTHING_TO_SAVE
- Each lesson should be a single sentence, under 20 words

Examples of GOOD lessons:
- "When searching for news, always include the year in the query"
- "JSON extraction from HTML needs to handle escaped quotes"
- "The fetch_url tool strips HTML tags automatically, no need for manual cleanup"

Examples of BAD lessons (don't save these):
- "Task completed successfully" (not actionable)
- "Used web_search and summarize tools" (just describing what happened)
- "Always double-check file paths" (too generic)

Output 0-2 lessons, or NOTHING_TO_SAVE."""

        try:
            reflection = await generate_content(
                system="You are a self-improving AI agent. Only extract genuinely new lessons. Be extremely selective.",
                user=reflection_prompt,
                temperature=0.2,
                max_tokens=200,
            )

            # Sanitize: prompt echoes and mid-sentence truncations previously
            # leaked into LESSONS context and contaminated future planning.
            cleaned = _clean_lessons(reflection)
            if not cleaned:
                logger.debug("No usable lessons from task %s", task.id)
                return

            # Check for novelty against existing reflections
            existing = [e.content for e in self.memory.get_by_category("reflection")]
            joined = "\n".join(cleaned)
            if _is_novel_reflection(joined, existing):
                self.memory.save_reflection(joined)
                logger.info("New lesson saved from task %s", task.id)
            else:
                logger.debug("Reflection not novel, skipping for task %s", task.id)

        except Exception:
            logger.debug("Self-reflection failed (non-critical)")

    def get_status(self) -> dict[str, Any]:
        """Return current orchestrator status."""
        return {
            "memory_size": self.memory.size,
            "available_tools": list_tools(),
            "running": self._running,
        }


orchestrator = Orchestrator()
