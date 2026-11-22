"""SQLite local registry for indexed skills, scores, slots, and recommendations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Recommendation, ScoreBreakdown, SkillRecord

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    domain TEXT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    url TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_content TEXT NOT NULL,
    stars INTEGER DEFAULT 0,
    stars_90d_growth INTEGER DEFAULT 0,
    curated_mentions INTEGER DEFAULT 0,
    days_since_update INTEGER DEFAULT 0,
    license_spdx TEXT DEFAULT '',
    has_scripts INTEGER DEFAULT 0,
    has_references INTEGER DEFAULT 0,
    estimated_tokens INTEGER DEFAULT 0,
    security_passed INTEGER DEFAULT 1,
    score_pop REAL DEFAULT 0,
    score_fresh REAL DEFAULT 0,
    score_struct REAL DEFAULT 0,
    score_domain REAL DEFAULT 0,
    score_trust REAL DEFAULT 0,
    score_total REAL DEFAULT 0,
    judge_notes TEXT DEFAULT '[]',
    slot TEXT,
    is_champion INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_domain ON skills(domain);
CREATE INDEX IF NOT EXISTS idx_skills_hash ON skills(content_hash);
CREATE INDEX IF NOT EXISTS idx_skills_slot ON skills(slot);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    domain TEXT NOT NULL,
    slot TEXT,
    challenger_id TEXT NOT NULL,
    champion_id TEXT,
    score_delta REAL NOT NULL,
    similarity REAL NOT NULL,
    margin_required REAL NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etag_cache (
    cache_key TEXT PRIMARY KEY,
    etag TEXT NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repo_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    stars INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_repo_snapshots_repo ON repo_snapshots(repo, recorded_at DESC);
"""


class SkillRegistry:
    """SQLite-backed store for skills, deduplication, scores, and recommendations."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def has_content_hash(self, content_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM skills WHERE content_hash = ? LIMIT 1", (content_hash,)
        ).fetchone()
        return row is not None

    def upsert_skill(self, record: SkillRecord) -> None:
        now = datetime.now(timezone.utc).isoformat()
        sc = record.score or ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self.conn.execute(
            """
            INSERT INTO skills (
                skill_id, name, description, domain, repo, path, url, commit_sha,
                content_hash, raw_content, stars, stars_90d_growth, curated_mentions,
                days_since_update, license_spdx, has_scripts, has_references,
                estimated_tokens, security_passed, score_pop, score_fresh, score_struct,
                score_domain, score_trust, score_total, judge_notes, slot, is_champion,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                domain=excluded.domain,
                commit_sha=excluded.commit_sha,
                content_hash=excluded.content_hash,
                raw_content=excluded.raw_content,
                stars=excluded.stars,
                stars_90d_growth=excluded.stars_90d_growth,
                curated_mentions=excluded.curated_mentions,
                days_since_update=excluded.days_since_update,
                license_spdx=excluded.license_spdx,
                has_scripts=excluded.has_scripts,
                has_references=excluded.has_references,
                estimated_tokens=excluded.estimated_tokens,
                security_passed=excluded.security_passed,
                score_pop=excluded.score_pop,
                score_fresh=excluded.score_fresh,
                score_struct=excluded.score_struct,
                score_domain=excluded.score_domain,
                score_trust=excluded.score_trust,
                score_total=excluded.score_total,
                judge_notes=excluded.judge_notes,
                slot=COALESCE(excluded.slot, skills.slot),
                is_champion=excluded.is_champion,
                updated_at=excluded.updated_at
            """,
            (
                record.skill_id,
                record.name,
                record.description,
                record.domain,
                record.repo,
                record.path,
                record.url,
                record.commit_sha,
                record.content_hash,
                record.raw_content,
                record.stars,
                record.stars_90d_growth,
                record.curated_mentions,
                record.days_since_update,
                record.license_spdx,
                int(record.has_scripts),
                int(record.has_references),
                record.estimated_tokens,
                int(record.security_passed),
                sc.popularity,
                sc.freshness,
                sc.structure,
                sc.domain,
                sc.trust,
                sc.total,
                json.dumps(sc.judge_notes, ensure_ascii=False),
                record.slot,
                int(record.is_champion),
                now,
            ),
        )
        self.conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> SkillRecord:
        return SkillRecord(
            skill_id=row["skill_id"],
            name=row["name"],
            description=row["description"],
            domain=row["domain"],
            repo=row["repo"],
            path=row["path"],
            url=row["url"],
            commit_sha=row["commit_sha"],
            content_hash=row["content_hash"],
            raw_content=row["raw_content"],
            stars=row["stars"],
            stars_90d_growth=row["stars_90d_growth"],
            curated_mentions=row["curated_mentions"],
            days_since_update=row["days_since_update"],
            license_spdx=row["license_spdx"],
            has_scripts=bool(row["has_scripts"]),
            has_references=bool(row["has_references"]),
            estimated_tokens=row["estimated_tokens"],
            security_passed=bool(row["security_passed"]),
            score=ScoreBreakdown(
                popularity=row["score_pop"],
                freshness=row["score_fresh"],
                structure=row["score_struct"],
                domain=row["score_domain"],
                trust=row["score_trust"],
                total=row["score_total"],
                judge_notes=json.loads(row["judge_notes"] or "[]"),
            ),
            slot=row["slot"],
            is_champion=bool(row["is_champion"]),
        )

    def list_skills(
        self,
        *,
        domain: str | None = None,
        champions_only: bool = False,
        candidates_only: bool = False,
    ) -> list[SkillRecord]:
        clauses = ["security_passed = 1"]
        params: list[object] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if champions_only:
            clauses.append("is_champion = 1")
        if candidates_only:
            clauses.append("is_champion = 0")
        sql = f"SELECT * FROM skills WHERE {' AND '.join(clauses)} ORDER BY score_total DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def save_recommendations(self, recs: list[Recommendation]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for r in recs:
            self.conn.execute(
                """
                INSERT INTO recommendations (
                    kind, domain, slot, challenger_id, champion_id,
                    score_delta, similarity, margin_required, rationale, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    r.kind,
                    r.domain,
                    r.slot,
                    r.challenger_id,
                    r.champion_id,
                    r.score_delta,
                    r.similarity,
                    r.margin_required,
                    r.rationale,
                    now,
                ),
            )
        self.conn.commit()

    def get_etag(self, cache_key: str) -> str | None:
        row = self.conn.execute(
            "SELECT etag FROM etag_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return str(row["etag"]) if row else None

    def set_etag(self, cache_key: str, etag: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO etag_cache (cache_key, etag, checked_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET etag=excluded.etag, checked_at=excluded.checked_at
            """,
            (cache_key, etag, now),
        )
        self.conn.commit()

    def list_blocked_skills(self, *, domain: str | None = None) -> list[SkillRecord]:
        clauses = ["security_passed = 0"]
        params: list[object] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        sql = f"SELECT * FROM skills WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def record_repo_snapshot(
        self, repo: str, stars: int, *, created_days_ago: int | None = None
    ) -> int:
        """Record a repo star snapshot and return estimated 90-day star growth momentum."""
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        prev = self.conn.execute(
            "SELECT stars, recorded_at FROM repo_snapshots WHERE repo = ? ORDER BY recorded_at ASC LIMIT 1",
            (repo,),
        ).fetchone()

        self.conn.execute(
            "INSERT INTO repo_snapshots (repo, stars, recorded_at) VALUES (?, ?, ?)",
            (repo, int(stars), now),
        )
        self.conn.commit()

        snapshot_growth = 0
        if prev is not None:
            try:
                prev_dt = datetime.fromisoformat(str(prev["recorded_at"]))
                elapsed_days = max(0.0, (now_dt - prev_dt).total_seconds() / 86400.0)
                delta = max(0, int(stars) - int(prev["stars"]))
                if elapsed_days >= 0.04 and delta > 0:  # >= ~1 hour
                    snapshot_growth = int(delta * (90.0 / max(1.0, elapsed_days)))
            except ValueError:
                snapshot_growth = 0

        age_growth = 0
        if created_days_ago is not None and stars > 0:
            age = max(1, int(created_days_ago))
            if age <= 90:
                age_growth = int(stars)
            else:
                age_growth = int(stars * (90.0 / age))

        return max(snapshot_growth, age_growth)

    def stats(self) -> dict[str, int]:
        total = self.conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        champions = self.conn.execute("SELECT COUNT(*) FROM skills WHERE is_champion = 1").fetchone()[0]
        blocked = self.conn.execute("SELECT COUNT(*) FROM skills WHERE security_passed = 0").fetchone()[0]
        pending_recs = self.conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE status = 'pending'"
        ).fetchone()[0]
        return {
            "total_skills": total,
            "champions": champions,
            "blocked": blocked,
            "pending_recommendations": pending_recs,
        }
