"""竞争解释、草稿权限与 WORM 发布。"""

import unittest

from src.gold_attribution.models import (
    AnalysisStatus,
    Claim,
    ForbiddenError,
    LifecycleError,
    Stance,
    ValidationError,
)
from src.gold_attribution.service import Actor

from tests._scenario import EVENT_ID, build_decided_event, claim, make_service


class AnalysisPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.macro = Actor("macro", "alice")
        self.ctx = build_decided_event(self.svc, self.macro)
        self.strategy = Actor("strategy", "bob")
        c = self.ctx
        # 解释 A：利率/定价单因
        self.svc.create_analysis_series("rates-only", EVENT_ID, "macro", "利率单因解释", self.macro)
        self.rates_rev1 = self.svc.save_draft(
            "rates-only", "利率单因解释", "加息已被提前定价，短跌后修复",
            [claim("c-pricing", "25bp 几乎被充分定价，公布的冲击有限",
                   support=(c.decision,), against=(), uncertain=()),
             claim("c-real-rate", "实际利率回落解释金价反弹",
                   support=(c.real_rate,), against=(), uncertain=())],
            self.macro, base_revision=0,
        )
        # 解释 B：资金流 + 官方购金 + 风险溢价（与 A 竞争）
        self.svc.create_analysis_series("flows-risk", EVENT_ID, "strategy", "资金流与风险解释",
                                        self.strategy)
        self.flows_rev1 = self.svc.save_draft(
            "flows-risk", "资金流与风险解释",
            "ETF 流入、央行购金与地缘/财政风险共同托底",
            [claim("c-flow", "ETF 持续流入与官方购金构成结构性买盘",
                   support=(c.etf, c.cb)),
             claim("c-risk", "地缘与财政信用风险抬升避险需求",
                   support=(c.geo, c.fiscal)),
             claim("c-rate-limited", "单用利率会忽略非利率因素的相互牵制",
                   support=(c.spot, c.futures), against=(c.decision,),
                   uncertain=(c.dxy,))],
            self.strategy, base_revision=0, competing_series=("rates-only",),
        )

    def test_draft_readable_only_by_owner_team(self) -> None:
        with self.assertRaises(ForbiddenError):
            self.svc.get_revision("rates-only", 1, self.strategy)
        # 未发布系列对其他团队在列表中不可见
        listing = self.svc.list_event_series(EVENT_ID, self.strategy)
        self.assertNotIn("rates-only", [x["series_id"] for x in listing])

    def test_owner_team_reads_own_draft(self) -> None:
        view = self.svc.get_revision("rates-only", 1, self.macro)
        self.assertEqual(view["status"], AnalysisStatus.DRAFT.value)
        stances = {cl["local_id"]: cl["stances"] for cl in view["claims"]}
        self.assertIn(Stance.SUPPORTING.value, stances["c-pricing"])

    def test_published_version_is_worm_and_world_readable(self) -> None:
        rev = self.svc.publish("rates-only", self.macro)
        self.assertEqual(rev, 1)
        view = self.svc.get_revision("rates-only", 1, self.strategy)
        self.assertEqual(view["status"], AnalysisStatus.PUBLISHED.value)

        # 已发布修订不能再发布/改写
        with self.assertRaises(LifecycleError):
            self.svc.publish("rates-only", self.macro)

        # 更正在同一系列形成新修订，旧版变为 superseded，但内容仍可原样取回
        self.clock.tick(30)
        self.svc.save_draft(
            "rates-only", "利率主因（修订）",
            "利率仍是主线，但承认资金流的边际作用",
            [claim("c-pricing", "25bp 被充分定价", support=(self.ctx.decision,)),
             claim("c-flow-concede", "不否认 ETF 流入的托底作用", support=(self.ctx.etf,))],
            self.macro, base_revision=1,
        )
        self.svc.publish("rates-only", self.macro)
        old = self.svc.get_revision("rates-only", 1, self.strategy)
        new = self.svc.get_revision("rates-only", 2, self.strategy)
        self.assertEqual(old["status"], AnalysisStatus.SUPERSEDED.value)
        self.assertEqual(new["status"], AnalysisStatus.PUBLISHED.value)
        self.assertEqual(old["summary"], "加息已被提前定价，短跌后修复")
        self.assertEqual(len(old["claims"]), 2)

    def test_non_owner_cannot_publish_or_edit(self) -> None:
        with self.assertRaises(ForbiddenError):
            self.svc.publish("rates-only", self.strategy)
        with self.assertRaises(ForbiddenError):
            self.svc.save_draft(
                "rates-only", "x", "x", [Claim("c", "x", {})],
                self.strategy, base_revision=1,
            )

    def test_cannot_create_series_for_other_team(self) -> None:
        with self.assertRaises(ForbiddenError):
            self.svc.create_analysis_series("sneak", EVENT_ID, "strategy", "t", self.macro)

    def test_competing_series_must_share_event(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.save_draft(
                "flows-risk", "t", "s",
                [claim("c-flow", "x")], self.strategy, base_revision=1,
                competing_series=("nonexistent-series",),
            )

    def test_evidence_from_other_event_cannot_be_linked(self) -> None:
        # 建第二个事件并取一条证据
        from datetime import datetime, timedelta, timezone
        from tests._scenario import DUE
        ny = timezone(timedelta(hours=-4))
        self.svc.create_event("fed-other", "另一场议息", DUE, self.macro)
        self.svc.record_expectation(
            "fed-other", "s", "v", datetime(2026, 9, 16, 9, tzinfo=timezone.utc),
            {"25bp": 1.0}, self.macro,
        )
        self.svc.freeze_expectations("fed-other", DUE, self.macro)
        other_decision = self.svc.record_decision(
            "fed-other", "FOMC", "v", DUE, {"basis_points": 25},
            "America/New_York", self.macro,
        )
        with self.assertRaises(ValidationError):
            self.svc.save_draft(
                "rates-only", "t", "s",
                [claim("c-cross", "挂别的事件的证据", support=(other_decision,))],
                self.macro, base_revision=1,
            )

    def test_trading_instruction_in_claim_is_rejected_and_logged(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.save_draft(
                "rates-only", "t", "建议立即买入黄金",
                [claim("c", "正文无问题")], self.macro, base_revision=1,
            )
        reasons = [r["reason"] for r in self.svc.list_rejected_writes()]
        self.assertIn("analysis_text", reasons)

    def test_factual_central_bank_statement_is_allowed(self) -> None:
        rev = self.svc.save_draft(
            "rates-only", "事实陈述", "官方部门月报显示央行持续买入黄金",
            [claim("c-fact", "新兴市场央行为分散储备持续买入黄金，属事实归因",
                   support=(self.ctx.cb,))],
            self.macro, base_revision=1,
        )
        self.assertEqual(rev, 2)


if __name__ == "__main__":
    unittest.main()
