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

- **`academic-research`** (`pinned`, $\Delta \ge 12$, 21 skills): Full research lifecycle — progress & workflow tracking (`vibe-research-workflow`, `idea-evaluator`, `hypothesis-generation`), paper crawling & literature review (`paper-download`, `paper-lookup`, `literature-review`, `citation-management`), experiments & testing (`experimental-design`, `baseline-runner`, `ml-experiment-tracker`, `statistical-analysis`, `scientific-visualization`), academic ethics & peer review (`plagiarism-detection-guide`, `web-scraping-ethics-guide`, `scientific-critical-thinking`, `peer-review`, `rebuttal-guidance`), and paper drafting (`academic-paper`) + `base-office`.
- **`project-mgmt`** (`track-with-diff`, $\Delta \ge 8$, 24 skills, auto-expand enabled): Full PM Command Center & governance (`project-command-center` with deterministic `audit_project_hub.py`, `senior-architect`, `prd`, `plan-grilling`, `pre-mortem`, `okr`, `project-health`, `senior-pm`, `roadmap-planning`, `sprint-operations`, `sprint-plan`, `backlog-triage`, `dependency-management`, `capacity-planner`, `change-management`, `roadmap-communication`, `cs-project-manager`, `cs-agile-product-owner`, `spec-driven-workflow`, `delivery-manager`, `after-action-report`) + `base-office`.
- **`exec-secretary-deck`** (`side-by-side`, $\Delta \ge 6$, 15 skills, auto-expand enabled): PowerPoint & board deck building, theme styling, brand guidelines, doc co-authoring, chief-of-staff briefings, calendar automation, charts/diagrams, meeting notes & internal comms + `base-office`.

## Quick Start

```bash
# Install in editable mode
pip install -e .

# Sync full upstream SKILL.md + scripts/references for all preset packs (uses GITHUB_TOKEN if set)
export GITHUB_TOKEN="ghp_your_token"
skillpack sync

# Check system status and configured packs
skillpack status

# Inspect skills inside a pack (or what is currently installed in a project/global dir)
skillpack show --pack academic-research
skillpack show --agent all --scope project --project-root .

# Discover newly exploding / trending skill repos across ALL domains (not limited to fixed seed repos)
skillpack discover --domain all --days 90 --min-stars 30

# Auto-expand ALL packs (academic, project-mgmt, exec-secretary-deck) with security-verified, non-colliding skills
skillpack expand --pack all --discover            # preview proposals
skillpack expand --pack all --auto --agent all    # auto-promote into packs.yaml & deploy to agy/claude/codex

# Deep security & prompt-injection audit over installed skills and local registry
skillpack audit --agent all --scope project --registry

# Crawl GitHub for additional domain candidates
skillpack crawl --domain academic --dry-run
skillpack crawl --domain academic

# Run Champion-Challenger recommendations against local registry
skillpack recommend --domain academic

# Assemble a skill pack directly for agy, claude, and codex (project or global scope)
skillpack assemble --pack academic-research --agent all --scope project --fetch --prune
# Switch pack cleanly (removes only skillpack-managed skills from the previous pack)
skillpack assemble --pack exec-secretary-deck --agent all --scope project --fetch --prune

# Check whether installed skills have upstream updates or stronger Challengers
skillpack check-updates --agent all --scope project --show-diff
# Apply available updates interactively (replace / keep / side-by-side) with automatic backup
skillpack check-updates --agent all --scope project --no-fetch --apply interactive --show-diff

# Or install into a custom directory
skillpack assemble --pack academic-research --target ./.skills/academic --fetch

# Inspect updates with semantic diff and selectively replace or keep
skillpack update --target ./.agents/skills --skill sprint-operations --incoming ./new/SKILL.md --decision interactive

# Roll back a skill to its previous backup
skillpack rollback --target ./.agents/skills --skill sprint-operations

# Launch interactive AI Assistants (agy / claude / codex) + live visual board for ALL 3 packs:
skillpack launch --pack project-mgmt --agent agy --project-root . --open          # PM & Workforce Board
skillpack launch --pack academic-research --agent claude --project-root . --open  # Research, Papers, Experiments & Ethics Board
skillpack launch --pack exec-secretary-deck --agent codex --project-root . --open # Schedule, Decks & Approval Comms Board
# Read-only GitHub PM sync (Issues, PRs, Milestones, CI) — STRICT NO PUSH UNTIL APPROVAL
skillpack github-sync --project-root .
```

## Multi-Agent Target Paths (`--agent`)

| Agent | `--scope project` | `--scope global` |
| :--- | :--- | :--- |
| `agy` (Antigravity) | `<project>/.agents/skills/` | `~/.gemini/config/skills/` |
| `claude` (Claude Code) | `<project>/.claude/skills/` | `~/.claude/skills/` |
| `codex` (OpenAI Codex) | `<project>/.agents/skills/` | `~/.codex/skills/` |

## Running Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
