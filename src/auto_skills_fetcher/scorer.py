"""Five-dimension scoring engine with domain rubrics and pluggable LLM-as-a-Judge."""

from __future__ import annotations

import math
import re
from typing import Any, Callable

from .models import ScoreBreakdown, SkillDocument, SkillRecord

DEFAULT_WEIGHTS: dict[str, float] = {
    "popularity": 0.20,
    "freshness": 0.15,
    "structure": 0.20,
    "domain": 0.30,
    "trust": 0.15,
}

PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC"}

DOMAIN_RUBRICS: dict[str, list[tuple[str, float, re.Pattern[str]]]] = {
    "academic": [
        (
            "Anti-hallucination citation verification (DOI/OpenAlex/PubMed/arXiv)",
            35.0,
            re.compile(r"\b(doi|openalex|pubmed|arxiv|crossref|verify[_\s-]?citations?|bibtex)\b", re.IGNORECASE),
        ),
        (
            "Systematic review / screening methodology (PRISMA, inclusion/exclusion)",
            35.0,
            re.compile(r"\b(prisma|inclusion|exclusion|systematic|screening|meta-analysis)\b", re.IGNORECASE),
        ),
        (
            "Structured academic output (IMRaD, bibliography, reproducibility)",
            30.0,
            re.compile(r"\b(imrad|bibliography|reproducib|manuscript|literature\s+review)\b", re.IGNORECASE),
        ),
    ],
    "project_mgmt": [
        (
            "Machine-readable status tracking (YAML/JSON/tracker schema)",
            35.0,
            re.compile(r"\b(yaml|json|status\s+file|tracker|backlog|milestone|sprint-status)\b", re.IGNORECASE),
        ),
        (
            "Capacity, dependency, and risk checks",
            35.0,
            re.compile(r"\b(capacity|dependenc|blocker|risk|prioritiz|velocity|feasibility)\b", re.IGNORECASE),
        ),
        (
            "Multi-audience reporting (executive vs internal/team variants)",
            30.0,
            re.compile(r"\b(executive|stakeholder|retrospective|standup|outcome\s+roadmap)\b", re.IGNORECASE),
        ),
    ],
    "secretary_deck": [
        (
            "Visual layout & slide discipline (one message per slide, hierarchy, templates)",
            40.0,
            re.compile(r"\b(layout|storyboard|one\s+message|visual\s+hierarchy|theme|template|slide\s+master)\b", re.IGNORECASE),
        ),
        (
            "Post-generation verification / rendering check",
            30.0,
            re.compile(r"\b(inspect|render|preview|verify|overflow|contrast|alignment)\b", re.IGNORECASE),
        ),
        (
            "External-action confirmation guardrails (email/calendar safety)",
            30.0,
            re.compile(r"\b(confirm|approval|draft|before\s+sending|action\s+items|calendar|meeting\s+notes)\b", re.IGNORECASE),
        ),
    ],
}

LLMJudgeFn = Callable[[SkillDocument, str], tuple[float, list[str]]]


def _clamp(val: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, val))


def score_popularity(stars: int, stars_90d_growth: int, curated_mentions: int) -> float:
    """Log-scaled stars + 90-day momentum + curated directory endorsements."""
    star_component = min(60.0, math.log10(max(0, stars) + 1) * 15.0)
    growth_component = min(20.0, math.log10(max(0, stars_90d_growth) + 1) * 8.0)
    curated_component = min(20.0, curated_mentions * 7.0)
    return _clamp(star_component + growth_component + curated_component)


def score_freshness(days_since_update: int, half_life_days: float = 180.0) -> float:
    """Exponential time-decay score based on days since last SKILL.md commit."""
    days = max(0, days_since_update)
    return _clamp(100.0 * math.exp(-days / max(1.0, half_life_days)))


def score_structure(doc: SkillDocument, max_tokens: int = 5000) -> tuple[float, list[str]]:
    """Static engineering-quality score for SKILL.md."""
    points = 0.0
    notes: list[str] = []
    lower_body = doc.body.lower()

    # 1. Explicit trigger guidance (when to use / not use)
    if re.search(r"(when\s+to\s+use|use\s+this\s+skill|triggers?\s+on|適用|何時使用)", lower_body):
        points += 25.0
    else:
        notes.append("Missing explicit 'When to Use' trigger guidance.")

    # 2. Examples or workflow steps
    if re.search(r"(example|workflow|step\s+1|phase\s+1|checklist|步驟|範例)", lower_body):
        points += 25.0
    else:
        notes.append("Missing concrete workflow steps or examples.")

    # 3. Verification / self-check guardrails
    if re.search(r"(verify|validation|check|guardrail|do\s+not|never|驗證|檢查)", lower_body):
        points += 20.0
    else:
        notes.append("Missing verification or guardrail instructions.")

    # 4. Supporting resources (scripts/ or references/)
    if doc.has_scripts or doc.has_references:
        points += 15.0

    # 5. Progressive-disclosure token efficiency
    if 150 <= doc.estimated_tokens <= max_tokens:
        points += 15.0
    elif doc.estimated_tokens < 150:
        points += 5.0
        notes.append("Skill instructions are very short (<150 tokens).")
    else:
        points += 5.0
        notes.append(f"Skill exceeds recommended token budget ({doc.estimated_tokens} > {max_tokens}).")

    return _clamp(points), notes


def score_domain_heuristic(doc: SkillDocument, domain: str) -> tuple[float, list[str]]:
    """Domain-specific rubric scoring (baseline when no live LLM judge is attached)."""
    rubric = DOMAIN_RUBRICS.get(domain)
    if not rubric:
        return 70.0, [f"No domain-specific rubric configured for '{domain}'; applied neutral baseline."]

    text = f"{doc.description}\n{doc.body}"
    score = 0.0
    notes: list[str] = []
    for label, weight, pattern in rubric:
        matches = pattern.findall(text)
        if len(matches) >= 2:
            score += weight
        elif len(matches) == 1:
            score += weight * 0.65
            notes.append(f"Partial domain coverage: {label}")
        else:
            notes.append(f"Missing domain criterion: {label}")
    return _clamp(score), notes


def score_trust(repo: str, license_spdx: str, trusted_orgs: set[str], security_passed: bool) -> float:
    """Trust score based on org reputation, license clarity, and security gate."""
    if not security_passed:
        return 0.0
    org = repo.split("/", 1)[0] if "/" in repo else ""
    points = 35.0
    if org in trusted_orgs:
        points += 35.0
    if license_spdx in PERMISSIVE_LICENSES:
        points += 20.0
    elif license_spdx:
        points += 10.0
    if security_passed:
        points += 10.0
    return _clamp(points)


def evaluate_skill(
    record: SkillRecord,
    doc: SkillDocument,
    *,
    config: dict[str, Any] | None = None,
    llm_judge: LLMJudgeFn | None = None,
) -> ScoreBreakdown:
    """Compute the full 5-dimension weighted score for a SkillRecord."""
    scoring_cfg = (config or {}).get("scoring", {})
    weights = scoring_cfg.get("weights", DEFAULT_WEIGHTS)
    half_life = float(scoring_cfg.get("freshness_half_life_days", 180.0))
    max_tokens = int(scoring_cfg.get("max_recommended_tokens", 5000))
    trusted_orgs = set((config or {}).get("trusted_orgs", []))

    s_pop = score_popularity(record.stars, record.stars_90d_growth, record.curated_mentions)
    s_fresh = score_freshness(record.days_since_update, half_life)
    s_struct, struct_notes = score_structure(doc, max_tokens)

    if llm_judge is not None:
        s_domain, domain_notes = llm_judge(doc, record.domain)
        s_domain = _clamp(s_domain)
    else:
        s_domain, domain_notes = score_domain_heuristic(doc, record.domain)

    s_trust = score_trust(record.repo, record.license_spdx, trusted_orgs, record.security_passed)

    total = (
        weights.get("popularity", 0.20) * s_pop
        + weights.get("freshness", 0.15) * s_fresh
        + weights.get("structure", 0.20) * s_struct
        + weights.get("domain", 0.30) * s_domain
        + weights.get("trust", 0.15) * s_trust
    )

    return ScoreBreakdown(
        popularity=round(s_pop, 2),
        freshness=round(s_fresh, 2),
        structure=round(s_struct, 2),
        domain=round(s_domain, 2),
        trust=round(s_trust, 2),
        total=round(_clamp(total), 2),
        judge_notes=struct_notes + domain_notes,
    )
