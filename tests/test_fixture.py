"""检查研究样例的基础结构。"""

import json
from pathlib import Path
import unittest


class PolicyEventFixtureTest(unittest.TestCase):
    def test_expectation_and_actual_are_kept(self) -> None:
        payload = json.loads(Path("fixtures/policy_event.json").read_text(encoding="utf-8"))
        self.assertIn("expected_basis_points", payload)
        self.assertIn("actual_basis_points", payload)
        self.assertTrue(payload["observations"])


if __name__ == "__main__":
    unittest.main()
