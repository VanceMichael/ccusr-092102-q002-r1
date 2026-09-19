"""HTTP 接口端到端测试（真实端口 + urllib，覆盖鉴权头与 403/404/409/422）。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.gold_attribution.http_app import build_server

EVENT = {
    "event_id": "fed-2026-09-16",
    "title": "9 月议息",
    "policy_body": "Federal Reserve",
    "scheduled_at": "2026-09-16T14:00:00-04:00",
    "freeze_at": "2026-09-16T13:30:00-04:00",
}


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "http.db")
        self.server = build_server(self.db_path, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, payload: dict | None = None,
                team: str = "macro", actor: str = "alice") -> tuple[int, dict]:
        data = None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if team is not None:
            headers["X-Team"] = team
        if actor is not None:
            headers["X-Actor"] = actor
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        req = urllib.request.Request(self.url("/health"))
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())["状态"], "服务已启动")

    def test_requires_identity_headers(self) -> None:
        code, body = self.request("POST", "/api/events", EVENT, team=None, actor=None)
        self.assertEqual(code, 422)
        self.assertEqual(body["错误"], "unprocessable")

    def test_full_scenario_over_http(self) -> None:
        code, ev = self.request("POST", "/api/events", EVENT)
        self.assertEqual(code, 200)

        code, exp = self.request("POST", f"/api/events/{EVENT['event_id']}/expectations", {
            "source": "FedWatch", "outcome": "hike25", "probability": 0.92,
            "collected_at": "2026-09-16T13:00:00-04:00", "source_version": "13:00"})
        self.assertEqual(code, 200)

        code, _ = self.request("POST", f"/api/events/{EVENT['event_id']}/freeze",
                               {"frozen_at": "2026-09-16T13:30:00-04:00"})
        self.assertEqual(code, 200)

        # 冻结后再登记预期 → 409
        code, body = self.request("POST", f"/api/events/{EVENT['event_id']}/expectations", {
            "source": "late", "outcome": "hold", "probability": 0.5,
            "collected_at": "2026-09-16T13:31:00-04:00"})
        self.assertEqual(code, 409)
        self.assertEqual(body["错误"], "conflict")

        code, dec = self.request("POST", f"/api/events/{EVENT['event_id']}/decisions", {
            "action": "hike", "basis_points": 25,
            "decided_at": "2026-09-16T14:00:00-04:00",
            "source": "FOMC", "source_version": "v1"})
        self.assertEqual(code, 200)

        code, obs = self.request("POST", f"/api/events/{EVENT['event_id']}/observations", {
            "series": "spot_gold", "label": "现货金低点", "value": 4281.5,
            "currency": "USD", "unit": "ounce", "market_tz": "America/New_York",
            "observed_at": "2026-09-16T14:05:00-04:00",
            "collected_at": "2026-09-16T14:06:00-04:00",
            "source": "LBMA", "source_version": "tick-1405"})
        self.assertEqual(code, 200)

        # 朴素时间戳 → 422
        code, body = self.request("POST", f"/api/events/{EVENT['event_id']}/observations", {
            "series": "spot_gold", "label": "x", "value": 1,
            "observed_at": "2026-09-16T14:05:00",
            "collected_at": "2026-09-16T14:06:00-04:00",
            "source": "s", "source_version": "1"})
        self.assertEqual(code, 422)

        analysis = {
            "title": "加息后的韧性",
            "body": "提前定价叠加资金流入，利率单因素解释不足。",
            "claims": [{
                "text": "25bp 兑现会前 92% 概率，冲击被提前消化。",
                "evidence": [
                    {"kind": "supporting", "target_type": "expectation",
                     "target_id": exp["id"]},
                    {"kind": "supporting", "target_type": "decision",
                     "target_id": dec["id"]},
                    {"kind": "contradicting", "target_type": "observation",
                     "target_id": obs["id"], "note": "短跌与修复并存"},
                ]}],
        }
        code, an = self.request("POST", f"/api/events/{EVENT['event_id']}/analyses", analysis)
        self.assertEqual(code, 200)
        self.assertEqual(an["status"], "draft")
        self.assertEqual(an["rev"], 1)

        # 他团队看不到草稿 → 404
        code, body = self.request("GET", f"/api/analyses/{an['id']}", team="rates", actor="bob")
        self.assertEqual(code, 404)

        # 乐观锁：错误 rev → 409 且回带当前内容
        code, body = self.request("PUT", f"/api/analyses/{an['id']}",
                                  {**analysis, "body": "过期版本的修改。", "expected_rev": 99})
        self.assertEqual(code, 409)
        self.assertEqual(body["细节"]["current_rev"], 1)
        self.assertIn("current_claims", body["细节"])

        # 正确 rev → 200
        code, updated = self.request("PUT", f"/api/analyses/{an['id']}",
                                     {**analysis, "body": "合并后的正文内容。", "expected_rev": 1})
        self.assertEqual(code, 200)
        self.assertEqual(updated["rev"], 2)

        # 无 expected_rev → 422
        code, _ = self.request("PUT", f"/api/analyses/{an['id']}", analysis)
        self.assertEqual(code, 422)

        # 交易指令护栏
        code, body = self.request("PUT", f"/api/analyses/{an['id']}",
                                  {**analysis, "body": "结论：立即买入黄金。",
                                   "expected_rev": 2})
        self.assertEqual(code, 422)
        self.assertEqual(body["错误"], "trading_instruction_blocked")

        # 发布 → 固化水位，他团队可见
        code, published = self.request("POST", f"/api/analyses/{an['id']}/publish")
        self.assertEqual(code, 200)
        self.assertIn("watermark", published)
        code, seen = self.request("GET", f"/api/analyses/{an['id']}",
                                  team="rates", actor="bob")
        self.assertEqual(code, 200)
        self.assertEqual(seen["status"], "published")

        # 已发布不可改写
        code, body = self.request("PUT", f"/api/analyses/{an['id']}",
                                  {**analysis, "expected_rev": 2})
        self.assertEqual(code, 409)

        # 修订 → 新草稿（seq=2），旧版本不变
        code, rev = self.request("POST", f"/api/analyses/{an['id']}/revise")
        self.assertEqual(code, 200)
        self.assertEqual(rev["seq"], 2)
        self.assertEqual(rev["revision_of_id"], an["id"])

        # 版本对比与审计
        code, diff = self.request("GET", f"/api/analyses/{an['id']}/diff/{rev['id']}")
        self.assertEqual(code, 200)
        code, audit = self.request("GET", f"/api/audit?event_id={EVENT['event_id']}")
        self.assertEqual(code, 200)
        actions = {row["action"] for row in audit["审计日志"]}
        self.assertIn("freeze_expectations", actions)
        self.assertIn("publish_analysis", actions)

    def test_unknown_route(self) -> None:
        code, body = self.request("GET", "/api/nope")
        self.assertEqual(code, 404)
        self.assertEqual(body["错误"], "not_found")


if __name__ == "__main__":
    unittest.main()
