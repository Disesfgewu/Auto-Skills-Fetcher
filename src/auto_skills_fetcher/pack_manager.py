"""Skill pack assembly, inheritance resolution, trigger collision checks, lockfile & selective updates."""

from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .matcher import cosine_similarity
from .parser import compute_content_hash


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

    def _write_lock(self, data: dict[str, Any]) -> None:
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def assemble(
        self,
        pack_name: str,
        skill_payloads: list[dict[str, Any]],
        *,
        update_policy: str = "track-with-diff",
    ) -> list[CollisionWarning]:
        """Write resolved skills into target_dir/<skill_name>/SKILL.md and record skills-lock.json."""
        self.target_dir.mkdir(parents=True, exist_ok=True)
        lock = self._read_lock()
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

            lock["skills"][name] = {
                "slot": item.get("slot"),
                "repo": item.get("repo", ""),
                "path": item.get("path", ""),
                "commit_sha": item.get("commit_sha", ""),
                "content_hash": compute_content_hash(content),
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
            base_entry = dict(lock["skills"].get(skill_name, {}))
            base_entry.update(
                {
                    "commit_sha": new_commit_sha,
                    "content_hash": compute_content_hash(new_content),
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
