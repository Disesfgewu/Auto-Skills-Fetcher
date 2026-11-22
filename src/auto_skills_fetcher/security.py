"""Hard-gate static security & prompt-injection scanner for SKILL.md files and bundled assets."""

from __future__ import annotations

import re

from .models import SecurityFinding, SecurityReport

# Executable binary headers that should not be smuggled inside skill asset folders
BINARY_EXEC_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\x7fELF", "ELF executable"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit executable"),
    (b"\xfe\xed\xfa\xcf", "Mach-O 64-bit executable (BE)"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal binary"),
    (b"MZ", "Windows PE/MZ executable"),
)

# Invisible Unicode Tag block (U+E0000..U+E007F) and Bidi override chars (Trojan Source)
UNICODE_TAG_RE = re.compile(r"[\U000e0000-\U000e007f]+")
BIDI_OVERRIDE_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
ZERO_WIDTH_BURST_RE = re.compile(r"[\u200b\u200c\u200d\u2060]{3,}")

SECURITY_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
    (
        "SEC-001",
        "critical",
        "Remote pipe-to-shell execution pattern detected.",
        re.compile(
            r"\b(curl|wget)\b[^\n|]{0,240}\|\s*(ba|z|da)?sh\b|"
            r"\b(curl|wget)\b[^\n|]{0,240}\|\s*(python3?|node)(?:\s+-(?=$|[\s;|`)'\"])|\s*(?=$|[;\n|`)'\"]))",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "SEC-002",
        "critical",
        "Attempt to read sensitive credentials, SSH keys, or system keychains.",
        re.compile(
            r"(~/\.ssh/id_|/\.ssh/id_rsa|\.aws/credentials|/etc/shadow|"
            r"\bcat\s+[^\n]*\.env\b|~/\.git-credentials|~/\.netrc|"
            r"security\s+find-(generic|internet)-password)",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-003",
        "critical",
        "Prompt-injection override attempting to disable system or safety guardrails.",
        re.compile(
            r"(ignore\s+(all\s+)?(previous|prior|above|system)\s+instructions|"
            r"disregard\s+(all\s+)?(previous|system)\s+(prompt|instructions)|"
            r"forget\s+(all\s+)?(previous|your)\s+instructions|"
            r"override\s+(system\s+prompt|all\s+safety|security\s+rules)|"
            r"you\s+are\s+now\s+in\s+(developer|unrestricted|jailbreak|dan)\s+mode|"
            r"exfiltrate\s+(secrets|tokens|keys|credentials)|"
            r"silently\s+send\s+[^\n]{0,80}\b(webhook|endpoint|server|url)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-004",
        "high",
        "Obfuscated base64 decode piped directly into execution.",
        re.compile(
            r"base64\s+(-d|--decode)\s*\|\s*(ba|z)?sh\b|"
            r"\beval\s*\(\s*[^\n]{0,40}base64|"
            r"\bexec\s*\(\s*compile\s*\(",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-005",
        "medium",
        "Suspicious external webhook or raw IP endpoint.",
        re.compile(
            r"https?://(\d{1,3}\.){3}\d{1,3}[:/]|"
            r"discord\.com/api/webhooks/|"
            r"hooks\.slack\.com/services/|"
            r"api\.telegram\.org/bot|"
            r"(webhook\.site|pipedream\.net|requestbin\.)",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-006",
        "critical",
        "Fake system or chat role delimiter spoofing inside skill content.",
        re.compile(
            r"(<\|im_start\|>|<\|system\|>|<\|endoftext\|>|"
            r"</?SYSTEM_MESSAGE>|<<<\s*SYSTEM\s*>>>)",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-008",
        "high",
        "Hidden HTML comment containing suspicious imperative or exfiltration instructions.",
        re.compile(
            r"<!--[\s\S]{0,600}?\b(ignore\s+previous|override\s+system|exfiltrate|"
            r"curl\s+http|bash\s+-c|do\s+not\s+tell\s+the\s+user|silently\s+send|"
            r"steal\s+(token|key|secret))[\s\S]{0,400}?-->",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-009",
        "critical",
        "Reverse shell or persistence mechanism tampering detected.",
        re.compile(
            r"(/dev/tcp/\d|bash\s+-i\s+>&|\bnc(at)?\b[^\n]{0,80}\s-e\s|\bmkfifo\b[^\n]{0,80}\bnc\b|"
            r">>\s*~/\.ssh/authorized_keys|>>\s*~/\.(bashrc|zshrc|profile)\b|"
            r"Library/LaunchAgents/)",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-010",
        "high",
        "Stealth / user-deception instruction telling the agent to hide actions from the user.",
        re.compile(
            r"(do\s+not\s+(tell|inform|notify|alert|mention\s+this\s+to)\s+the\s+user|"
            r"hide\s+this\s+(action|output|command|step)\s+from\s+the\s+user|"
            r"without\s+the\s+user'?s?\s+(knowledge|consent|awareness)|"
            r"never\s+reveal\s+that\s+you\s+(ran|executed|sent))",
            re.IGNORECASE,
        ),
    ),
    (
        "SEC-011",
        "high",
        "Suspected environment secret exfiltration via HTTP request or markdown image beacon.",
        re.compile(
            r"(\b(curl|wget)\b[^\n]{0,200}(-d|--data|--data-raw|-F|--form)[^\n]{0,160}"
            r"\$?\{?(GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_SECRET_ACCESS_KEY|NPM_TOKEN)\}?"
            r"|!\[[^\]]*\]\(https?://[^)\s]*(\$\{?[A-Z_]*(TOKEN|KEY|SECRET)|\{?(api_key|secret|token)\}?))",
            re.IGNORECASE,
        ),
    ),
]


def _scan_unicode_smuggling(filename: str, text: str) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    if UNICODE_TAG_RE.search(text):
        findings.append(
            SecurityFinding(
                rule_id="SEC-007",
                severity="critical",
                description="Invisible Unicode Tag characters (U+E0000..U+E007F) detected — classic stealth prompt injection.",
                evidence=f"{filename}: contains invisible Unicode Tag sequence",
            )
        )
    if BIDI_OVERRIDE_RE.search(text):
        findings.append(
            SecurityFinding(
                rule_id="SEC-007",
                severity="critical",
                description="Unicode bidirectional override characters detected (Trojan Source smuggling).",
                evidence=f"{filename}: contains bidi override control characters",
            )
        )
    if ZERO_WIDTH_BURST_RE.search(text):
        findings.append(
            SecurityFinding(
                rule_id="SEC-007",
                severity="high",
                description="Suspicious sequence of invisible zero-width characters detected.",
                evidence=f"{filename}: contains repeated zero-width characters",
            )
        )
    return findings


def _scan_binary_assets(raw_assets: dict[str, bytes]) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    for rel_path, data in raw_assets.items():
        if not data:
            continue
        for magic, label in BINARY_EXEC_MAGICS:
            if data.startswith(magic):
                findings.append(
                    SecurityFinding(
                        rule_id="SEC-012",
                        severity="high",
                        description=f"Bundled compiled binary ({label}) found inside skill assets.",
                        evidence=f"{rel_path}: starts with {label} header",
                    )
                )
                break
    return findings


def scan_skill_security(
    content: str,
    extra_scripts: dict[str, str] | None = None,
    raw_assets: dict[str, bytes] | None = None,
) -> SecurityReport:
    """Run zero-tolerance static security & prompt-injection checks over SKILL.md and bundled assets."""
    findings: list[SecurityFinding] = []
    targets: list[tuple[str, str]] = [("SKILL.md", content)]
    if extra_scripts:
        targets.extend(extra_scripts.items())

    for filename, text in targets:
        findings.extend(_scan_unicode_smuggling(filename, text))
        for rule_id, severity, desc, pattern in SECURITY_RULES:
            for match in pattern.finditer(text):
                snippet = match.group(0).replace("\n", " ")[:120]
                findings.append(
                    SecurityFinding(
                        rule_id=rule_id,
                        severity=severity,
                        description=desc,
                        evidence=f"{filename}: {snippet}",
                    )
                )

    if raw_assets:
        findings.extend(_scan_binary_assets(raw_assets))

    blocked = any(f.severity in {"critical", "high"} for f in findings)
    return SecurityReport(passed=not blocked, blocked=blocked, findings=findings)
