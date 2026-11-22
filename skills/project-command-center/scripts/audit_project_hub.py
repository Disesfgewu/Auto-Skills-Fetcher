#!/usr/bin/env python3
"""Deterministic PM audit helper for project-hub/ (traceability, schedule, capacity, quality gate)."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required (pip install PyYAML).", file=sys.stderr)
    raise SystemExit(2)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _parse_date(val: Any) -> date | None:
    if not val:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    try:
        return date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def audit_project_hub(hub_dir: Path, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    issues_red: list[str] = []
    issues_amber: list[str] = []

    charter_ok = (hub_dir / "CHARTER.md").is_file()
    arch_ok = (hub_dir / "ARCHITECTURE.md").is_file()
    if not charter_ok:
        issues_amber.append("Missing CHARTER.md (專案計劃書).")
    if not arch_ok:
        issues_amber.append("Missing ARCHITECTURE.md (專案完整架構圖).")

    obj_data = _load_yaml(hub_dir / "objectives.yaml")
    task_data = _load_yaml(hub_dir / "tasks.yaml")
    team_data = _load_yaml(hub_dir / "team.yaml")

    objectives = {
        str(o["id"]): o for o in obj_data.get("objectives", []) if isinstance(o, dict) and o.get("id")
    }
    tasks = [t for t in task_data.get("tasks", []) if isinstance(t, dict) and t.get("id")]
    tasks_by_id = {str(t["id"]): t for t in tasks}
    team = {
        str(m["id"]): m for m in team_data.get("members", []) if isinstance(m, dict) and m.get("id")
    }

    # 1. Objective <-> Task traceability
    tasks_by_obj: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in tasks:
        tid = str(t["id"])
        oid = str(t.get("objective_id") or "")
        if not oid or oid not in objectives:
            issues_red.append(
                f"[DRIFT] Task {tid} ('{t.get('title', '')}') has invalid/missing objective_id '{oid}'."
            )
        else:
            tasks_by_obj[oid].append(t)

    objective_summary: list[dict[str, Any]] = []
    for oid, obj in objectives.items():
        linked = tasks_by_obj.get(oid, [])
        if not linked:
            issues_amber.append(f"[UNCOVERED] Objective {oid} ('{obj.get('title', '')}') has 0 assigned tasks.")
        done_n = sum(1 for t in linked if t.get("status") == "done" and t.get("qa_status") == "passed")
        pct = round((done_n / len(linked)) * 100.0, 1) if linked else 0.0
        objective_summary.append(
            {
                "id": oid,
                "title": obj.get("title", ""),
                "target_date": str(obj.get("target_date", "")),
                "total_tasks": len(linked),
                "verified_done_tasks": done_n,
                "completion_pct": pct,
            }
        )

    # 2. Schedule, dependencies, and quality gate checks
    load_by_member: dict[str, float] = defaultdict(float)
    for t in tasks:
        tid = str(t["id"])
        status = str(t.get("status", "todo"))
        qa_status = str(t.get("qa_status", "pending"))
        due = _parse_date(t.get("due_date"))
        oid = str(t.get("objective_id") or "")
        obj_due = _parse_date(objectives[oid].get("target_date")) if oid in objectives else None

        # Schedule checks
        if due and status != "done":
            days_left = (due - today).days
            if days_left < 0:
                issues_red.append(
                    f"[OVERDUE] Task {tid} ('{t.get('title', '')}') is {-days_left}d overdue (due {due}, status={status})."
                )
            elif days_left <= 3 and status == "todo":
                issues_amber.append(
                    f"[AT-RISK] Task {tid} due in {days_left}d ({due}) is still 'todo'."
                )
        if due and obj_due and due > obj_due:
            issues_red.append(
                f"[TIMELINE-MISMATCH] Task {tid} due {due} exceeds parent Objective {oid} target_date {obj_due}."
            )

        # Dependency checks
        for dep in t.get("depends_on") or []:
            dep_id = str(dep)
            if dep_id not in tasks_by_id:
                issues_red.append(f"[BROKEN-DEP] Task {tid} depends on unknown task '{dep_id}'.")
                continue
            dep_due = _parse_date(tasks_by_id[dep_id].get("due_date"))
            if due and dep_due and dep_due > due:
                issues_amber.append(
                    f"[DEP-ORDER] Task {tid} (due {due}) is scheduled before dependency {dep_id} (due {dep_due})."
                )

        # Workforce load
        assignee = str(t.get("assignee") or "")
        hours = float(t.get("estimated_hours") or 0.0)
        if status != "done":
            if not assignee:
                issues_amber.append(f"[UNASSIGNED] Active task {tid} has no assignee.")
            else:
                load_by_member[assignee] += hours
                if team and assignee not in team:
                    issues_amber.append(f"[UNKNOWN-OWNER] Task {tid} assigned to '{assignee}' not listed in team.yaml.")

        # Quality gate (Definition of Done)
        criteria = t.get("acceptance_criteria") or []
        failed_ac = [c.get("criterion", "?") for c in criteria if isinstance(c, dict) and not c.get("passed")]
        evidence = t.get("evidence") or []
        if status == "done":
            if qa_status != "passed":
                issues_red.append(
                    f"[QA-GATE] Task {tid} marked 'done' but qa_status='{qa_status}' (must be 'passed')."
                )
            if failed_ac:
                issues_red.append(
                    f"[QA-GATE] Task {tid} marked 'done' with {len(failed_ac)} unmet acceptance criteria: {failed_ac[:2]}."
                )
            if not evidence:
                issues_amber.append(
                    f"[QA-EVIDENCE] Task {tid} marked 'done' with no verification evidence recorded."
                )

    # 3. Cycle detection on task dependencies
    visiting: set[str] = set()
    visited: set[str] = set()

    def _dfs(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dep in tasks_by_id.get(node, {}).get("depends_on") or []:
            if str(dep) in tasks_by_id and _dfs(str(dep)):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for tid in tasks_by_id:
        if _dfs(tid):
            issues_red.append(f"[CYCLE] Circular task dependency detected involving '{tid}'.")
            break

    # 4. Team capacity check
    workload_summary: list[dict[str, Any]] = []
    for mid, member in team.items():
        cap = float(member.get("weekly_capacity_hours") or 40.0)
        assigned = round(load_by_member.get(mid, 0.0), 1)
        util = round((assigned / cap) * 100.0, 1) if cap > 0 else 0.0
        if util > 100.0:
            issues_amber.append(
                f"[OVERLOAD] Member '{mid}' assigned {assigned}h open work vs {cap}h weekly capacity ({util}%)."
            )
        workload_summary.append(
            {"member": mid, "role": member.get("role", ""), "open_hours": assigned, "weekly_capacity": cap, "utilization_pct": util}
        )

    rag = "RED" if issues_red else ("AMBER" if issues_amber else "GREEN")
    return {
        "as_of": today.isoformat(),
        "rag_status": rag,
        "docs": {"charter_present": charter_ok, "architecture_present": arch_ok},
        "objectives": objective_summary,
        "workload": workload_summary,
        "red_issues": issues_red,
        "amber_issues": issues_amber,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit project-hub/ alignment, schedule, workload, and QA gates.")
    parser.add_argument("--hub", default="./project-hub", help="Path to project-hub directory.")
    parser.add_argument("--json", action="store_true", help="Output JSON.")
    args = parser.parse_args()

    result = audit_project_hub(Path(args.hub))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Project Hub Audit ({result['as_of']}) — Overall RAG: {result['rag_status']}")
        print(
            f"  Docs: CHARTER.md={'OK' if result['docs']['charter_present'] else 'MISSING'}, "
            f"ARCHITECTURE.md={'OK' if result['docs']['architecture_present'] else 'MISSING'}"
        )
        print("  Objectives:")
        for o in result["objectives"]:
            print(
                f"    - {o['id']} ({o['title']}): {o['verified_done_tasks']}/{o['total_tasks']} verified "
                f"({o['completion_pct']}%, target={o['target_date']})"
            )
        if result["workload"]:
            print("  Team Workload:")
            for w in result["workload"]:
                print(
                    f"    - {w['member']} ({w['role']}): {w['open_hours']}h / {w['weekly_capacity']}h "
                    f"({w['utilization_pct']}%)"
                )
        if result["red_issues"]:
            print("  RED Issues (Immediate Action Required):")
            for r in result["red_issues"]:
                print(f"    ! {r}")
        if result["amber_issues"]:
            print("  AMBER Warnings:")
            for a in result["amber_issues"]:
                print(f"    * {a}")
    return 1 if result["rag_status"] == "RED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
