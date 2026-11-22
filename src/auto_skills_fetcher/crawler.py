"""GitHub API crawler with size-partitioned queries, ETag caching, and seed watching."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

GITHUB_API_BASE = "https://api.github.com"


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
