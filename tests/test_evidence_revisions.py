"""跨市场证据：原币种、原时区、来源版本与修订链。"""

import unittest
from datetime import datetime, timezone

from src.gold_attribution.models import EvidenceKind, ValidationError
from src.gold_attribution.service import Actor

from tests._scenario import DUE, EVENT_ID, NY, build_decided_event, make_service


class EvidenceRevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.alice = Actor("macro", "alice")
        self.ctx = build_decided_event(self.svc, self.alice)

    def test_original_currency_and_tz_preserved(self) -> None:
        chains = {e.kind: e for e in self.svc.list_evidence_chains(EVENT_ID)}
        spot = chains[EvidenceKind.SPOT_GOLD]
        # list_evidence_chains 只返回每链最新；SGE 是独立 stream
        all_chains = self.svc.list_evidence_chains(EVENT_ID)
        sge = next(e for e in all_chains if e.source == "SGE")
        self.assertEqual(sge.currency, "CNY")
        self.assertEqual(sge.market_tz, "Asia/Shanghai")
        self.assertEqual(sge.observed_at.utcoffset().total_seconds(), 8 * 3600)
        self.assertIsNotNone(spot)
        # 原始纽约偏移不被转换
        lbma = next(e for e in all_chains if e.source == "LBMA")
        self.assertEqual(lbma.observed_at.utcoffset(), NY.utcoffset(DUE))
        self.assertEqual(lbma.currency, "USD")

    def test_price_requires_iso_currency(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.attach_evidence(
                EVENT_ID, EvidenceKind.SPOT_GOLD, "X", "v1",
                datetime(2026, 9, 16, 15, tzinfo=NY), {"price": 1.0},
                "America/New_York", self.alice, currency="美元",
            )

    def test_source_revision_appends_new_version_without_overwrite(self) -> None:
        v1 = self.ctx.spot
        self.clock.tick(60)
        v2 = self.svc.attach_evidence(
            EVENT_ID, EvidenceKind.SPOT_GOLD, "LBMA", "PM-fix-2026-09-16-REV1",
            datetime(2026, 9, 16, 15, 0, tzinfo=NY),
            {"price": 4304.90, "intraday_low": 4278.10, "note": "定盘价小幅修订"},
            "America/New_York", self.alice, currency="USD",
            revision_of_version="PM-fix-2026-09-16",
            revision_note="LBMA 事后修订 PM 定盘价 -0.30 美元",
        )
        self.assertNotEqual(v1, v2)
        with self.svc.db.tx() as c:
            old = self.svc.db.get_evidence(c, v1)
            new = self.svc.db.get_evidence(c, v2)
            self.assertIn("4305.2", old["payload"])  # 旧行原样保留
            self.assertEqual(new["supersedes_id"], v1)
            self.assertEqual(new["seq"], old["seq"] + 1)
            self.assertEqual(new["chain_id"], old["chain_id"])

    def test_same_source_version_cannot_be_overwritten(self) -> None:
        # 不带 revision_of_version 又撞上同一版本流 → 拒绝，必须显式声明更正
        with self.assertRaises(ValidationError):
            self.svc.attach_evidence(
                EVENT_ID, EvidenceKind.SPOT_GOLD, "LBMA", "PM-fix-2026-09-16",
                datetime(2026, 9, 16, 15, 0, tzinfo=NY), {"price": 9999.0},
                "America/New_York", self.alice, currency="USD",
            )

    def test_next_day_observation_is_separate_chain_not_correction(self) -> None:
        next_day = self.svc.attach_evidence(
            EVENT_ID, EvidenceKind.SPOT_GOLD, "LBMA", "AM-fix-2026-09-17",
            datetime(2026, 9, 17, 10, 30, tzinfo=NY), {"price": 4310.0},
            "America/New_York", self.alice, currency="USD",
        )
        with self.svc.db.tx() as c:
            row = self.svc.db.get_evidence(c, next_day)
            self.assertEqual(row["seq"], 1)
            self.assertIsNone(row["supersedes_id"])

    def test_revision_requires_note(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.attach_evidence(
                EVENT_ID, EvidenceKind.ETF_HOLDING, "GLD-holdings", "2026-09-16b",
                datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc),
                {"tonnes": 946.0}, "UTC", self.alice,
                revision_of_version="2026-09-16",
            )

    def test_revision_must_target_existing_version(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.attach_evidence(
                EVENT_ID, EvidenceKind.ETF_HOLDING, "GLD-holdings", "2026-09-16x",
                datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc),
                {"tonnes": 946.0}, "UTC", self.alice,
                revision_of_version="1900-01-01", revision_note="改",
            )

    def test_window_times_must_keep_original_tz(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.attach_evidence(
                EVENT_ID, EvidenceKind.USD_WINDOW, "ICE-DXY", "bad-window",
                datetime(2026, 9, 16, 16, tzinfo=NY),
                {"index_level": 98.0, "window_start": "2026-09-16T13:30:00"},
                "America/New_York", self.alice,
            )


if __name__ == "__main__":
    unittest.main()
