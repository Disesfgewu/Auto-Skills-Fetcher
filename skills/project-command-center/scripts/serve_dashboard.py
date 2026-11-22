#!/usr/bin/env python3
"""Live local visual frontend server for project-command-center (zero npm/external deps)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required (pip install PyYAML).", file=sys.stderr)
    raise SystemExit(2)

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DASHBOARD_HTML = SKILL_ROOT / "assets" / "dashboard.html"


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_project_hub", SCRIPT_DIR / "audit_project_hub.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def init_sample_hub(hub_dir: Path) -> None:
    """Create a starter project-hub/ workspace if missing so the dashboard can run immediately."""
    hub_dir.mkdir(parents=True, exist_ok=True)
    if not (hub_dir / "CHARTER.md").exists():
        (hub_dir / "CHARTER.md").write_text(
            "# 專案計劃書 (Project Charter)\n\n"
            "## 專案目標\n"
            "- 建立可追蹤架構、任務時程、人力負荷與產出品質的專案管理工作流。\n\n"
            "## 範疇 (In-Scope)\n"
            "- 核心後端 API 與資料庫\n"
            "- 前端儀表板與品質驗收測試\n",
            encoding="utf-8",
        )
    if not (hub_dir / "ARCHITECTURE.md").exists():
        (hub_dir / "ARCHITECTURE.md").write_text(
            "# 專案完整架構圖 (Architecture)\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            "  UI[\"PM Dashboard UI (OBJ-02)\"] --> API[\"Core API Gateway (OBJ-01)\"]\n"
            "  API --> DB[\"Registry Database (OBJ-01)\"]\n"
            "  API --> QA[\"Automated QA Gate (OBJ-02)\"]\n"
            "```\n",
            encoding="utf-8",
        )
    if not (hub_dir / "team.yaml").exists():
        (hub_dir / "team.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "members": [
                        {"id": "alice", "name": "Alice (Tech Lead)", "role": "Architecture & Backend", "skills": ["backend", "architecture"], "weekly_capacity_hours": 32},
                        {"id": "bob", "name": "Bob (Engineer)", "role": "API & Testing", "skills": ["python", "qa"], "weekly_capacity_hours": 35},
                        {"id": "carol", "name": "Carol (Frontend)", "role": "UI & Visualization", "skills": ["frontend", "ux"], "weekly_capacity_hours": 30},
                    ],
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    if not (hub_dir / "objectives.yaml").exists():
        (hub_dir / "objectives.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "project_name": "Project Command Center Demo",
                    "objectives": [
                        {
                            "id": "OBJ-01",
                            "title": "完成核心後端架構與資料庫 API",
                            "target_date": "2026-12-10",
                            "owner": "alice",
                            "architecture_modules": ["Core API Gateway", "Registry Database"],
                            "success_criteria": ["所有 API 整合測試通過"],
                        },
                        {
                            "id": "OBJ-02",
                            "title": "完成視覺化儀表板與品質硬閘門",
                            "target_date": "2026-12-18",
                            "owner": "carol",
                            "architecture_modules": ["PM Dashboard UI", "Automated QA Gate"],
                            "success_criteria": ["人力負荷與任務對齊視覺化上線"],
                        },
                    ],
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    if not (hub_dir / "tasks.yaml").exists():
        (hub_dir / "tasks.yaml").write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "tasks": [
                        {
                            "id": "T-001",
                            "title": "建立資料庫 Schema 與索引",
                            "objective_id": "OBJ-01",
                            "architecture_module": "Registry Database",
                            "assignee": "alice",
                            "due_date": "2026-11-30",
                            "estimated_hours": 12,
                            "depends_on": [],
                            "status": "done",
                            "qa_status": "passed",
                            "evidence": ["unittest 11/11 passed"],
                            "acceptance_criteria": [{"criterion": "Schema 完整支援查詢索引", "passed": True}],
                        },
                        {
                            "id": "T-002",
                            "title": "實作核心 REST API 端點",
                            "objective_id": "OBJ-01",
                            "architecture_module": "Core API Gateway",
                            "assignee": "bob",
                            "due_date": "2026-12-06",
                            "estimated_hours": 18,
                            "depends_on": ["T-001"],
                            "status": "in_progress",
                            "qa_status": "pending",
                            "evidence": [],
                            "acceptance_criteria": [{"criterion": "端點規格與架構圖一致", "passed": False}],
                        },
                        {
                            "id": "T-003",
                            "title": "建立人力分配與甘特圖視覺化頁面",
                            "objective_id": "OBJ-02",
                            "architecture_module": "PM Dashboard UI",
                            "assignee": "carol",
                            "due_date": "2026-12-12",
                            "estimated_hours": 20,
                            "depends_on": ["T-002"],
                            "status": "in_progress",
                            "qa_status": "pending",
                            "evidence": [],
                            "acceptance_criteria": [{"criterion": "可即時顯示超載警示與改派負責人", "passed": False}],
                        },
                    ],
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )


def build_state_payload(hub_dir: Path) -> dict[str, Any]:
    audit_mod = _load_audit_module()
    audit = audit_mod.audit_project_hub(hub_dir)

    def _read_yaml(name: str) -> dict[str, Any]:
        p = hub_dir / name
        if not p.is_file():
            return {}
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    obj_data = _read_yaml("objectives.yaml")
    task_data = _read_yaml("tasks.yaml")
    team_data = _read_yaml("team.yaml")

    charter_md = (hub_dir / "CHARTER.md").read_text(encoding="utf-8") if (hub_dir / "CHARTER.md").is_file() else ""
    arch_md = (hub_dir / "ARCHITECTURE.md").read_text(encoding="utf-8") if (hub_dir / "ARCHITECTURE.md").is_file() else ""

    return {
        "hub_path": str(hub_dir.resolve()),
        "project_name": obj_data.get("project_name") or hub_dir.resolve().parent.name,
        "objectives": obj_data.get("objectives", []),
        "tasks": task_data.get("tasks", []),
        "team": team_data.get("members", []),
        "charter_md": charter_md,
        "architecture_md": arch_md,
        "audit": audit,
    }


def apply_task_patch(hub_dir: Path, payload: dict[str, Any]) -> bool:
    task_id = str(payload.get("task_id") or "")
    tasks_path = hub_dir / "tasks.yaml"
    if not task_id or not tasks_path.is_file():
        return False
    data = yaml.safe_load(tasks_path.read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks", [])
    updated = False
    allowed_fields = {"assignee", "status", "due_date", "qa_status", "estimated_hours"}
    for t in tasks:
        if isinstance(t, dict) and str(t.get("id")) == task_id:
            for k, v in payload.items():
                if k in allowed_fields:
                    t[k] = v
            updated = True
            break
    if updated:
        tasks_path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120),
            encoding="utf-8",
        )
    return updated


def create_server(hub_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    html_bytes = DASHBOARD_HTML.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in {"/", "/index.html"}:
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", html_bytes)
                return
            if path == "/api/state":
                payload = json.dumps(build_state_payload(hub_dir), ensure_ascii=False).encode("utf-8")
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", payload)
                return
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path != "/api/tasks/update":
                self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found")
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(min(length, 65_536))
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"invalid json"}')
                return
            ok = apply_task_patch(hub_dir, body if isinstance(body, dict) else {})
            status = HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND
            self._send(status, "application/json", json.dumps({"ok": ok}).encode("utf-8"))

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the live visual PM & workforce dashboard.")
    parser.add_argument("--hub", default="./project-hub", help="Path to project-hub directory.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    parser.add_argument("--open", action="store_true", help="Open dashboard URL in browser on startup.")
    parser.add_argument("--init-sample", action="store_true", help="Initialize starter project-hub/ if missing.")
    args = parser.parse_args(argv)

    hub_dir = Path(args.hub)
    if args.init_sample:
        init_sample_hub(hub_dir)

    server = create_server(hub_dir, host=args.host, port=args.port)
    actual_port = server.server_address[1]
    url = f"http://{args.host}:{actual_port}"
    print(f"PM Command Center Dashboard running at {url} (hub: {hub_dir.resolve()})")
    print("Press Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
