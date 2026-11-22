# Auto-Skills-Fetcher (`skillpack`)

Automated discovery, security auditing, 5-dimension scoring, Champion–Challenger recommendation, and modular pack assembly for open-standard [Agent Skills (`SKILL.md`)](https://agentskills.io/home).

## Architecture Overview

1. **Multi-Source Crawler (`crawler.py`)**: Combines daily seed-repo watchlist monitoring with size-partitioned GitHub Code Search queries (`filename:SKILL.md`) to work around the 1,000-result API cap (inspired by [GitSkills](https://arxiv.org/html/2608.10906v1)).
2. **Hard-Gate Validator & Security Scanner (`parser.py`, `security.py`)**: Enforces valid YAML frontmatter (`name`, `description`), deduplicates via canonical SHA-256 content hashes, and blocks suspicious shell/exfiltration/prompt-injection patterns.
3. **5-Dimension Scoring Engine (`scorer.py`)**:
   $$S_{\text{total}} = 0.20\,S_{\text{pop}} + 0.15\,S_{\text{fresh}} + 0.20\,S_{\text{struct}} + 0.30\,S_{\text{domain}} + 0.15\,S_{\text{trust}}$$
   Includes domain-specific rubrics for **Academic Research**, **Project Management**, and **Executive Secretary & Presentations**, plus a pluggable LLM-as-a-Judge hook.
4. **Champion–Challenger Matcher (`matcher.py`)**: Matches discovered candidates to existing pack slots via cosine similarity and only recommends replacement when $S_{\text{challenger}} - S_{\text{champion}} \ge \Delta_{\text{margin}}$ (domain hysteresis: Academic `12`, Project Management `8`, Secretary/Deck `6`).
5. **Pack Assembler & Selective Updater (`pack_manager.py`, `cli.py`)**: Resolves pack inheritance (`extends: [base-office]`), detects trigger-description collisions, manages `skills-lock.json`, generates semantic diffs, and supports `replace`, `keep`, `side-by-side`, and one-command `rollback`.

## Preset Skill Packs (`config/packs.yaml`)

- **`academic-research`** (`pinned`, $\Delta \ge 12$): Literature review, citation/BibTeX verification, research lookup, academic paper drafting + `base-office`.
- **`project-mgmt`** (`track-with-diff`, $\Delta \ge 8$): Roadmap planning, sprint operations, multi-audience roadmap communication, YAML sprint status tracker + `base-office`.
- **`exec-secretary-deck`** (`side-by-side`, $\Delta \ge 6$): PowerPoint generation, theme styling, internal communications, meeting notes + `base-office`.

## Quick Start

```bash
# Install in editable mode
pip install -e .

# Initialize local SQLite registry from preset packs
skillpack init

# Check system status and configured packs
skillpack status

# Crawl GitHub for a domain (requires GITHUB_TOKEN for live API calls)
export GITHUB_TOKEN="ghp_your_token"
skillpack crawl --domain academic --dry-run

# Run Champion-Challenger recommendations against local registry
skillpack recommend --domain academic

# Assemble a skill pack into a target .skills directory (with collision check)
skillpack assemble --pack academic-research --target ./.skills/academic

# Inspect updates with semantic diff and selectively replace or keep
skillpack update --pack project-mgmt --strategy interactive

# Roll back a skill to its previous backup
skillpack rollback --pack project-mgmt --skill sprint-operations
```

## Running Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
