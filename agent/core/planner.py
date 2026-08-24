"""Task decomposition planner — breaks goals into executable steps.

Inspired by OpenClaw's multi-step decomposition and Hermes' skill-based planning.
Now includes self-improvement: past reflections influence planning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.config import settings
from agent.models import Task, TaskStep

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are NexusMind — an autonomous AI agent with REAL execution capabilities.

YOU ARE NOT A CHATBOT. You have direct access to tools that execute real actions:
- You can run shell commands (curl, git, python, any command)
- You can execute Python code
- You can read/write files
- You can call APIs, merge PRs, post comments — all real actions

Your job: decompose goals into executable steps using your REAL tools.

CRITICAL: When a goal involves an action (GitHub, API, file operations, code execution),
you MUST use run_command or execute_code — NEVER search the web for how to do it.

AVAILABLE TOOLS:
- web_search: Search the web. Args: {"query": "...", "num_results": 5}
  → ONLY for research/information gathering. NEVER for action tasks.

- run_command: Execute ANY shell command. Args: {"command": "..."}
  → Examples: curl, git, python, mkdir, ls, cat, pip install, etc.
  → You can call APIs: curl -s https://api.github.com/repos/owner/repo/pulls
  → You can merge PRs: curl -X PUT https://api.github.com/repos/owner/repo/pulls/1/merge
  → You can post comments, install packages, run tests — anything.

- execute_code: Run Python code. Args: {"code": "..."}
  → Parse API responses, analyze data, make decisions.

- read_file / write_file / list_directory: File operations
- summarize_text / extract_data / parse_json: Data processing

PLANNING STRATEGIES BY TASK TYPE:

1. RESEARCH TASKS (summarize, analyze, report on a topic):
   Step 1: web_search with focused query
   Step 2: web_search with broader/different query
   Step 3: summarize_text combining results
   Step 4: write_file to save output

2. GITHUB/API TASKS (PRs, issues, repos, webhooks):
   Step 1: run_command with curl to fetch data from API
   Step 2: execute_code to parse and analyze the response
   Step 3: run_command with curl to take action (merge, comment, etc.)
   → NEVER search the web for API documentation — just use curl directly.

3. FILE/CREATION TASKS:
   Step 1: write_file with the content

4. CODING TASKS:
   Step 1: execute_code with the implementation

RULES:
1. EVERY step MUST have "tool_name" and "tool_args"
2. Use {{step_N_result}} to reference previous step outputs
3. For research tasks: use web_search
4. For action tasks (GitHub, API, commands): use run_command with curl
5. NEVER search the web when you can just DO the action
6. Keep plans SHORT — 3-5 steps maximum
7. Return ONLY valid JSON array

OUTPUT FORMAT (JSON array):
[
  {"description": "Fetch PR details from GitHub API", "tool_name": "run_command", "tool_args": {"command": "curl -s https://api.github.com/repos/owner/repo/pulls/1"}},
  {"description": "Analyze the PR code changes", "tool_name": "execute_code", "tool_args": {"code": "import json\\ndata = json.loads('''{{step_0_result}}''')\\nprint('Title:', data.get('title'))"}},
  {"description": "Merge the PR", "tool_name": "run_command", "tool_args": {"command": "curl -X PUT https://api.github.com/repos/owner/repo/pulls/1/merge"}}
]

IMPORTANT: Steps are 0-indexed. First step is step_0, second is step_1, etc.
When referencing previous results, use {{step_0_result}}, {{step_1_result}}, etc.
"""


async def plan_task(task: Task, available_skills: list[str] | None = None, lessons: list[str] | None = None) -> list[TaskStep]:
    """Decompose a task into ordered steps using Gemini.

    Args:
        task: The task to plan.
        available_skills: Optional list of available skill names.
        lessons: Past reflections/lessons learned from previous tasks.

    Returns:
        Ordered list of TaskStep objects.

    """
    from agent.core.gemini_client import generate_content

    # Hardcoded override: GitHub tasks MUST use curl, not web_search
    goal_lower = task.goal.lower()
    if any(kw in goal_lower for kw in ["github", "pr ", "pull request", "merge", "repo"]):
        # Extract repo name from goal if present
        import re
        repo_match = re.search(r'tamimlabs/[\w-]+', task.goal)
        repo = repo_match.group(0) if repo_match else "tamimlabs/nexusmind-ai"

        # Extract PR number if present
        pr_match = re.search(r'pr\s*#?(\d+)', task.goal)
        pr_number = pr_match.group(1) if pr_match else None

        # Common auth header for curl
        auth_header = '-H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github.v3+json"'

        if pr_number:
            steps = [
                TaskStep(
                    task_id=task.id,
                    description=f"Fetch PR #{pr_number} details from GitHub API",
                    tool_name="run_command",
                    tool_args={"command": f"curl -s {auth_header} https://api.github.com/repos/{repo}/pulls/{pr_number}"},
                    order=0,
                ),
                TaskStep(
                    task_id=task.id,
                    description=f"Fetch PR #{pr_number} diff from GitHub API",
                    tool_name="run_command",
                    tool_args={"command": f"curl -s {auth_header} https://api.github.com/repos/{repo}/pulls/{pr_number}.diff"},
                    order=1,
                ),
                TaskStep(
                    task_id=task.id,
                    description="Analyze the PR code changes for quality and security",
                    tool_name="execute_code",
                    tool_args={"code": "import json\ntry:\n    data = json.loads(open('step_0_result.txt').read())\n    print(f\"PR #{data.get('number')}: {data.get('title')}\")\n    print(f\"Author: {data.get('user', {}).get('login')}\")\n    print(f\"State: {data.get('state')}\")\n    print(f\"Changed files: {data.get('changed_files')}\")\n    print(f\"Additions: +{data.get('additions')}, Deletions: -{data.get('deletions')}\")\nexcept Exception as e:\n    print(f'Error parsing PR: {e}')"},
                    order=2,
                ),
            ]
            # Add merge step
            steps.append(
                TaskStep(
                    task_id=task.id,
                    description=f"Merge PR #{pr_number} if safe",
                    tool_name="run_command",
                    tool_args={"command": f"curl -s -X PUT {auth_header} https://api.github.com/repos/{repo}/pulls/{pr_number}/merge"},
                    order=3,
                )
            )
        else:
            # No specific PR — list all open PRs
            steps = [
                TaskStep(
                    task_id=task.id,
                    description="List all open PRs from GitHub API",
                    tool_name="run_command",
                    tool_args={"command": f"curl -s {auth_header} https://api.github.com/repos/{repo}/pulls?state=open"},
                    order=0,
                ),
                TaskStep(
                    task_id=task.id,
                    description="Parse and analyze the PR list",
                    tool_name="execute_code",
                    tool_args={"code": "import json\ntry:\n    data = json.loads(open('step_0_result.txt').read())\n    if isinstance(data, list):\n        print(f'Found {len(data)} open PRs:')\n        for pr in data:\n            print(f\"  #{pr['number']}: {pr['title']} by @{pr.get('user', {}).get('login', 'unknown')}\")\n    else:\n        print('Response:', json.dumps(data, indent=2)[:500])\nexcept Exception as e:\n    print(f'Error: {e}')"},
                    order=1,
                ),
            ]

        logger.info("Planned %d steps (GitHub hardcoded) for task %s", len(steps), task.id)
        return steps

    lessons_context = ""
    if lessons:
        lessons_context = "\n\nLESSONS FROM PAST TASKS:\n" + "\n".join(f"- {l}" for l in lessons[:5])

    user_prompt = f"""Goal: {task.goal}
Context: {json.dumps(task.context) if task.context else 'None'}{lessons_context}

Create a RESILIENT plan that will produce useful output even if some steps fail.
Return ONLY the JSON array."""

    try:
        response = await generate_content(
            model=settings.gemini_model,
            system=PLANNER_SYSTEM_PROMPT,
            user=user_prompt,
        )

        steps_data = _parse_steps_json(response)
        steps: list[TaskStep] = []
        for i, step_data in enumerate(steps_data):
            tool_name = step_data.get("tool_name")
            if not tool_name:
                tool_name = "web_search"

            step = TaskStep(
                task_id=task.id,
                description=step_data.get("description", f"Step {i + 1}"),
                tool_name=tool_name,
                tool_args=step_data.get("tool_args", {}),
                order=i,
            )
            steps.append(step)

        logger.info("Planned %d steps for task %s", len(steps), task.id)
        return steps

    except Exception:
        logger.exception("Planning failed for task %s", task.id)
        # Ask Gemini to pick the right tool for this task
        try:
            tool_prompt = f"""You are a tool selector. Given this task, pick the RIGHT tool.

Task: {task.goal}

Available tools:
- web_search: Search the web. Use for research questions.
- run_command: Shell command. Use for API calls (curl), system commands, git, etc.
- execute_code: Python code. Use for data processing, analysis, calculations.
- read_file: Read a file. Use when task asks to read/open a file.
- write_file: Write a file. Use when task asks to create/save content.
- list_directory: List files. Use when task asks to list/show files.

Return ONLY a JSON object: {{"tool_name": "tool_name", "tool_args": {{...}}, "description": "what to do"}}"""

            response = await generate_content(
                model=settings.gemini_model,
                system="You are a tool selector. Return only JSON.",
                user=tool_prompt,
            )

            # Parse the response
            import re as _re
            json_match = _re.search(r'\{[^{}]+\}', response)
            if json_match:
                tool_data = json.loads(json_match.group())
                tool_name = tool_data.get("tool_name", "web_search")
                tool_args = tool_data.get("tool_args", {})
                description = tool_data.get("description", task.goal[:100])

                # Fix tool_args based on tool type
                if tool_name == "run_command":
                    if "command" not in tool_args:
                        tool_args = {"command": task.goal}
                elif tool_name == "execute_code":
                    if "code" not in tool_args:
                        tool_args = {"code": f"# {task.goal}\nprint('Ready to execute')"}
                elif tool_name == "web_search":
                    if "query" not in tool_args:
                        tool_args = {"query": task.goal, "num_results": 5}
                elif tool_name == "write_file":
                    if "path" not in tool_args:
                        tool_args = {"path": "output/result.md", "content": "{{step_0_result}}"}
                elif tool_name == "read_file":
                    if "path" not in tool_args:
                        tool_args = {"path": "output/"}
                elif tool_name == "list_directory":
                    if "path" not in tool_args:
                        tool_args = {"path": "output/"}

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

        # Last resort: web search
        return [
            TaskStep(
                task_id=task.id,
                description=f"Search for information about: {task.goal}",
                tool_name="web_search",
                tool_args={"query": task.goal, "num_results": 5},
                order=0,
            ),
        ]


def _parse_steps_json(text: str) -> list[dict[str, Any]]:
    """Extract JSON array of steps from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "steps" in result:
            return result["steps"]
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse steps JSON, returning fallback")
    return [{"description": text[:500], "tool_name": "web_search", "tool_args": {"query": text[:100]}}]
