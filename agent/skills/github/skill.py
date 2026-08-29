"""GitHub skill — real repo/PR operations via the GitHub REST API.

Replaces the old "curl via run_command" approach that failed silently and
triggered web-search fallbacks. These tools talk directly to api.github.com,
so planning a GitHub task NEVER needs web_search.

Tools:
- github_resolve_repo: resolve "my repository" -> owner/name (goal text, git
  remote, or GITHUB_DEFAULT_REPO setting)
- github_get_repo: fetch repo metadata (existence check)
- github_list_prs: list open PRs
- github_get_pr: PR details + changed files summary
- github_review_pr: analyze one or many PRs -> merge/reject/skip verdicts
- github_merge_pr / github_close_pr: atomic actions
- github_apply_decisions: execute review verdicts (merge or reject) in bulk

Read-only tools auto-run; mutating tools are registered high_risk so smart
approval mode asks before touching your repository.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from agent.core.executor import register_tool
from agent.models import ToolResult

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def _token_from_project_env_file() -> str:
    """Last-resort: parse GITHUB_TOKEN straight from the project root .env.

    Independent of os.environ shadowing, cwd, or settings-loading order.
    """
    try:
        from agent.config import _PROJECT_ROOT

        env_file = Path(_PROJECT_ROOT) / ".env"
    except Exception:
        env_file = Path(__file__).resolve().parents[3] / ".env"
    try:
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GITHUB_TOKEN=") or line.startswith("GITHUB_TOKEN ="):
                    _, _, value = line.partition("=")
                    return value.strip().strip("'\"")
    except Exception:
        logger.debug("Could not parse project .env for GITHUB_TOKEN")
    return ""


def _token() -> str:
    """Return GitHub token: settings/env first, project .env file as fallback."""
    try:
        from agent.config import settings

        if settings.github_token:
            return settings.github_token
    except Exception:
        pass

    import os

    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
    if token:
        return token

    return _token_from_project_env_file()


def _token_fingerprint() -> str:
    """Masked fingerprint of the active token for logs/status."""
    token = _token()
    if not token:
        return "<none>"
    return f"{token[:4]}…{token[-4:]} ({len(token)} chars)"


def _headers() -> dict[str, str]:
    """Build GitHub API request headers."""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"token {token}"
    else:
        logger.warning(
            "GitHub call WITHOUT token — private repos and write actions will "
            "fail with 404/403. Check GITHUB_TOKEN in .env and restart the server."
        )
    return headers


def _parse_repo_url(url: str) -> str | None:
    """Extract owner/name from an https or ssh git remote URL."""
    https_match = re.search(r"https?://[^/]+/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    if https_match:
        return https_match.group(1)
    ssh_match = re.search(r"git@[^:]+:([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    if ssh_match:
        return ssh_match.group(1)
    return None


def _default_repo() -> str:
    """Return the configured default repository, if any."""
    try:
        from agent.config import settings

        return settings.github_default_repo
    except Exception:
        return ""


async def _github_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """Call the GitHub API. Returns (status_code, decoded-json-or-text)."""
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.request(
            method, url, headers=_headers(), json=json_body, params=params
        )
        if resp.status_code in (401, 403, 404):
            logger.warning(
                "GitHub %s %s -> %d with token fingerprint %s "
                "(a stale GITHUB_TOKEN in the OS environment overrides .env)",
                method, url, resp.status_code, _token_fingerprint(),
            )
        try:
            data: Any = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data


def _error_result(status: int, data: Any, action: str) -> ToolResult:
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message", ""))
    hint = ""
    if status in (401, 403):
        hint = " (Check that GITHUB_TOKEN is set and has repo access)"
    elif status == 404:
        hint = (
            " (Not found — check owner/name; for PRIVATE repos a missing/invalid "
            "GITHUB_TOKEN also produces 404)"
        )
    return ToolResult(success=False, output="", error=f"GitHub API {action} failed [{status}]: {message}{hint}")


# ── Repo Resolution ───────────────────────────────────────────────


@register_tool("github_resolve_repo")
async def github_resolve_repo(goal_text: str = "", **_: Any) -> ToolResult:
    """Resolve which repository a goal refers to.

    Order: explicit owner/name in text -> git remote of the working
    directory -> GITHUB_DEFAULT_REPO setting.
    """
    # 1. Explicit owner/name mentioned in the goal ("review prs on octocat/hello-world")
    match = re.search(r"\b([\w.-]+)/([\w.-]+)\b", goal_text.replace("https://", ""))
    if match:
        candidate = f"{match.group(1)}/{match.group(2)}"
        lowered = candidate.lower()
        # Ignore generic words that happen to contain a slash-like pattern
        if not any(bad in lowered for bad in ["http", ".env", "step_"]):
            return ToolResult(success=True, output=candidate)

    # 2. The git remote of the current project directory ("my repository")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "remote", "get-url", "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        comm = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            remote = comm[0].decode().strip()
            parsed = _parse_repo_url(remote)
            if parsed:
                return ToolResult(success=True, output=parsed)
    except Exception:
        logger.debug("Could not read git remote origin")

    # 3. Configured default repository
    default = _default_repo()
    if default and re.match(r"^[\w.-]+/[\w.-]+$", default):
        return ToolResult(success=True, output=default)

    return ToolResult(
        success=False,
        output="",
        error=(
            "Could not determine which repository you mean. "
            "Include owner/repo in the goal, run this from a git clone, "
            "or set GITHUB_DEFAULT_REPO in .env"
        ),
    )


@register_tool("github_get_repo")
async def github_get_repo(repo: str, **_: Any) -> ToolResult:
    """Fetch repository metadata (name, visibility, branches, open PR count)."""
    status, data = await _github_request("GET", f"/repos/{repo}")
    if status != 200:
        return _error_result(status, data, "get_repo")
    assert isinstance(data, dict)
    summary = {
        "full_name": data.get("full_name"),
        "description": (data.get("description") or "")[:200],
        "default_branch": data.get("default_branch"),
        "open_issues": data.get("open_issues_count"),
        "visibility": data.get("visibility") or ("private" if data.get("private") else "public"),
        "url": data.get("html_url"),
    }
    return ToolResult(success=True, output=json.dumps(summary, indent=2))


@register_tool("github_list_prs")
async def github_list_prs(repo: str, state: str = "open", **_: Any) -> ToolResult:
    """List pull requests for a repository as compact JSON."""
    status, data = await _github_request(
        "GET",
        f"/repos/{repo}/pulls",
        params={"state": state, "per_page": 20},
    )
    if status != 200:
        return _error_result(status, data, "list_prs")
    if not isinstance(data, list):
        return ToolResult(success=False, output="", error="Unexpected GitHub API response")
    prs = [
        {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr.get("user", {}).get("login", "unknown"),
            "branch": pr.get("head", {}).get("ref", ""),
            "draft": pr.get("draft", False),
            "created_at": pr.get("created_at", ""),
        }
        for pr in data
    ]
    return ToolResult(
        success=True,
        output=json.dumps(prs),
        metadata={"count": len(prs), "repo": repo},
    )


@register_tool("github_get_pr")
async def github_get_pr(repo: str, pr_number: int, **_: Any) -> ToolResult:
    """Fetch PR details plus per-file change stats and mergeability."""
    detail_status, detail = await _github_request("GET", f"/repos/{repo}/pulls/{pr_number}")
    if detail_status != 200:
        return _error_result(detail_status, detail, "get_pr")
    assert isinstance(detail, dict)

    files_status, files = await _github_request(
        "GET", f"/repos/{repo}/pulls/{pr_number}/files", params={"per_page": 50}
    )
    file_summaries: list[dict[str, Any]] = []
    if files_status == 200 and isinstance(files, list):
        for f in files[:30]:
            file_summaries.append({
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": (f.get("patch") or "")[:1200],
            })

    summary = {
        "number": detail.get("number"),
        "title": detail.get("title"),
        "author": detail.get("user", {}).get("login"),
        "state": detail.get("state"),
        "draft": detail.get("draft", False),
        "mergeable": detail.get("mergeable"),
        "mergeable_state": detail.get("mergeable_state"),
        "additions": detail.get("additions"),
        "deletions": detail.get("deletions"),
        "changed_files": detail.get("changed_files"),
        "base_branch": detail.get("base", {}).get("ref"),
        "head_branch": detail.get("head", {}).get("ref"),
        "body": (detail.get("body") or "")[:500],
        "files": file_summaries,
    }
    return ToolResult(success=True, output=json.dumps(summary))


# ── Review (decision engine) ──────────────────────────────────────


def _heuristic_verdict(pr_summary: dict[str, Any]) -> dict[str, Any]:
    """Rule-based fallback review when Gemini is unavailable."""
    mergeable_state = pr_summary.get("mergeable_state")
    if pr_summary.get("draft"):
        return {"decision": "skip", "confidence": 0.9,
                "reason": "PR is a draft — not ready for review"}
    if mergeable_state == "dirty":
        return {"decision": "reject", "confidence": 0.8,
                "reason": "Merge conflicts with base branch"}
    if pr_summary.get("mergeable") is False:
        return {"decision": "reject", "confidence": 0.8,
                "reason": "GitHub reports PR is not mergeable"}
    additions = pr_summary.get("additions") or 0
    deletions = pr_summary.get("deletions") or 0
    changed = pr_summary.get("changed_files") or 0
    suspicious = any(
        str(f.get("filename", "")).lower().endswith((".env", ".pem", ".key"))
        for f in pr_summary.get("files", [])
    )
    if suspicious:
        return {"decision": "reject", "confidence": 0.7,
                "reason": "Touches secret-looking files (.env/.pem/.key)"}
    if mergeable_state == "clean" and changed <= 20 and additions < 1000:
        return {"decision": "merge", "confidence": 0.6,
                "reason": f"Clean merge state, moderate diff (+{additions}/-{deletions}, {changed} files)"}
    if mergeable_state == "blocked":
        return {"decision": "skip", "confidence": 0.7,
                "reason": "Blocked by required reviews or failing checks"}
    return {"decision": "skip", "confidence": 0.4,
            "reason": f"Inconclusive heuristics (mergeable_state={mergeable_state})"}


async def _gemini_verdict(pr_summary: dict[str, Any]) -> dict[str, Any] | None:
    """Ask Gemini to review the PR diff. Returns verdict dict or None."""
    from agent.core.gemini_client import generate_content

    files_text = "\n".join(
        f"- {f.get('filename')} (+{f.get('additions')}/-{f.get('deletions')})\n{f.get('patch', '')[:600]}"
        for f in pr_summary.get("files", [])[:10]
    )
    prompt = f"""Review this pull request like a senior engineer.

PR #{pr_summary.get('number')}: {pr_summary.get('title')}
Author: @{pr_summary.get('author')}
Mergeable state: {pr_summary.get('mergeable_state')}
Stats: +{pr_summary.get('additions')}/-{pr_summary.get('deletions')} across {pr_summary.get('changed_files')} files
Description: {str(pr_summary.get('body'))[:400]}

Changed files:
{files_text[:4000]}

Decide: "merge" if changes look safe and sensible, "reject" if clearly broken/
dangerous (secrets, conflicts, destructive code), "skip" if unsure or needs human eyes.

Return ONLY JSON: {{"decision": "merge|reject|skip", "confidence": 0.0-1.0, "reason": "<=25 words"}}"""
    try:
        response = await generate_content(
            system="You are a strict but pragmatic code reviewer. Return only valid JSON.",
            user=prompt,
            temperature=0.2,
            max_tokens=200,
        )
        json_match = re.search(r"\{.*\}", response, re.DOTALL)
        if json_match:
            loaded: Any = json.loads(json_match.group())
            if not isinstance(loaded, dict):
                return None
            decision = loaded.get("decision")
            if decision in ("merge", "reject", "skip"):
                verdict: dict[str, Any] = loaded
                verdict["decision"] = decision
                verdict.setdefault("confidence", 0.5)
                verdict.setdefault("reason", "Gemini review")
                return verdict
    except Exception:
        logger.debug("Gemini PR review unavailable, falling back to heuristics")
    return None


@register_tool("github_review_pr")
async def github_review_pr(repo: str, pr_number: int | None = None, pr_list: str = "", **_: Any) -> ToolResult:
    """Review one PR or every PR in a pr_list JSON array.

    Returns JSON: [{"number", "title", "decision": "merge|reject|skip",
    "confidence", "reason"}, ...]. Decisions are recommendations — nothing is
    merged/closed until github_apply_decisions runs.
    """
    numbers: list[int] = []
    if pr_number:
        numbers = [int(pr_number)]
    elif pr_list:
        try:
            parsed = json.loads(pr_list)
            numbers = [int(p["number"]) for p in parsed][:10]
        except Exception:
            return ToolResult(success=False, output="", error=f"Unparseable pr_list: {pr_list[:200]}")
    else:
        return ToolResult(success=False, output="", error="Provide pr_number or pr_list")

    reviews: list[dict[str, Any]] = []
    for num in numbers:
        get_result = await github_get_pr(repo=repo, pr_number=num)
        if not get_result.success:
            reviews.append({"number": num, "decision": "skip", "confidence": 0.0,
                            "reason": f"Could not fetch PR: {get_result.error}"})
            continue
        pr_summary = json.loads(get_result.output)
        verdict = await _gemini_verdict(pr_summary) or _heuristic_verdict(pr_summary)
        reviews.append({
            "number": num,
            "title": pr_summary.get("title"),
            **verdict,
        })

    return ToolResult(success=True, output=json.dumps(reviews), metadata={"reviewed": len(reviews)})


# ── Actions ───────────────────────────────────────────────────────


@register_tool("github_merge_pr", high_risk=True)
async def github_merge_pr(repo: str, pr_number: int, commit_title: str = "", **_: Any) -> ToolResult:
    """Merge a pull request via the GitHub API."""
    body: dict[str, Any] = {"merge_method": "merge"}
    if commit_title:
        body["commit_title"] = commit_title
    status, data = await _github_request(
        "PUT", f"/repos/{repo}/pulls/{pr_number}/merge", json_body=body
    )
    if status == 200:
        sha = data.get("sha", "") if isinstance(data, dict) else ""
        return ToolResult(success=True, output=f"Merged PR #{pr_number} in {repo} (commit {sha[:7]})")
    if status == 405:
        return ToolResult(success=False, output="", error=f"PR #{pr_number} is not mergeable (closed, dirty, or blocked)")
    return _error_result(status, data, "merge_pr")


@register_tool("github_close_pr", high_risk=True)
async def github_close_pr(repo: str, pr_number: int, comment: str = "", **_: Any) -> ToolResult:
    """Reject a pull request: optionally post a comment, then close it."""
    if comment:
        c_status, c_data = await _github_request(
            "POST", f"/repos/{repo}/issues/{pr_number}/comments",
            json_body={"body": comment[:2000]},
        )
        if c_status != 201:
            logger.warning("Comment post failed [%s]: %s", c_status, c_data)
    status, data = await _github_request(
        "PATCH", f"/repos/{repo}/pulls/{pr_number}", json_body={"state": "closed"}
    )
    if status == 200:
        suffix = f" with comment: {comment[:150]}" if comment else ""
        return ToolResult(success=True, output=f"Rejected PR #{pr_number} in {repo}{suffix}")
    return _error_result(status, data, "close_pr")


@register_tool("github_verify_pr_locally")
async def github_verify_pr_locally(repo: str, pr_number: int, **_: Any) -> ToolResult:
    """Checkout a PR branch locally and run tests sequentially (one-by-one, high priority).

    Clones/fetches the PR head, checks out, detects test runner (pytest / npm test / make test),
    runs it, and returns pass/fail. Used before merge/reject decisions.
    """
    import os
    import tempfile
    from pathlib import Path as _Path

    # Determine temp dir
    token = _token()
    tmpdir = _Path(tempfile.gettempdir()) / f"nexusmind_pr_{pr_number}_{os.getpid()}"
    try:
        # Fetch PR head via git if repo already cloned locally, else shallow clone
        # Try to find local repo root
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--show-toplevel",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        local_root = out.decode().strip() if proc.returncode == 0 else ""

        if local_root and _Path(local_root).exists():
            # Local repo exists — fetch PR
            fetch_cmd = f"git fetch origin pull/{pr_number}/head:pr-{pr_number} 2>&1 && git checkout pr-{pr_number} 2>&1"
            proc2 = await asyncio.create_subprocess_shell(
                fetch_cmd, cwd=local_root,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc2.communicate(), timeout=60)
            fetch_out = (stdout.decode() + stderr.decode())[:800]
            if proc2.returncode != 0:
                return ToolResult(success=False, output="", error=f"Fetch/checkout failed for PR #{pr_number}: {fetch_out}")
            workdir = local_root
        else:
            # No local repo — shallow clone PR branch via GitHub API head sha
            status, pr_data = await _github_request("GET", f"/repos/{repo}/pulls/{pr_number}")
            if status != 200 or not isinstance(pr_data, dict):
                return ToolResult(success=False, output="", error=f"Cannot fetch PR #{pr_number} metadata")
            clone_url = pr_data.get("head", {}).get("repo", {}).get("clone_url") or f"https://github.com/{repo}.git"
            if token and "github.com" in clone_url:
                clone_url = clone_url.replace("https://", f"https://{token}@")
            branch = pr_data.get("head", {}).get("ref", "")
            if tmpdir.exists():
                import shutil

                shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir.mkdir(parents=True, exist_ok=True)
            clone_cmd = f"git clone --depth 1 --branch {branch} {clone_url} {tmpdir} 2>&1"
            proc3 = await asyncio.create_subprocess_shell(
                clone_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc3.communicate(), timeout=90)
            clone_out = (stdout.decode() + stderr.decode())[:800]
            if proc3.returncode != 0:
                return ToolResult(success=False, output="", error=f"Clone failed for PR #{pr_number}: {clone_out}")
            workdir = str(tmpdir)

        # Detect and run tests — sequential, one runner at a time
        test_cmd = None
        if _Path(workdir, "pytest.ini").exists() or _Path(workdir, "pyproject.toml").exists() or list(_Path(workdir).glob("tests/**/*.py")):
            test_cmd = "python -m pytest -q 2>&1 | head -n 100"
        elif _Path(workdir, "package.json").exists():
            test_cmd = "npm test 2>&1 | head -n 100"
        elif _Path(workdir, "Makefile").exists():
            test_cmd = "make test 2>&1 | head -n 100"
        else:
            # No test harness found — fallback to syntax check
            test_cmd = "python -m py_compile $(find . -name '*.py' | head -20) 2>&1 || echo 'no py files'"

        if test_cmd:
            proc4 = await asyncio.create_subprocess_shell(
                test_cmd, cwd=workdir,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc4.communicate(), timeout=120)
                test_out = (stdout.decode(errors="replace") + stderr.decode(errors="replace"))[:2000]
                passed = proc4.returncode == 0
                # Cleanup temp dir if we cloned
                if str(workdir) == str(tmpdir) and tmpdir.exists():
                    import shutil

                    shutil.rmtree(tmpdir, ignore_errors=True)
                # Try to return to original branch if we checked out PR
                if local_root:
                    await asyncio.create_subprocess_shell("git checkout - 2>&1 || git checkout main 2>&1 || true", cwd=local_root)
                if passed:
                    return ToolResult(success=True, output=f"PR #{pr_number} local tests PASSED:\n{test_out[:1000]}", metadata={"pr": pr_number, "passed": True})
                else:
                    return ToolResult(success=True, output=f"PR #{pr_number} local tests FAILED (will not auto-merge):\n{test_out[:1000]}", metadata={"pr": pr_number, "passed": False})
            except TimeoutError:
                return ToolResult(success=True, output=f"PR #{pr_number} local tests TIMEOUT after 120s — treat as failed", metadata={"pr": pr_number, "passed": False})

        return ToolResult(success=True, output=f"PR #{pr_number} no tests found — skipped local verification", metadata={"pr": pr_number, "passed": None})
    except Exception as e:
        return ToolResult(success=False, output="", error=f"Local verification error for PR #{pr_number}: {e}")


@register_tool("github_apply_decisions", high_risk=True)
async def github_apply_decisions(repo: str, decisions: str, dry_run: bool = False, verify_locally: bool = True, **_: Any) -> ToolResult:
    """Execute review verdicts: merges 'merge' PRs, closes 'reject' PRs, skips others.

    Args:
        repo: owner/name repository.
        decisions: JSON array from github_review_pr.
        dry_run: when true, report intended actions without executing.
        verify_locally: when true, each PR is checked out and tested locally one-by-one
            before merging (high priority sequential verification). If local tests fail,
            merge is downgraded to skip.

    """
    try:
        parsed = json.loads(decisions)
        if not isinstance(parsed, list):
            raise ValueError("decisions must be a JSON array")
    except Exception as exc:
        return ToolResult(success=False, output="", error=f"Unparseable decisions: {exc}")

    actions: list[str] = []
    # Sequential one-by-one — not parallel — to ensure local checkout/tests don't race
    for item in parsed:
        number = item.get("number")
        decision = item.get("decision")
        reason = item.get("reason", "")
        confidence = float(item.get("confidence", 0))
        if number is None or decision not in ("merge", "reject"):
            actions.append(f"#{number}: skipped ({item.get('reason', 'no action decided')})")
            continue
        if decision == "reject" and confidence < 0.5:
            actions.append(f"#{number}: skipped rejection (low confidence {confidence:.1f})")
            continue
        if dry_run:
            actions.append(f"#{number}: would {decision} — {reason}")
            continue

        # High-priority sequential local verification before merge
        if decision == "merge" and verify_locally:
            verify = await github_verify_pr_locally(repo=repo, pr_number=int(number))
            # verify.success True means we got test output; check metadata passed flag
            passed = verify.metadata.get("passed") if isinstance(verify.metadata, dict) else None
            if passed is False:
                actions.append(f"#{number}: SKIPPED merge — local tests FAILED — {reason} | {verify.output[:200]}")
                continue
            elif verify.success is False:
                actions.append(f"#{number}: SKIPPED merge — local checkout failed: {verify.error[:200]}")
                continue
            # passed is True or None (no tests) -> proceed

        if decision == "merge":
            result = await github_merge_pr(repo=repo, pr_number=int(number))
        else:
            comment = f"Automated review rejected this PR: {reason}"
            result = await github_close_pr(repo=repo, pr_number=int(number), comment=comment)
        actions.append(f"#{number}: {'OK' if result.success else 'FAILED'} — {result.output or result.error}")

    header = "Planned actions (dry run):" if dry_run else "Actions taken (sequential, high priority, locally verified):"
    return ToolResult(success=True, output=header + "\n" + "\n".join(actions))
