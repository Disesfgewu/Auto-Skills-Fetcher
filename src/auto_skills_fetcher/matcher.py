"""Slot matching (cosine similarity) and Champion-Challenger replacement recommender."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .models import Recommendation, SkillRecord

TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "when", "into",
    "skill", "skills", "agent", "use", "using", "based", "tool", "tools", "file",
}


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 1 or "\u4e00" <= t <= "\u9fff"]


def cosine_similarity(text_a: str, text_b: str) -> float:
    """Compute lightweight token-frequency cosine similarity in [0, 1]."""
    vec_a = Counter(tokenize(text_a))
    vec_b = Counter(tokenize(text_b))
    if not vec_a or not vec_b:
        return 0.0
    common = set(vec_a) & set(vec_b)
    dot = sum(vec_a[k] * vec_b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def skill_signature(record: SkillRecord) -> str:
    return f"{record.name} {record.description}"


def recommend_replacements(
    champions: list[SkillRecord],
    candidates: list[SkillRecord],
    *,
    config: dict[str, Any] | None = None,
) -> list[Recommendation]:
    """Compare challengers against slot champions using similarity + hysteresis margins."""
    scoring_cfg = (config or {}).get("scoring", {})
    sim_threshold = float(scoring_cfg.get("slot_similarity_threshold", 0.78))
    new_cap_min = float(scoring_cfg.get("new_capability_min_score", 82.0))
    margins: dict[str, float] = scoring_cfg.get(
        "hysteresis_margins",
        {"academic": 12.0, "project_mgmt": 8.0, "secretary_deck": 6.0, "base": 10.0},
    )

    recommendations: list[Recommendation] = []
    for cand in candidates:
        if not cand.security_passed or cand.score is None:
            continue

        # Find best matching champion in the same domain
        same_domain_champs = [c for c in champions if c.domain == cand.domain and c.score is not None]
        best_champ: SkillRecord | None = None
        best_sim = 0.0

        for champ in same_domain_champs:
            # Explicit slot hint counts as exact slot match
            if cand.slot and champ.slot and cand.slot == champ.slot:
                sim = 1.0
            else:
                sim = cosine_similarity(skill_signature(cand), skill_signature(champ))
            if sim > best_sim:
                best_sim = sim
                best_champ = champ

        margin = float(margins.get(cand.domain, 10.0))

        if best_champ is not None and best_sim >= sim_threshold and best_champ.score is not None:
            delta = round(cand.score.total - best_champ.score.total, 2)
            if delta >= margin:
                recommendations.append(
                    Recommendation(
                        kind="replace",
                        domain=cand.domain,
                        slot=best_champ.slot,
                        challenger_id=cand.skill_id,
                        challenger_name=cand.name,
                        challenger_repo=cand.repo,
                        challenger_score=cand.score.total,
                        champion_id=best_champ.skill_id,
                        champion_name=best_champ.name,
                        champion_score=best_champ.score.total,
                        score_delta=delta,
                        similarity=round(best_sim, 3),
                        margin_required=margin,
                        rationale=(
                            f"Challenger '{cand.name}' ({cand.score.total}) exceeds champion "
                            f"'{best_champ.name}' ({best_champ.score.total}) by +{delta} "
                            f"(required >= {margin}, similarity={best_sim:.2f})."
                        ),
                    )
                )
        elif cand.score.total >= new_cap_min:
            recommendations.append(
                Recommendation(
                    kind="new_capability",
                    domain=cand.domain,
                    slot=None,
                    challenger_id=cand.skill_id,
                    challenger_name=cand.name,
                    challenger_repo=cand.repo,
                    challenger_score=cand.score.total,
                    score_delta=cand.score.total,
                    similarity=round(best_sim, 3),
                    margin_required=new_cap_min,
                    rationale=(
                        f"High-scoring new capability '{cand.name}' ({cand.score.total} >= {new_cap_min}) "
                        f"with no overlapping slot in domain '{cand.domain}'."
                    ),
                )
            )

    recommendations.sort(key=lambda r: r.score_delta, reverse=True)
    return recommendations
