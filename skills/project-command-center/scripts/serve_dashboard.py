#!/usr/bin/env python3
"""Interactive PM Assistant & Live Visual Workforce Dashboard powered by agy, claude, or codex."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
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


def detect_available_agents() -> dict[str, str | None]:
    """Detect installed local CLI binaries for agy, claude, and codex."""
    return {name: shutil.which(name) for name in ("agy", "claude", "codex")}


def _resolve_codex_model(preferred: str | None = None) -> str | None:
    if preferred:
        return preferred
    cache_path = Path.home() / ".codex" / "models_cache.json"
    if cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            for m in data.get("models", []):
                slug = m.get("slug") or m.get("id")
                if slug and slug not in {"codex-auto-review"}:
                    return str(slug)
        except Exception:
            return None
    return None


def build_agent_command(
    agent: str,
    prompt: str,
    *,
    continue_session: bool = False,
    codex_model: str | None = None,
) -> list[str]:
    """Construct safe argv list (shell=False) for agy, claude, or codex."""
    if agent == "agy":
        cmd = ["agy", "-p", prompt, "--mode", "accept-edits", "--print-timeout", "180s"]
        if continue_session:
            cmd.append("--continue")
        return cmd
    if agent == "claude":
        cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
        if continue_session:
            cmd.append("--continue")
        return cmd
    if agent == "codex":
        cmd = ["codex", "exec", "--skip-git-repo-check", "--full-auto"]
        model = _resolve_codex_model(codex_model)
        if model:
            cmd.extend(["-m", model])
        cmd.append(prompt)
        return cmd
    raise ValueError(f"Unsupported agent engine: {agent}")


def build_pm_prompt(
    user_message: str,
    hub_dir: Path,
    *,
    history: list[dict[str, str]] | None = None,
    include_history: bool = False,
) -> str:
    """Wrap user's PM request with live project-hub audit state so agy/claude/codex answers & edits smoothly."""
    live_state = build_state_payload(hub_dir)
    compact_snapshot = {
        "project_name": live_state["project_name"],
        "audit": live_state["audit"],
        "team": live_state["team"],
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
            for t in live_state["tasks"]
        ],
    }
    lines = [
        "你是本專案的互動式「專案管理指揮中心助手」(請遵循 project-command-center skill)。",
        f"專案治理資料夾位於: {hub_dir.resolve()}",
        "包含: CHARTER.md (計劃書), ARCHITECTURE.md (完整架構圖), objectives.yaml (專案目標), tasks.yaml (派發任務/截止日/品質驗收), team.yaml (人力與每週產能)。",
        "以下是最新的專案即時稽核與人力負荷快照（JSON）：",
        json.dumps(compact_snapshot, ensure_ascii=False),
        "規則：",
        "1. 優先根據上方快照或直接讀寫 project-hub/ 檔案回答與處理，非必要請勿執行 shell 指令。",
        "2. 若使用者要求派發/調整任務、平衡人力、更新進度或補充驗收條件，請直接編輯 project-hub/ 下對應檔案，前端視覺化看板會自動即時刷新。",
        "3. 請用繁體中文、條理清晰簡潔回覆，並明確列出已修改的項目或具體建議。",
    ]
    if include_history and history:
        recent = history[-6:]
        lines.append("\n近期對話摘要：")
        for msg in recent:
            role = "使用者" if msg.get("role") == "user" else f"助手({msg.get('agent', '')})"
            lines.append(f"- {role}: {str(msg.get('content', ''))[:400]}")
    lines.append(f"\n使用者指示：{user_message}")
    return "\n".join(lines)


class AssistantSession:
    """Thread-safe interactive chat session backed by local agy / claude / codex CLI."""

    def __init__(
        self,
        hub_dir: Path,
        project_root: Path,
        *,
        default_agent: str = "agy",
        codex_model: str | None = None,
    ) -> None:
        self.hub_dir = hub_dir.resolve()
        self.project_root = project_root.resolve()
        self.available = detect_available_agents()
        if default_agent in self.available and self.available[default_agent]:
            self.active_agent = default_agent
        else:
            self.active_agent = next((k for k, v in self.available.items() if v), default_agent)
        self.codex_model = codex_model
        self.messages: list[dict[str, Any]] = []
        self.turn_count_by_agent: dict[str, int] = {"agy": 0, "claude": 0, "codex": 0}
        self.busy = False
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.messages.clear()
            self.turn_count_by_agent = {"agy": 0, "claude": 0, "codex": 0}

    def snapshot(self) -> dict[str, Any]:
        return {
            "active_agent": self.active_agent,
            "available_agents": {k: bool(v) for k, v in self.available.items()},
            "busy": self.busy,
            "messages": list(self.messages),
        }

    def send_message(self, user_text: str, *, agent: str | None = None) -> dict[str, Any]:
        chosen = agent or self.active_agent
        if chosen not in {"agy", "claude", "codex"}:
            return {"ok": False, "error": f"不支援的助手引擎: {chosen}"}
        if not self.available.get(chosen):
            return {"ok": False, "error": f"本機未偵測到 `{chosen}` CLI，請切換其他已安裝的引擎。"}

        with self._lock:
            if self.busy:
                return {"ok": False, "error": "助手正在處理上一筆請求，請稍候完成。"}
            self.busy = True
            self.active_agent = chosen
            now = datetime.now(timezone.utc).isoformat()
            self.messages.append({"role": "user", "content": user_text, "agent": chosen, "ts": now})
            has_prior = self.turn_count_by_agent.get(chosen, 0) > 0
            prompt = build_pm_prompt(
                user_text,
                self.hub_dir,
                history=self.messages[:-1],
                include_history=(chosen == "codex" or not has_prior),
            )
            cmd = build_agent_command(
                chosen,
                prompt,
                continue_session=(has_prior and chosen in {"agy", "claude"}),
                codex_model=self.codex_model,
            )

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=210,
                env=dict(os.environ),
            )
            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            reply = stdout if stdout else (stderr or f"({chosen} 已完成執行，無額外文字輸出)")
            if proc.returncode != 0 and stdout:
                reply = f"{stdout}\n\n[exit={proc.returncode}] {stderr}".strip()
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            reply = f"執行逾時（{chosen} 超過 210 秒未完成），請縮小任務範圍後重試。"
            ok = False
        except OSError as exc:
            reply = f"無法啟動 `{chosen}` CLI: {exc}"
            ok = False
        finally:
            with self._lock:
                self.busy = False

        reply_msg = {
            "role": "assistant",
            "content": reply,
            "agent": chosen,
            "ok": ok,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self.messages.append(reply_msg)
            if ok:
                self.turn_count_by_agent[chosen] = self.turn_count_by_agent.get(chosen, 0) + 1
        return {"ok": ok, "message": reply_msg, "state": build_state_payload(self.hub_dir, session=self)}


def init_sample_hub(hub_dir: Path) -> None:
    """Create a starter project-hub/ workspace if missing so the assistant can run immediately."""
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
                            "evidence": ["unittest 12/12 passed"],
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


def build_state_payload(hub_dir: Path, *, session: AssistantSession | None = None) -> dict[str, Any]:
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
        "assistant": session.snapshot() if session else {"active_agent": "agy", "available_agents": {}, "busy": False, "messages": []},
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


def create_server(
    hub_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    project_root: Path | None = None,
    default_agent: str = "agy",
    codex_model: str | None = None,
) -> ThreadingHTTPServer:
    html_bytes = DASHBOARD_HTML.read_bytes()
    root_dir = (project_root or hub_dir.resolve().parent).resolve()
    session = AssistantSession(
        hub_dir, root_dir, default_agent=default_agent, codex_model=codex_model
    )

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

        def _read_json(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(min(length, 131_072))
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                return body if isinstance(body, dict) else {}
            except json.JSONDecodeError:
                return None

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in {"/", "/index.html"}:
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", html_bytes)
                return
            if path == "/api/state":
                payload = json.dumps(
                    build_state_payload(hub_dir, session=session), ensure_ascii=False
                ).encode("utf-8")
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", payload)
                return
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            body = self._read_json()
            if body is None:
                self._send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"invalid json"}')
                return

            if path == "/api/tasks/update":
                ok = apply_task_patch(hub_dir, body)
                status = HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND
                self._send(status, "application/json", json.dumps({"ok": ok}).encode("utf-8"))
                return

            if path == "/api/chat":
                message = str(body.get("message") or "").strip()
                agent = body.get("agent")
                if not message:
                    self._send(HTTPStatus.BAD_REQUEST, "application/json", b'{"ok":false,"error":"empty message"}')
                    return
                res = session.send_message(message, agent=str(agent) if agent else None)
                self._send(
                    HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST,
                    "application/json; charset=utf-8",
                    json.dumps(res, ensure_ascii=False).encode("utf-8"),
                )
                return

            if path == "/api/chat/reset":
                session.reset()
                self._send(HTTPStatus.OK, "application/json", b'{"ok":true}')
                return

            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the interactive PM Assistant & live visual workforce dashboard."
    )
    parser.add_argument("--hub", default="./project-hub", help="Path to project-hub directory.")
    parser.add_argument("--project-root", default=".", help="Workspace root where agy/claude/codex runs.")
    parser.add_argument(
        "--agent",
        choices=["agy", "claude", "codex"],
        default="agy",
        help="Default interactive AI assistant engine (agy, claude, or codex).",
    )
    parser.add_argument("--codex-model", help="Optional model override for codex exec.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    parser.add_argument("--open", action="store_true", help="Open browser automatically on startup.")
    parser.add_argument("--init-sample", action="store_true", help="Initialize starter project-hub/ if missing.")
    args = parser.parse_args(argv)

    hub_dir = Path(args.hub)
    if args.init_sample:
        init_sample_hub(hub_dir)

    server = create_server(
        hub_dir,
        host=args.host,
        port=args.port,
        project_root=Path(args.project_root),
        default_agent=args.agent,
        codex_model=args.codex_model,
    )
    actual_port = server.server_address[1]
    url = f"http://{args.host}:{actual_port}"
    print(f"Interactive PM Assistant ({args.agent}) + Visual Board running at {url}")
    print(f"  Project root: {Path(args.project_root).resolve()}")
    print(f"  Project hub:  {hub_dir.resolve()}")
    print("Press Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPM Assistant stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
