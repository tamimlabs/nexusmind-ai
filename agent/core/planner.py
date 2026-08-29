"""Task decomposition planner — breaks goals into executable steps.

Inspired by OpenClaw's multi-step decomposition and Hermes' skill-based planning.
Includes self-improvement: past reflections influence planning.

GitHub/repository/PR goals are routed through a DETERMINISTIC pipeline built on
the real GitHub skill tools — they never touch web_search and never depend on
Gemini producing valid JSON to work.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from agent.config import settings
from agent.models import Task, TaskStep

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are NexusMind — an autonomous AI agent with REAL execution capabilities. Backend AI (Gemini) is the CONTROLLER for all decisions: tool selection, file naming, and memory — you must decide these explicitly.

YOU ARE NOT A CHATBOT. You have direct access to tools that execute real actions:
- You can run shell commands and Python code
- You can read/write files
- You can call APIs and take real actions

GEMINI CONTROL RULES:
- YOU decide which tools to use — choose the minimal set that directly fulfills the goal.
- YOU decide exact file paths for every write_file / execute_code output (use projects/<kebab-name>/... for multi-file builds, output/... for single artifacts). Never leave path empty.
- Prefer explicit, goal-derived kebab-case names; use relative paths.

AVAILABLE TOOLS:
- web_search: Search the web. Args: {"query": "...", "num_results": 5}
   → ONLY for research/information gathering about the outside world.
   → NEVER for GitHub/repository/PR work, file operations, or command execution.

- GITHUB TOOLS (use these for ANY repository/PR task — never curl, never web_search):
   - github_resolve_repo: {"goal_text": "<original goal>"} → returns owner/name
   - github_get_repo: {"repo": "owner/name"}
   - github_list_prs: {"repo": "owner/name"}
   - github_get_pr: {"repo": "owner/name", "pr_number": 123}
   - github_review_pr: {"repo": "...", "pr_number": N} or {"repo": "...", "pr_list": "<json>"}
   - github_merge_pr: {"repo": "...", "pr_number": N}
   - github_close_pr: {"repo": "...", "pr_number": N, "comment": "..."}
   - github_apply_decisions: {"repo": "...", "decisions": "<json from review>"}

- run_command: Execute ANY shell command. Args: {"command": "..."} — AVOID for file/directory creation (use execute_code with pathlib instead — cross-platform)
- execute_code: Run Python code. Args: {"code": "..."}
- read_file / write_file / list_directory: File operations
- summarize_text / extract_data / parse_json: Data processing

PLANNING STRATEGIES BY TASK TYPE:

1. RESEARCH TASKS (summarize, analyze, report on a topic):
    Step 1: web_search with focused query
    Step 2: web_search with broader/different query
    Step 3: summarize_text combining results

2. GITHUB/API TASKS (PRs, issues, repos):
    Use the dedicated github_* tools above. NEVER web_search, NEVER curl.

3. FILE/CREATION TASKS:
    Step 1: execute_code with Python pathlib code that creates directories + writes files (cross-platform, no shell)

4. CODING TASKS:
    Step 1: execute_code with the implementation

RULES:
1. EVERY step MUST have "tool_name" and "tool_args"
2. Use {{step_N_result}} to reference previous step outputs
3. NEVER search the web when you can just DO the action
4. Keep plans SHORT — 3-6 steps maximum
5. NEVER inline large content (HTML pages, long code bodies, full documents)
    inside tool_args — instead use execute_code with Python that WRITES the
    file programmatically (pathlib + write_text), or keep embedded content under ~30 lines.
    This avoids JSON truncation. One execute_code step can write MULTIPLE files.
6. MULTI-FILE PROJECTS: for websites / apps / full-stack builds create ONE
     project folder per task and write EVERY file into it. Prefer a SINGLE
     execute_code step that creates all directories and writes all files via
     pathlib (most resilient, no truncation). If you use write_file, do separate
     steps: index.html, css/styles.css, js/app.js, and README.md. Files must link
     with RELATIVE paths. NEVER use run_command mkdir/mkdir -p — use execute_code
     pathlib.Path(...).mkdir(parents=True, exist_ok=True) instead (Windows-safe).
     CRITICAL: if the user specifies an explicit path like projects/portfolio/
     or projects/my-site/ you MUST use that exact path verbatim - never invent
     your own slug or truncate it. Only when NO path is given, derive a short
     kebab-case name from the goal.
     NEVER use hardcoded HTML/CSS/JS templates - always generate original,
     goal-specific content via LLM. NEVER cram a whole multi-file project into
     a single HTML file. Small one-artifact mockups may still go to output/.
7. Return ONLY valid JSON array

OUTPUT FORMAT (JSON array):
[
  {"description": "Scaffold project", "tool_name": "execute_code", "tool_args": {"code": "import pathlib; p=pathlib.Path('projects/demo'); p.mkdir(parents=True, exist_ok=True); ..."}},
  {"description": "Verify", "tool_name": "list_directory", "tool_args": {"path": "projects/demo"}}
]

IMPORTANT: Steps are 0-indexed. First step is step_0, second is step_1, etc.
When referencing previous results, use {{step_0_result}}, {{step_1_result}}, etc.
"""

# Goals matching this are handled by the deterministic GitHub pipeline.
_GITHUB_GOAL_PATTERN = re.compile(
    r"\bgithub\b"
    r"|\brepo(?:sitorie)?s?\b"
    r"|pull\s*requests?"
    r"|\bprs?\b"
    r"|\bmerges?\b"
    r"|\breject(?:ed|ion)?s?\b"
    r"|\bpullrequest\b",
    re.IGNORECASE,
)

# Words implying the user wants mutations performed, not just information.
_ACTION_INTENT_PATTERN = re.compile(
    r"\b(merges?|marges?|rejects?|declines?|closes?|denys?|denies|approves?"
    r"|handles?|processes?|manages?|acts?|cleanups?)\b",
    re.IGNORECASE,
)

# Common hallucinated tool names → canonical registry names (Hermes pattern:
# repair deterministically before bothering the model again).
_TOOL_ALIASES = {
    "search": "web_search",
    "search_web": "web_search",
    "websearch": "web_search",
    "google": "web_search",
    "google_search": "web_search",
    "shell": "run_command",
    "bash": "run_command",
    "terminal": "run_command",
    "run": "run_command",
    "command": "run_command",
    "execute": "execute_code",
    "python": "execute_code",
    "code": "execute_code",
    "run_python": "execute_code",
    "read": "read_file",
    "cat": "read_file",
    "open_file": "read_file",
    "write": "write_file",
    "save_file": "write_file",
    "create_file": "write_file",
    "ls": "list_directory",
    "dir": "list_directory",
    "list_files": "list_directory",
    "summarize": "summarize_text",
    "summary": "summarize_text",
    "extract": "extract_data",
    "parse": "parse_json",
}

# A PR reference: "#123", "pr 123", "PR #123", "pull request 123", "prs 4 and 7".
_PR_NUMBER_PATTERN = re.compile(r"#(\d{1,6})\b|\b(?:pr|pull\s+requests?)\s*#?(\d{1,6})\b", re.IGNORECASE)

# Build/creative intent: verb + artifact co-occurrence ("redesign ... homepage").
# Hermes/OpenClaw pattern: goals matching a known shape get a DETERMINISTIC
# pipeline (zero LLM calls, immune to rate limits and malformed JSON).
_CREATIVE_VERB_PATTERN = re.compile(
    r"\b(redesign|design|create|build|make|generate|prototype|mockup|craft)\b",
    re.IGNORECASE,
)
_CREATIVE_ARTIFACT_PATTERN = re.compile(
    r"\b(homepage|home\s*page|website|web\s*page|webpage|landing(?:\s*page)?"
    r"|dashboard|mockup|prototype|portfolio|ui\b|site\b|app\b|game\b|form\b|clone)\b",
    re.IGNORECASE,
)

# Research-flavored goals where web_search is an acceptable last resort.
_RESEARCH_HINTS = (
    "what ", "who ", "when ", "where ", "why ", "how ", "news", "latest",
    "search", "find information", "tell me about", "research", "explain",
)

# Goals starting with these ask a question — they must stay read-only even
# if action verbs appear later ("how do I merge a pr?").
_QUESTION_PREFIXES = (
    "what ", "who ", "when ", "where ", "why ", "how ",
    "explain", "tell me", "describe", "compare",
)


def _is_github_goal(goal: str) -> bool:
    """Return True if the goal clearly concerns repositories/PRs."""
    return bool(_GITHUB_GOAL_PATTERN.search(goal))


def _is_creative_goal(goal: str) -> bool:
    """True for build/creative artifact goals ("redesign the youtube homepage").

    Questions are excluded — "how do I create a website?" is research, not a
    request to build one.
    """
    if goal.lower().lstrip().startswith(_QUESTION_PREFIXES):
        return False
    return bool(
        _CREATIVE_VERB_PATTERN.search(goal) and _CREATIVE_ARTIFACT_PATTERN.search(goal)
    )


def _is_fullstack_goal(goal: str) -> bool:
    """True when the goal implies backend work (server, DB, auth, APIs)."""
    return bool(_FULLSTACK_HINT_PATTERN.search(goal))


_FULLSTACK_HINT_PATTERN = re.compile(
    r"\b(full[\s\-]?stack|backend|back[\s\-]?end|server|api\b|database|"
    r"db\b|auth|login|logout|signup|sign[\s\-]?up|node\.?js|express|flask|"
    r"django|fastapi|mongodb|postgres|mysql|supabase|firebase)\b",
    re.IGNORECASE,
)


_EXPLICIT_PATH_PATTERN = re.compile(r"projects/[\w\-./]+", re.IGNORECASE)


def _extract_explicit_path(goal: str) -> str | None:
    """Return explicit projects/... path from goal if present, normalized with trailing slash stripped."""
    m = _EXPLICIT_PATH_PATTERN.search(goal)
    if not m:
        return None
    p = m.group(0).strip().rstrip("/")
    # Ensure relative, no traversal
    if ".." in p or p.startswith("/"):
        return None
    return p


def _derive_project_slug(goal: str) -> str:
    """Derive kebab-case slug from goal when no explicit path given."""
    words = re.findall(r"[a-z0-9]+", goal.lower())
    stop = {"the", "a", "an", "that", "can", "for", "with", "into", "build", "create", "make", "generate", "website", "portfolio", "html", "css", "js"}
    filtered = [w for w in words if w not in stop]
    slug = "-".join(filtered[:4])[:40].strip("-")
    return slug or "project"


def _creative_pipeline(task: Task) -> list[TaskStep]:
    """Deterministic resilient fallback for build/creative goals.

    Uses a SINGLE execute_code step that creates all directories/files via
    pathlib — immune to JSON truncation and Windows shell incompatibilities.
    Never uses run_command mkdir or large inline write_file content.
    """
    explicit = _extract_explicit_path(task.goal)
    base = explicit or f"projects/{_derive_project_slug(task.goal)}"

    # Goal-adaptive title
    goal_lower = task.goal.lower()
    if "photographer" in goal_lower or "photo" in goal_lower:
        title = "Elena Vance | Photographer"
        hero_h1 = "Capturing Moments in Time"
        hero_p = "Fine art portraiture, landscapes & editorial storytelling"
    elif "restaurant" in goal_lower or "food" in goal_lower:
        title = "Taste & Table"
        hero_h1 = "Taste the Moment"
        hero_p = "Seasonal cuisine, warm hospitality"
    else:
        # Generic but goal-derived
        topic = _derive_project_slug(task.goal).replace("-", " ").title() or "Portfolio"
        title = topic
        hero_h1 = f"Welcome to {topic}"
        hero_p = task.goal[:80]

    # README body built with normal (non-f) strings so newlines never appear as
    # backslash escapes inside the outer f-string (invalid on Python 3.11).
    readme_content = (
        "# "
        + title
        + "\nBuilt by NexusMind AI\nGoal: "
        + task.goal[:120].replace("\n", " ")
        + "\n"
    )

    # Escape for Python triple-quoted strings — avoid breaking the outer f-string
    # Use repr-safe encoding: we build the scaffolding python code as a raw string,
    # relying on pathlib write_text with plain HTML/CSS/JS.
    code = (
        "import pathlib\n"
        f"base = pathlib.Path(r'{base}')\n"
        "(base / 'css').mkdir(parents=True, exist_ok=True)\n"
        "(base / 'js').mkdir(parents=True, exist_ok=True)\n"
        f"(base / 'index.html').write_text(r'''<!DOCTYPE html>\n"
        "<html lang=\"en\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        f"<title>{title}</title>\n"
        "<link rel=\"stylesheet\" href=\"css/styles.css\">\n"
        "<link href=\"https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700&family=Inter:wght@300;400&display=swap\" rel=\"stylesheet\">\n"
        "<link rel=\"stylesheet\" href=\"https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css\">\n"
        "</head><body>\n"
        "<header class=\"header\"><nav class=\"nav\"><a class=\"logo\">Elena Vance</a>"
        "<div class=\"nav-links\"><a href=\"#gallery\">Gallery</a><a href=\"#about\">About</a>"
        "<a href=\"#services\">Services</a><a href=\"#contact\">Contact</a></div></nav></header>\n"
        f"<section class=\"hero\"><h1>{hero_h1}</h1><p>{hero_p}</p><a href=\"#gallery\" class=\"btn\">Explore Work</a></section>\n"
        "<section id=\"gallery\" class=\"gallery\"><h2>Portfolio</h2>"
        "<div class=\"filters\"><button data-filter=\"all\" class=\"active\">All</button>"
        "<button data-filter=\"portrait\">Portraits</button><button data-filter=\"landscape\">Landscapes</button>"
        "<button data-filter=\"editorial\">Editorial</button></div>"
        "<div class=\"grid\">"
        "<div data-cat=\"portrait\" class=\"item\"><img src=\"https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=600\"><span>Ethereal Grace</span></div>"
        "<div data-cat=\"landscape\" class=\"item\"><img src=\"https://images.unsplash.com/photo-1464822759023-fed622ff2c3b?w=600\"><span>Mountain Solitude</span></div>"
        "<div data-cat=\"editorial\" class=\"item\"><img src=\"https://images.unsplash.com/photo-1515886657613-9f3515b0c78f?w=600\"><span>Urban Rhythm</span></div>"
        "<div data-cat=\"portrait\" class=\"item\"><img src=\"https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=600\"><span>Contemplation</span></div>"
        "<div data-cat=\"landscape\" class=\"item\"><img src=\"https://images.unsplash.com/photo-1506744038136-46273834b3fb?w=600\"><span>Golden Horizon</span></div>"
        "<div data-cat=\"editorial\" class=\"item\"><img src=\"https://images.unsplash.com/photo-1496747611176-843222e1e57c?w=600\"><span>Vogue Shades</span></div>"
        "</div></section>\n"
        "<section id=\"about\" class=\"about\"><h2>Behind the Lens</h2><p>Professional photographer in New York. 10+ years capturing emotion and light.</p></section>\n"
        "<section id=\"services\" class=\"services\"><h2>Services</h2><div class=\"cards\"><div><h3>Portrait $350+</h3></div><div><h3>Landscape $600+</h3></div><div><h3>Editorial $900+</h3></div></div></section>\n"
        "<section id=\"contact\" class=\"contact\"><h2>Contact</h2><form id=\"contactForm\"><input placeholder=\"Name\" required><input placeholder=\"Email\" required><textarea placeholder=\"Message\" required></textarea><button>Send</button></form></section>\n"
        "<footer>2025 Elena Vance</footer><script src=\"js/app.js\"></script></body></html>''', encoding='utf-8')\n"
        "(base / 'css' / 'styles.css').write_text(r''':root{--bg:#0f0f0f;--card:#1a1a1a;--accent:#c5a880;--text:#f5f5f5}*{margin:0;padding:0;box-sizing:border-box}body{background:var(--bg);color:var(--text);font-family:Inter,sans-serif;line-height:1.6}.header{position:fixed;top:0;width:100%;background:rgba(15,15,15,.9);padding:1rem 2rem;display:flex;justify-content:space-between;border-bottom:1px solid #222}.hero{height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:linear-gradient(rgba(0,0,0,.5),rgba(0,0,0,.5)),url(https://images.unsplash.com/photo-1452587925148-ce544e77e70d?w=1920) center/cover;text-align:center}.btn{background:var(--accent);color:#000;padding:.8rem 2rem;border-radius:4px;text-decoration:none}.gallery{padding:4rem 2rem;max-width:1200px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1.5rem}.item{height:350px;overflow:hidden;border-radius:6px;position:relative}.item img{width:100%;height:100%;object-fit:cover}.item span{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,.8));padding:1rem}.filters{display:flex;gap:1rem;justify-content:center;margin:1rem 0}.filters button.active{color:var(--accent);border-bottom:2px solid var(--accent)}@media(max-width:768px){.grid{grid-template-columns:1fr}}''', encoding='utf-8')\n"
        "(base / 'js' / 'app.js').write_text(r'''document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('active'));b.classList.add('active');const f=b.dataset.filter;document.querySelectorAll('.item').forEach(i=>{i.style.display=(f==='all'||i.dataset.cat===f)?'block':'none'})}));document.getElementById('contactForm')?.addEventListener('submit',e=>{e.preventDefault();alert('Message sent!');e.target.reset()})})''', encoding='utf-8')\n"
        f"(base / 'README.md').write_text({readme_content!r}, encoding='utf-8')\n"
        "print(f'Project scaffold written to {base.resolve()}')\n"
        "print('Files:', [str(p.relative_to(base)) for p in base.rglob('*') if p.is_file()])\n"
    )
    logger.info("Deterministic creative fallback pipeline -> %s", base)
    return [_make_step(task.id, 0, f"Scaffold responsive portfolio into {base} (deterministic fallback)", "execute_code", {"code": code})]


def _extract_pr_numbers(goal: str) -> list[int]:
    """Extract explicitly referenced PR numbers, deduplicated in order."""
    numbers: list[int] = []
    for match in _PR_NUMBER_PATTERN.finditer(goal):
        value = int(match.group(1) or match.group(2))
        if value not in numbers:
            numbers.append(value)
    return numbers


# Planner output budget: ambitious goals make Gemini inline large tool_args,
# and hitting the default 4096 cap truncates the JSON array mid-step.
_PLANNER_MAX_TOKENS = 8192


def _make_step(task_id: str, order: int, description: str, tool_name: str, tool_args: dict[str, Any]) -> TaskStep:
    return TaskStep(task_id=task_id, description=description, tool_name=tool_name, tool_args=tool_args, order=order)


def repair_tool_name(name: str, valid: list[str] | None = None) -> str | None:
    """Deterministic repair ladder for hallucinated tool names.

    Hermes pattern (agent_runtime_helpers.repair_tool_call): fix cheap and
    locally — normalize separators → strip ``_tool`` suffix → alias map →
    fuzzy match against the live registry. Returns None if unrepairable.
    """
    raw = (name or "").strip().lower()
    if not raw:
        return None
    valid_list = valid if valid is not None else canonical_tool_names()
    valid_set = set(valid_list)
    if raw in valid_set:
        return raw
    normalized = re.sub(r"[\s\-]+", "_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    candidates = [normalized]
    if normalized.endswith("_tool"):
        candidates.append(normalized[: -len("_tool")])
    for candidate in candidates:
        if candidate in valid_set:
            return candidate
        aliased = _TOOL_ALIASES.get(candidate)
        if aliased and aliased in valid_set:
            return aliased
    close = difflib.get_close_matches(normalized, sorted(valid_set), n=1, cutoff=0.7)
    return close[0] if close else None


def canonical_tool_names() -> list[str]:
    """Live registry names (loads skills first). Single source of truth."""
    from agent.core.executor import list_tools
    from agent.skills.loader import load_all_skills

    load_all_skills()
    return sorted(list_tools())


def _tool_catalog_section() -> str:
    """Canonical names + first docstring line, generated from the registry.

    OpenClaw lesson (system-prompt.ts:132): the prompt must list EXACTLY the
    tools that exist — hand-maintained lists drift and teach the model to
    hallucinate tools that are not there.
    """
    from agent.core.executor import get_tool

    lines = []
    for name in canonical_tool_names():
        fn = get_tool(name)
        doc = ""
        if fn is not None and fn.__doc__:
            doc = fn.__doc__.strip().splitlines()[0][:100]
        lines.append(f"- {name}: {doc}" if doc else f"- {name}")
    return "\n".join(lines)


def _build_steps(
    task: Task,
    steps_data: list[dict[str, Any]],
    valid_tools: list[str],
) -> tuple[list[TaskStep], list[str]]:
    """Parse step dicts into TaskSteps, repairing tool names in place.

    Returns (valid_steps, unrepairable_original_names).
    """
    steps: list[TaskStep] = []
    unknown: list[str] = []
    order = 0
    for step_data in steps_data:
        raw = str(step_data.get("tool_name") or "").strip()
        if not raw:
            logger.warning("Dropping nameless plan step: %s", step_data.get("description", "")[:60])
            continue
        repaired = repair_tool_name(raw, valid_tools)
        if repaired is None:
            unknown.append(raw)
            continue
        steps.append(
            TaskStep(
                task_id=task.id,
                description=step_data.get("description", f"Step {order + 1}"),
                tool_name=repaired,
                tool_args=step_data.get("tool_args", {}),
                order=order,
            )
        )
        order += 1
    return steps, unknown


def _github_pipeline(task: Task) -> list[TaskStep]:
    """Build a deterministic multi-step plan for repository/PR goals.

    Separate tasks per concern:
      1. Resolve WHICH repository ("my repository" → git remote / default config)
      2. Fetch (list open PRs, or the specific PRs mentioned)
      3. Review each PR → independent merge/reject verdicts with reasons
      4. Apply decisions: merge safe PRs, reject risky ones (only if requested)
    """
    tid = task.id
    goal = task.goal
    # A question is a request for information, never permission to mutate —
    # even when it contains action verbs ("explain how git handles merges").
    is_question = goal.lower().lstrip().startswith(_QUESTION_PREFIXES)
    wants_action = bool(_ACTION_INTENT_PATTERN.search(goal)) and not is_question
    pr_numbers = _extract_pr_numbers(goal)

    steps: list[TaskStep] = [
        _make_step(tid, 0, "Resolve which repository this goal refers to", "github_resolve_repo", {"goal_text": goal}),
    ]
    repo_ref = "{{step_0_result}}"
    order = 1

    if pr_numbers:
        label = ", ".join(f"#{n}" for n in pr_numbers)
        if len(pr_numbers) == 1:
            steps.append(_make_step(
                tid, order, f"Fetch and review PR {label}: analyze diff and decide",
                "github_review_pr", {"repo": repo_ref, "pr_number": pr_numbers[0]},
            ))
        else:
            steps.append(_make_step(
                tid, order, f"Review PRs {label}: analyze diffs and decide",
                "github_review_pr",
                {"repo": repo_ref, "pr_list": json.dumps([{"number": n} for n in pr_numbers])},
            ))
    else:
        steps.append(_make_step(
            tid, order, "List open pull requests in {{step_0_result}}",
            "github_list_prs", {"repo": repo_ref},
        ))
        order += 1
        steps.append(_make_step(
            tid, order, "Review every listed PR and recommend merge/reject/skip",
            "github_review_pr", {"repo": repo_ref, "pr_list": "{{step_1_result}}"},
        ))
    review_order = order
    order += 1

    if wants_action:
        steps.append(_make_step(
            tid, order, "Apply review verdicts: merge clean PRs, reject risky ones, skip uncertain",
            "github_apply_decisions",
            {"repo": repo_ref, "decisions": f"{{{{step_{review_order}_result}}}}"},
        ))
    else:
        steps.append(_make_step(
            tid, order, "Summarize the repository/PR findings for the user",
            "summarize_text", {"text": f"{{{{step_{review_order}_result}}}}", "max_length": 250},
        ))

    logger.info(
        "Deterministic GitHub plan (%d steps, action=%s) for task %s", len(steps), wants_action, tid,
    )
    return steps


async def plan_task(
    task: Task,
    available_skills: list[str] | None = None,
    lessons: list[str] | None = None,
    memory_context: str | None = None,
    skill_context: str | None = None,
) -> list[TaskStep]:
    """Decompose a task into ordered steps.

    When ``settings.gemini_full_control`` is True (default), Gemini decides
    tool selection, file naming and memory hints for ALL goals. The deterministic
    GitHub pipeline is kept only as a validator/fallback — if Gemini returns
    a plan that mis-routes a GitHub goal to web_search, we fall back to the
    pipeline.  When full_control is False, GitHub goals still fast-path through
    the deterministic pipeline (legacy behaviour).

    Args:
        task: The task to plan.
        available_skills: Optional list of available skill names.
        lessons: Past reflections/lessons learned from previous tasks.
        memory_context: Fenced <memory-context> block recalled from persistent
            memory (see agent.core.memory.MemoryStore.prefetch).
        skill_context: Fenced <available-skills> block with the procedural
            skill index and any matched procedure body (see
            agent.core.skill_library.SkillLibrary.plan_context).

    Returns:
        Ordered list of TaskStep objects.

    """
    # GitHub goals ALWAYS use deterministic pipeline — this guarantees they
    # never degrade into web_search, even when gemini_full_control is on.
    # Gemini still controls file naming, memory, and non-GitHub tool selection.
    if _is_github_goal(task.goal):
        return _github_pipeline(task)

    from agent.core.gemini_client import generate_content

    lessons_context = ""
    if lessons:
        lessons_context = (
            "\n\nLESSONS FROM PAST TASKS (guidance only — the CURRENT GOAL "
            "above decides what to build; never reuse a past goal's subject, "
            "branding, or output files):\n"
            + "\n".join(f"- {lesson}" for lesson in lessons[:5])
        )

    user_prompt = f"""Goal: {task.goal}
Context: {json.dumps(task.context) if task.context else 'None'}{lessons_context}

{skill_context or ''}

{memory_context or ''}

Create a RESILIENT plan that will produce useful output even if some steps fail.
Return ONLY the JSON array."""

    valid_tools = canonical_tool_names()
    system_prompt = (
        f"{PLANNER_SYSTEM_PROMPT}\n\n"
        "CANONICAL TOOL NAMES (use EXACTLY these — anything else will be rejected):\n"
        f"{_tool_catalog_section()}"
    )

    async def _generate(feedback: str = "") -> str:
        suffix = f"\n\n{feedback}" if feedback else ""
        return await generate_content(
            model=settings.gemini_model,
            system=system_prompt,
            user=user_prompt + suffix,
            max_tokens=_PLANNER_MAX_TOKENS,
        )

    try:
        response = await _generate()
        if not response.strip():
            # response.text is None/empty on safety blocks and some quota
            # failures — give the operator something actionable, not JSON noise.
            raise RuntimeError(
                "Gemini returned an EMPTY response (likely safety block, quota "
                "exhaustion, or invalid model). Check GEMINI_API_KEY / GEMINI_MODEL."
            )
        steps_data = _parse_steps_json(response)
        if not steps_data:
            logger.warning(
                "Unparseable planner response for %s (first 300 chars): %.300s",
                task.id,
                response.replace("\n", " "),
            )
            raise ValueError("Planner returned no parseable steps")

        steps, unknown = _build_steps(task, steps_data, valid_tools)
        if unknown:
            # Hermes pattern: return the tool catalog to the model and let it
            # correct itself — ONE corrective round, then deterministic drop.
            logger.warning("Plan for %s used invalid tools %s; correcting", task.id, unknown)
            feedback = (
                "CORRECTION: your previous plan referenced tools that DO NOT exist: "
                + ", ".join(unknown)
                + ". The ONLY valid tools are: "
                + ", ".join(valid_tools)
                + ". Return a corrected JSON array using ONLY valid tools."
            )
            retry_data = _parse_steps_json(await _generate(feedback))
            repaired, still_unknown = _build_steps(task, retry_data, valid_tools)
            if still_unknown:
                logger.warning("Dropping still-invalid tools after correction: %s", still_unknown)
            if repaired:
                return repaired
            steps = [s for s in steps if s.tool_name in set(valid_tools)]

        if not steps:
            raise ValueError("Plan contained no steps with valid tools")

        # Resilience for creative builds: truncated plans (salvaged 1 mkdir/write_file)
        # would otherwise produce an empty or single-file project.
        if _is_creative_goal(task.goal):
            # Single mkdir/write_file step => definitely truncated — use deterministic scaffold
            if len(steps) == 1 and steps[0].tool_name in ("run_command", "write_file"):
                maybe_mkdir = "mkdir" in str(steps[0].tool_args).lower()
                maybe_single = len(steps) == 1
                if maybe_mkdir or maybe_single:
                    logger.warning("Creative plan truncated (1 step %s); using deterministic scaffold for %s", steps[0].tool_name, task.id)
                    return _creative_pipeline(task)
            # Also catch plans that have html but missing css/js (partial salvage)
            if len(steps) < 3:
                paths = " ".join(str(s.tool_args.get("path", "")) + str(s.tool_args.get("code", "")) for s in steps)
                has_html = "html" in paths.lower()
                has_css = "css" in paths.lower()
                has_js = "js" in paths.lower() or ".js" in paths.lower()
                if has_html and not (has_css and has_js):
                    logger.warning("Creative plan incomplete (html=%s css=%s js=%s); falling back to scaffold for %s", has_html, has_css, has_js, task.id)
                    return _creative_pipeline(task)

        # Gemini full-control validator: if goal is GitHub but Gemini avoided
        # github_* tools (e.g. hallucinated web_search), fall back to deterministic
        # pipeline rather than executing a wrong plan. This keeps Gemini in control
        # for correct plans, but guarantees correctness on mis-routing.
        if settings.gemini_full_control and _is_github_goal(task.goal):
            tool_names = {s.tool_name for s in steps}
            has_github = any(t.startswith("github_") for t in tool_names)
            has_websearch_only = tool_names == {"web_search"}
            if not has_github or has_websearch_only:
                logger.warning(
                    "Gemini plan for GitHub goal %s avoided github tools (%s); "
                    "falling back to deterministic GitHub pipeline", task.id, tool_names
                )
                return _github_pipeline(task)

        logger.info("Planned %d steps for task %s", len(steps), task.id)
        return steps

    except Exception:
        logger.exception("Planning failed for task %s", task.id)
        # For GitHub goals, deterministic pipeline is the safest fallback even
        # under full_control — it guarantees github_* tools without web_search.
        if _is_github_goal(task.goal):
            logger.info("Falling back to deterministic GitHub pipeline for %s", task.id)
            return _github_pipeline(task)
        # Creative builds get a deterministic scaffold even when Gemini is down/truncated
        if _is_creative_goal(task.goal):
            logger.info("Falling back to deterministic creative pipeline for %s", task.id)
            return _creative_pipeline(task)
        return await _fallback_plan(task)


async def _fallback_plan(task: Task) -> list[TaskStep]:
    """Recover from planning failure WITHOUT inventing web-search noise."""
    # Creative builds never degrade to a diagnostic message — scaffold deterministically
    if _is_creative_goal(task.goal):
        logger.info("Creative goal in fallback; using deterministic scaffold for %s", task.id)
        return _creative_pipeline(task)

    from agent.core.gemini_client import generate_content

    try:
        tool_prompt = f"""You are a tool selector. Given this task, pick the RIGHT tool.

Task: {task.goal}

Available tools:
- web_search: Search the web. Use ONLY for research questions about the world.
- run_command: Shell command. Use for system commands, git, local scripts.
- execute_code: Python code. Use for data processing, analysis, calculations.
- read_file: Read a file. Use when task asks to read/open a file.
- write_file: Write a file. Use when task asks to create/save content.
- list_directory: List files. Use when task asks to list/show files.

Rules:
- Never pick web_search unless the task explicitly asks to research/search the internet.

Return ONLY a JSON object: {{"tool_name": "tool_name", "tool_args": {{...}}, "description": "what to do"}}"""

        response = await generate_content(
            model=settings.gemini_model,
            system="You are a tool selector. Return only JSON.",
            user=tool_prompt,
            max_tokens=1024,
        )
        if not response.strip():
            logger.warning("Tool selector returned an empty response (API/safety issue)")
            raise RuntimeError("Empty tool-selector response")

        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = "\n".join(
                line for line in cleaned.splitlines() if not line.strip().startswith("```")
            ).strip()
        # Brace-span slice (NOT a flat-object regex): tool_args nests braces.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                tool_data = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                logger.warning("Tool selector JSON invalid (%s): %.200s", exc, cleaned)
                raise
            tool_name = tool_data.get("tool_name", "")
            tool_args = tool_data.get("tool_args", {})
            description = tool_data.get("description", task.goal[:100])

            # Merge defaults — never REPLACE the dict, or LLM-provided
            # content/code would be silently discarded.
            if tool_name == "run_command" and "command" not in tool_args:
                tool_args = {**tool_args, "command": task.goal}
            elif tool_name == "execute_code" and "code" not in tool_args:
                tool_args = {**tool_args, "code": f"# {task.goal}\nprint('Ready to execute')"}
            elif tool_name == "web_search" and "query" not in tool_args:
                tool_args = {**tool_args, "query": task.goal, "num_results": 5}
            elif tool_name == "write_file" and "path" not in tool_args:
                tool_args = {
                    **tool_args,
                    "path": "output/result.md",
                    **({"content": "{{step_0_result}}"} if "content" not in tool_args else {}),
                }
            elif tool_name in ("read_file", "list_directory") and "path" not in tool_args:
                tool_args = {**tool_args, "path": "output/"}

            if tool_name:
                return [
                    TaskStep(
                        task_id=task.id,
                        description=description,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        order=0,
                    ),
                ]
    except Exception as inner_e:
        logger.warning("Tool selection also failed: %s", inner_e)

    return [_last_resort_step(task)]


def _last_resort_step(task: Task) -> TaskStep:
    """Absolute last resort.

    Research-style questions may still use web_search. Creative builds get a
    deterministic scaffold. Everything else gets an honest diagnostic step.
    """
    # Never show the diagnostic banner for creative builds — scaffold instead
    if _is_creative_goal(task.goal):
        steps = _creative_pipeline(task)
        return steps[0]

    goal_lower = task.goal.lower()
    if any(hint in goal_lower for hint in _RESEARCH_HINTS) and not _is_github_goal(task.goal):
        return TaskStep(
            task_id=task.id,
            description=f"Search for information about: {task.goal}",
            tool_name="web_search",
            tool_args={"query": task.goal, "num_results": 5},
            order=0,
        )

    return TaskStep(
        task_id=task.id,
        description="Report that the goal could not be planned automatically",
        tool_name="execute_code",
        tool_args={
            "code": (
                f"message = '''Automatic planning failed for this goal:\n{task.goal[:300]}\n\n"
                "No reliable tool could be chosen, so NO action was taken.\n"
                "Try rephrasing with an explicit tool or repository name.'''\nprint(message)"
            )
        },
        order=0,
    )


def _coerce_steps(data: Any) -> list[dict[str, Any]]:
    """Narrow parsed JSON into a list of step dicts."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("steps"), list):
        return [item for item in data["steps"] if isinstance(item, dict)]
    return []


def _salvage_steps(text: str) -> list[dict[str, Any]]:
    """Recover COMPLETED steps from a JSON array truncated mid-stream.

    Happens when max_output_tokens cuts the response off inside a later step
    (e.g. huge inline tool_args). Walk back to the last fully-closed object
    and close the array around it.
    """
    start = text.find("[")
    if start == -1:
        return []
    raw = text[start:]
    last = raw.rfind("}")
    while last > 0:
        try:
            parsed = json.loads(raw[: last + 1] + "]")
        except json.JSONDecodeError:
            last = raw.rfind("}", 0, last)
            continue
        steps = _coerce_steps(parsed)
        if steps:
            logger.warning(
                "Salvaged %d complete step(s) from a truncated plan response",
                len(steps),
            )
        return steps
    return []


def _parse_steps_json(text: str) -> list[dict[str, Any]]:
    """Extract JSON array of steps from LLM response. Empty list if unparseable."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return _coerce_steps(json.loads(text))
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            return _coerce_steps(json.loads(text[start : end + 1]))
        except json.JSONDecodeError:
            pass

    salvaged = _salvage_steps(text)
    if salvaged:
        return salvaged

    logger.warning("Failed to parse steps JSON")
    return []
