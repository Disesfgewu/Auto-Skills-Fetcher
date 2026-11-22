"""Read-only GitHub PM sync + approval-gated write proposal queue.

HARD SECURITY / GOVERNANCE INVARIANT:
- Syncing from GitHub is strictly READ-ONLY (HTTP GET only).
- NO GitHub write (issue creation/update/comment) is EVER executed without explicit human approval.
- `git push` is NEVER executed automatically by this module.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

GITHUB_API_BASE = "https://api.github.com"
ALLOWED_APPROVED_API_ACTIONS = {"create_issue", "update_issue", "comment_issue"}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
        encoding="utf-8",
    )


def parse_github_repo_from_url(remote_url: str) -> str | None:
    """Extract 'owner/repo' from HTTPS or SSH GitHub remote URL."""
    url = remote_url.strip()
    m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def detect_git_remote_repo(project_root: Path) -> str | None:
    """Detect GitHub 'owner/repo' from local git origin remote (read-only)."""
    git_cfg = project_root / ".git" / "config"
    if git_cfg.is_file():
        text = git_cfg.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if "github.com" in line and "url" in line:
                repo = parse_github_repo_from_url(line.split("=", 1)[-1])
                if repo:
                    return repo
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return parse_github_repo_from_url(proc.stdout.strip())
    except OSError:
        return None
    return None


def resolve_github_token(explicit_token: str | None = None) -> str:
    if explicit_token:
        return explicit_token
    env_tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_tok:
        return env_tok
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except OSError:
        pass
    return ""


def _github_request(
    method: str,
    endpoint: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    url = f"{GITHUB_API_BASE}{endpoint}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "auto-skills-fetcher-pm/0.1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def ensure_github_hub_files(hub_dir: Path, *, repo: str | None = None) -> dict[str, Any]:
    """Ensure github.yaml (strict no-push policy) and pending_github_actions.yaml exist."""
    cfg_path = hub_dir / "github.yaml"
    cfg = _read_yaml(cfg_path)
    if not cfg:
        cfg = {
            "version": 1,
            "repo": repo or "",
            "read_only_sync": True,
            "strict_no_push_until_approval": True,
            "allow_auto_git_push": False,
        }
        _write_yaml(cfg_path, cfg)
    elif repo and not cfg.get("repo"):
        cfg["repo"] = repo
        _write_yaml(cfg_path, cfg)

    queue_path = hub_dir / "pending_github_actions.yaml"
    if not queue_path.is_file():
        _write_yaml(queue_path, {"version": 1, "strict_no_push_until_approval": True, "proposals": []})
    return cfg


def sync_github_readonly(
    hub_dir: Path,
    *,
    repo: str | None = None,
    project_root: Path | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Strictly READ-ONLY sync of GitHub milestones, issues, PRs, and CI runs into github_snapshot.yaml."""
    cfg = ensure_github_hub_files(hub_dir, repo=repo)
    target_repo = (
        repo
        or str(cfg.get("repo") or "").strip()
        or detect_git_remote_repo(project_root or hub_dir.resolve().parent)
        or ""
    )
    if not target_repo:
        raise ValueError("No GitHub repository configured or detected. Pass --repo owner/name or set project-hub/github.yaml.")

    if cfg.get("repo") != target_repo:
        cfg["repo"] = target_repo
        _write_yaml(hub_dir / "github.yaml", cfg)

    tok = resolve_github_token(token)
    raw_milestones = _github_request("GET", f"/repos/{target_repo}/milestones?state=all&per_page=30", token=tok)
    raw_issues = _github_request("GET", f"/repos/{target_repo}/issues?state=all&per_page=50", token=tok)
    raw_prs = _github_request("GET", f"/repos/{target_repo}/pulls?state=all&per_page=30", token=tok)
    try:
        raw_runs = _github_request("GET", f"/repos/{target_repo}/actions/runs?per_page=10", token=tok)
        runs_list = raw_runs.get("workflow_runs", []) if isinstance(raw_runs, dict) else []
    except urllib.error.HTTPError:
        runs_list = []

    milestones = [
        {
            "number": m.get("number"),
            "title": m.get("title", ""),
            "state": m.get("state", "open"),
            "due_on": (m.get("due_on") or "")[:10],
            "open_issues": int(m.get("open_issues") or 0),
            "closed_issues": int(m.get("closed_issues") or 0),
            "url": m.get("html_url", ""),
        }
        for m in (raw_milestones if isinstance(raw_milestones, list) else [])
    ]

    # GitHub /issues includes PRs; filter them out cleanly
    issues = [
        {
            "number": i.get("number"),
            "title": i.get("title", ""),
            "state": i.get("state", "open"),
            "assignees": [a.get("login", "") for a in (i.get("assignees") or [])],
            "labels": [lb.get("name", "") for lb in (i.get("labels") or [])],
            "milestone": (i.get("milestone") or {}).get("title", ""),
            "updated_at": (i.get("updated_at") or "")[:10],
            "url": i.get("html_url", ""),
        }
        for i in (raw_issues if isinstance(raw_issues, list) else [])
        if "pull_request" not in i
    ]

    prs = [
        {
            "number": p.get("number"),
            "title": p.get("title", ""),
            "state": "merged" if p.get("merged_at") else p.get("state", "open"),
            "draft": bool(p.get("draft")),
            "author": (p.get("user") or {}).get("login", ""),
            "head_branch": (p.get("head") or {}).get("ref", ""),
            "base_branch": (p.get("base") or {}).get("ref", ""),
            "updated_at": (p.get("updated_at") or "")[:10],
            "url": p.get("html_url", ""),
        }
        for p in (raw_prs if isinstance(raw_prs, list) else [])
    ]

    ci_runs = [
        {
            "id": r.get("id"),
            "name": r.get("name", ""),
            "branch": r.get("head_branch", ""),
            "status": r.get("status", ""),
            "conclusion": r.get("conclusion") or "in_progress",
            "url": r.get("html_url", ""),
        }
        for r in runs_list[:8]
    ]

    snapshot = {
        "version": 1,
        "repo": target_repo,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "milestones": milestones,
        "issues": issues,
        "pull_requests": prs,
        "ci_runs": ci_runs,
    }
    _write_yaml(hub_dir / "github_snapshot.yaml", snapshot)
    return snapshot


def load_github_pm_state(hub_dir: Path, *, project_root: Path | None = None) -> dict[str, Any]:
    """Read local GitHub config, cached read-only snapshot, and pending approval queue."""
    detected = detect_git_remote_repo(project_root or hub_dir.resolve().parent)
    cfg = _read_yaml(hub_dir / "github.yaml")
    snap = _read_yaml(hub_dir / "github_snapshot.yaml")
    queue = _read_yaml(hub_dir / "pending_github_actions.yaml")

    repo = str(cfg.get("repo") or snap.get("repo") or detected or "")
    return {
        "repo": repo,
        "strict_no_push_until_approval": True,
        "allow_auto_git_push": False,
        "synced_at": snap.get("synced_at", ""),
        "milestones": snap.get("milestones", []),
        "issues": snap.get("issues", []),
        "pull_requests": snap.get("pull_requests", []),
        "ci_runs": snap.get("ci_runs", []),
        "pending_proposals": queue.get("proposals", []),
    }


def queue_github_proposal(
    hub_dir: Path,
    *,
    action_type: str,
    title: str,
    payload: dict[str, Any],
    rationale: str = "",
) -> dict[str, Any]:
    """Stage a proposed GitHub write action into pending_github_actions.yaml (NEVER pushes automatically)."""
    ensure_github_hub_files(hub_dir)
    qpath = hub_dir / "pending_github_actions.yaml"
    data = _read_yaml(qpath)
    proposals = data.setdefault("proposals", [])
    prop_id = f"GH-{len(proposals) + 1:03d}"
    entry = {
        "id": prop_id,
        "action_type": action_type,  # create_issue | update_issue | comment_issue | git_push (manual-only)
        "title": title,
        "payload": payload,
        "rationale": rationale,
        "status": "awaiting_approval",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    proposals.append(entry)
    _write_yaml(qpath, data)
    return entry


def reject_github_proposal(hub_dir: Path, proposal_id: str) -> bool:
    qpath = hub_dir / "pending_github_actions.yaml"
    data = _read_yaml(qpath)
    for p in data.get("proposals", []):
        if isinstance(p, dict) and str(p.get("id")) == proposal_id:
            p["status"] = "rejected"
            p["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            _write_yaml(qpath, data)
            return True
    return False


def execute_approved_proposal(
    hub_dir: Path,
    proposal_id: str,
    *,
    explicit_approval: bool = False,
    token: str | None = None,
) -> dict[str, Any]:
    """Execute a queued GitHub action ONLY when explicit_approval=True. Never executes git push."""
    if not explicit_approval:
        raise PermissionError("STRICT GUARDRAIL: GitHub write blocked — explicit user approval is required.")

    qpath = hub_dir / "pending_github_actions.yaml"
    data = _read_yaml(qpath)
    cfg = _read_yaml(hub_dir / "github.yaml")
    repo = str(cfg.get("repo") or "").strip()

    target = None
    for p in data.get("proposals", []):
        if isinstance(p, dict) and str(p.get("id")) == proposal_id:
            target = p
            break
    if target is None:
        raise KeyError(f"Proposal '{proposal_id}' not found.")
    if target.get("status") != "awaiting_approval":
        raise ValueError(f"Proposal '{proposal_id}' is not awaiting approval (status={target.get('status')}).")

    action_type = str(target.get("action_type") or "")
    # Absolute prohibition on automated git push
    if action_type == "git_push":
        target["status"] = "approved_manual_only"
        target["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        _write_yaml(qpath, data)
        return {
            "ok": True,
            "executed": False,
            "message": "已核准為手動項目：系統嚴禁自動執行 git push，請由你在終端機親自確認並推送。",
        }

    if action_type not in ALLOWED_APPROVED_API_ACTIONS:
        raise ValueError(f"Unsupported or disallowed GitHub action_type: '{action_type}'.")
    if not repo:
        raise ValueError("No GitHub repository configured in project-hub/github.yaml.")

    tok = resolve_github_token(token)
    if not tok:
        raise PermissionError("GITHUB_TOKEN (or `gh auth login`) is required to execute approved GitHub actions.")

    payload = target.get("payload") or {}
    if action_type == "create_issue":
        res = _github_request(
            "POST",
            f"/repos/{repo}/issues",
            token=tok,
            body={
                "title": payload.get("title") or target.get("title"),
                "body": payload.get("body", ""),
                "assignees": payload.get("assignees", []),
                "labels": payload.get("labels", []),
            },
        )
    elif action_type == "update_issue":
        num = int(payload["issue_number"])
        patch_body = {k: v for k, v in payload.items() if k in {"title", "body", "state", "assignees", "labels"}}
        res = _github_request("PATCH", f"/repos/{repo}/issues/{num}", token=tok, body=patch_body)
    elif action_type == "comment_issue":
        num = int(payload["issue_number"])
        res = _github_request(
            "POST",
            f"/repos/{repo}/issues/{num}/comments",
            token=tok,
            body={"body": payload.get("body", "")},
        )
    else:
        raise ValueError(f"Unsupported action_type: {action_type}")

    target["status"] = "executed"
    target["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    target["result_url"] = res.get("html_url", "") if isinstance(res, dict) else ""
    _write_yaml(qpath, data)
    return {"ok": True, "executed": True, "result_url": target["result_url"]}
