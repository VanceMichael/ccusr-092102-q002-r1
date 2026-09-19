"""公布前冻结与生命周期：预期概率与采集时刻冻结后不可补录/修改。"""

import unittest
from datetime import datetime, timezone

from src.gold_attribution.models import EventState, LifecycleError
from src.gold_attribution.service import Actor
from src.gold_attribution.storage import Ledger
from src.gold_attribution.service import AttributionService

from tests._scenario import DUE, EVENT_ID, FakeClock, make_service


class FreezeLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.alice = Actor("macro", "alice")
        self.svc.create_event(EVENT_ID, "9月议息", DUE, self.alice)

    def test_naive_market_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(Exception, "时区"):
            self.svc.record_expectation(
                EVENT_ID, "s", "v1", datetime(2026, 9, 16, 9, 0),
                {"25bp": 1.0}, self.alice,
            )

    def test_probability_distribution_must_sum_to_one(self) -> None:
        with self.assertRaisesRegex(Exception, "概率之和"):
            self.svc.record_expectation(
                EVENT_ID, "s", "v1",
                datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc),
                {"25bp": 0.7, "hold": 0.7}, self.alice,
            )

    def test_freeze_blocks_later_expectations_and_logs_rejection(self) -> None:
        self.svc.record_expectation(
            EVENT_ID, "FedWatch", "v1",
            datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc),
            {"25bp": 0.97, "hold": 0.03}, self.alice,
        )
        self.svc.freeze_expectations(EVENT_ID, DUE, self.alice)
        self.assertEqual(self.svc.get_event(EVENT_ID).state, EventState.FROZEN)

        with self.assertRaises(LifecycleError):
            self.svc.record_expectation(
                EVENT_ID, "迟到投行", "v2",
                datetime(2026, 9, 16, 13, 55, tzinfo=timezone.utc),
                {"25bp": 1.0}, self.alice,
            )
        rejected = self.svc.list_rejected_writes()
        self.assertEqual([r["reason"] for r in rejected], ["frozen_expectation_write"])
        # 被拒内容没有进入预期表
        sources = [e.source for e in self.svc.list_expectations(EVENT_ID)]
        self.assertEqual(sources, ["FedWatch"])

    def test_cannot_freeze_twice(self) -> None:
        self.svc.freeze_expectations(EVENT_ID, DUE, self.alice)
        with self.assertRaises(LifecycleError):
            self.svc.freeze_expectations(EVENT_ID, DUE, self.alice)

    def test_market_evidence_rejected_before_decision(self) -> None:
        self.svc.freeze_expectations(EVENT_ID, DUE, self.alice)
        from src.gold_attribution.models import EvidenceKind
        with self.assertRaises(LifecycleError):
            self.svc.attach_evidence(
                EVENT_ID, EvidenceKind.SPOT_GOLD, "LBMA", "pm",
                datetime(2026, 9, 16, 15, 0, tzinfo=DUE.tzinfo),
                {"price": 4300.0}, "America/New_York", self.alice, currency="USD",
            )

    def test_analysis_series_requires_decided_event(self) -> None:
        with self.assertRaises(LifecycleError):
            self.svc.create_analysis_series("s1", EVENT_ID, "macro", "t", self.alice)


if __name__ == "__main__":
    unittest.main()
