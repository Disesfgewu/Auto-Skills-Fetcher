---
name: project-command-center
description: >-
  Unified Project Management (PM) Command Center & Governance Assistant. Use when tracking a project's
  full architecture diagram and proposal/charter, verifying whether dispatched tasks and deadlines stay on
  schedule, mapping one or multiple tasks to specific project objectives, balancing team workforce/RACI
  capacity, auditing deliverable output quality (QA / acceptance criteria / Definition of Done), or
  checking whether the project is moving in the right direction and on time.
---

# Project Command Center (專案管理指揮中心與治理助手)

## When to Use
Use this skill whenever the user asks to:
- 追蹤與核對專案**完整架構圖**（Architecture Diagram）與**計劃書／規格書**（Proposal / Charter / PRD），確保實作與任務沒有偏離方向。
- 建立或檢查**目標 ↔ 任務群組**的對應關係（一個或多個任務共同完成特定里程碑或目的）。
- 盤點目前**派發的任務、截止時間點、前後置依賴**，評估能否如期完成。
- 安排與檢核**人力配置與工作負荷**（誰負責什麼、RACI、有沒有人超載或成為單點瓶頸）。
- 進行**產出品質檢測**（Acceptance Criteria、測試證據、Definition of Done 硬閘門）。
- 產出專案健康度儀表板（紅黃綠燈 RAG Status）並提供具體矯正處置方案。

## Single Source of Truth Workspace (`project-hub/`)

Maintain these canonical files inside `<project_root>/project-hub/` (see [project-hub-schema.yaml](./references/project-hub-schema.yaml) for full templates):

1. `CHARTER.md` — 專案計劃書（背景、核心目標、範疇 In-Scope / Out-of-Scope、成功指標、總時程里程碑）。
2. `ARCHITECTURE.md` — 專案完整架構圖（以 Mermaid `flowchart` / `sequenceDiagram` 標示所有子系統、資料流、介面契約，並標註每個模組對應的 `objective_id`）。
3. `objectives.yaml` — 專案目的與里程碑清單（每個目的可由 1 至多個任務達成，附帶 `target_date` 與 `architecture_modules`）。
4. `tasks.yaml` — 已派發任務追蹤表（每筆任務必須綁定 `objective_id`、`assignee`、`due_date`、`estimated_hours`、`depends_on`、`status`、`acceptance_criteria`、`qa_status`、`evidence`）。
5. `team.yaml` — 人力安排與產能表（成員角色、專長、`weekly_capacity_hours`、RACI 權責）。

## Workflow

### Step 1: Initialize or Load `project-hub/`
- Check whether `project-hub/` exists in the workspace.
- If missing, initialize `CHARTER.md`, `ARCHITECTURE.md`, `objectives.yaml`, `tasks.yaml`, and `team.yaml` from existing project docs/codebase and the schema in [project-hub-schema.yaml](./references/project-hub-schema.yaml).
- Never fabricate completed tasks, test results, or architecture modules that do not exist in the codebase or user-provided plans.

### Step 2: Run Deterministic PM & Quality Audit Script
Run the bundled audit helper to compute exact traceability, schedule, workload, and quality gate findings:

```bash
python3 ./scripts/audit_project_hub.py --hub ./project-hub
```

Verify that the script exits cleanly and inspect every reported finding across all 5 control planes:
1. **Direction & Architecture Alignment (方向與架構對齊)**：
   - Every active task MUST map to a valid `objective_id` in `objectives.yaml` (flag **orphan tasks** drifting outside the project charter).
   - Every objective MUST reference affected modules in `ARCHITECTURE.md` and have at least one task assigned (flag **uncovered objectives**).
   - Compare implemented modules/files against `ARCHITECTURE.md` and flag architecture drift.
2. **Task & Timeline Feasibility (任務與時間點檢核)**：
   - Check overdue tasks (`due_date < today` and not `done`), tasks due within 7 days still `todo`, and tasks whose `due_date` is **after** their parent objective's `target_date`.
   - Validate `depends_on` graph for missing IDs, circular dependencies, or downstream tasks scheduled before upstream dependencies finish.
3. **Objective Delivery Roll-up (多任務達成單一目的追蹤)**：
   - Group all tasks by `objective_id`, compute weighted completion percentage, list blocking tasks, and explicitly state whether the grouped tasks are sufficient to satisfy the objective's `success_criteria`.
4. **Workforce & Capacity Balance (人力安排與負荷檢核)**：
   - Sum open `estimated_hours` per `assignee` for active/upcoming windows against `weekly_capacity_hours` in `team.yaml`.
   - Flag overloaded members (`utilization > 100%`), unassigned critical-path tasks, and single-point-of-failure owners.
5. **Deliverable Quality Gate (產出品質硬閘門 / DoD)**：
   - A task **MUST NOT** be marked `done` unless:
     - Every item in `acceptance_criteria` has `passed: true`, AND
     - `qa_status` is `passed`, AND
     - Concrete `evidence` (PR link, test log, commit SHA, screenshot, or review note) is recorded.
   - If a task claims `status: done` while failing any quality criterion, downgrade it to `needs_rework` and list exact remediation steps.

### Step 3: Present Executive PM Health Report & Actionable Steering
Always produce a structured report containing:
- **Overall Project RAG Status**: `GREEN` (on track) / `AMBER` (at-risk, needs adjustment) / `RED` (off-track / blocked / quality failure).
- **Architecture & Charter Drift Check**: Exact gaps between `CHARTER.md`, `ARCHITECTURE.md`, and current tasks/code.
- **Objective Progress Matrix**: Each `objective_id` → linked tasks → progress % → target date → on-track verdict.
- **Schedule & Critical-Path Risks**: Overdue or dependency-blocked tasks with concrete recovery options (re-scope, parallelize, shift owner).
- **Team Workload & Rebalancing Plan**: Per-member load vs capacity with specific task reassignment recommendations.
- **Quality Gate Audit**: Passed vs failed deliverables and required QA fixes before sign-off.

## Guardrails
- **Never mark a task complete without quality evidence**: Always verify tests, build output, or review artifacts before changing `qa_status` to `passed`.
- **Never silently change project scope or deadlines**: Propose explicit trade-off options (Scope vs Schedule vs Workforce) and ask the PM/user to confirm before altering `CHARTER.md` or milestone dates.
- **Keep `ARCHITECTURE.md` synchronized**: Whenever a major component or interface changes, update the Mermaid diagram in `ARCHITECTURE.md` in the same review cycle.
