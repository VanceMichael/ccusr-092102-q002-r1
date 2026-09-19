"""场景装载测试与原 fixture 结构检查。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.gold_attribution.ledger import Ledger
from src.gold_attribution.seed import load_scenario
from src.gold_attribution.storage import Storage

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class ScenarioFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = json.loads(
            (FIXTURES / "fed_2026_09_16_scenario.json").read_text(encoding="utf-8"))
        self.db = Storage(":memory:")
        self.result = load_scenario(self.db, json.loads(json.dumps(self.scenario)))
        self.ledger = Ledger(self.db)
        self.ids = self.result["ids"]

    def test_expectations_frozen_before_decision(self) -> None:
        event = self.ledger.get_event("fed-2026-09-16")
        self.assertEqual(event["expectations_frozen"], 1)
        exps = self.ledger.list_expectations("fed-2026-09-16")
        self.assertEqual(len(exps), 4)
        self.assertTrue(all(e["frozen_at"] == "2026-09-16T13:30:00-04:00" for e in exps))

    def test_decision_revision_chain_kept(self) -> None:
        decisions = self.ledger.list_decisions("fed-2026-09-16")
        self.assertEqual(len(decisions), 2)
        latest = decisions[-1]
        self.assertEqual(latest["source_version"], "statement-2026-09-16-erratum")
        self.assertEqual(latest["replaces_id"], decisions[0]["id"])
        # 初版仍可被引用与读取
        self.assertEqual(decisions[0]["source_version"], "statement-2026-09-16")

    def test_cross_market_original_fields_preserved(self) -> None:
        obs = self.ledger.list_observations("fed-2026-09-16")
        by_label = {o["label"]: o for o in obs}
        gld = by_label["SPDR GLD 持仓（伦敦时区披露）"]
        self.assertEqual(gld["market_tz"], "Europe/London")          # 原时区
        self.assertEqual(gld["observed_at"], "2026-09-16T00:00:00+01:00")
        self.assertEqual(gld["unit"], "tonne")
        geo = by_label["中东航运风险升级通报"]
        self.assertEqual(geo["market_tz"], "Asia/Singapore")
        self.assertIsNone(geo["value"])                              # 文本类材料
        self.assertEqual(geo["payload"]["风险类型"], "地缘")
        dip = next(iter(o for o in obs if o["source_version"] == "tick-1405"))
        self.assertEqual(dip["value"], 4281.5)
        self.assertIsNone(dip["replaces_id"])

    def test_watermark_folds_correction(self) -> None:
        wm = self.ledger.current_watermark("fed-2026-09-16")
        dips = [o for o in wm["observations_latest"] if o["label"].startswith("LBMA 现货金·公布后")]
        self.assertEqual(len(dips), 1)
        self.assertEqual(dips[0]["source_version"], "tick-1405-corrected")
        self.assertEqual(dips[0]["value"], 4284.2)

    def test_two_competing_published_analyses_with_watermarks(self) -> None:
        ids = self.result["analysis_ids"]
        self.assertEqual(len(ids), 2)
        for aid in ids:
            an = self.ledger.get_analysis(aid, team="macro")
            self.assertEqual(an["status"], "published")
            self.assertTrue(an["claims"])
            self.assertIn("watermark", an)
        chains = self.ledger.list_analyses("fed-2026-09-16", team="macro")["chains"]
        self.assertEqual(len(chains), 2)

    def test_all_three_evidence_kinds_present(self) -> None:
        kinds: set[str] = set()
        for aid in self.result["analysis_ids"]:
            for claim in self.ledger.get_analysis(aid, team="macro")["claims"]:
                kinds.update(e["kind"] for e in claim["evidence"])
        self.assertEqual(kinds, {"supporting", "contradicting", "uncertainty"})


class LegacyFixtureTest(unittest.TestCase):
    """保留并校验最初的简版 fixture。"""

    def test_expectation_and_actual_are_kept(self) -> None:
        payload = json.loads((FIXTURES / "policy_event.json").read_text(encoding="utf-8"))
        self.assertIn("expected_basis_points", payload)
        self.assertIn("actual_basis_points", payload)
        self.assertTrue(payload["observations"])


if __name__ == "__main__":
    unittest.main()
