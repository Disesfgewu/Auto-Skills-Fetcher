"""GitHub API crawler with size-partitioned queries, ETag caching, and seed watching."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

GITHUB_API_BASE = "https://api.github.com"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
ROOT_SKILL_SUBDIRS = ("scripts/", "references/", "examples/", "resources/", "assets/")


@dataclass
class CrawledSkillItem:
    """Raw candidate fetched from GitHub before parsing & scoring."""

    repo: str
    path: str
    url: str
    commit_sha: str
    raw_content: str
    domain: str
    stars: int = 0
    stars_90d_growth: int = 0
    curated_mentions: int = 0
    days_since_update: int = 0
    license_spdx: str = ""
    has_scripts: bool = False
    has_references: bool = False


def build_partitioned_queries(
    domain_cfg: dict[str, Any],
    *,
    pushed_after: str | None = None,
) -> list[str]:
    """Generate size-partitioned GitHub Code Search queries to bypass the 1,000-result cap."""
    keywords = domain_cfg.get("keywords", [])
    size_buckets = domain_cfg.get("size_buckets", ["1500..25000"])
    if not keywords:
        return []

    queries: list[str] = []
    for bucket in size_buckets:
        for kw in keywords:
            parts = ["filename:SKILL.md", kw, f"size:{bucket}"]
            if pushed_after:
                parts.append(f"pushed:>{pushed_after}")
            queries.append(" ".join(parts))
    return queries


class GitHubCrawler:
    """Zero-dependency GitHub REST & Code Search client with ETag support."""

    def __init__(self, token: str | None = None, timeout: int = 20) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.timeout = timeout
        self._repo_cache: dict[str, dict[str, Any]] = {}
        self._tree_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _headers(self, etag: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "auto-skills-fetcher/0.1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def _request_json(
        self, url: str, etag: str | None = None
    ) -> tuple[int, dict[str, Any] | list[Any] | None, str | None]:
        req = urllib.request.Request(url, headers=self._headers(etag))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body, resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, None, etag
            raise

    def fetch_skill_file(
        self, repo: str, path: str, *, ref: str | None = None, etag: str | None = None
    ) -> tuple[str | None, str | None, str | None]:
        """Fetch raw SKILL.md text, blob SHA, and ETag from a repository."""
        encoded_path = urllib.parse.quote(path.lstrip("/"))
        url = f"{GITHUB_API_BASE}/repos/{repo}/contents/{encoded_path}"
        if ref:
            url += f"?ref={urllib.parse.quote(ref)}"
        status, payload, new_etag = self._request_json(url, etag=etag)
        if status == 304 or not isinstance(payload, dict):
            return None, None, new_etag

        content_b64 = payload.get("content", "")
        raw_text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        return raw_text, str(payload.get("sha", "")), new_etag

    def fetch_repo_metadata(self, repo: str) -> dict[str, Any]:
        """Fetch and cache repository stars, freshness, license, and default branch."""
        if repo in self._repo_cache:
            return self._repo_cache[repo]

        url = f"{GITHUB_API_BASE}/repos/{repo}"
        try:
            _, payload, _ = self._request_json(url)
        except urllib.error.HTTPError:
            meta = {"stars": 0, "days_since_update": 30, "license_spdx": "", "default_branch": "main"}
            self._repo_cache[repo] = meta
            return meta

        if not isinstance(payload, dict):
            meta = {"stars": 0, "days_since_update": 30, "license_spdx": "", "default_branch": "main"}
            self._repo_cache[repo] = meta
            return meta

        pushed_at = payload.get("pushed_at") or payload.get("updated_at")
        days_since = 30
        if isinstance(pushed_at, str):
            try:
                dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                days_since = max(0, (datetime.now(timezone.utc) - dt).days)
            except ValueError:
                days_since = 30

        lic = payload.get("license") or {}
        spdx = str(lic.get("spdx_id") or "") if isinstance(lic, dict) else ""
        if spdx == "NOASSERTION":
            spdx = ""

        meta = {
            "stars": int(payload.get("stargazers_count") or 0),
            "days_since_update": days_since,
            "license_spdx": spdx,
            "default_branch": str(payload.get("default_branch") or "main"),
        }
        self._repo_cache[repo] = meta
        return meta

    def _get_repo_tree(self, repo: str, branch: str) -> list[dict[str, Any]]:
        key = (repo, branch)
        if key in self._tree_cache:
            return self._tree_cache[key]

        url = f"{GITHUB_API_BASE}/repos/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1"
        try:
            _, payload, _ = self._request_json(url)
        except urllib.error.HTTPError:
            self._tree_cache[key] = []
            return []

        tree = payload.get("tree", []) if isinstance(payload, dict) else []
        self._tree_cache[key] = tree if isinstance(tree, list) else []
        return self._tree_cache[key]

    def _fetch_raw_bytes(
        self, repo: str, path: str, branch: str, blob_sha: str | None = None
    ) -> bytes:
        encoded_path = urllib.parse.quote(path.lstrip("/"))
        raw_url = f"{GITHUB_RAW_BASE}/{repo}/{urllib.parse.quote(branch)}/{encoded_path}"
        headers = {"User-Agent": "auto-skills-fetcher/0.1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(raw_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            if not blob_sha:
                raise
            blob_url = f"{GITHUB_API_BASE}/repos/{repo}/git/blobs/{blob_sha}"
            _, payload, _ = self._request_json(blob_url)
            if isinstance(payload, dict) and payload.get("content"):
                return base64.b64decode(payload["content"])
            raise

    def fetch_skill_bundle(
        self,
        repo: str,
        skill_md_path: str,
        *,
        ref: str | None = None,
        max_files: int = 50,
        max_file_bytes: int = 1_000_000,
    ) -> tuple[str | None, str | None, dict[str, bytes]]:
        """Fetch SKILL.md and all sibling assets (scripts, references, etc.) for a skill."""
        meta = self.fetch_repo_metadata(repo)
        branch = ref or meta.get("default_branch", "main")
        raw_text, sha, _ = self.fetch_skill_file(repo, skill_md_path, ref=branch)
        if not raw_text:
            return None, None, {}

        normalized = skill_md_path.lstrip("/")
        parent_dir = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
        tree = self._get_repo_tree(repo, branch)

        extra_files: dict[str, bytes] = {}
        for entry in tree:
            if len(extra_files) >= max_files:
                break
            if entry.get("type") != "blob":
                continue
            entry_path = str(entry.get("path") or "")
            if entry_path == normalized:
                continue
            size = int(entry.get("size") or 0)
            if size > max_file_bytes:
                continue

            if parent_dir:
                prefix = parent_dir + "/"
                if not entry_path.startswith(prefix):
                    continue
                rel_path = entry_path[len(prefix) :]
            else:
                if not entry_path.startswith(ROOT_SKILL_SUBDIRS):
                    continue
                rel_path = entry_path

            try:
                data = self._fetch_raw_bytes(repo, entry_path, branch, blob_sha=entry.get("sha"))
                extra_files[rel_path] = data
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue

        return raw_text, sha, extra_files

    def search_domain(
        self,
        domain: str,
        domain_cfg: dict[str, Any],
        *,
        per_page: int = 30,
        max_items: int = 60,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute partitioned Code Search queries for a domain (or return query plan on dry_run)."""
        queries = build_partitioned_queries(domain_cfg)
        if dry_run:
            return [{"domain": domain, "query": q} for q in queries]

        results: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for q in queries:
            if len(results) >= max_items:
                break
            url = (
                f"{GITHUB_API_BASE}/search/code?"
                f"q={urllib.parse.quote(q)}&per_page={min(per_page, 100)}"
            )
            _, payload, _ = self._request_json(url)
            if not isinstance(payload, dict):
                continue
            for item in payload.get("items", []):
                repo_full = item.get("repository", {}).get("full_name", "")
                file_path = item.get("path", "")
                key = f"{repo_full}/{file_path}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append(
                    {
                        "domain": domain,
                        "repo": repo_full,
                        "path": file_path,
                        "url": item.get("html_url", ""),
                        "sha": item.get("sha", ""),
                    }
                )
                if len(results) >= max_items:
                    break
        return results
