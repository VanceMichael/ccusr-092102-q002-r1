"""结论页的数据水位还原与版本差异：新资料改变了哪段判断。"""

import unittest
from datetime import datetime, timezone

from src.gold_attribution.models import EvidenceKind
from src.gold_attribution.service import Actor

from tests._scenario import EVENT_ID, build_decided_event, claim, make_service


class WatermarkDiffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.alice = Actor("macro", "alice")
        self.ctx = build_decided_event(self.svc, self.alice)
        self.svc.create_analysis_series("conclusion", EVENT_ID, "macro", "归因结论", self.alice)
        self.svc.save_draft(
            "conclusion", "归因结论 v1", "利率主导，资金流待观察",
            [claim("c-rate", "定价充分 + 实际利率回落主导短跌后反弹",
                   support=(self.ctx.decision, self.ctx.real_rate)),
             claim("c-flow-pending", "ETF 与央行购金可能托底，证据仍在累积",
                   uncertain=(self.ctx.etf, self.ctx.cb))],
            self.alice, base_revision=0,
        )
        self.svc.publish("conclusion", self.alice)
        self.wm_v1 = self.svc.get_revision("conclusion", 1, self.alice)["data_watermark_at"]

    def test_conclusion_page_restores_water_at_publication(self) -> None:
        visible_before = {
            (e["kind"], e["source"]) for e in self.wm_v1_visible()
        }
        # 新资料到达：次日 ETF 数据 + 央行新披露
        self.clock.tick(86400)
        late_etf = self.svc.attach_evidence(
            EVENT_ID, EvidenceKind.ETF_HOLDING, "GLD-holdings", "2026-09-17",
            datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc),
            {"tonnes": 955.6, "daily_change_tonnes": 10.3},
            "UTC", self.alice,
        )
        self.clock.tick(60)

        page = self.svc.get_revision("conclusion", 1, self.alice)
        wm = page["evidence_watermark"]
        # 旧版水位：late_etf 不在当时可见集合里
        visible_ids = {e["evidence_id"] for e in wm["visible_evidence"]}
        self.assertNotIn(late_etf, visible_ids)
        # 但页面显式列出“水位之后才到达”的材料
        later = {e["evidence_id"] for e in wm["later_evidence"]}
        self.assertIn(late_etf, later)
        self.assertEqual(wm["as_of_utc"], self.wm_v1)
        self.assertEqual(len(visible_before), len(wm["visible_evidence"]))

    def wm_v1_visible(self):
        return self.svc.get_revision("conclusion", 1, self.alice)[
            "evidence_watermark"]["visible_evidence"]

    def test_diff_shows_which_claim_new_material_changed(self) -> None:
        self.clock.tick(86400)
        new_etf = self.svc.attach_evidence(
            EVENT_ID, EvidenceKind.ETF_HOLDING, "GLD-holdings", "2026-09-17",
            datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc),
            {"tonnes": 955.6, "daily_change_tonnes": 10.3},
            "UTC", self.alice,
        )
        new_cb = self.svc.attach_evidence(
            EVENT_ID, EvidenceKind.CENTRAL_BANK_BUY, "官方部门月报", "2026-09-prelim",
            datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc),
            {"tonnes": 37.0, "note": "9月上半月初步统计"},
            "UTC", self.alice, stream_key="sep-prelim",
        )
        self.clock.tick(60)
        # 新分析（不覆盖 v1）：把资金流从“不确定”改为“支持”，且引用新资料
        self.svc.save_draft(
            "conclusion", "归因结论 v2", "资金流证据增强，判断修正为多因素共振",
            [claim("c-rate", "定价充分 + 实际利率回落主导短跌后反弹",
                   support=(self.ctx.decision, self.ctx.real_rate)),
             claim("c-flow-pending", "ETF 与官方购金已构成可验证的结构性支撑",
                   support=(self.ctx.etf, new_etf, self.ctx.cb, new_cb))],
            self.alice, base_revision=1,
        )
        self.svc.publish("conclusion", self.alice)

        diff = self.svc.diff_revisions("conclusion", 1, 2, self.alice)
        self.assertTrue(diff["summary_changed"])
        changed_ids = {c["local_id"]: c for c in diff["claims_changed"]}
        self.assertIn("c-flow-pending", changed_ids)
        # 立场从 uncertain 调整为 supporting
        sc = changed_ids["c-flow-pending"]["stance_changes"]
        self.assertTrue(sc["supporting"]["linked"])
        self.assertTrue(sc["uncertainty"]["unlinked"])
        # 水位窗口内的新资料
        new_ids = {e["evidence_id"] for e in diff["new_evidence_in_window"]}
        self.assertEqual(new_ids, {new_etf, new_cb})
        # 新资料改变了哪段判断：被新版主张实际引用才标记 changed_claims
        for e in diff["new_evidence_in_window"]:
            self.assertEqual(e["changed_claims"], ["c-flow-pending"])
        self.assertEqual(diff["new_evidence_not_used"], [])
        # 未被改动的主张保持不变
        self.assertIn("c-rate", diff["claims_unchanged"])

    def test_pinned_evidence_version_flagged_when_corrected_later(self) -> None:
        # v1 引用的 ETF 版本在发布后被来源修订
        self.clock.tick(86400)
        revised_etf = self.svc.attach_evidence(
            EVENT_ID, EvidenceKind.ETF_HOLDING, "GLD-holdings", "2026-09-16-REV1",
            datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc),
            {"tonnes": 944.1, "daily_change_tonnes": 5.6,
             "note": "托管行事后调整"},
            "UTC", self.alice,
            revision_of_version="2026-09-16",
            revision_note="托管行对账后下修 1.2 吨",
        )
        page = self.svc.get_revision("conclusion", 1, self.alice)
        pin = None
        for cl in page["claims"]:
            for items in cl["stances"].values():
                for item in items:
                    if item["evidence"].id == self.ctx.etf:
                        pin = item
        self.assertIsNotNone(pin)
        self.assertTrue(pin["pinned_version_superseded"])
        self.assertTrue(pin["correction_arrived_after_watermark"])
        self.assertNotEqual(pin["current_head_version"], pin["pinned_version"])
        # 钉版内容仍是发布时看到的数字
        self.assertEqual(pin["evidence"].payload["tonnes"], 945.3)

        # 新分析可以引用修订版；旧发布版保持原样
        self.clock.tick(10)
        self.svc.save_draft(
            "conclusion", "归因结论 v2", "按修订后持仓数据更新",
            [claim("c-flow-pending", "ETF 仍在流入，但单日增量下修",
                   support=(revised_etf,))],
            self.alice, base_revision=1,
        )
        old = self.svc.get_revision("conclusion", 1, self.alice)
        old_etf_link = [
            it for cl in old["claims"] for it in sum(cl["stances"].values(), [])
            if it["evidence"].id == self.ctx.etf
        ][0]
        self.assertEqual(old_etf_link["evidence"].payload["tonnes"], 945.3)


if __name__ == "__main__":
    unittest.main()
