"""Task decomposition planner — breaks goals into executable steps.

Inspired by OpenClaw's multi-step decomposition and Hermes' skill-based planning.
Includes self-improvement: past reflections influence planning.

GitHub/repository/PR goals are routed through a DETERMINISTIC pipeline built on
the real GitHub skill tools — they never touch web_search and never depend on
Gemini producing valid JSON to work.
"""

from __future__ import annotations

import difflib
import hashlib
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


# ── Goal-adaptive creative scaffold (deterministic fallback) ────────────────
# The resilient fallback must NOT render the same frozen site for every goal.
# A theme is picked from goal keywords, then brand / section copy / images /
# accent are all derived from the goal. Two DIFFERENT "build a website" tasks
# therefore produce recognizably DIFFERENT builds even when Gemini is down
# (regression: every creative goal used to collapse into one hardcoded
# photographer portfolio — deleting memory changed nothing).

_THEME_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("photographer", ("photograph", "photographer", "camera", "portrait", "lens")),
    ("restaurant", ("restaurant", "cafe", "coffee", "food", "bistro", "menu", "bakery")),
    ("tech", ("tech", "startup", "saas", "software", "app", "ai", "api", "developer", "code", "crypto", "web3", "blockchain", "data")),
    ("fitness", ("gym", "fitness", "workout", "train", "yoga", "wellness", "health", "coach")),
    ("ecommerce", ("shop", "store", "ecommerce", "e-commerce", "product", "commerce", "marketplace", "sell")),
    ("blog", ("blog", "news", "article", "magazine", "journal", "writing")),
    ("music", ("music", "band", "artist", "concert", "song", "album", "dj")),
    ("school", ("school", "edu", "academy", "course", "learn", "tutor", "class")),
    ("travel", ("travel", "tour", "trip", "hotel", "beach", "vacation")),
)


def _render(template: str, **values: str) -> str:
    """Replace ``{{key}}`` placeholders in a plain string (no f-string nesting)."""
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


_SITE_BASE_CSS = """*{margin:0;padding:0;box-sizing:border-box}body{background:var(--bg);color:var(--text);font-family:Inter,sans-serif;line-height:1.6}.header{position:fixed;top:0;width:100%;background:rgba(15,15,15,.9);padding:1rem 2rem;display:flex;justify-content:space-between;align-items:center;z-index:10;border-bottom:1px solid #222}.logo{font-family:'Playfair Display',serif;font-weight:700;font-size:1.3rem}.nav{display:flex;width:100%;justify-content:space-between}.nav-links{display:flex;gap:1.2rem}.nav-links a{color:var(--text);text-decoration:none;opacity:.85}.nav-links a:hover{color:var(--accent)}.hero{height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:linear-gradient(rgba(0,0,0,.55),rgba(0,0,0,.55)),url(__HERO_IMAGE__) center/cover;text-align:center;padding:0 1rem}.kicker{letter-spacing:.35em;text-transform:uppercase;color:var(--accent);font-size:.8rem;margin-bottom:1rem}.hero h1{font-family:'Playfair Display',serif;font-size:clamp(2.4rem,6vw,4.5rem);margin-bottom:1rem}.hero p{opacity:.9;max-width:640px;margin:0 auto 1.5rem}.btn{background:var(--accent);color:#000;padding:.8rem 2rem;border-radius:4px;text-decoration:none;font-weight:600}.section{padding:5rem 2rem;max-width:1200px;margin:auto}.section h2{font-family:'Playfair Display',serif;font-size:2.2rem;margin-bottom:2rem}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1.5rem}.item{height:320px;overflow:hidden;border-radius:6px;position:relative}.item img{width:100%;height:100%;object-fit:cover}.item span{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,.85));padding:.8rem 1rem}.filters{display:flex;gap:1rem;justify-content:center;margin-bottom:2rem;flex-wrap:wrap}.filters button{background:none;border:1px solid #444;color:var(--text);padding:.4rem 1rem;border-radius:99px;cursor:pointer}.filters button.active{background:var(--accent);color:#000;border-color:var(--accent)}.about{display:grid;grid-template-columns:1.2fr .8fr;gap:3rem;padding-bottom:0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1.5rem}.cards div{border:1px solid #262626;border-radius:8px;padding:1.5rem}.cards h3{color:var(--accent);margin-bottom:.5rem}.contact form{display:flex;flex-direction:column;gap:1rem;max-width:560px}.contact input,.contact textarea{background:#111;border:1px solid #333;color:var(--text);padding:.8rem;border-radius:6px;font:inherit}.contact form button{background:var(--accent);color:#000;border:none;padding:.9rem;border-radius:6px;font-weight:600;cursor:pointer}footer{padding:2rem;text-align:center;color:var(--text);opacity:.6;border-top:1px solid #222}@media(max-width:768px){.grid{grid-template-columns:1fr}.about{grid-template-columns:1fr}.nav-links{gap:.6rem;font-size:.85rem}}
"""

# (unsplash photo id, caption) — theme-specific so no two themes share imagery.
_THEME_DEFS: dict[str, dict[str, Any]] = {
    "photographer": {
        "brand": "{{topic}} Atelier",
        "kicker": "Photography Studio",
        "hero_h1": "Light is our language",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1534528741775-53994a69daeb", "{{topic}} series one"),
            ("photo-1464822759023-fed622ff2c3b", "{{topic}} series two"),
            ("photo-1515886657613-9f3515b0c78f", "{{topic}} series three"),
            ("photo-1507003211169-0a1dd7228f2d", "{{topic}} series four"),
            ("photo-1506744038136-46273834b3fb", "{{topic}} series five"),
            ("photo-1496747611176-843222e1e57c", "{{topic}} series six"),
        ],
        "about_h2": "Behind the lens",
        "about_p": "Independent {{topic}} photography practice — portrait, landscape and editorial storytelling since 2019.",
        "services": [
            ("Portrait session", "Hourly rate, curated contact sheet"),
            ("Editorial brief", "Concept, crew and art direction"),
            ("Print & gallery", "Archival printing and framing"),
        ],
        "bg": "#0f0f0f",
        "hero_img": "photo-1452587925148-ce544e77e70d",
    },
    "restaurant": {
        "brand": "{{topic}} Table",
        "kicker": "Kitchen & Bar",
        "hero_h1": "Seasonal, honest, memorable",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1517248135467-4c7edcad34c4", "{{topic}} plate one"),
            ("photo-1555396273-367ea4eb4db5", "{{topic}} plate two"),
            ("photo-1504674900247-0877df9cc836", "{{topic}} plate three"),
            ("photo-1414235077428-338989a2e8c0", "{{topic}} dining room"),
            ("photo-1556740738-b6a63e27c4df", "{{topic}} cocktails"),
            ("photo-1467003909585-2f8a72700288", "{{topic}} dessert"),
        ],
        "about_h2": "Our kitchen",
        "about_p": "{{topic}} cuisine built on local, seasonal produce — wood-fired, house-made, and always honest.",
        "services": [
            ("Tasting menu", "Seven courses, wine pairing"),
            ("Private dining", "Chef's table for up to 14"),
            ("Catering", "Events and studio dinners"),
        ],
        "bg": "#130b08",
        "hero_img": "photo-1414235077428-338989a2e8c0",
    },
    "tech": {
        "brand": "{{topic}} Systems",
        "kicker": "Software Studio",
        "hero_h1": "Ship {{topic}} products faster",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1555066931-4365d14bab8c", "{{topic}} dashboard"),
            ("photo-1517180102446-f3ece451e9d8", "{{topic}} terminal"),
            ("photo-1558494949-ef010cbdcc31", "{{topic}} infrastructure"),
            ("photo-1526374965328-7f61d4dc18c5", "{{topic}} pipeline"),
            ("photo-1522071820081-009f0129c71c", "{{topic}} team"),
            ("photo-1531403009284-440f080d1e12", "{{topic}} design"),
        ],
        "about_h2": "The studio",
        "about_p": "A focused {{topic}} engineering studio — design, build, scale, and operate software for small teams and ambitious founders.",
        "services": [
            ("Product sprints", "Two-week increments, shipped"),
            ("Cloud ops", "Kubernetes, terraform, observability"),
            ("AI engineering", "RAG, agents, evaluation loops"),
        ],
        "bg": "#0a0f14",
        "hero_img": "photo-1481487196290-c152efe083f5",
    },
    "fitness": {
        "brand": "{{topic}} Movement",
        "kicker": "Training Studio",
        "hero_h1": "Move better. Live stronger.",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1517836357463-d25dfeac3438", "{{topic}} strength"),
            ("photo-1541534741688-6078c6bfb5c5", "{{topic}} mobility"),
            ("photo-1571019613454-1cb2f99b2d8b", "{{topic}} endurance"),
            ("photo-1550345332-09e3ac987658", "{{topic}} session"),
            ("photo-1583454110551-21f2fa2afe61", "{{topic}} recovery"),
            ("photo-1549060279-7e168fcee0c2", "{{topic}} open air"),
        ],
        "about_h2": "The method",
        "about_p": "Evidence-based {{topic}} coaching for all levels — small classes, honest programming, and community that shows up.",
        "services": [
            ("Coaching plans", "12-week progressive programs"),
            ("Group classes", "Morning and evening sessions"),
            ("Nutrition basics", "Fuel for your training"),
        ],
        "bg": "#0f0f12",
        "hero_img": "photo-1517836357463-d25dfeac3438",
    },
    "ecommerce": {
        "brand": "{{topic}} Store",
        "kicker": "Independent Shop",
        "hero_h1": "Buy {{topic}} you'll love",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1441986300917-64674bd600d8", "{{topic}} item one"),
            ("photo-1442512595331-e89e73853f31", "{{topic}} item two"),
            ("photo-1505740420928-5e560c06d30e", "{{topic}} item three"),
            ("photo-1523275335684-37898b6baf30", "{{topic}} item four"),
            ("photo-1553062407-98eeb64c6a62", "{{topic}} item five"),
            ("photo-1542291026-7eec264c27ff", "{{topic}} item six"),
        ],
        "about_h2": "Why shop here",
        "about_p": "We source {{topic}} carefully, ship fast, and stand behind every order with a no-questions return policy.",
        "services": [
            ("Free shipping", "On orders over $60"),
            ("30-day returns", "No questions asked"),
            ("Loyalty", "Earn points on every order"),
        ],
        "bg": "#0e0d0d",
        "hero_img": "photo-1441986300917-64674bd600d8",
    },
    "blog": {
        "brand": "{{topic}} Journal",
        "kicker": "Words & Ideas",
        "hero_h1": "Notes on {{topic}}",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1499750310107-5fef28a66643", "{{topic}} essay one"),
            ("photo-1455390582262-044cdead277a", "{{topic}} essay two"),
            ("photo-1504711434969-e33886168f5c", "{{topic}} essay three"),
            ("photo-1434030216411-0b793f4b4173", "{{topic}} essay four"),
            ("photo-1516321318423-f06f85e504b3", "{{topic}} essay five"),
            ("photo-1488190211105-8b0e65b80b4e", "{{topic}} essay six"),
        ],
        "about_h2": "The editorial desk",
        "about_p": "Long-form {{topic}} writing — reported, edited, and published on a schedule that respects your time.",
        "services": [
            ("Weekly brief", "The signal, distilled"),
            ("Deep reads", "One big idea, properly argued"),
            ("Archive", "Search the full {{topic}} library"),
        ],
        "bg": "#0e0e0b",
        "hero_img": "photo-1504711434969-e33886168f5c",
    },
    "music": {
        "brand": "{{topic}} Sound",
        "kicker": "Artist Collective",
        "hero_h1": "Hear {{topic}} live",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1493225457124-a3eb161ffa5f", "{{topic}} stage one"),
            ("photo-1470225620780-dba8ba36b745", "{{topic}} live"),
            ("photo-1459749411175-04bf5292ceea", "{{topic}} studio"),
            ("photo-1514320291840-2e0a9bf2a9ae", "{{topic}} crowd"),
            ("photo-1510915361894-db8b60106cb1", "{{topic}} practice"),
            ("photo-1511671782779-c97d3d27a1d4", "{{topic}} session"),
        ],
        "about_h2": "The band",
        "about_p": "{{topic}} artists making records and playing rooms that matter — follow along for releases and dates.",
        "services": [
            ("Live shows", "Tour and residency dates"),
            ("New releases", "Singles, EPs and albums"),
            ("Merch", "Vinyl, tees and posters"),
        ],
        "bg": "#0f0b10",
        "hero_img": "photo-1470225620780-dba8ba36b745",
    },
    "school": {
        "brand": "{{topic}} Academy",
        "kicker": "Learning Platform",
        "hero_h1": "Learn {{topic}} properly",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1503676260728-1c00da094a0b", "{{topic}} course one"),
            ("photo-1524178232363-1fb2b075b655", "{{topic}} classroom"),
            ("photo-1516321318423-f06f85e504b3", "{{topic}} workshop"),
            ("photo-1509062522246-3755977927d7", "{{topic}} cohort"),
            ("photo-1580894732444-8ecded7900cd", "{{topic}} lab"),
            ("photo-1517245386807-bb43f82c33c4", "{{topic}} meetup"),
        ],
        "about_h2": "How we teach",
        "about_p": "Structured {{topic}} curriculum, real projects, and mentors who give feedback — learn by building.",
        "services": [
            ("Beginner track", "Foundations in 8 weeks"),
            ("Advanced cohort", "Project-based, portfolio-ready"),
            ("1:1 mentorship", "Weekly office hours"),
        ],
        "bg": "#0b0e11",
        "hero_img": "photo-1503676260728-1c00da094a0b",
    },
    "travel": {
        "brand": "{{topic}} Journeys",
        "kicker": "Travel Studio",
        "hero_h1": "Explore {{topic}}",
        "hero_p": "{{goal}}",
        "gallery": [
            ("photo-1488646953014-85cb44e25828", "{{topic}} destination one"),
            ("photo-1502920917128-1aa500764cbd", "{{topic}} destination two"),
            ("photo-1502602898657-3e91760cbb34", "{{topic}} destination three"),
            ("photo-1518684079-3c830dcef090", "{{topic}} destination four"),
            ("photo-1476514525535-07fb3b4ae5f1", "{{topic}} destination five"),
            ("photo-1507525428034-b723cf961d3e", "{{topic}} destination six"),
        ],
        "about_h2": "The itinerary",
        "about_p": "Curated {{topic}} trips — hand-picked stays, local guides, and itineraries that leave room to wander.",
        "services": [
            ("Small-group tours", "Max 12 travelers"),
            ("Private itineraries", "Built around you"),
            ("Local guides", "Community-first partners"),
        ],
        "bg": "#0b1013",
        "hero_img": "photo-1502920917128-1aa500764cbd",
    },
}

_ACCENT_PALETTE: tuple[str, ...] = (
    "#c5a880", "#e76f51", "#2a9d8f", "#457b9d", "#9b5de5",
    "#ef476f", "#00a8a8", "#f18f01", "#6a4e23", "#02735e",
    "#a71e34", "#5c6bc0",
)


def _goal_hash(goal: str) -> int:
    return int(hashlib.md5(goal.encode("utf-8")).hexdigest()[:8], 16)


def _pick_theme(goal: str) -> dict[str, Any]:
    goal_lower = goal.lower()
    for name, keywords in _THEME_KEYWORDS:
        if any(keyword in goal_lower for keyword in keywords):
            return _THEME_DEFS[name]
    return _THEME_DEFS["photographer"]


def _topic_for(goal: str) -> str:
    """Goal-derived subject words; falls back to a hash-stamped code so even
    two vague 'make a website' goals render distinct branding."""
    words = [w for w in re.findall(r"[a-z0-9]+", goal.lower()) if w not in {"the", "a", "an", "that", "can", "for", "with", "into", "build", "create", "make", "generate", "website", "web", "page", "homepage", "landing", "portfolio", "site", "html", "css", "js", "new", "one", "me", "please"}]
    if words:
        return " ".join(words[:3]).title()
    return f"Project {_goal_hash(goal) % 1000:03d}"


def _creative_pipeline(task: Task) -> list[TaskStep]:
    """Deterministic resilient fallback for build/creative goals.

    Uses a SINGLE execute_code step that creates all directories/files via
    pathlib — immune to JSON truncation and Windows shell incompatibilities.
    Never uses run_command mkdir or large inline write_file content.

    The scaffold is GOAL-ADAPTIVE: keywords select a theme, then branding,
    section copy, imagery and the accent color are derived from the goal so
    two different tasks never render the same frozen site.
    """
    explicit = _extract_explicit_path(task.goal)
    base = explicit or f"projects/{_derive_project_slug(task.goal)}"

    theme = _pick_theme(task.goal)
    topic = _topic_for(task.goal)
    accent = _ACCENT_PALETTE[_goal_hash(task.goal) % len(_ACCENT_PALETTE)]
    goal_snippet = re.sub(r"['\"\\\r\n]+", " ", task.goal[:100])

    brand = _render(theme["brand"], topic=topic)
    hero_h1 = _render(theme["hero_h1"], topic=topic)
    hero_p = _render(theme["hero_p"], topic=topic, goal=goal_snippet)
    hero_img = f"https://images.unsplash.com/{theme['hero_img']}?w=1920"

    shift = _goal_hash(task.goal) % len(theme["gallery"])
    gallery_items = theme["gallery"][shift:] + theme["gallery"][:shift]
    gallery_html = "".join(
        f'<div class="item"><img src="https://images.unsplash.com/{img}?w=600" loading="lazy" alt="{_render(cap, topic=topic)}"><span>{_render(cap, topic=topic)}</span></div>\n'
        for img, cap in gallery_items
    )

    nav_labels = [(sid, _render(label, topic=topic)) for sid, label in theme.get("nav", [("gallery", "Work"), ("about", "About"), ("services", "Services"), ("contact", "Contact")])]
    nav_html = "".join(f'<a href="#{sid}">{label}</a>' for sid, label in nav_labels)
    about_html = f"<h2>{_render(theme['about_h2'], topic=topic)}</h2><p>{_render(theme['about_p'], topic=topic)}</p>"
    cards_html = "".join(
        f"<div><h3>{_render(h3, topic=topic)}</h3><p>{_render(p, topic=topic)}</p></div>\n"
        for h3, p in theme["services"]
    )
    cta_label = f"Explore {topic}"
    css = (
        f":root{{--bg:{theme['bg']};--card:#1a1a1a;--accent:{accent};--text:#f5f5f5}}"
        + _SITE_BASE_CSS.replace("__HERO_IMAGE__", hero_img)
    )
    readme_text = (
        f"# {brand}\n\n{hero_p}\n\nBuilt by NexusMind AI\n"
        f"Goal: {goal_snippet[:120]}\n"
    )

    code = (
        "import pathlib\n"
        f"base = pathlib.Path(r'{base}')\n"
        "(base / 'css').mkdir(parents=True, exist_ok=True)\n"
        "(base / 'js').mkdir(parents=True, exist_ok=True)\n"
        f"(base / 'index.html').write_text(r'''<!DOCTYPE html>\n"
        "<html lang=\"en\"><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        f"<title>{brand} - {topic}</title>\n"
        "<link rel=\"stylesheet\" href=\"css/styles.css\">\n"
        "<link href=\"https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700&family=Inter:wght@300;400&display=swap\" rel=\"stylesheet\">\n"
        "</head><body>\n"
        f"<header class=\"header\"><nav class=\"nav\"><a class=\"logo\">{brand}</a>"
        f"<div class=\"nav-links\">{nav_html}</div></nav></header>\n"
        f"<section class=\"hero\"><p class=\"kicker\">{_render(theme['kicker'], topic=topic)}</p><h1>{hero_h1}</h1><p>{hero_p}</p><a href=\"#gallery\" class=\"btn\">{cta_label}</a></section>\n"
        f"<section id=\"gallery\" class=\"section gallery\"><h2>It starts with {topic}</h2>"
        "<div class=\"filters\"><button data-filter=\"all\" class=\"active\">All</button>"
        "<button data-filter=\"featured\">Featured</button><button data-filter=\"new\">New</button></div>"
        f"<div class=\"grid\">{gallery_html}</div></section>\n"
        f"<section id=\"about\" class=\"section about\">{about_html}</section>\n"
        f"<section id=\"services\" class=\"section services\"><h2>What we offer</h2><div class=\"cards\">{cards_html}</div></section>\n"
        "<section id=\"contact\" class=\"section contact\"><h2>Get in touch</h2><form id=\"contactForm\"><input placeholder=\"Name\" required><input placeholder=\"Email\" required><textarea placeholder=\"Message\" required></textarea><button>Send</button></form></section>\n"
        f"<footer>{_goal_hash(task.goal) % 10000:04d} {brand}</footer><script src=\"js/app.js\"></script></body></html>''', encoding='utf-8')\n"
        f"(base / 'css' / 'styles.css').write_text(r'''{css}''', encoding='utf-8')\n"
        "(base / 'js' / 'app.js').write_text(r'''document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('active'));b.classList.add('active');const f=b.dataset.filter;document.querySelectorAll('.item').forEach(i=>{i.style.display=(f==='all'||i.dataset.cat===f||f==='featured'||f==='new')?'block':'none'})}));document.getElementById('contactForm')?.addEventListener('submit',e=>{e.preventDefault();alert('Message sent!');e.target.reset()})})''', encoding='utf-8')\n"
        f"(base / 'README.md').write_text({readme_text!r}, encoding='utf-8')\n"
        "print(f'Project scaffold written to {base.resolve()}')\n"
        "print('Files:', [str(p.relative_to(base)) for p in base.rglob('*') if p.is_file()])\n"
    )
    theme_name = next(
        (name for name, keywords in _THEME_KEYWORDS if any(k in task.goal.lower() for k in keywords)),
        "photographer",
    )
    logger.info("Deterministic creative fallback (%s theme, accent %s) -> %s", theme_name, accent, base)
    return [_make_step(task.id, 0, f"Build {topic} website in {base} (goal-adaptive fallback)", "execute_code", {"code": code})]


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
        # would otherwise produce an empty or single-file project. This must NOT
        # hijack complete plans — only genuinely degenerate ones:
        #   * a single run_command that just mkdirs (no content),
        #   * a single write_file with no actual content (ghost file).
        if _is_creative_goal(task.goal):
            if len(steps) == 1 and steps[0].tool_name in ("run_command", "write_file"):
                args = steps[0].tool_args or {}
                args_s = str(args).lower()
                has_substance = any(
                    isinstance(args.get(k), str) and args.get(k, "").strip()
                    for k in ("content", "text", "markup", "body", "code")
                )
                is_ghost = "mkdir" in args_s or (steps[0].tool_name == "write_file" and not has_substance)
                if is_ghost:
                    logger.warning("Creative plan truncated (ghost step %s); using deterministic scaffold for %s", steps[0].tool_name, task.id)
                    return _creative_pipeline(task)
            # Also catch 2-step plans that have html but NO css AND no js
            # (partial salvage: index.html survived, the rest was lost).
            if len(steps) == 2:
                paths = " ".join(str(s.tool_args.get("path", "")) + str(s.tool_args.get("code", "")) for s in steps)
                has_html = "html" in paths.lower()
                has_css = "css" in paths.lower()
                has_js = "js" in paths.lower() or ".js" in paths.lower()
                if has_html and not has_css and not has_js:
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
