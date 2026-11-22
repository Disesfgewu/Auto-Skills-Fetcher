"""Core data models for auto-skills-fetcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillDocument:
    """Parsed representation of a SKILL.md file."""

    name: str
    description: str
    body: str
    raw_content: str
    content_hash: str
    estimated_tokens: int
    frontmatter: dict[str, Any] = field(default_factory=dict)
    has_scripts: bool = False
    has_references: bool = False
    has_assets: bool = False


@dataclass
class SecurityFinding:
    """Single security rule match inside a skill or bundled script."""

    rule_id: str
    severity: str  # "critical" | "high" | "medium"
    description: str
    evidence: str


@dataclass
class SecurityReport:
    """Result of the hard-gate static security scan."""

    passed: bool
    blocked: bool
    findings: list[SecurityFinding] = field(default_factory=list)


@dataclass
class ScoreBreakdown:
    """Five-dimension weighted score (0-100)."""

    popularity: float
    freshness: float
    structure: float
    domain: float
    trust: float
    total: float
    judge_notes: list[str] = field(default_factory=list)


@dataclass
class SkillRecord:
    """Normalized skill entity stored in the local registry."""

    skill_id: str
    name: str
    description: str
    domain: str
    repo: str
    path: str
    url: str
    commit_sha: str
    content_hash: str
    raw_content: str
    stars: int = 0
    stars_90d_growth: int = 0
    curated_mentions: int = 0
    days_since_update: int = 0
    license_spdx: str = ""
    has_scripts: bool = False
    has_references: bool = False
    estimated_tokens: int = 0
    security_passed: bool = True
    score: ScoreBreakdown | None = None
    slot: str | None = None
    is_champion: bool = False


@dataclass
class Recommendation:
    """Champion-Challenger replacement or new-capability proposal."""

    kind: str  # "replace" | "new_capability"
    domain: str
    slot: str | None
    challenger_id: str
    challenger_name: str
    challenger_repo: str
    challenger_score: float
    champion_id: str | None = None
    champion_name: str | None = None
    champion_score: float | None = None
    score_delta: float = 0.0
    similarity: float = 0.0
    margin_required: float = 0.0
    rationale: str = ""
