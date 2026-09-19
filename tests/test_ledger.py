"""领域服务测试：冻结、修订链、水位、WORM、并发、团队隔离、护栏。"""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path

from src.gold_attribution.errors import (
    ConflictError,
    NotFound,
    PermissionDenied,
    TradingInstructionError,
    ValidationError,
)
from src.gold_attribution.ledger import Ledger
from src.gold_attribution.storage import Storage

ALICE = {"actor": "alice", "team": "macro"}
BOB = {"actor": "bob", "team": "rates"}

EVENT = {
    "event_id": "fed-2026-09-16",
    "title": "9 月议息",
    "policy_body": "Federal Reserve",
    "scheduled_at": "2026-09-16T14:00:00-04:00",
    "freeze_at": "2026-09-16T13:30:00-04:00",
}


def make_ledger() -> Ledger:
    return Ledger(Storage(":memory:"))


def seeded() -> tuple[Ledger, dict[str, int]]:
    lg = make_ledger()
    lg.create_event(EVENT, **ALICE)
    eid = EVENT["event_id"]
    exp = lg.add_expectation(eid, {"source": "FedWatch", "outcome": "hike25", "probability": 0.92,
                                   "collected_at": "2026-09-16T13:00:00-04:00",
                                   "source_version": "13:00"}, **ALICE)
    dec = lg.add_decision(eid, {"action": "hike", "basis_points": 25,
                                "decided_at": "2026-09-16T14:00:00-04:00",
                                "source": "FOMC", "source_version": "v1"}, **ALICE)
    dip = lg.add_observation(eid, {"series": "spot_gold", "label": "现货金低点",
                                   "value": 4281.5, "currency": "USD", "unit": "ounce",
                                   "market_tz": "America/New_York",
                                   "observed_at": "2026-09-16T14:05:00-04:00",
                                   "collected_at": "2026-09-16T14:06:00-04:00",
                                   "source": "LBMA", "source_version": "tick-1405"}, **ALICE)
    rec = lg.add_observation(eid, {"series": "spot_gold", "label": "现货金收复",
                                   "value": 4302.0, "currency": "USD", "unit": "ounce",
                                   "market_tz": "America/New_York",
                                   "observed_at": "2026-09-16T15:30:00-04:00",
                                   "collected_at": "2026-09-16T15:31:00-04:00",
                                   "source": "LBMA", "source_version": "tick-1530"}, **ALICE)
    lg.freeze_expectations(eid, {"frozen_at": "2026-09-16T13:30:00-04:00"}, **ALICE)
    return lg, {"event_id": eid, "exp": exp["id"], "dec": dec["id"],
                "dip": dip["id"], "rec": rec["id"]}


CLAIM = ("加息 25bp 与会前 92% 概率一致，冲击被提前定价，"
         "现货金短跌后收复说明单靠利率无法解释。")


def analysis_payload(ids: dict[str, int], *, body: str = "竞争解释正文，足够长的一段。") -> dict:
    return {
        "title": "加息后的黄金韧性",
        "body": body,
        "claims": [
            {"text": CLAIM,
             "evidence": [
                 {"kind": "supporting", "target_type": "expectation", "target_id": ids["exp"]},
                 {"kind": "supporting", "target_type": "decision", "target_id": ids["dec"]},
                 {"kind": "contradicting", "target_type": "observation",
                  "target_id": ids["dip"], "note": "瞬时低点与利率主导叙事有张力"},
                 {"kind": "supporting", "target_type": "observation", "target_id": ids["rec"]},
             ]},
            {"text": "数据存在披露滞后，盘中资金流时点尚不能确认。",
             "evidence": [{"kind": "uncertainty", "target_type": "observation",
                           "target_id": ids["rec"], "note": "待 ETF 数据复核"}]},
        ],
    }


class FreezeTests(unittest.TestCase):
    def test_freeze_blocks_late_changes(self) -> None:
        lg, ids = seeded()
        with self.assertRaises(ConflictError):
            lg.add_expectation(ids["event_id"],
                               {"source": "late", "outcome": "hold", "probability": 0.5,
                                "collected_at": "2026-09-16T13:31:00-04:00"}, **ALICE)
        with self.assertRaises(ConflictError):
            lg.freeze_expectations(ids["event_id"], None, **ALICE)

    def test_collection_after_freeze_deadline_rejected_even_before_explicit_freeze(self) -> None:
        lg = make_ledger()
        lg.create_event(EVENT, **ALICE)
        with self.assertRaises(ConflictError):
            lg.add_expectation(EVENT["event_id"],
                               {"source": "s", "outcome": "hold", "probability": 0.5,
                                "collected_at": "2026-09-16T13:31:00-04:00"}, **ALICE)

    def test_naive_timestamp_rejected(self) -> None:
        lg = make_ledger()
        lg.create_event(EVENT, **ALICE)
        with self.assertRaises(ValidationError):
            lg.add_expectation(EVENT["event_id"],
                               {"source": "s", "outcome": "hold", "probability": 0.5,
                                "collected_at": "2026-09-16T13:00:00"}, **ALICE)

    def test_probability_range(self) -> None:
        lg = make_ledger()
        lg.create_event(EVENT, **ALICE)
        with self.assertRaises(ValidationError):
            lg.add_expectation(EVENT["event_id"],
                               {"source": "s", "outcome": "hold", "probability": 1.2,
                                "collected_at": "2026-09-16T13:00:00-04:00"}, **ALICE)


class ObservationRevisionTests(unittest.TestCase):
    def test_correction_appends_and_folds_at_watermark(self) -> None:
        lg, ids = seeded()
        revised = lg.add_observation(ids["event_id"], {
            "series": "spot_gold", "label": "现货金低点", "value": 4284.2,
            "currency": "USD", "unit": "ounce", "market_tz": "America/New_York",
            "observed_at": "2026-09-16T14:05:00-04:00",
            "collected_at": "2026-09-16T16:00:00-04:00",
            "source": "LBMA", "source_version": "tick-1405-corrected",
            "replaces_id": ids["dip"]}, **ALICE)
        # 旧记录仍在
        self.assertEqual(lg._observation(ids["dip"])["value"], 4281.5)
        latest = {(o["series"], o["label"]): o
                  for o in lg.current_watermark(ids["event_id"])["observations_latest"]}
        self.assertEqual(latest[("spot_gold", "现货金低点")]["id"], revised["id"])
        self.assertEqual(latest[("spot_gold", "现货金低点")]["replaces_id"], ids["dip"])

    def test_revision_chain_cannot_fork(self) -> None:
        lg, ids = seeded()
        lg.add_observation(ids["event_id"], {
            "series": "spot_gold", "label": "现货金低点", "value": 4284.2,
            "observed_at": "2026-09-16T14:05:00-04:00",
            "collected_at": "2026-09-16T16:00:00-04:00",
            "source": "LBMA", "source_version": "v2", "replaces_id": ids["dip"]}, **ALICE)
        with self.assertRaises(ConflictError):
            lg.add_observation(ids["event_id"], {
                "series": "spot_gold", "label": "现货金低点", "value": 4280.0,
                "observed_at": "2026-09-16T14:05:00-04:00",
                "collected_at": "2026-09-16T17:00:00-04:00",
                "source": "LBMA", "source_version": "v3", "replaces_id": ids["dip"]}, **ALICE)

    def test_revision_requires_same_series_and_label(self) -> None:
        lg, ids = seeded()
        with self.assertRaises(ValidationError):
            lg.add_observation(ids["event_id"], {
                "series": "gold_futures", "label": "现货金低点", "value": 1,
                "observed_at": "2026-09-16T14:05:00-04:00",
                "collected_at": "2026-09-16T16:00:00-04:00",
                "source": "CME", "source_version": "v2", "replaces_id": ids["dip"]}, **ALICE)

    def test_original_currency_and_timezone_preserved(self) -> None:
        lg, ids = seeded()
        dip = lg._observation(ids["dip"])
        self.assertEqual(dip["currency"], "USD")
        self.assertEqual(dip["market_tz"], "America/New_York")
        self.assertEqual(dip["observed_at"], "2026-09-16T14:05:00-04:00")
        self.assertEqual(dip["source_version"], "tick-1405")

    def test_unknown_series_rejected(self) -> None:
        lg, ids = seeded()
        with self.assertRaises(ValidationError):
            lg.add_observation(ids["event_id"], {"series": "crypto", "label": "x",
                               "observed_at": "2026-09-16T14:05:00-04:00",
                               "collected_at": "2026-09-16T14:06:00-04:00",
                               "source": "s", "source_version": "1"}, **ALICE)


class AnalysisTests(unittest.TestCase):
    def test_draft_only_visible_to_owner_team(self) -> None:
        lg, ids = seeded()
        an = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        self.assertEqual(lg.get_analysis(an["id"], **ALICE)["status"], "draft")
        with self.assertRaises(NotFound):
            lg.get_analysis(an["id"], **BOB)
        chains = lg.list_analyses(ids["event_id"], team="rates")["chains"]
        self.assertEqual(chains, [])

    def test_publish_requires_frozen_expectations(self) -> None:
        lg = make_ledger()
        lg.create_event(EVENT, **ALICE)
        an = lg.create_analysis(EVENT["event_id"], {
            "title": "t", "body": "b",
            "claims": [{"text": "没有冻结预期也能写，但不能发布。", "evidence": []}]}, **ALICE)
        with self.assertRaises(ConflictError):
            lg.publish_analysis(an["id"], **ALICE)

    def test_published_version_immutable_and_visible(self) -> None:
        lg, ids = seeded()
        an = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        lg.publish_analysis(an["id"], **ALICE)
        lg.get_analysis(an["id"], **BOB)  # 发布后跨团队可见
        with self.assertRaises(ConflictError):
            lg.update_draft(an["id"], {**analysis_payload(ids), "expected_rev": 1}, **ALICE)

    def test_revision_forms_new_version_without_overwriting(self) -> None:
        lg, ids = seeded()
        v1 = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        lg.publish_analysis(v1["id"], **ALICE)
        rev = lg.revise_analysis(v1["id"], **ALICE)
        self.assertEqual(rev["seq"], 2)
        self.assertEqual(rev["revision_of_id"], v1["id"])
        self.assertEqual(rev["status"], "draft")
        self.assertEqual(rev["claims"][0]["text"], CLAIM)  # 复制判断作为起点
        # 旧版本内容原样保留
        old = lg.get_analysis(v1["id"], **ALICE)
        self.assertEqual(old["seq"], 1)
        self.assertEqual(old["status"], "published")
        # 修订草稿期间不能再开第二个修订
        with self.assertRaises(ConflictError):
            lg.revise_analysis(v1["id"], **ALICE)
        # 只能修订已发布版本
        with self.assertRaises(ConflictError):
            lg.revise_analysis(rev["id"], **ALICE)

    def test_other_team_cannot_touch_draft(self) -> None:
        lg, ids = seeded()
        an = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        with self.assertRaises(PermissionDenied):
            lg.update_draft(an["id"], {**analysis_payload(ids), "expected_rev": 1}, **BOB)
        with self.assertRaises(PermissionDenied):
            lg.publish_analysis(an["id"], **BOB)

    def test_evidence_must_belong_to_same_event(self) -> None:
        lg, ids = seeded()
        other_event = dict(EVENT, event_id="ecb-2026-09-17",
                           scheduled_at="2026-09-17T08:15:00-04:00",
                           freeze_at="2026-09-17T07:45:00-04:00")
        lg.create_event(other_event, **ALICE)
        foreign = lg.add_expectation(other_event["event_id"],
                                     {"source": "s", "outcome": "hold", "probability": 0.5,
                                      "collected_at": "2026-09-17T07:00:00-04:00"}, **ALICE)
        payload = analysis_payload(ids)
        payload["claims"][0]["evidence"][0]["target_id"] = foreign["id"]
        with self.assertRaises(NotFound):
            lg.create_analysis(ids["event_id"], payload, **ALICE)

    def test_competing_explanations_coexist(self) -> None:
        lg, ids = seeded()
        a1 = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        lg.publish_analysis(a1["id"], **ALICE)
        p2 = {
            "title": "竞争解释：风险溢价主导", "body": "地缘与财政信用才是边际推手。",
            "claims": [{"text": "期金突破时点贴近地缘晨报，风险溢价解释更有力。",
                        "evidence": [{"kind": "supporting", "target_type": "decision",
                                      "target_id": ids["dec"]}]}],
        }
        a2 = lg.create_analysis(ids["event_id"], p2, **BOB)
        lg.publish_analysis(a2["id"], **BOB)
        chains = lg.list_analyses(ids["event_id"], team="macro")["chains"]
        self.assertEqual(len(chains), 2)


class ConcurrencyTests(unittest.TestCase):
    def test_optimistic_lock_conflict_then_explicit_merge(self) -> None:
        lg, ids = seeded()
        an = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        # A 先提交成功
        payload = analysis_payload(ids, body="同事甲合并后的正文。")
        lg.update_draft(an["id"], {**payload, "expected_rev": 1}, actor="a2", team="macro")
        # B 基于旧 rev 提交 → 409，响应带回当前内容供人工合并
        stale = analysis_payload(ids, body="同事乙基于旧版本的正文。")
        with self.assertRaises(ConflictError) as ctx:
            lg.update_draft(an["id"], {**stale, "expected_rev": 1}, actor="b2", team="macro")
        self.assertEqual(ctx.exception.details["current_rev"], 2)
        self.assertIn("current_body", ctx.exception.details)
        # B 显式合并后以新 rev 重提 → 成功；不允许无条件覆盖
        merged = analysis_payload(ids, body="同事乙人工合并双方内容后的正文。")
        again = lg.update_draft(an["id"], {**merged, "expected_rev": 2}, actor="b2", team="macro")
        self.assertEqual(again["rev"], 3)
        with self.assertRaises(ValidationError):
            lg.update_draft(an["id"], {**merged}, actor="b2", team="macro")

    def test_parallel_updates_exactly_one_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c.db")
            seed_db = Storage(path)
            slg = Ledger(seed_db)
            slg.create_event(EVENT, **ALICE)
            exp = slg.add_expectation(EVENT["event_id"],
                                      {"source": "s", "outcome": "hike25", "probability": 0.9,
                                       "collected_at": "2026-09-16T13:00:00-04:00"}, **ALICE)
            slg.freeze_expectations(EVENT["event_id"],
                                    {"frozen_at": "2026-09-16T13:30:00-04:00"}, **ALICE)
            an = slg.create_analysis(EVENT["event_id"], {
                "title": "t", "body": "初始正文，足够长。",
                "claims": [{"text": "初始判断内容，足够长。", "evidence": [
                    {"kind": "supporting", "target_type": "expectation",
                     "target_id": exp["id"]}]}]}, **ALICE)
            seed_db.close()

            results: list[str] = []
            lock = threading.Lock()

            def worker(actor: str) -> None:
                db = Storage(path)
                lg = Ledger(db)
                try:
                    lg.update_draft(an["id"], {
                        "title": "t", "body": f"{actor} 的正文内容。",
                        "claims": [{"text": f"{actor} 的判断内容。", "evidence": []}],
                        "expected_rev": 1}, actor=actor, team="macro")
                    with lock:
                        results.append("ok")
                except ConflictError:
                    with lock:
                        results.append("conflict")
                finally:
                    db.close()

            t1 = threading.Thread(target=worker, args=("w1",))
            t2 = threading.Thread(target=worker, args=("w2",))
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(sorted(results), ["conflict", "ok"])


class TradingGuardTests(unittest.TestCase):
    def test_instructional_text_blocked(self) -> None:
        lg, ids = seeded()
        bad = analysis_payload(ids, body="结论：建议买入黄金并加仓。")
        with self.assertRaises(TradingInstructionError):
            lg.create_analysis(ids["event_id"], bad, **ALICE)

    def test_descriptive_purchase_narrative_allowed(self) -> None:
        lg, ids = seeded()
        ok = analysis_payload(ids, body="央行购金与储备多元化属于结构性需求的描述性判断。")
        an = lg.create_analysis(ids["event_id"], ok, **ALICE)
        self.assertEqual(an["status"], "draft")


class WatermarkAndDiffTests(unittest.TestCase):
    def test_published_watermark_reconstructs_then_state(self) -> None:
        lg, ids = seeded()
        an = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        v1 = lg.publish_analysis(an["id"], **ALICE)
        self.assertEqual(len(v1["watermark"]["observations_latest"]), 2)
        frozen_values = [o["value"] for o in v1["watermark"]["observations_latest"]]

        # 公布后：更正旧数据 + 加入期金突破；已发布水位必须不变
        lg.add_observation(ids["event_id"], {
            "series": "spot_gold", "label": "现货金低点", "value": 4284.2,
            "observed_at": "2026-09-16T14:05:00-04:00",
            "collected_at": "2026-09-16T16:00:00-04:00",
            "source": "LBMA", "source_version": "tick-1405-corrected",
            "replaces_id": ids["dip"]}, **ALICE)
        lg.add_observation(ids["event_id"], {
            "series": "gold_futures", "label": "COMEX 期金", "value": 4408.0,
            "currency": "USD", "unit": "ounce", "market_tz": "America/New_York",
            "observed_at": "2026-09-17T10:00:00-04:00",
            "collected_at": "2026-09-17T10:01:00-04:00",
            "source": "CME", "source_version": "sess-0917"}, **ALICE)
        restored = lg.get_analysis(an["id"], **ALICE)
        restored_values = [o["value"] for o in restored["watermark"]["observations_latest"]]
        self.assertEqual(restored_values, frozen_values)
        # 当前水位则反映新材料
        self.assertEqual(len(lg.current_watermark(ids["event_id"])["observations_latest"]), 3)

        rev = lg.revise_analysis(an["id"], **ALICE)
        new_payload = analysis_payload(ids, body="修订版：纳入期金突破与低点更正后的判断。")
        new_payload["claims"].append({
            "text": "纽约期金次日突破 4400，边际驱动需纳入风险溢价候选。",
            "evidence": [{"kind": "uncertainty", "target_type": "observation",
                          "target_id": ids["dip"], "note": "驱动权重待定"}]})
        lg.update_draft(rev["id"], {**new_payload, "expected_rev": 1}, **ALICE)
        v2 = lg.publish_analysis(rev["id"], **ALICE)
        diff = lg.diff_analyses(an["id"], rev["id"], team="macro")
        positions = [(c["position"], c["change"]) for c in diff["claim_changes"]]
        self.assertIn((3, "added"), positions)
        self.assertTrue(diff["body_changed"])
        wm = diff["watermark_change"]
        self.assertTrue(wm["available"])
        added_series = {o["series"] for o in wm["observations_added"]}
        self.assertIn("gold_futures", added_series)
        self.assertEqual(len(wm["observations_revised"]), 1)
        revised_item = wm["observations_revised"][0]
        self.assertEqual(revised_item["series"], "spot_gold")
        self.assertEqual(revised_item["value"], 4284.2)
        self.assertEqual(revised_item["replaces_id"], ids["dip"])
        v2_spot = {o["label"]: o["value"]
                   for o in v2["watermark"]["observations_latest"]
                   if o["series"] == "spot_gold"}
        self.assertEqual(v2_spot["现货金低点"], 4284.2)

    def test_diff_rejects_cross_chain(self) -> None:
        lg, ids = seeded()
        a1 = lg.create_analysis(ids["event_id"], analysis_payload(ids), **ALICE)
        lg.publish_analysis(a1["id"], **ALICE)
        a2 = lg.create_analysis(ids["event_id"],
                                {**analysis_payload(ids), "title": "另一条链"}, **BOB)
        lg.publish_analysis(a2["id"], **BOB)
        with self.assertRaises(ValidationError):
            lg.diff_analyses(a1["id"], a2["id"], team="macro")


if __name__ == "__main__":
    unittest.main()
