"""并发修改：乐观锁冲突必须显式合并或拒绝。"""

import unittest

from src.gold_attribution.models import ConflictError, LifecycleError, ValidationError
from src.gold_attribution.service import Actor

from tests._scenario import EVENT_ID, build_decided_event, claim, make_service


class ConcurrentDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.alice = Actor("macro", "alice")
        self.carol = Actor("macro", "carol")  # 同一团队的第二位分析师
        self.ctx = build_decided_event(self.svc, self.alice)
        self.svc.create_analysis_series("shared", EVENT_ID, "macro", "团队合写解释", self.alice)
        # rev1：alice 起稿
        self.svc.save_draft(
            "shared", "v1", "初版：利率主线",
            [claim("c-rate", "利率是主因", support=(self.ctx.decision,)),
             claim("c-etf", "ETF 作用待定", uncertain=(self.ctx.etf,))],
            self.alice, base_revision=0,
        )
        # rev2：carol 在 alice 起稿后推进（alice 此刻仍持有 base_revision=1）
        self.svc.save_draft(
            "shared", "v2", "补充资金流",
            [claim("c-rate", "利率是主因", support=(self.ctx.decision,)),
             claim("c-etf", "ETF 流入构成支撑", support=(self.ctx.etf,))],
            self.carol, base_revision=1,
        )

    def test_stale_base_raises_conflict(self) -> None:
        with self.assertRaises(ConflictError) as cm:
            self.svc.save_draft(
                "shared", "v3-stale", "alice 的迟到改动",
                [claim("c-geo", "地缘风险也重要", support=(self.ctx.geo,))],
                self.alice, base_revision=1,
            )
        self.assertEqual(cm.exception.current_head, 2)
        self.assertEqual(cm.exception.base_revision, 1)

    def test_choice_theirs_rejects_our_change(self) -> None:
        kept = self.svc.resolve_conflict(
            "shared", "ignored", "ignored", [], self.alice,
            base_revision=1, choice="theirs",
        )
        self.assertEqual(kept, 2)  # head 不变，无新修订
        view = self.svc.get_revision("shared", 2, self.alice)
        self.assertEqual(view["summary"], "补充资金流")

    def test_choice_ours_overwrites_with_audit(self) -> None:
        rev = self.svc.resolve_conflict(
            "shared", "v3-ours", "回到利率单因",
            [claim("c-rate", "利率是唯一主因")],
            self.alice, base_revision=1, choice="ours",
        )
        self.assertEqual(rev, 3)
        view = self.svc.get_revision("shared", 3, self.alice)
        self.assertEqual([c["local_id"] for c in view["claims"]], ["c-rate"])
        audit = [a["action"] for a in self.svc.list_audit()]
        self.assertIn("draft_override", audit)

    def test_choice_merge_requires_explicit_resolution_per_claim(self) -> None:
        # 未覆盖全部 local_id -> 拒绝
        with self.assertRaises(ValidationError):
            self.svc.resolve_conflict(
                "shared", "v3-merge", "合并版",
                [claim("c-rate", "利率是主因，资金流也有贡献")],
                self.alice, base_revision=1, choice="merge",
                claim_resolution={"c-rate": "theirs"},  # 漏了 c-etf
            )

    def test_choice_merge_combines_claims_explicitly(self) -> None:
        rev = self.svc.resolve_conflict(
            "shared", "v3-merge", "合并版",
            [claim("c-rate", "利率是主因；ETF 流入提供边际支撑（合并稿）")],
            self.alice, base_revision=1, choice="merge",
            claim_resolution={"c-rate": "merged", "c-etf": "theirs"},
        )
        self.assertEqual(rev, 3)
        view = self.svc.get_revision("shared", 3, self.alice)
        ids = [c["local_id"] for c in view["claims"]]
        self.assertEqual(ids, ["c-etf", "c-rate"])
        rate = next(c for c in view["claims"] if c["local_id"] == "c-rate")
        self.assertIn("合并稿", rate["text"])
        etf = next(c for c in view["claims"] if c["local_id"] == "c-etf")
        self.assertIn("ETF 流入构成支撑", etf["text"])

    def test_merge_drop_is_explicit(self) -> None:
        rev = self.svc.resolve_conflict(
            "shared", "v3-drop", "精简版", [],
            self.alice, base_revision=1, choice="merge",
            claim_resolution={"c-rate": "theirs", "c-etf": "dropped"},
        )
        view = self.svc.get_revision("shared", rev, self.alice)
        self.assertEqual([c["local_id"] for c in view["claims"]], ["c-rate"])

    def test_cannot_resolve_when_not_conflicted(self) -> None:
        with self.assertRaises(LifecycleError):
            self.svc.resolve_conflict(
                "shared", "x", "x", [], self.carol,
                base_revision=2, choice="theirs",
            )


if __name__ == "__main__":
    unittest.main()
