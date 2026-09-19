"""归因账本的 HTTP 接口（仅用标准库 http.server）。

约定：

* 所有 ``/api/*`` 请求需携带请求头 ``X-Actor``（操作人）与 ``X-Team``（所属团队）；
  团队身份用于草稿可见性与并发归属判定。
* 请求/响应均为 ``application/json; charset=utf-8``。
* 领域错误按其 ``status`` 映射（403/404/409/422）；409 的响应体携带当前版本，
  调用方必须人工合并后以最新 ``rev`` 重提，系统不做静默合并。

线程模型：``ThreadingHTTPServer`` 下每个线程持有独立的 SQLite 连接，
因此数据库路径必须是文件（``:memory:`` 仅用于单线程脚本）。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import LedgerError
from .ledger import Ledger
from .storage import Storage


class App:
    """按线程提供独立的 Storage/Ledger。"""

    def __init__(self, db_path: str) -> None:
        if db_path == ":memory:":
            raise ValueError("HTTP 服务多线程运行，db_path 不能为 :memory:，请指定文件路径")
        self.db_path = db_path
        bootstrap = Storage(db_path)  # 启动时一次性完成建表
        bootstrap.close()


Route = tuple[str, re.Pattern[str], Callable[..., Any]]


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    routes: list[Route] = [
        ("POST", re.compile(r"^/api/events$"),
         lambda h, p, b, a, t: h.ledger.create_event(b, actor=a, team=t)),
        ("GET", re.compile(r"^/api/events/(?P<event_id>[^/]+)$"),
         lambda h, p, b, a, t: h.ledger.get_event(p["event_id"])),
        ("POST", re.compile(r"^/api/events/(?P<event_id>[^/]+)/expectations$"),
         lambda h, p, b, a, t: h.ledger.add_expectation(p["event_id"], b, actor=a, team=t)),
        ("GET", re.compile(r"^/api/events/(?P<event_id>[^/]+)/expectations$"),
         lambda h, p, b, a, t: h.ledger.list_expectations(p["event_id"])),
        ("POST", re.compile(r"^/api/events/(?P<event_id>[^/]+)/freeze$"),
         lambda h, p, b, a, t: h.ledger.freeze_expectations(p["event_id"], b, actor=a, team=t)),
        ("POST", re.compile(r"^/api/events/(?P<event_id>[^/]+)/decisions$"),
         lambda h, p, b, a, t: h.ledger.add_decision(p["event_id"], b, actor=a, team=t)),
        ("GET", re.compile(r"^/api/events/(?P<event_id>[^/]+)/decisions$"),
         lambda h, p, b, a, t: h.ledger.list_decisions(p["event_id"])),
        ("POST", re.compile(r"^/api/events/(?P<event_id>[^/]+)/observations$"),
         lambda h, p, b, a, t: h.ledger.add_observation(p["event_id"], b, actor=a, team=t)),
        ("GET", re.compile(r"^/api/events/(?P<event_id>[^/]+)/observations$"),
         lambda h, p, b, a, t: h.ledger.list_observations(
             p["event_id"], series=h.query.get("series", [None])[0])),
        ("GET", re.compile(r"^/api/events/(?P<event_id>[^/]+)/watermark$"),
         lambda h, p, b, a, t: h.ledger.current_watermark(p["event_id"])),
        ("POST", re.compile(r"^/api/events/(?P<event_id>[^/]+)/analyses$"),
         lambda h, p, b, a, t: h.ledger.create_analysis(p["event_id"], b, actor=a, team=t)),
        ("GET", re.compile(r"^/api/events/(?P<event_id>[^/]+)/analyses$"),
         lambda h, p, b, a, t: h.ledger.list_analyses(p["event_id"], team=t)),
        ("GET", re.compile(r"^/api/analyses/(?P<analysis_id>\d+)$"),
         lambda h, p, b, a, t: h.ledger.get_analysis(int(p["analysis_id"]), actor=a, team=t)),
        ("PUT", re.compile(r"^/api/analyses/(?P<analysis_id>\d+)$"),
         lambda h, p, b, a, t: h.ledger.update_draft(int(p["analysis_id"]), b, actor=a, team=t)),
        ("POST", re.compile(r"^/api/analyses/(?P<analysis_id>\d+)/publish$"),
         lambda h, p, b, a, t: h.ledger.publish_analysis(int(p["analysis_id"]), actor=a, team=t)),
        ("POST", re.compile(r"^/api/analyses/(?P<analysis_id>\d+)/revise$"),
         lambda h, p, b, a, t: h.ledger.revise_analysis(int(p["analysis_id"]), actor=a, team=t)),
        ("GET", re.compile(r"^/api/analyses/(?P<from_id>\d+)/diff/(?P<to_id>\d+)$"),
         lambda h, p, b, a, t: h.ledger.diff_analyses(
             int(p["from_id"]), int(p["to_id"]), team=t)),
        ("GET", re.compile(r"^/api/audit$"),
         lambda h, p, b, a, t: h.list_audit(h.query)),
    ]

    class Handler(BaseHTTPRequestHandler):
        _ledger: Ledger | None = None

        @property
        def ledger(self) -> Ledger:
            # 每个 HTTP 请求使用独立连接，_dispatch 结束时关闭
            if self._ledger is None:
                self._ledger = Ledger(Storage(self.server.app.db_path, initialize=False))  # type: ignore[attr-defined]
            return self._ledger

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            self.query = parse_qs(parsed.query)
            self._ledger = None

            if path == "/health":
                if method != "GET":
                    self.send_error(405)
                else:
                    self._send(200, {"状态": "服务已启动"})
                return

            if not path.startswith("/api/"):
                self._send(404, {"错误": "not_found", "说明": f"路径不存在：{path}"})
                return

            actor = self.headers.get("X-Actor", "").strip()
            team = self.headers.get("X-Team", "").strip()
            if not actor or not team:
                self._send(422, {"错误": "unprocessable",
                                 "说明": "所有 /api 请求必须携带 X-Actor 与 X-Team 请求头"})
                return

            try:
                self._route(method, path, actor, team)
            finally:
                if self._ledger is not None:
                    self._ledger.db.close()

        def _route(self, method: str, path: str, actor: str, team: str) -> None:
            for route_method, pattern, handler in routes:
                match = pattern.match(path)
                if match and method == route_method:
                    break
            else:
                # 路径存在于其他方法下 → 405；否则 404
                if any(pattern.match(path) for _, pattern, _ in routes):
                    self._send(405, {"错误": "method_not_allowed",
                                     "说明": f"该路径不支持 {method}"})
                else:
                    self._send(404, {"错误": "not_found", "说明": f"路径不存在：{path}"})
                return

            body: Any = {}
            if method in ("POST", "PUT"):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        self._send(422, {"错误": "unprocessable",
                                         "说明": f"请求体不是合法 JSON：{exc}"})
                        return
                if not isinstance(body, dict):
                    self._send(422, {"错误": "unprocessable",
                                     "说明": "请求体必须是 JSON 对象"})
                    return
            try:
                result = handler(self, match.groupdict(), body, actor, team)
            except LedgerError as exc:
                self._send(exc.status, exc.to_dict())
                return
            self._send(200, result)

        def list_audit(self, query: dict[str, str]) -> dict[str, Any]:
            """审计日志只读浏览，便于复盘；过滤参数 entity / event_id。"""

            sql = "SELECT * FROM audit_log"
            where: list[str] = []
            params: list[str] = []
            if "entity" in query:
                where.append("entity = ?")
                params.append(query["entity"][0])
            if "event_id" in query:
                where.append("json_extract(detail, '$.event_id') = ?")
                params.append(query["event_id"][0])
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT 200"
            storage = self.ledger.db
            rows = [dict(r) for r in storage.rows(sql, params)]
            for row in rows:
                try:
                    row["detail"] = json.loads(row["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return {"审计日志": rows}

        def log_message(self, fmt: str, *args: Any) -> None:
            return  # 测试与运行时保持安静

    Handler.server_version = "GoldAttribution/0.1"
    return Handler


def build_server(db_path: str, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    app = App(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.app = app  # type: ignore[attr-defined]
    return server
