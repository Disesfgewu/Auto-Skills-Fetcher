"""Skill pack assembly, inheritance resolution, trigger collision checks, lockfile & selective updates."""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .matcher import cosine_similarity
from .parser import compute_content_hash
from .security import scan_skill_security

SUPPORTED_AGENTS = ("agy", "claude", "codex")


def resolve_agent_targets(
    agent: str,
    *,
    scope: str = "project",
    project_root: str | Path = ".",
    home_dir: str | Path | None = None,
) -> list[tuple[str, Path]]:
    """Resolve native skill directories for agy, claude, codex, or all (deduplicated)."""
    if scope not in {"project", "global"}:
        raise ValueError(f"Unsupported scope '{scope}'. Use 'project' or 'global'.")

    requested = list(SUPPORTED_AGENTS) if agent == "all" else [agent]
    for ag in requested:
        if ag not in SUPPORTED_AGENTS:
            raise ValueError(f"Unsupported agent '{ag}'. Supported: {', '.join(SUPPORTED_AGENTS)}, all.")

    root = Path(project_root).expanduser().resolve()
    home = Path(home_dir).expanduser().resolve() if home_dir else Path.home()

    if scope == "project":
        mapping = {
            "agy": root / ".agents" / "skills",
            "codex": root / ".agents" / "skills",
            "claude": root / ".claude" / "skills",
        }
    else:
        mapping = {
            "agy": home / ".gemini" / "config" / "skills",
            "claude": home / ".claude" / "skills",
            "codex": home / ".codex" / "skills",
        }

    # Deduplicate shared directories (e.g., agy + codex share .agents/skills in project scope)
    by_path: dict[Path, list[str]] = {}
    for ag in requested:
        target = mapping[ag]
        by_path.setdefault(target, []).append(ag)

    return [(",".join(ags), path) for path, ags in by_path.items()]


def bundle_cache_dir(db_path: str | Path, skill_id: str) -> Path:
    """Return local cache directory storing bundled scripts/references for a skill."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "__", skill_id).strip("_")
    return Path(db_path).parent / "bundles" / safe


def save_skill_bundle(bundle_dir: Path, extra_files: dict[str, bytes]) -> None:
    """Persist fetched sibling files for a skill inside its local bundle cache."""
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    if not extra_files:
        return
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in extra_files.items():
        clean_rel = Path(rel_path)
        if clean_rel.is_absolute() or ".." in clean_rel.parts:
            continue
        dest = bundle_dir / clean_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


@dataclass
class CollisionWarning:
    """Warning raised when two skills in the same pack have overlapping trigger descriptions."""

    skill_a: str
    skill_b: str
    similarity: float


@dataclass
class DiffReport:
    """Structured diff between an installed skill and a proposed replacement/update."""

    skill_name: str
    old_hash: str
    new_hash: str
    added_lines: int
    removed_lines: int
    unified_diff: str
    summary: str


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def save_yaml_config(path: str | Path, data: dict[str, Any]) -> None:
    """Write YAML config cleanly while preserving key order and UTF-8 text."""
    Path(path).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
        encoding="utf-8",
    )


@dataclass
class ExpansionProposal:
    """Proposed automatic pack expansion (add new capability or replace weaker champion)."""

    action: str  # "add" | "replace"
    pack_name: str
    domain: str
    slot: str
    skill_id: str
    name: str
    repo: str
    path: str
    url: str
    score: float
    stars: int
    stars_90d_growth: int
    replaces_name: str | None = None
    replaces_score: float | None = None
    similarity: float = 0.0
    rationale: str = ""


def plan_pack_expansion(
    packs_cfg: dict[str, Any],
    sources_cfg: dict[str, Any],
    pack_name: str,
    *,
    champions_by_id: dict[str, Any],
    candidates: list[Any],
    min_score_override: float | None = None,
    max_add_override: int | None = None,
    allow_replace: bool = False,
    collision_threshold: float = 0.72,
) -> list[ExpansionProposal]:
    """Plan safe, non-colliding auto-expansion proposals for any configured pack."""
    packs = packs_cfg.get("packs", {})
    if pack_name not in packs:
        raise KeyError(f"Unknown pack '{pack_name}'.")

    pack_cfg = packs[pack_name]
    domain = str(pack_cfg.get("domain", "base"))
    ae_cfg = pack_cfg.get("auto_expand", {}) if isinstance(pack_cfg.get("auto_expand"), dict) else {}
    if ae_cfg.get("enabled") is False:
        return []

    scoring_cfg = sources_cfg.get("scoring", {})
    sim_threshold = float(scoring_cfg.get("slot_similarity_threshold", 0.78))
    margin = float(
        pack_cfg.get(
            "hysteresis_margin",
            scoring_cfg.get("hysteresis_margins", {}).get(domain, 10.0),
        )
    )
    min_score = (
        float(min_score_override)
        if min_score_override is not None
        else float(ae_cfg.get("min_score", 78.0))
    )
    max_skills = int(ae_cfg.get("max_skills", 25))
    max_add = (
        int(max_add_override)
        if max_add_override is not None
        else int(ae_cfg.get("max_add_per_run", 3))
    )
    can_replace = bool(allow_replace or ae_cfg.get("allow_replace", False))

    resolved = resolve_pack_skills(packs_cfg, pack_name)
    existing_ids = {f"{item['repo']}:{item['path']}" for item in resolved}
    existing_names = {item["name"] for item in resolved}
    existing_sigs: list[tuple[dict[str, Any], str, float]] = []
    for item in resolved:
        sid = f"{item['repo']}:{item['path']}"
        rec = champions_by_id.get(sid)
        sig = f"{rec.name} {rec.description}" if rec else item["name"]
        sc = rec.score.total if (rec and rec.score) else 0.0
        existing_sigs.append((item, sig, sc))

    # Rank candidates by total score then 90d growth momentum
    ranked = sorted(
        candidates,
        key=lambda c: (
            c.score.total if c.score else 0.0,
            getattr(c, "stars_90d_growth", 0),
            getattr(c, "stars", 0),
        ),
        reverse=True,
    )

    proposals: list[ExpansionProposal] = []
    accepted_sigs: list[str] = []
    added_count = 0
    current_total = len(resolved)

    for cand in ranked:
        if cand.domain != domain:
            continue
        if not cand.security_passed or cand.score is None:
            continue
        # Defense-in-depth: re-verify static prompt-injection & security gate
        if not scan_skill_security(cand.raw_content).passed:
            continue
        if cand.skill_id in existing_ids or cand.name in existing_names:
            continue
        if cand.score.total < min_score:
            continue

        cand_sig = f"{cand.name} {cand.description}"
        best_item: dict[str, Any] | None = None
        best_sim = 0.0
        best_score = 0.0
        for item, sig, sc in existing_sigs:
            sim = cosine_similarity(cand_sig, sig)
            if sim > best_sim:
                best_sim = sim
                best_item = item
                best_score = sc

        # Prevent collisions with candidates already accepted in this same expansion run
        if any(cosine_similarity(cand_sig, prev_sig) >= collision_threshold for prev_sig in accepted_sigs):
            continue

        if best_item is not None and best_sim >= sim_threshold:
            delta = round(cand.score.total - best_score, 2)
            if can_replace and delta >= margin and best_item.get("source_pack") == pack_name:
                proposals.append(
                    ExpansionProposal(
                        action="replace",
                        pack_name=pack_name,
                        domain=domain,
                        slot=str(best_item.get("slot") or f"{domain}:{cand.name}"),
                        skill_id=cand.skill_id,
                        name=cand.name,
                        repo=cand.repo,
                        path=cand.path,
                        url=cand.url,
                        score=cand.score.total,
                        stars=cand.stars,
                        stars_90d_growth=cand.stars_90d_growth,
                        replaces_name=best_item["name"],
                        replaces_score=round(best_score, 2),
                        similarity=round(best_sim, 3),
                        rationale=(
                            f"Replaces '{best_item['name']}' ({best_score:.1f}) with '{cand.name}' "
                            f"({cand.score.total:.1f}, +{delta:.1f} >= {margin}, sim={best_sim:.2f})."
                        ),
                    )
                )
                existing_names.add(cand.name)
                existing_ids.add(cand.skill_id)
                accepted_sigs.append(cand_sig)
            continue

        # Reject ambiguous mid-similarity zone [collision_threshold, sim_threshold)
        if best_sim >= collision_threshold:
            continue

        if added_count >= max_add or current_total + added_count >= max_skills:
            continue

        clean_slot_name = re.sub(r"[^a-z0-9-]+", "-", cand.name.lower()).strip("-") or "skill"
        proposals.append(
            ExpansionProposal(
                action="add",
                pack_name=pack_name,
                domain=domain,
                slot=f"{domain}:{clean_slot_name}",
                skill_id=cand.skill_id,
                name=cand.name,
                repo=cand.repo,
                path=cand.path,
                url=cand.url,
                score=cand.score.total,
                stars=cand.stars,
                stars_90d_growth=cand.stars_90d_growth,
                similarity=round(best_sim, 3),
                rationale=(
                    f"Adds new non-colliding capability '{cand.name}' "
                    f"(score={cand.score.total:.1f}, stars={cand.stars}, 90d_growth={cand.stars_90d_growth}, "
                    f"max_sim={best_sim:.2f} < {collision_threshold})."
                ),
            )
        )
        existing_names.add(cand.name)
        existing_ids.add(cand.skill_id)
        accepted_sigs.append(cand_sig)
        added_count += 1

    return proposals


def apply_pack_expansion(
    packs_cfg: dict[str, Any],
    proposals: list[ExpansionProposal],
) -> int:
    """Mutate packs_cfg in memory with approved ExpansionProposals; returns count applied."""
    applied = 0
    packs = packs_cfg.get("packs", {})
    for prop in proposals:
        pack = packs.get(prop.pack_name)
        if not isinstance(pack, dict):
            continue
        skills_list = pack.setdefault("skills", [])
        entry = {
            "slot": prop.slot,
            "name": prop.name,
            "repo": prop.repo,
            "path": prop.path,
            "url": prop.url,
        }
        if prop.action == "replace" and prop.replaces_name:
            replaced = False
            for idx, item in enumerate(skills_list):
                if item.get("name") == prop.replaces_name or item.get("slot") == prop.slot:
                    skills_list[idx] = entry
                    replaced = True
                    break
            if not replaced:
                skills_list.append(entry)
            applied += 1
        elif prop.action == "add":
            if not any(
                item.get("name") == prop.name
                or (item.get("repo") == prop.repo and item.get("path") == prop.path)
                for item in skills_list
            ):
                skills_list.append(entry)
                applied += 1
    return applied


def resolve_pack_skills(packs_cfg: dict[str, Any], pack_name: str) -> list[dict[str, Any]]:
    """Resolve `extends` inheritance chain for a pack, overriding duplicate slots cleanly."""
    packs = packs_cfg.get("packs", {})
    if pack_name not in packs:
        raise KeyError(f"Unknown pack '{pack_name}'. Available: {sorted(packs.keys())}")

    visited: set[str] = set()
    ordered_by_slot: dict[str, dict[str, Any]] = {}

    def _collect(name: str) -> None:
        if name in visited:
            raise ValueError(f"Circular pack inheritance detected at '{name}'.")
        visited.add(name)
        cfg = packs[name]
        for parent in cfg.get("extends", []):
            _collect(parent)
        for item in cfg.get("skills", []):
            slot_key = item.get("slot") or item["name"]
            ordered_by_slot[slot_key] = {**item, "source_pack": name}

    _collect(pack_name)
    return list(ordered_by_slot.values())


def detect_trigger_collisions(
    skills: list[tuple[str, str]],
    *,
    threshold: float = 0.72,
) -> list[CollisionWarning]:
    """Detect pairs of skills whose descriptions are likely to collide during agent selection."""
    warnings: list[CollisionWarning] = []
    for i in range(len(skills)):
        name_a, desc_a = skills[i]
        for j in range(i + 1, len(skills)):
            name_b, desc_b = skills[j]
            sim = cosine_similarity(desc_a, desc_b)
            if sim >= threshold:
                warnings.append(
                    CollisionWarning(skill_a=name_a, skill_b=name_b, similarity=round(sim, 3))
                )
    return warnings


def build_semantic_diff(skill_name: str, old_content: str, new_content: str) -> DiffReport:
    """Compute unified diff and a concise human-readable change summary."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{skill_name}/SKILL.md (installed)",
            tofile=f"{skill_name}/SKILL.md (incoming)",
            lineterm="",
        )
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

    # Highlight new markdown headings or guardrail changes
    new_headings = [
        ln[1:].strip() for ln in diff_lines if ln.startswith("+#") and not ln.startswith("+++")
    ]
    heading_note = f"; added sections: {', '.join(new_headings[:3])}" if new_headings else ""
    summary = f"+{added}/-{removed} lines changed{heading_note}"

    return DiffReport(
        skill_name=skill_name,
        old_hash=compute_content_hash(old_content),
        new_hash=compute_content_hash(new_content),
        added_lines=added,
        removed_lines=removed,
        unified_diff="\n".join(diff_lines),
        summary=summary,
    )


class PackManager:
    """Manages assembled skill directories, skills-lock.json, selective updates, and rollbacks."""

    def __init__(self, target_dir: str | Path) -> None:
        self.target_dir = Path(target_dir)
        self.lock_path = self.target_dir / "skills-lock.json"
        self.backup_dir = self.target_dir / ".backups"

    def _read_lock(self) -> dict[str, Any]:
        if not self.lock_path.exists():
            return {"version": 1, "pack": "", "skills": {}}
        return json.loads(self.lock_path.read_text(encoding="utf-8"))

    def read_lock(self) -> dict[str, Any]:
        """Return parsed skills-lock.json contents (or empty structure if absent)."""
        return self._read_lock()

    def list_unmanaged_skills(self) -> list[str]:
        """List skill directories containing SKILL.md that are not managed by skillpack."""
        if not self.target_dir.is_dir():
            return []
        managed = set(self._read_lock().get("skills", {}).keys())
        unmanaged: list[str] = []
        for child in sorted(self.target_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in managed:
                continue
            if (child / "SKILL.md").is_file():
                unmanaged.append(child.name)
        return unmanaged

    @staticmethod
    def _copy_bundle(bundle_dir_raw: str | Path | None, dest_dir: Path) -> int:
        if not bundle_dir_raw:
            return 0
        bdir = Path(bundle_dir_raw)
        if not bdir.is_dir():
            return 0
        count = 0
        for src_file in bdir.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(bdir)
            dest_file = dest_dir / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            if dest_file.suffix == ".sh":
                os.chmod(dest_file, 0o755)
            count += 1
        return count

    def _write_lock(self, data: dict[str, Any]) -> None:
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def assemble(
        self,
        pack_name: str,
        skill_payloads: list[dict[str, Any]],
        *,
        update_policy: str = "track-with-diff",
        prune_previous: bool = False,
    ) -> list[CollisionWarning]:
        """Write resolved skills (plus bundled assets) into target_dir/<name>/ and record lockfile."""
        self.target_dir.mkdir(parents=True, exist_ok=True)
        lock = self._read_lock()
        previous_managed = set(lock.get("skills", {}).keys())
        incoming_names = {item["name"] for item in skill_payloads}

        if prune_previous:
            for old_name in sorted(previous_managed - incoming_names):
                old_dir = self.target_dir / old_name
                if old_dir.is_dir():
                    shutil.rmtree(old_dir)
                lock.get("skills", {}).pop(old_name, None)

        lock["pack"] = pack_name
        lock["update_policy"] = update_policy
        now = datetime.now(timezone.utc).isoformat()

        desc_pairs: list[tuple[str, str]] = []
        for item in skill_payloads:
            name = item["name"]
            content = item["raw_content"]
            skill_dir = self.target_dir / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

            bundled_count = self._copy_bundle(item.get("bundle_dir"), skill_dir)

            lock["skills"][name] = {
                "slot": item.get("slot"),
                "repo": item.get("repo", ""),
                "path": item.get("path", ""),
                "commit_sha": item.get("commit_sha", ""),
                "content_hash": compute_content_hash(content),
                "bundled_files": bundled_count,
                "state": "pinned" if update_policy == "pinned" else "tracked",
                "installed_at": now,
            }
            desc_pairs.append((name, item.get("description", name)))

        self._write_lock(lock)
        return detect_trigger_collisions(desc_pairs)

    def apply_update(
        self,
        skill_name: str,
        new_content: str,
        *,
        new_commit_sha: str,
        decision: str,  # "replace" | "keep" | "side-by-side"
        new_repo: str | None = None,
        bundle_dir: str | Path | None = None,
    ) -> str:
        """Apply user's explicit decision on an incoming skill update."""
        lock = self._read_lock()
        skill_file = self.target_dir / skill_name / "SKILL.md"
        if not skill_file.exists():
            raise FileNotFoundError(f"Skill '{skill_name}' is not installed in {self.target_dir}.")

        if decision == "keep":
            if skill_name in lock["skills"]:
                lock["skills"][skill_name]["state"] = "pinned"
                self._write_lock(lock)
            return f"Kept original '{skill_name}' and marked as pinned."

        now = datetime.now(timezone.utc).isoformat()
        if decision == "side-by-side":
            alt_name = f"{skill_name}-v2"
            alt_dir = self.target_dir / alt_name
            alt_dir.mkdir(parents=True, exist_ok=True)
            (alt_dir / "SKILL.md").write_text(new_content, encoding="utf-8")
            bundled_count = self._copy_bundle(bundle_dir, alt_dir)
            base_entry = dict(lock["skills"].get(skill_name, {}))
            base_entry.update(
                {
                    "commit_sha": new_commit_sha,
                    "content_hash": compute_content_hash(new_content),
                    "bundled_files": bundled_count,
                    "state": "side-by-side",
                    "installed_at": now,
                }
            )
            if new_repo:
                base_entry["repo"] = new_repo
            lock["skills"][alt_name] = base_entry
            self._write_lock(lock)
            return f"Installed challenger side-by-side as '{alt_name}'."

        if decision == "replace":
            # Back up current version before overwriting
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = self.backup_dir / skill_name / f"{ts}_SKILL.md"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_file, backup_path)

            skill_file.write_text(new_content, encoding="utf-8")
            bundled_count = self._copy_bundle(bundle_dir, skill_file.parent)
            entry = lock["skills"].setdefault(skill_name, {})
            entry.update(
                {
                    "commit_sha": new_commit_sha,
                    "content_hash": compute_content_hash(new_content),
                    "last_backup": str(backup_path),
                    "state": "tracked",
                    "updated_at": now,
                }
            )
            if bundle_dir is not None:
                entry["bundled_files"] = bundled_count
            if new_repo:
                entry["repo"] = new_repo
            self._write_lock(lock)
            return f"Replaced '{skill_name}' (backup saved to {backup_path.name})."

        raise ValueError(f"Unsupported update decision: {decision}")

    def rollback(self, skill_name: str) -> str:
        """Restore the most recent backup of a replaced skill."""
        lock = self._read_lock()
        entry = lock["skills"].get(skill_name)
        if not entry or not entry.get("last_backup"):
            raise FileNotFoundError(f"No backup recorded for '{skill_name}'.")

        backup_path = Path(entry["last_backup"])
        if not backup_path.exists():
            raise FileNotFoundError(f"Backup file missing: {backup_path}")

        skill_file = self.target_dir / skill_name / "SKILL.md"
        shutil.copy2(backup_path, skill_file)
        restored_content = skill_file.read_text(encoding="utf-8")
        entry["content_hash"] = compute_content_hash(restored_content)
        entry["state"] = "rolled-back"
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_lock(lock)
        return f"Rolled back '{skill_name}' from {backup_path.name}."
