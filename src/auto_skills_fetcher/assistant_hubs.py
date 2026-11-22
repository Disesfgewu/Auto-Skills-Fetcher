"""Pack-specific workspace hubs, audit evaluators, and interactive assistant prompts.

Supports all three domain packs:
- `project-mgmt` -> `project-hub/` (architecture, charter, objectives, tasks, team capacity, QA gate)
- `academic-research` -> `research-hub/` (proposal, milestones, papers/PRISMA, experiments, ethics gate)
- `exec-secretary-deck` -> `secretary-hub/` (briefing, schedule, action items, slide decks, approval comms)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

DEFAULT_HUB_DIRNAME: dict[str, str] = {
    "project-mgmt": "project-hub",
    "academic-research": "research-hub",
    "exec-secretary-deck": "secretary-hub",
}


def default_hub_dirname(pack_name: str) -> str:
    return DEFAULT_HUB_DIRNAME.get(pack_name, "project-hub")


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
        encoding="utf-8",
    )


def _parse_date(val: Any) -> date | None:
    if not val:
        return None
    try:
        return date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sample Hub Initializers
# ---------------------------------------------------------------------------


def init_academic_hub(hub_dir: Path) -> None:
    hub_dir.mkdir(parents=True, exist_ok=True)
    if not (hub_dir / "PROPOSAL.md").exists():
        (hub_dir / "PROPOSAL.md").write_text(
            "# 研究計劃書與論文架構 (Research Proposal)\n\n"
            "## 研究問題與核心假說\n"
            "- 探討自動化技能檢索與安全硬閘門如何提升 AI Agent 在學術研究流程中的正確性與可重現性。\n\n"
            "## 研究方法流程圖\n"
            "```mermaid\n"
            "flowchart LR\n"
            "  A[\"文獻爬取 (arXiv / OpenAlex)\"] --> B[\"PRISMA 篩選與引文查核\"]\n"
            "  B --> C[\"對照實驗與基準測試\"]\n"
            "  C --> D[\"統計檢定與視覺化\"]\n"
            "  D --> E[\"學術倫理/抄襲查核與投稿\"]\n"
            "```\n",
            encoding="utf-8",
        )
    if not (hub_dir / "progress.yaml").exists():
        _write_yaml(
            hub_dir / "progress.yaml",
            {
                "version": 1,
                "project_name": "Academic Research Hub",
                "target_venue": "ACL / NeurIPS / IEEE",
                "milestones": [
                    {"id": "M-01", "stage": "literature", "title": "完成 PRISMA 系統性文獻爬取與篩選", "due_date": "2026-12-01", "status": "done", "completion_pct": 100, "notes": "已收集 42 篇核心論文"},
                    {"id": "M-02", "stage": "experiment", "title": "完成 Baseline 與主要模型對照實驗", "due_date": "2026-12-10", "status": "in_progress", "completion_pct": 60, "notes": "尚缺消融實驗 Ablation Study"},
                    {"id": "M-03", "stage": "ethics_review", "title": "完成原創性/抄襲查核、引文驗證與可重現性稽核", "due_date": "2026-12-16", "status": "todo", "completion_pct": 20, "notes": "需確認所有 DOI 與 BibTeX"},
                    {"id": "M-04", "stage": "writing", "title": "完成 IMRaD 論文初稿與圖表標準化", "due_date": "2026-12-22", "status": "todo", "completion_pct": 0, "notes": ""},
                ],
            },
        )
    if not (hub_dir / "papers.yaml").exists():
        _write_yaml(
            hub_dir / "papers.yaml",
            {
                "version": 1,
                "papers": [
                    {"id": "P-001", "title": "GitSkills: Large-Scale Empirical Study of Agent Skills", "authors": "Zhang et al.", "year": 2026, "source": "arXiv:2608.10906", "status": "included", "citation_verified": True, "key_finding": "提出分區爬取與技能品質評估框架"},
                    {"id": "P-002", "title": "PRISMA 2020 Explanation and Elaboration", "authors": "Page et al.", "year": 2021, "source": "DOI:10.1136/bmj.n160", "status": "included", "citation_verified": True, "key_finding": "系統性文獻回顧標準回報清單"},
                    {"id": "P-003", "title": "Automated Literature Screening with LLMs", "authors": "Lin et al.", "year": 2025, "source": "arXiv:2504.01922", "status": "screened", "citation_verified": False, "key_finding": "待查核 DOI 與實驗設定細節"},
                ],
            },
        )
    if not (hub_dir / "experiments.yaml").exists():
        _write_yaml(
            hub_dir / "experiments.yaml",
            {
                "version": 1,
                "experiments": [
                    {"id": "EXP-01", "name": "Baseline Keyword Retrieval", "method": "BM25 + Regex", "dataset": "SkillBench-v1", "primary_metric": "F1 = 0.71", "baseline_delta": "+0.00", "reproducible": True, "status": "done", "seed": 42},
                    {"id": "EXP-02", "name": "5-Dimension Hybrid Scorer", "method": "Multi-Dim + Security Gate", "dataset": "SkillBench-v1", "primary_metric": "F1 = 0.86", "baseline_delta": "+0.15", "reproducible": True, "status": "done", "seed": 42},
                    {"id": "EXP-03", "name": "Ablation without Security Hard-Gate", "method": "No SEC-Gate", "dataset": "Adversarial-100", "primary_metric": "Pending", "baseline_delta": "-", "reproducible": False, "status": "running", "seed": 42},
                ],
            },
        )
    if not (hub_dir / "ethics_audit.yaml").exists():
        _write_yaml(
            hub_dir / "ethics_audit.yaml",
            {
                "version": 1,
                "checks": [
                    {"id": "ETH-01", "category": "Citation Integrity", "item": "所有引用皆經 DOI / OpenAlex / arXiv 查核無幻覺引用", "status": "warning", "evidence": "P-003 尚未驗證 DOI"},
                    {"id": "ETH-02", "category": "Originality & Plagiarism", "item": "論文段落原創性與改寫相似度檢查", "status": "pending", "evidence": "待初稿完成後執行"},
                    {"id": "ETH-03", "category": "Data & Scraping Ethics", "item": "論文與資料集爬取遵守 API 條款與開放授權", "status": "passed", "evidence": "皆使用 GitHub / arXiv / OpenAlex 官方 API"},
                    {"id": "ETH-04", "category": "Reproducibility", "item": "隨機種子、環境依賴與實驗腳本皆已固定記錄", "status": "passed", "evidence": "Seed=42 記錄於 experiments.yaml"},
                ],
            },
        )


def init_secretary_hub(hub_dir: Path) -> None:
    hub_dir.mkdir(parents=True, exist_ok=True)
    if not (hub_dir / "BRIEFING.md").exists():
        (hub_dir / "BRIEFING.md").write_text(
            "# 主管每日簡報與決策備忘 (Executive Briefing)\n\n"
            "## 今日重點摘要\n"
            "- 下午季度進度簡報需完成最終版投影片視覺檢核（`DECK-01`）。\n"
            "- 有 1 封對外合作確認信件待主管審批後才可寄出（`COM-01`）。\n\n"
            "## 會議注意事項\n"
            "- 提醒確認下週跨部門里程碑與人力資源核准。\n",
            encoding="utf-8",
        )
    if not (hub_dir / "schedule.yaml").exists():
        _write_yaml(
            hub_dir / "schedule.yaml",
            {
                "version": 1,
                "project_name": "Executive Secretary & Deck Hub",
                "events": [
                    {"id": "EVT-01", "title": "Q4 專案進度與路線圖檢討會", "date": "2026-11-23", "time": "10:00-11:00", "attendees": ["CEO", "PM Lead", "Tech Lead"], "prep_status": "ready", "deck_linked": "DECK-01", "location": "會議室 A / Meet"},
                    {"id": "EVT-02", "title": "外部研究合作夥伴對齊會議", "date": "2026-11-24", "time": "14:30-15:30", "attendees": ["Partner PI", "Director"], "prep_status": "in_progress", "deck_linked": "DECK-02", "location": "Zoom"},
                ],
            },
        )
    if not (hub_dir / "action_items.yaml").exists():
        _write_yaml(
            hub_dir / "action_items.yaml",
            {
                "version": 1,
                "items": [
                    {"id": "ACT-01", "meeting": "週一主管例會", "task": "彙整各組本週交付清單並更新 Q4 董事會簡報數據", "owner": "Secretary", "due_date": "2026-11-23", "priority": "P0", "status": "in_progress"},
                    {"id": "ACT-02", "meeting": "週一主管例會", "task": "整理會議紀要與決議事項並分發與會人員", "owner": "Secretary", "due_date": "2026-11-22", "priority": "P1", "status": "done"},
                    {"id": "ACT-03", "meeting": "合作對齊會前準備", "task": "確認合作備忘錄重點並排定簽署時程", "owner": "Director", "due_date": "2026-11-25", "priority": "P1", "status": "todo"},
                ],
            },
        )
    if not (hub_dir / "decks.yaml").exists():
        _write_yaml(
            hub_dir / "decks.yaml",
            {
                "version": 1,
                "decks": [
                    {"id": "DECK-01", "title": "Q4 季度營運與研發進度簡報", "audience": "Executive / Board", "theme": "Executive Dark Slate", "slides_count": 14, "due_date": "2026-11-23", "status": "visual_qa", "file_path": "./decks/q4-review.pptx", "review_notes": "待確認第 8 頁圖表字體對比度"},
                    {"id": "DECK-02", "title": "研究合作提案簡報", "audience": "External Partners", "theme": "Clean Academic Light", "slides_count": 10, "due_date": "2026-11-24", "status": "drafting", "file_path": "./decks/partner-pitch.pptx", "review_notes": "尚待補齊里程碑甘特頁"},
                ],
            },
        )
    if not (hub_dir / "comms_drafts.yaml").exists():
        _write_yaml(
            hub_dir / "comms_drafts.yaml",
            {
                "version": 1,
                "drafts": [
                    {"id": "COM-01", "channel": "Email", "recipient": "partner-lead@example.org", "subject": "Re: 下週研究合作會議議程與簡報初稿確認", "status": "awaiting_approval", "summary": "確認 11/24 14:30 議程、附帶討論題綱，需主管核准後才寄出"},
                    {"id": "COM-02", "channel": "Internal Comms", "recipient": "#all-hands", "subject": "本週專案進度摘要與重要時程提醒", "status": "draft", "summary": "每週五團隊同步公告草稿"},
                ],
            },
        )


def init_hub_for_pack(pack_name: str, hub_dir: Path, *, pm_initializer: Any = None) -> None:
    if pack_name == "academic-research":
        init_academic_hub(hub_dir)
    elif pack_name == "exec-secretary-deck":
        init_secretary_hub(hub_dir)
    elif pm_initializer is not None:
        pm_initializer(hub_dir)


# ---------------------------------------------------------------------------
# State Builders & Audits for Academic and Secretary Hubs
# ---------------------------------------------------------------------------


def build_academic_state(hub_dir: Path, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    prog = _read_yaml(hub_dir / "progress.yaml")
    papers_data = _read_yaml(hub_dir / "papers.yaml")
    exps_data = _read_yaml(hub_dir / "experiments.yaml")
    eth_data = _read_yaml(hub_dir / "ethics_audit.yaml")
    proposal_md = (hub_dir / "PROPOSAL.md").read_text(encoding="utf-8") if (hub_dir / "PROPOSAL.md").is_file() else ""

    milestones = prog.get("milestones", [])
    papers = papers_data.get("papers", [])
    experiments = exps_data.get("experiments", [])
    checks = eth_data.get("checks", [])

    red_issues: list[str] = []
    amber_issues: list[str] = []

    for m in milestones:
        due = _parse_date(m.get("due_date"))
        if due and m.get("status") != "done" and due < today:
            red_issues.append(f"[OVERDUE] 研究里程碑 {m.get('id')} ({m.get('title')}) 已逾期（原定 {due}）。")
    unverified_citations = [p for p in papers if p.get("status") in {"included", "screened"} and not p.get("citation_verified")]
    if unverified_citations:
        amber_issues.append(f"[CITATION] 有 {len(unverified_citations)} 篇文獻尚未完成 DOI/arXiv 引文查核：{', '.join(str(p.get('id')) for p in unverified_citations[:4])}。")
    for c in checks:
        st = c.get("status")
        if st == "failed":
            red_issues.append(f"[ETHICS-FAIL] {c.get('id')} ({c.get('category')}): {c.get('item')}")
        elif st == "warning":
            amber_issues.append(f"[ETHICS-WARN] {c.get('id')} ({c.get('category')}): {c.get('item')}")
    non_repro = [e for e in experiments if e.get("status") == "done" and not e.get("reproducible")]
    if non_repro:
        red_issues.append(f"[REPRO] 已完成實驗 {', '.join(str(e.get('id')) for e in non_repro)} 未標記為可重現。")

    rag = "RED" if red_issues else ("AMBER" if amber_issues else "GREEN")
    return {
        "pack": "academic-research",
        "hub_path": str(hub_dir.resolve()),
        "project_name": prog.get("project_name") or "Academic Research Hub",
        "target_venue": prog.get("target_venue", ""),
        "milestones": milestones,
        "papers": papers,
        "experiments": experiments,
        "ethics_checks": checks,
        "proposal_md": proposal_md,
        "audit": {
            "as_of": today.isoformat(),
            "rag_status": rag,
            "red_issues": red_issues,
            "amber_issues": amber_issues,
        },
    }


def build_secretary_state(hub_dir: Path, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    sched = _read_yaml(hub_dir / "schedule.yaml")
    act = _read_yaml(hub_dir / "action_items.yaml")
    decks_data = _read_yaml(hub_dir / "decks.yaml")
    comms_data = _read_yaml(hub_dir / "comms_drafts.yaml")
    briefing_md = (hub_dir / "BRIEFING.md").read_text(encoding="utf-8") if (hub_dir / "BRIEFING.md").is_file() else ""

    events = sched.get("events", [])
    items = act.get("items", [])
    decks = decks_data.get("decks", [])
    drafts = comms_data.get("drafts", [])

    red_issues: list[str] = []
    amber_issues: list[str] = []

    for it in items:
        due = _parse_date(it.get("due_date"))
        if due and it.get("status") != "done" and due < today:
            red_issues.append(f"[OVERDUE] 會議待辦 {it.get('id')} ({it.get('task')}) 已逾期（截止 {due}）。")
    for dk in decks:
        due = _parse_date(dk.get("due_date"))
        if due and dk.get("status") != "ready" and (due - today).days <= 1:
            amber_issues.append(f"[DECK-DUE] 簡報 {dk.get('id')} ({dk.get('title')}) 將於 {due} 到期，目前狀態為 {dk.get('status')}。")
    awaiting = [d for d in drafts if d.get("status") == "awaiting_approval"]
    if awaiting:
        amber_issues.append(f"[APPROVAL] 有 {len(awaiting)} 份外寄/公告草稿等待主管確認核准：{', '.join(str(d.get('id')) for d in awaiting)}。")

    rag = "RED" if red_issues else ("AMBER" if amber_issues else "GREEN")
    return {
        "pack": "exec-secretary-deck",
        "hub_path": str(hub_dir.resolve()),
        "project_name": sched.get("project_name") or "Executive Secretary & Deck Hub",
        "events": events,
        "action_items": items,
        "decks": decks,
        "comms_drafts": drafts,
        "briefing_md": briefing_md,
        "audit": {
            "as_of": today.isoformat(),
            "rag_status": rag,
            "red_issues": red_issues,
            "amber_issues": amber_issues,
        },
    }


def apply_pack_item_patch(pack_name: str, hub_dir: Path, payload: dict[str, Any]) -> bool:
    """Apply lightweight interactive UI updates back to the pack's YAML files."""
    item_id = str(payload.get("id") or payload.get("task_id") or "")
    kind = str(payload.get("kind") or "")
    if not item_id:
        return False

    mapping: dict[tuple[str, str], tuple[str, str, set[str]]] = {
        ("academic-research", "milestone"): ("progress.yaml", "milestones", {"status", "completion_pct", "due_date", "notes"}),
        ("academic-research", "paper"): ("papers.yaml", "papers", {"status", "citation_verified", "key_finding"}),
        ("academic-research", "experiment"): ("experiments.yaml", "experiments", {"status", "reproducible", "primary_metric", "baseline_delta"}),
        ("academic-research", "ethics"): ("ethics_audit.yaml", "checks", {"status", "evidence"}),
        ("exec-secretary-deck", "action_item"): ("action_items.yaml", "items", {"status", "priority", "owner", "due_date"}),
        ("exec-secretary-deck", "deck"): ("decks.yaml", "decks", {"status", "review_notes", "due_date"}),
        ("exec-secretary-deck", "draft"): ("comms_drafts.yaml", "drafts", {"status"}),
        ("exec-secretary-deck", "event"): ("schedule.yaml", "events", {"prep_status"}),
    }
    spec = mapping.get((pack_name, kind))
    if not spec:
        return False
    fname, list_key, allowed = spec
    fpath = hub_dir / fname
    data = _read_yaml(fpath)
    rows = data.get(list_key, [])
    updated = False
    for row in rows:
        if isinstance(row, dict) and str(row.get("id")) == item_id:
            for k, v in payload.items():
                if k in allowed:
                    row[k] = v
            updated = True
            break
    if updated:
        _write_yaml(fpath, data)
    return updated


def build_pack_assistant_prompt(
    pack_name: str,
    user_message: str,
    hub_dir: Path,
    live_state: dict[str, Any],
    *,
    history: list[dict[str, str]] | None = None,
    include_history: bool = False,
) -> str:
    """Build domain-tailored prompt embedding live workspace snapshot for agy/claude/codex."""
    if pack_name == "academic-research":
        role_desc = "你是本專案的互動式「學術研究指揮中心助手」（請活用已安裝的 academic-research 技能包：進度追蹤、paper-download/paper-lookup、literature-review、citation-management、experimental-design、baseline-runner、ml-experiment-tracker、statistical-analysis、plagiarism-detection-guide、scientific-critical-thinking、peer-review）。"
        files_desc = "包含: PROPOSAL.md (研究計劃書), progress.yaml (研究里程碑), papers.yaml (論文爬取與篩選庫), experiments.yaml (實驗與基準測試紀錄), ethics_audit.yaml (學術倫理/原創性/可重現性稽核)。"
        snapshot = {
            "project_name": live_state.get("project_name"),
            "target_venue": live_state.get("target_venue"),
            "audit": live_state.get("audit"),
            "milestones": live_state.get("milestones"),
            "papers": live_state.get("papers"),
            "experiments": live_state.get("experiments"),
            "ethics_checks": live_state.get("ethics_checks"),
        }
    elif pack_name == "exec-secretary-deck":
        role_desc = "你是本專案的互動式「個人秘書與簡報助手」（請活用已安裝的 exec-secretary-deck 技能包：pptx、board-deck-builder、theme-factory、brand-guidelines、meeting-notes、calendar-automation、chief-of-staff、internal-comms、chart-designer、diagram-creator）。安全護欄：任何對外寄出信件或行事曆邀請前，必須先設為 awaiting_approval 並請求使用者確認！"
        files_desc = "包含: BRIEFING.md (主管簡報與決策備忘), schedule.yaml (會議行程), action_items.yaml (會議紀要待辦), decks.yaml (簡報製作進度), comms_drafts.yaml (待確認溝通草稿)。"
        snapshot = {
            "project_name": live_state.get("project_name"),
            "audit": live_state.get("audit"),
            "events": live_state.get("events"),
            "action_items": live_state.get("action_items"),
            "decks": live_state.get("decks"),
            "comms_drafts": live_state.get("comms_drafts"),
        }
    else:
        role_desc = (
            "你是本專案的互動式「專案管理指揮中心助手」（請遵循 project-command-center 與已安裝的 project-mgmt 技能包）。"
            "最高安全紅線（STRICT NO PUSH UNTIL APPROVAL）：你可以唯讀檢視 GitHub Issues / PRs / Milestones / CI 狀態，"
            "但【嚴禁】自行執行 git push、gh issue create/edit/close、gh pr create/merge 或任何會寫入 GitHub 的操作！"
            "任何需要寫入 GitHub 的建議（如開 Issue、改派負責人、留言），只能先寫入 project-hub/pending_github_actions.yaml（status: awaiting_approval）等待使用者明確核准！"
        )
        files_desc = "包含: CHARTER.md (計劃書), ARCHITECTURE.md (完整架構圖), objectives.yaml (專案目標), tasks.yaml (派發任務/截止日/品質驗收), team.yaml (人力與每週產能), github_snapshot.yaml (GitHub 唯讀同步快照), pending_github_actions.yaml (待核准 GitHub 操作佇列)。"
        gh = live_state.get("github") or {}
        snapshot = {
            "project_name": live_state.get("project_name"),
            "audit": live_state.get("audit"),
            "team": live_state.get("team"),
            "tasks": [
                {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "objective_id": t.get("objective_id"),
                    "assignee": t.get("assignee"),
                    "due_date": t.get("due_date"),
                    "estimated_hours": t.get("estimated_hours"),
                    "status": t.get("status"),
                    "qa_status": t.get("qa_status"),
                }
                for t in live_state.get("tasks", [])
            ],
            "github": {
                "repo": gh.get("repo", ""),
                "strict_no_push_until_approval": True,
                "synced_at": gh.get("synced_at", ""),
                "open_issues": [i for i in gh.get("issues", []) if i.get("state") == "open"][:15],
                "open_prs": [p for p in gh.get("pull_requests", []) if p.get("state") == "open"][:10],
                "milestones": gh.get("milestones", [])[:10],
                "pending_proposals": gh.get("pending_proposals", []),
            },
        }

    lines = [
        role_desc,
        f"工作區資料夾位於: {hub_dir.resolve()}",
        files_desc,
        "以下是最新的即時工作區快照（JSON）：",
        json.dumps(snapshot, ensure_ascii=False),
        "規則：",
        "1. 優先根據上方快照或直接讀寫工作區檔案回答與處理，非必要請勿執行需要額外權限的 shell 指令。",
        "2. 若使用者要求新增／調整項目、平衡負荷、紀錄實驗、更新倫理審查、規劃簡報或起草信件，請直接編輯工作區對應 YAML/MD 檔案，左側視覺化前端會即時更新。",
        "3. 請用繁體中文、條理清晰簡潔回覆，並列出已更新項目或具體建議。",
    ]
    if include_history and history:
        lines.append("\n近期對話摘要：")
        for msg in history[-6:]:
            role = "使用者" if msg.get("role") == "user" else f"助手({msg.get('agent', '')})"
            lines.append(f"- {role}: {str(msg.get('content', ''))[:400]}")
    lines.append(f"\n使用者指示：{user_message}")
    return "\n".join(lines)
