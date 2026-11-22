"""GitHub API crawler with size-partitioned queries, ETag caching, and seed watching."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

        created_at = payload.get("created_at")
        created_days_ago = 365
        if isinstance(created_at, str):
            try:
                cdt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                created_days_ago = max(1, (datetime.now(timezone.utc) - cdt).days)
            except ValueError:
                created_days_ago = 365

        lic = payload.get("license") or {}
        spdx = str(lic.get("spdx_id") or "") if isinstance(lic, dict) else ""
        if spdx == "NOASSERTION":
            spdx = ""

        meta = {
            "stars": int(payload.get("stargazers_count") or 0),
            "days_since_update": days_since,
            "created_days_ago": created_days_ago,
            "license_spdx": spdx,
            "default_branch": str(payload.get("default_branch") or "main"),
            "description": str(payload.get("description") or ""),
            "topics": list(payload.get("topics") or []),
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

    def build_trending_repo_queries(
        self,
        domain: str,
        domain_cfg: dict[str, Any],
        *,
        days: int = 90,
        min_stars: int = 25,
    ) -> list[str]:
        """Build GitHub Repository Search queries to discover breakout & surging skill repos."""
        now = datetime.now(timezone.utc)
        created_cutoff = (now - timedelta(days=max(7, days))).strftime("%Y-%m-%d")
        pushed_cutoff = (now - timedelta(days=min(30, max(7, days // 3)))).strftime("%Y-%m-%d")

        topics = [str(t).strip() for t in domain_cfg.get("topics", []) if str(t).strip()]
        raw_kws = [str(k).strip().strip('"') for k in domain_cfg.get("keywords", [])[:4] if str(k).strip()]
        domain_hint = topics[0].replace("-", " ") if topics else (raw_kws[0] if raw_kws else domain)

        queries = [
            # Newly created viral/breakout agent skill repos in this domain
            f'"SKILL.md" {domain_hint} created:>{created_cutoff} stars:>={min_stars}',
            f'skills {domain_hint} created:>{created_cutoff} stars:>={min_stars}',
            # Recently surging active skill repos across GitHub (not limited to seed list)
            f'"agent skills" {domain_hint} pushed:>{pushed_cutoff} stars:>={max(min_stars, 50)}',
            f'"claude skills" {domain_hint} pushed:>{pushed_cutoff} stars:>={max(min_stars, 50)}',
        ]
        return queries

    def search_trending_repos(
        self,
        domain: str,
        domain_cfg: dict[str, Any],
        *,
        days: int = 90,
        min_stars: int = 25,
        max_repos: int = 10,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Discover newly exploding or rapidly rising skill repositories across all of GitHub."""
        queries = self.build_trending_repo_queries(
            domain, domain_cfg, days=days, min_stars=min_stars
        )
        if dry_run:
            return [{"domain": domain, "query": q} for q in queries]

        now = datetime.now(timezone.utc)
        by_repo: dict[str, dict[str, Any]] = {}
        for q in queries:
            url = (
                f"{GITHUB_API_BASE}/search/repositories?"
                f"q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page={min(max_repos, 20)}"
            )
            try:
                _, payload, _ = self._request_json(url)
            except urllib.error.HTTPError:
                continue
            if not isinstance(payload, dict):
                continue
            for item in payload.get("items", []):
                full_name = str(item.get("full_name") or "")
                if not full_name or full_name in by_repo:
                    continue
                stars = int(item.get("stargazers_count") or 0)
                created_days = 365
                created_at = item.get("created_at")
                if isinstance(created_at, str):
                    try:
                        cdt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        created_days = max(1, (now - cdt).days)
                    except ValueError:
                        created_days = 365

                pushed_days = 30
                pushed_at = item.get("pushed_at")
                if isinstance(pushed_at, str):
                    try:
                        pdt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                        pushed_days = max(0, (now - pdt).days)
                    except ValueError:
                        pushed_days = 30

                lic = item.get("license") or {}
                spdx = str(lic.get("spdx_id") or "") if isinstance(lic, dict) else ""
                if spdx == "NOASSERTION":
                    spdx = ""

                velocity = round(stars / max(1, created_days), 2)
                entry = {
                    "repo": full_name,
                    "url": str(item.get("html_url") or f"https://github.com/{full_name}"),
                    "description": str(item.get("description") or ""),
                    "stars": stars,
                    "created_days_ago": created_days,
                    "days_since_update": pushed_days,
                    "velocity_per_day": velocity,
                    "license_spdx": spdx,
                    "default_branch": str(item.get("default_branch") or "main"),
                    "topics": list(item.get("topics") or []),
                }
                by_repo[full_name] = entry
                self._repo_cache[full_name] = {
                    "stars": stars,
                    "days_since_update": pushed_days,
                    "created_days_ago": created_days,
                    "license_spdx": spdx,
                    "default_branch": entry["default_branch"],
                    "description": entry["description"],
                    "topics": entry["topics"],
                }

        ranked = sorted(
            by_repo.values(),
            key=lambda r: (r["velocity_per_day"], r["stars"]),
            reverse=True,
        )
        return ranked[:max_repos]

    def discover_skills_in_repo(
        self,
        repo: str,
        *,
        branch: str | None = None,
        domain_keywords: list[str] | None = None,
        max_skills: int = 5,
    ) -> list[str]:
        """Find SKILL.md paths inside a repository, prioritizing paths relevant to domain keywords."""
        meta = self.fetch_repo_metadata(repo)
        br = branch or meta.get("default_branch", "main")
        tree = self._get_repo_tree(repo, br)
        paths = [
            str(e.get("path"))
            for e in tree
            if e.get("type") == "blob" and str(e.get("path") or "").endswith("SKILL.md")
        ]
        if not paths:
            return []

        if domain_keywords:
            norm_kws = [
                k.strip().strip('"').lower().replace(" ", "-")
                for k in domain_keywords
                if k.strip()
            ]
            norm_words = [
                w for k in domain_keywords for w in k.strip().strip('"').lower().split() if len(w) > 2
            ]

            def _relevance(p: str) -> tuple[int, int]:
                pl = p.lower()
                score = sum(3 for kw in norm_kws if kw in pl) + sum(1 for w in norm_words if w in pl)
                return (score, -len(p))

            paths.sort(key=_relevance, reverse=True)

        return paths[:max_skills]
