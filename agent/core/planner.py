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

PLANNER_SYSTEM_PROMPT = """You are NexusMind — an autonomous AI agent with REAL execution capabilities.

YOU ARE NOT A CHATBOT. You have direct access to tools that execute real actions:
- You can run shell commands and Python code
- You can read/write files
- You can call APIs and take real actions

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

- run_command: Execute ANY shell command. Args: {"command": "..."}
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
   Step 1: write_file with the content

4. CODING TASKS:
   Step 1: execute_code with the implementation

RULES:
1. EVERY step MUST have "tool_name" and "tool_args"
2. Use {{step_N_result}} to reference previous step outputs
3. NEVER search the web when you can just DO the action
4. Keep plans SHORT — 3-6 steps maximum
5. NEVER inline large content (HTML pages, long code bodies, full documents)
   inside tool_args — instead use execute_code with Python that WRITES the
   file programmatically, or keep embedded content under ~30 lines
6. MULTI-FILE PROJECTS: for websites / apps / full-stack builds create ONE
   project folder per task — projects/<short-name>/ — and write EVERY file
   into it via separate steps: index.html, css/styles.css, js/app.js,
   backend file (e.g. server.py) when relevant, and README.md.
   Files must link each other with RELATIVE paths (e.g. css/styles.css).
   NEVER cram a whole multi-file project into a single HTML file.
   Small one-artifact mockups may still go to output/.
7. Return ONLY valid JSON array

OUTPUT FORMAT (JSON array):
[
  {"description": "Fetch data", "tool_name": "run_command", "tool_args": {"command": "..."}},
  {"description": "Analyze result", "tool_name": "execute_code", "tool_args": {"code": "..."}}
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

# Emitted into the generated script only for full-stack goals: a dependency-free
# stdlib dev server (static files + JSON API stub) as projects/<slug>/server.py.
_SERVER_PY_SNIPPET = (
    "\n"
    "server_py = '''"
    "\"\"\"Tiny stdlib dev server: static files + JSON API stub.\"\"\"\\n"
    "import http.server\\n"
    "import json\\n"
    "import pathlib\\n"
    "import socketserver\\n\\n"
    "ROOT = pathlib.Path(__file__).parent\\n\\n\\n"
    "class Handler(http.server.SimpleHTTPRequestHandler):\\n"
    "    def __init__(self, *args, **kwargs):\\n"
    "        super().__init__(*args, directory=str(ROOT), **kwargs)\\n\\n"
    "    def do_GET(self):\\n"
    "        if self.path.startswith('/api/'):\\n"
    "            body = json.dumps({'ok': True, 'endpoint': self.path}).encode()\\n"
    "            self.send_response(200)\\n"
    "            self.send_header('Content-Type', 'application/json')\\n"
    "            self.send_header('Content-Length', str(len(body)))\\n"
    "            self.end_headers()\\n"
    "            self.wfile.write(body)\\n"
    "        else:\\n"
    "            super().do_GET()\\n\\n\\n"
    "if __name__ == '__main__':\\n"
    "    with socketserver.TCPServer(('', 8000), Handler) as httpd:\\n"
    "        print('Serving on http://localhost:8000')\\n"
    "        httpd.serve_forever()\\n"
    "'''\\n"
    "(root / 'server.py').write_text(server_py, encoding='utf-8')\\n"
)


def _creative_pipeline(task: Task) -> list[TaskStep]:
    """Deterministic build plan for creative goals — no LLM dependency.

    Generates a complete project FOLDER under ``projects/<slug>/`` (HTML +
    CSS + JS + README, plus a stdlib ``server.py`` for full-stack goals), so
    a failed/rate-limited planner still ships a real multi-file artifact
    instead of the "No reliable tool could be chosen" dead end.
    """
    words = re.findall(r"[a-z0-9]+", task.goal.lower())
    slug = (
        "-".join(w for w in words if w not in {"the", "a", "an", "that", "can", "for", "with"})[:40]
        or "artifact"
    )
    title = (task.goal.strip()[:80] or "NexusMind Artifact").replace('"', "'")
    fullstack = _is_fullstack_goal(task.goal)

    files_block = (
        "(root / 'index.html').write_text(html.replace('NEXUSMIND_TITLE', title), encoding='utf-8')\n"
        "(root / 'css' / 'styles.css').write_text(css, encoding='utf-8')\n"
        "(root / 'js' / 'app.js').write_text(js, encoding='utf-8')\n"
        "(root / 'README.md').write_text(readme, encoding='utf-8')\n"
    )

    generator = (
        "from pathlib import Path\n"
        "import json\n\n"
        f"root = Path('projects') / json.loads({json.dumps(json.dumps(slug))})\n"
        "(root / 'css').mkdir(parents=True, exist_ok=True)\n"
        "(root / 'js').mkdir(parents=True, exist_ok=True)\n"
        f"title = json.loads({json.dumps(json.dumps(title))})\n\n"
        "html = '''<!DOCTYPE html>\n"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>NEXUSMIND_TITLE</title>\n"
        "<link rel=\"stylesheet\" href=\"css/styles.css\">\n"
        "</head><body>\n"
        "<header><div class=logo>&#9654;</div><input placeholder='Search'>"
        "<div style=margin-left:auto class=s>NexusMind &#10022;</div></header>\n"
        "<div class=chipbar id=chips></div>\n"
        "<div class=grid id=grid></div>\n"
        "<script src=\"js/app.js\"></script>\n"
        "</body></html>''';\n\n"
        "css = '''\n"
        ":root{--bg:#0f0f17;--card:#181827;--accent:#ff3d5a;--text:#e8e8f0;--muted:#9a9ab0}\n"
        "*{box-sizing:border-box;margin:0;padding:0}\n"
        "body{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;min-height:100vh}\n"
        "header{display:flex;align-items:center;gap:16px;padding:16px 28px;"
        "position:sticky;top:0;background:rgba(15,15,23,.92);backdrop-filter:blur(8px);z-index:10}\n"
        ".logo{width:38px;height:26px;border-radius:7px;background:var(--accent);"
        "display:grid;place-items:center;font-size:13px;color:#fff}\n"
        "input{flex:1;max-width:520px;padding:10px 18px;border-radius:999px;border:1px solid #2c2c40;"
        "background:var(--card);color:var(--text);outline:none}\n"
        ".chipbar{display:flex;gap:10px;padding:14px 28px;overflow-x:auto}\n"
        ".chip{padding:7px 15px;border-radius:999px;background:var(--card);font-size:13px;"
        "cursor:pointer;white-space:nowrap;border:1px solid transparent}\n"
        ".chip:hover,.chip.active{background:var(--accent);color:#fff}\n"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px;padding:6px 28px 40px}\n"
        ".thumb{aspect-ratio:16/9;border-radius:14px;position:relative;overflow:hidden;"
        "background:linear-gradient(135deg,#23233a,#151524)}\n"
        ".thumb::after{content:'';position:absolute;inset:0;background:"
        "radial-gradient(circle at 70% 30%,rgba(255,61,90,.35),transparent 55%)}\n"
        ".badge{position:absolute;right:10px;bottom:10px;background:rgba(0,0,0,.8);"
        "font-size:11px;padding:2px 7px;border-radius:5px;z-index:1}\n"
        ".meta{display:flex;gap:12px;margin-top:11px}\n"
        ".avatar{width:36px;height:36px;border-radius:50%;background:#26263c;flex-shrink:0}\n"
        ".t{font-size:14.5px;font-weight:600;line-height:1.35;margin-bottom:4px}\n"
        ".s{font-size:12.5px;color:var(--muted)}\n"
        "''';\n\n"
        "js = '''\n"
        "const chips=['All','Featured','New','Trending','Collections','Live'];\n"
        "chips.forEach((c,i)=>{const d=document.createElement('div');d.className='chip'+(i?'':' active');\n"
        "d.textContent=c;d.onclick=()=>document.querySelectorAll('.chip').forEach(x=>x.classList.remove('active'))||d.classList.add('active');\n"
        "document.getElementById('chips').appendChild(d)});\n"
        "const titles=['Concept one — hero section','Concept two — immersive grid',\n"
        "'Concept three — ambient mode','Concept four — focus feed','Concept five — gesture nav'];\n"
        "for(let i=0;i<12;i++){const t=titles[i%titles.length];\n"
        "document.getElementById('grid').insertAdjacentHTML('beforeend',\n"
        "`<div><div class=thumb><span class=badge>${(i+3)*2}:${i%6}0</span></div>\n"
        "<div class=meta><div class=avatar></div><div><div class=t>${i+1}. ${t}</div>\n"
        "<div class=s>NexusMind Labs &#8226; ${(i+1)*11}K views</div></div></div></div>`)};\n"
        "''';\n\n"
        "readme = '''# NEXUSMIND_TITLE\n\nGenerated by NexusMind AI.\n\n"
        "## Structure\\n"
        "- index.html - entry page\\n"
        "- css/styles.css - design system\\n"
        "- js/app.js - interactions\\n"
        + ("- server.py - stdlib dev server (python server.py)\\n" if fullstack else "")
        + "''';\n"
        "readme = readme.replace('NEXUSMIND_TITLE', title)\n"
        + (_SERVER_PY_SNIPPET if fullstack else "")
        + "\n" + files_block +
        "print('Project scaffold written to', root.resolve())\n"
    )

    logger.info(
        "Deterministic creative plan (projects/%s/%s) for task %s",
        slug, "+server.py" if fullstack else "", task.id,
    )
    extra = ", server.py" if fullstack else ""
    return [
        TaskStep(
            task_id=task.id,
            description=(
                f"Scaffold multi-file project: projects/{slug}/ "
                f"(index.html, css/styles.css, js/app.js, README.md{extra})"
            ),
            tool_name="execute_code",
            tool_args={"code": generator},
            order=0,
        ),
    ]


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

    GitHub/repository/PR goals bypass Gemini entirely and get a deterministic
    pipeline — this guarantees they never degrade into web searches.

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

        logger.info("Planned %d steps for task %s", len(steps), task.id)
        return steps

    except Exception:
        logger.exception("Planning failed for task %s", task.id)
        # Hermes/OpenClaw ladder: deterministic pipeline first (zero API calls),
        # then LLM tool-selection, then honest last resort.
        if _is_creative_goal(task.goal):
            return _creative_pipeline(task)
        return await _fallback_plan(task)


async def _fallback_plan(task: Task) -> list[TaskStep]:
    """Recover from planning failure WITHOUT inventing web-search noise."""
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

            if tool_name == "run_command" and "command" not in tool_args:
                tool_args = {"command": task.goal}
            elif tool_name == "execute_code" and "code" not in tool_args:
                tool_args = {"code": f"# {task.goal}\nprint('Ready to execute')"}
            elif tool_name == "web_search" and "query" not in tool_args:
                tool_args = {"query": task.goal, "num_results": 5}
            elif tool_name == "write_file" and "path" not in tool_args:
                tool_args = {"path": "output/result.md", "content": "{{step_0_result}}"}
            elif tool_name in ("read_file", "list_directory") and "path" not in tool_args:
                tool_args = {"path": "output/"}

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

    Research-style questions may still use web_search. Everything else gets an
    honest diagnostic step instead of a random web search full of noise.
    """
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
