"""Hard-gate static security scanner for SKILL.md files and bundled scripts."""

from __future__ import annotations

import re

from .models import SecurityFinding, SecurityReport

SECURITY_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
    (
        "SEC-001",
        "critical",
        "Remote pipe-to-shell execution pattern detected.",
        re.compile(r"\b(curl|wget)\b[^\n|]{0,200}\|\s*(ba)?sh\b", re.IGNORECASE),
    ),
    (
        "SEC-002",
        "critical",
        "Attempt to read sensitive credentials or SSH keys.",
        re.compile(r"(~/\.ssh|/\.ssh/id_rsa|\.aws/credentials|/etc/shadow|\bcat\s+\.env\b)", re.IGNORECASE),
    ),
    (
        "SEC-003",
        "critical",
        "Prompt-injection override attempting to disable system guardrails.",
        re.compile(
            r"(ignore\s+all\s+previous\s+instructions|disregard\s+system\s+prompt|"
            r"exfiltrate\s+(secrets|tokens|keys)|silently\s+send\s+.*\b(webhook|endpoint)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-004",
        "high",
        "Obfuscated base64 decode piped directly into execution.",
        re.compile(r"base64\s+(-d|--decode)\s*\|\s*(ba)?sh|eval\s*\(\s*base64", re.IGNORECASE),
    ),
    (
        "SEC-005",
        "medium",
        "Suspicious external webhook or raw IP exfiltration endpoint.",
        re.compile(r"https?://(\d{1,3}\.){3}\d{1,3}[:/]|discord\.com/api/webhooks/", re.IGNORECASE),
    ),
]


def scan_skill_security(content: str, extra_scripts: dict[str, str] | None = None) -> SecurityReport:
    """Run zero-tolerance static security checks over SKILL.md and any bundled scripts."""
    findings: list[SecurityFinding] = []
    targets: list[tuple[str, str]] = [("SKILL.md", content)]
    if extra_scripts:
        targets.extend(extra_scripts.items())

    for filename, text in targets:
        for rule_id, severity, desc, pattern in SECURITY_RULES:
            for match in pattern.finditer(text):
                snippet = match.group(0)[:120]
                findings.append(
                    SecurityFinding(
                        rule_id=rule_id,
                        severity=severity,
                        description=desc,
                        evidence=f"{filename}: {snippet}",
                    )
                )

    blocked = any(f.severity in {"critical", "high"} for f in findings)
    return SecurityReport(passed=not blocked, blocked=blocked, findings=findings)
