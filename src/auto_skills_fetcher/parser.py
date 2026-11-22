"""SKILL.md parser, validator, token estimator, and canonical SHA-256 hasher."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml

from .models import SkillDocument

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


class SkillParseError(ValueError):
    """Raised when a SKILL.md violates the Agent Skills open specification."""


def canonicalize_content(text: str) -> str:
    """Normalize line endings and trailing whitespace for stable deduplication."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip() + "\n"


def compute_content_hash(text: str) -> str:
    """Compute SHA-256 hash over canonicalized SKILL.md content."""
    return hashlib.sha256(canonicalize_content(text).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Lightweight language-agnostic token estimate (handles English and CJK)."""
    if not text:
        return 0
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    non_cjk_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    punctuation = len(re.findall(r"[^\w\s\u4e00-\u9fff]", text))
    return int(cjk_chars * 1.5 + non_cjk_words * 1.3 + punctuation * 0.4)


def parse_skill_md(
    raw_content: str,
    *,
    has_scripts: bool = False,
    has_references: bool = False,
    has_assets: bool = False,
) -> SkillDocument:
    """Parse and validate a SKILL.md string against the Agent Skills frontmatter spec."""
    match = FRONTMATTER_RE.match(raw_content.lstrip("\ufeff"))
    if not match:
        raise SkillParseError("Missing YAML frontmatter delimited by '---'.")

    fm_raw, body = match.group(1), match.group(2).strip()
    try:
        fm: Any = yaml.safe_load(fm_raw)
    except yaml.YAMLError as exc:
        raise SkillParseError(f"Invalid YAML frontmatter: {exc}") from exc

    if not isinstance(fm, dict):
        raise SkillParseError("YAML frontmatter must be a mapping.")

    name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()

    if not name:
        raise SkillParseError("Frontmatter missing required non-empty 'name'.")
    if not description:
        raise SkillParseError("Frontmatter missing required non-empty 'description'.")
    if not body:
        raise SkillParseError("SKILL.md body instructions cannot be empty.")

    return SkillDocument(
        name=name,
        description=description,
        body=body,
        raw_content=raw_content,
        content_hash=compute_content_hash(raw_content),
        estimated_tokens=estimate_tokens(raw_content),
        frontmatter=fm,
        has_scripts=has_scripts,
        has_references=has_references,
        has_assets=has_assets,
    )
