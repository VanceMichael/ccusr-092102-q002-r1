"""审计哈希链、WORM 防篡改与持久化。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.gold_attribution.models import AnalysisStatus
from src.gold_attribution.service import Actor
from src.gold_attribution.storage import Ledger
from src.gold_attribution.service import AttributionService

from tests._scenario import EVENT_ID, build_decided_event, claim, make_service


class AuditPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.clock = make_service()
        self.alice = Actor("macro", "alice")
        self.ctx = build_decided_event(self.svc, self.alice)
        self.svc.create_analysis_series("s", EVENT_ID, "macro", "结论", self.alice)
        self.svc.save_draft(
            "s", "结论 v1", "摘要",
            [claim("c1", "多因素共振", support=(self.ctx.spot, self.ctx.etf))],
            self.alice, base_revision=0,
        )
        self.svc.publish("s", self.alice)

    def test_audit_chain_verifies_and_records_lifecycle(self) -> None:
        self.assertTrue(self.svc.verify_audit_chain())
        actions = {row["action"] for row in self.svc.list_audit()}
        self.assertIn("expectations_freeze", actions)
        self.assertIn("decision_record", actions)
        self.assertIn("evidence_attach", actions)
        self.assertIn("analysis_publish", actions)

    def test_tampering_an_audit_row_breaks_chain(self) -> None:
        conn: sqlite3.Connection = self.svc.db.conn
        conn.execute("UPDATE audit_log SET entity_id='forged' WHERE seq=1")
        conn.commit()
        self.assertFalse(self.svc.verify_audit_chain())

    def test_tampering_a_published_revision_is_detected_on_read(self) -> None:
        conn = self.svc.db.conn
        conn.execute(
            "UPDATE analysis_revisions SET summary='被改写的结论' WHERE revision=1 AND series_id='s'"
        )
        conn.commit()
        with self.assertRaises(Exception):  # LifecycleError：哈希校验失败
            self.svc.get_revision("s", 1, self.alice)

    def test_persistence_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            svc1 = AttributionService(Ledger(path))
            build_decided_event(svc1, self.alice)
            svc1.create_analysis_series("s", EVENT_ID, "macro", "结论", self.alice)
            svc1.save_draft(
                "s", "结论 v1", "摘要",
                [claim("c1", "多因素共振")], self.alice, base_revision=0,
            )
            svc1.publish("s", self.alice)
            self.assertTrue(svc1.verify_audit_chain())
            svc1.db.close()

            db2 = Ledger(path)
            svc2 = AttributionService(db2)
            self.assertEqual(svc2.get_event(EVENT_ID).event_id, EVENT_ID)
            view = svc2.get_revision("s", 1, self.alice)
            self.assertEqual(view["status"], AnalysisStatus.PUBLISHED.value)
            self.assertTrue(svc2.verify_audit_chain())
            db2.close()

    def test_no_order_or_execution_api_exists(self) -> None:
        # 服务的公共 API 面不含任何下单/执行方法
        forbidden = [
            name for name in dir(AttributionService)
            if any(token in name.lower()
                   for token in ("order", "trade", "execute", "fill", "position_adj"))
        ]
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
