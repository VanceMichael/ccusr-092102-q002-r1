"""把场景 JSON 装载进归因账本（fixtures/fed_2026_09_16_scenario.json）。

场景文件中用 ``ref`` 给记录起稳定名字，证据与 ``replaces_ref`` 通过名字引用，
装载时统一解析为数据库编号。默认流程：登记预期 → 冻结 → 录入决定与观测 →
创建两份彼此竞争的分析并发布（发布版本固化数据水位）。

用法::

    python3 -m gold_attribution.seed fixtures/fed_2026_09_16_scenario.json --db /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ledger import Ledger
from .storage import Storage


def load_scenario(db: Storage, scenario: dict, *, publish: bool = True,
                  actor: str = "seed", team: str = "macro") -> dict[str, int]:
    ledger = Ledger(db)
    ids: dict[str, int] = {}

    event = dict(scenario["event"])
    ledger.create_event(event, actor=actor, team=team)
    event_id = event["event_id"]

    for exp in scenario.get("expectations", []):
        ref = exp.pop("ref")
        ids[ref] = ledger.add_expectation(event_id, exp, actor=actor, team=team)["id"]

    frozen_at = scenario.get("freeze_at")
    ledger.freeze_expectations(event_id,
                               {"frozen_at": frozen_at} if frozen_at else None,
                               actor=actor, team=team)

    for dec in scenario.get("decisions", []):
        ref = dec.pop("ref")
        if "replaces_ref" in dec:
            dec["replaces_id"] = ids[dec.pop("replaces_ref")]
        ids[ref] = ledger.add_decision(event_id, dec, actor=actor, team=team)["id"]

    for obs in scenario.get("observations", []):
        ref = obs.pop("ref")
        if "replaces_ref" in obs:
            obs["replaces_id"] = ids[obs.pop("replaces_ref")]
        ids[ref] = ledger.add_observation(event_id, obs, actor=actor, team=team)["id"]

    prefix_type = {"exp": "expectation", "dec": "decision", "obs": "observation"}
    analysis_ids: list[int] = []
    for analysis in scenario.get("analyses", []):
        owner = analysis.get("team", team)
        payload = {"title": analysis["title"], "body": analysis["body"], "claims": []}
        for claim in analysis["claims"]:
            links = []
            for ev in claim.get("evidence", []):
                ev = dict(ev)
                ref = ev.pop("ref")
                kind, name = ref.split(":", 1)
                ev["target_type"] = prefix_type[kind]
                ev["target_id"] = ids[f"{kind}:{name}"]
                links.append(ev)
            payload["claims"].append({"text": claim["text"], "evidence": links})
        created = ledger.create_analysis(event_id, payload, actor=actor, team=owner)
        analysis_ids.append(created["id"])
        if publish:
            ledger.publish_analysis(created["id"], actor=actor, team=owner)

    return {"event_id": event_id, "ids": ids, "analysis_ids": analysis_ids}


def main() -> None:
    parser = argparse.ArgumentParser(description="装载归因账本场景")
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--db", required=True)
    parser.add_argument("--no-publish", action="store_true")
    args = parser.parse_args()
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    db = Storage(args.db)
    result = load_scenario(db, scenario, publish=not args.no_publish)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
