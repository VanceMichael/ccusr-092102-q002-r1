"""归因账本领域服务。

职责（对应投委会复盘需求）：

1. **预期冻结**：政策公布前按事件登记各来源预期概率；到达冻结截止后，
   只能整体冻结，不能再补录或改动。
2. **材料归集**：实际决定、美元/实际利率窗口、黄金现货与期货、ETF 持仓、
   官方购金、风险材料挂到同一事件；保留原币种、原时区、来源版本与修订链。
3. **竞争解释**：分析师创建分析，逐条写判断并关联支持 / 相反 / 不确定证据。
4. **数据水位**：发布时固化全部预期、决定与各序列最新版本的快照；
   任何结论页面都可还原当时水位，并标出新材料改变了哪段判断。
5. **WORM 与并发**：发布版本不可变，更正只能生成链上的新版本；
   草稿更新必须携带 ``expected_rev``，冲突一律 409，由人显式合并后重提，
   系统不做静默合并。
6. **团队可见性**：未发布草稿只有所属团队可读。
7. **护栏**：分析文本不得包含交易指令性表述。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .errors import (
    ConflictError,
    NotFound,
    PermissionDenied,
    TradingInstructionError,
    ValidationError,
)
from .storage import Storage, dumps, utcnow_iso

KNOWN_SERIES = {
    "spot_gold",            # 现货黄金
    "gold_futures",         # 纽约期金
    "dollar_index",         # 美元指数窗口
    "real_yield",           # 实际利率窗口（TIPS）
    "etf_holdings",         # 黄金 ETF 持仓
    "central_bank_purchase",  # 官方（央行）购金
    "risk_material",        # 地缘 / 财政信用等风险材料
}

EVIDENCE_KINDS = ("supporting", "contradicting", "uncertainty")
EVIDENCE_KIND_CN = {
    "supporting": "支持证据",
    "contradicting": "相反证据",
    "uncertainty": "不确定性",
}

# 交易指令护栏：研究结论只做归因，不得输出可执行的交易指令。
# 描述性词汇（如“多头/空头回补”“央行购金”作为行情叙述）不在拦截范围内。
_TRADE_PATTERNS = [
    r"买入", r"卖出", r"加仓", r"减仓", r"建仓", r"平仓", r"清仓",
    r"做空", r"做多", r"抄底", r"建议买", r"建议卖", r"立即买", r"立即卖",
    r"\bbuy\b", r"\bsell\b", r"\bgo\s+long\b", r"\bgo\s+short\b",
]
_TRADE_RE = re.compile("|".join(_TRADE_PATTERNS), re.IGNORECASE)


def _aware_ts(value: Any, field: str) -> str:
    """校验时间戳：必须是带显式 UTC 偏移量的 ISO 8601 字符串（保留原时区原文）。"""

    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} 必须是带时区偏移的 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法的 ISO 8601 时间：{value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} 缺少时区偏移，跨市场记录不得使用朴素时间：{value}")
    return value


def _require(obj: dict[str, Any], key: str, field_cn: str) -> Any:
    val = obj.get(key)
    if val is None or (isinstance(val, str) and not val.strip()):
        raise ValidationError(f"缺少必填字段：{field_cn}（{key}）")
    return val


class Ledger:
    """领域服务；每个方法在一次调用内提交事务并写审计日志。"""

    def __init__(self, storage: Storage) -> None:
        self.db = storage

    # -- 审计 ---------------------------------------------------------------

    def _audit(
        self,
        actor: str,
        team: str | None,
        action: str,
        entity: str,
        entity_id: str | int | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO audit_log(ts, actor, team, action, entity, entity_id, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (utcnow_iso(), actor, team, action, entity,
             None if entity_id is None else str(entity_id), dumps(detail or {})),
        )

    # -- 事件 ---------------------------------------------------------------

    def create_event(self, payload: dict[str, Any], *, actor: str, team: str) -> dict[str, Any]:
        event_id = _require(payload, "event_id", "事件编号")
        title = _require(payload, "title", "事件标题")
        policy_body = _require(payload, "policy_body", "政策主体")
        scheduled_at = _aware_ts(_require(payload, "scheduled_at", "计划公布时刻"), "scheduled_at")
        freeze_at = _aware_ts(_require(payload, "freeze_at", "预期冻结截止"), "freeze_at")
        if datetime.fromisoformat(freeze_at) >= datetime.fromisoformat(scheduled_at):
            raise ValidationError("冻结截止必须早于政策计划公布时刻")
        if self.db.one("SELECT 1 FROM events WHERE event_id=?", (event_id,)):
            raise ConflictError(f"事件已存在：{event_id}")
        now = utcnow_iso()
        self.db.execute(
            "INSERT INTO events(event_id,title,policy_body,scheduled_at,freeze_at,"
            "expectations_frozen,created_at,created_by) VALUES (?,?,?,?,?,0,?,?)",
            (event_id, title, policy_body, scheduled_at, freeze_at, now, actor),
        )
        self._audit(actor, team, "create_event", "event", event_id)
        self.db.commit()
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM events WHERE event_id=?", (event_id,))
        if row is None:
            raise NotFound(f"事件不存在：{event_id}")
        return dict(row)

    # -- 预期登记 / 冻结 -----------------------------------------------------

    def add_expectation(self, event_id: str, payload: dict[str, Any], *,
                        actor: str, team: str) -> dict[str, Any]:
        event = self.get_event(event_id)
        if event["expectations_frozen"]:
            raise ConflictError(
                "预期已于公布前冻结，不能再补录或修改；后续观点请写入分析",
                details={"frozen_at": self._frozen_at(event_id)},
            )
        source = _require(payload, "source", "预期来源")
        outcome = _require(payload, "outcome", "政策结果标签")
        probability = _require(payload, "probability", "概率")
        if not isinstance(probability, (int, float)) or not 0.0 <= float(probability) <= 1.0:
            raise ValidationError("probability 必须是 0 到 1 之间的数字")
        collected_at = _aware_ts(_require(payload, "collected_at", "采集时刻"), "collected_at")
        if datetime.fromisoformat(collected_at) >= datetime.fromisoformat(event["freeze_at"]):
            raise ConflictError(
                "采集时刻晚于冻结截止，该来源预期不能再入账",
                details={"freeze_at": event["freeze_at"], "collected_at": collected_at},
            )
        source_version = payload.get("source_version")
        dup = self.db.one(
            "SELECT id FROM expectations WHERE event_id=? AND source=? AND outcome=? AND collected_at=?",
            (event_id, source, outcome, collected_at),
        )
        if dup is not None:
            raise ConflictError("同一来源、同一结果、同一采集时刻的预期已登记，不能重复入账")
        cur = self.db.execute(
            "INSERT INTO expectations(event_id,source,source_version,outcome,probability,"
            "collected_at) VALUES (?,?,?,?,?,?)",
            (event_id, source, source_version, outcome, float(probability), collected_at),
        )
        self._audit(actor, team, "add_expectation", "expectation", cur.lastrowid,
                    {"source": source, "outcome": outcome})
        self.db.commit()
        return self._expectation(cur.lastrowid)

    def _frozen_at(self, event_id: str) -> str | None:
        row = self.db.one(
            "SELECT MIN(frozen_at) AS f FROM expectations WHERE event_id=? AND frozen_at IS NOT NULL",
            (event_id,),
        )
        return None if row is None else row["f"]

    def freeze_expectations(self, event_id: str, payload: dict[str, Any] | None, *,
                            actor: str, team: str) -> dict[str, Any]:
        """政策公布前执行：为全部已登记预期打上同一冻结时刻。冻结不可逆。"""

        event = self.get_event(event_id)
        if event["expectations_frozen"]:
            raise ConflictError("预期已经冻结，冻结操作不可重复",
                                details={"frozen_at": self._frozen_at(event_id)})
        frozen_at = _aware_ts(
            (payload or {}).get("frozen_at") or utcnow_iso(), "frozen_at"
        )
        if datetime.fromisoformat(frozen_at) > datetime.fromisoformat(event["scheduled_at"]):
            raise ConflictError("冻结时刻不得晚于政策计划公布时刻")
        n = self.db.one(
            "SELECT COUNT(*) AS c FROM expectations WHERE event_id=?", (event_id,))["c"]
        if n == 0:
            raise ValidationError("尚未登记任何来源预期，无可冻结对象")
        n = self.db.execute(
            "UPDATE expectations SET frozen_at=? WHERE event_id=? AND frozen_at IS NULL",
            (frozen_at, event_id),
        ).rowcount
        self.db.execute("UPDATE events SET expectations_frozen=1 WHERE event_id=?", (event_id,))
        self._audit(actor, team, "freeze_expectations", "event", event_id,
                    {"event_id": event_id, "frozen_at": frozen_at, "rows": n})
        self.db.commit()
        return {"event_id": event_id, "frozen_at": frozen_at, "frozen_rows": n}

    def list_expectations(self, event_id: str) -> list[dict[str, Any]]:
        self.get_event(event_id)
        return [dict(r) for r in self.db.rows(
            "SELECT * FROM expectations WHERE event_id=? ORDER BY id", (event_id,))]

    def _expectation(self, exp_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM expectations WHERE id=?", (exp_id,))
        if row is None:
            raise NotFound(f"预期记录不存在：{exp_id}")
        return dict(row)

    # -- 实际决定（修订追加） -------------------------------------------------

    def add_decision(self, event_id: str, payload: dict[str, Any], *,
                     actor: str, team: str) -> dict[str, Any]:
        self.get_event(event_id)
        action = _require(payload, "action", "决定类型（hike/hold/cut）")
        if action not in ("hike", "hold", "cut"):
            raise ValidationError("action 只能是 hike / hold / cut")
        basis_points = _require(payload, "basis_points", "基点变动")
        if not isinstance(basis_points, int):
            raise ValidationError("basis_points 必须是整数")
        decided_at = _aware_ts(_require(payload, "decided_at", "公布时刻"), "decided_at")
        source = _require(payload, "source", "来源")
        source_version = _require(payload, "source_version", "来源版本")
        replaces_id = payload.get("replaces_id")
        if replaces_id is not None:
            old = self.db.one(
                "SELECT * FROM decisions WHERE id=? AND event_id=?",
                (replaces_id, event_id),
            )
            if old is None:
                raise ValidationError(f"replaces_id 指向的决定版本不存在：{replaces_id}")
            if self.db.one("SELECT 1 FROM decisions WHERE replaces_id=?", (replaces_id,)):
                raise ConflictError(f"决定版本 {replaces_id} 已有替代记录，不能分叉修订链")
        cur = self.db.execute(
            "INSERT INTO decisions(event_id,basis_points,action,statement_summary,"
            "decided_at,source,source_version,recorded_at,replaces_id,recorded_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, basis_points, action, payload.get("statement_summary"),
             decided_at, source, source_version, utcnow_iso(), replaces_id, actor),
        )
        self._audit(actor, team, "add_decision", "decision", cur.lastrowid,
                    {"action": action, "basis_points": basis_points,
                     **({"replaces_id": replaces_id} if replaces_id else {})})
        self.db.commit()
        return self._decision(cur.lastrowid)

    def _decision(self, decision_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM decisions WHERE id=?", (decision_id,))
        if row is None:
            raise NotFound(f"决定记录不存在：{decision_id}")
        return dict(row)

    def list_decisions(self, event_id: str) -> list[dict[str, Any]]:
        self.get_event(event_id)
        return [dict(r) for r in self.db.rows(
            "SELECT * FROM decisions WHERE event_id=? ORDER BY id", (event_id,))]

    # -- 跨市场观测（原币种/原时区/修订链） ------------------------------------

    def add_observation(self, event_id: str, payload: dict[str, Any], *,
                        actor: str, team: str) -> dict[str, Any]:
        self.get_event(event_id)
        series = _require(payload, "series", "序列")
        if series not in KNOWN_SERIES:
            raise ValidationError(f"未知序列：{series}",
                                  details={"allowed": sorted(KNOWN_SERIES)})
        label = _require(payload, "label", "标签")
        value = payload.get("value")
        if value is not None and not isinstance(value, (int, float)):
            raise ValidationError("value 必须是数字或 null（文本材料可放入 label/payload）")
        observed_at = _aware_ts(_require(payload, "observed_at", "行情时刻"), "observed_at")
        collected_at = _aware_ts(_require(payload, "collected_at", "采集时刻"), "collected_at")
        source = _require(payload, "source", "来源")
        source_version = _require(payload, "source_version", "来源版本")
        currency = payload.get("currency")
        unit = payload.get("unit")
        market_tz = payload.get("market_tz")
        replaces_id = payload.get("replaces_id")
        if replaces_id is not None:
            old = self.db.one(
                "SELECT * FROM observations WHERE id=? AND event_id=?",
                (replaces_id, event_id),
            )
            if old is None:
                raise ValidationError(f"replaces_id 指向的观测不存在：{replaces_id}")
            if old["series"] != series:
                raise ValidationError("修订记录与被替代记录必须属于同一序列")
            if old["label"] != label:
                raise ValidationError("修订记录与被替代记录必须使用相同标签，修订链才能在水位中折叠")
            if self.db.one("SELECT 1 FROM observations WHERE replaces_id=?", (replaces_id,)):
                raise ConflictError(f"观测 {replaces_id} 已有替代记录，不能分叉修订链")
        extra = payload.get("payload") or {}
        if not isinstance(extra, dict):
            raise ValidationError("payload 必须是对象")
        cur = self.db.execute(
            "INSERT INTO observations(event_id,series,label,value,currency,unit,market_tz,"
            "observed_at,source,source_version,collected_at,replaces_id,payload,recorded_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, series, label,
             None if value is None else float(value), currency, unit, market_tz,
             observed_at, source, source_version, collected_at, replaces_id,
             dumps(extra), actor),
        )
        self._audit(actor, team, "add_observation", "observation", cur.lastrowid,
                    {"series": series, **({"replaces_id": replaces_id} if replaces_id else {})})
        self.db.commit()
        return self._observation(cur.lastrowid)

    def _observation(self, obs_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM observations WHERE id=?", (obs_id,))
        if row is None:
            raise NotFound(f"观测记录不存在：{obs_id}")
        out = dict(row)
        out["payload"] = _loads(out["payload"])
        return out

    def list_observations(self, event_id: str, *, series: str | None = None) -> list[dict[str, Any]]:
        self.get_event(event_id)
        if series is not None:
            if series not in KNOWN_SERIES:
                raise ValidationError(f"未知序列：{series}",
                                      details={"allowed": sorted(KNOWN_SERIES)})
            rows = self.db.rows(
                "SELECT * FROM observations WHERE event_id=? AND series=? ORDER BY id",
                (event_id, series),
            )
        else:
            rows = self.db.rows(
                "SELECT * FROM observations WHERE event_id=? ORDER BY id", (event_id,))
        return [self._observation(r["id"]) for r in rows]

    def _latest_observations(self, event_id: str) -> list[dict[str, Any]]:
        """每个 (series,label) 沿修订链取最新版本——即发布时刻的数据水位。"""

        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for obs in self.list_observations(event_id):
            latest[(obs["series"], obs["label"])] = obs
        return [latest[k] for k in sorted(latest)]

    # -- 数据水位 -------------------------------------------------------------

    def current_watermark(self, event_id: str) -> dict[str, Any]:
        event = self.get_event(event_id)
        return {
            "event": event,
            "computed_at": utcnow_iso(),
            "expectations": self.list_expectations(event_id),
            "decisions": self.list_decisions(event_id),
            "observations_latest": self._latest_observations(event_id),
        }

    def _published_watermark(self, analysis_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT watermark FROM analyses WHERE id=?", (analysis_id,))
        return _loads(row["watermark"]) if row and row["watermark"] else {}

    # -- 分析：竞争解释、证据关联 ---------------------------------------------

    def _guard_trading(self, *texts: str) -> None:
        hits = sorted({
            m.group(0) for t in texts if isinstance(t, str)
            for m in (_TRADE_RE.search(t),) if m
        })
        if hits:
            raise TradingInstructionError(
                "分析内容含交易指令性表述；账本只承载归因研究，不得输出交易指令",
                details={"命中": hits},
            )

    def _normalize_claims(self, event_id: str, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("claims 必须是非空数组（逐条判断并关联证据）")
        claims: list[dict[str, Any]] = []
        for pos, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise ValidationError(f"第 {pos} 条判断不是对象")
            text = _require(item, "text", f"第 {pos} 条判断内容")
            if len(text.strip()) < 5:
                raise ValidationError(f"第 {pos} 条判断过短，无法构成归因结论")
            self._guard_trading(text)
            links: list[dict[str, Any]] = []
            for j, ev in enumerate(item.get("evidence", []), start=1):
                if not isinstance(ev, dict):
                    raise ValidationError(f"第 {pos} 条判断的第 {j} 条证据不是对象")
                kind = _require(ev, "kind", "证据类型")
                if kind not in EVIDENCE_KINDS:
                    raise ValidationError(
                        f"证据类型只能是 {EVIDENCE_KINDS}（支持/相反/不确定）")
                target_type = _require(ev, "target_type", "证据目标类型")
                target_id = _require(ev, "target_id", "证据目标编号")
                if not isinstance(target_id, int):
                    raise ValidationError("target_id 必须是整数编号")
                if target_type not in ("observation", "decision", "expectation"):
                    raise ValidationError("target_type 只能是 observation/decision/expectation")
                table = {
                    "observation": "observations",
                    "decision": "decisions",
                    "expectation": "expectations",
                }[target_type]
                hit = self.db.one(
                    f"SELECT id FROM {table} WHERE id=? AND event_id=?",
                    (target_id, event_id),
                )
                if hit is None:
                    raise NotFound(
                        f"第 {pos} 条判断引用的{target_type}#{target_id} 不属于本事件或不存在")
                note = ev.get("note")
                if note is not None:
                    self._guard_trading(note)
                links.append({"kind": kind, "target_type": target_type,
                              "target_id": target_id, "note": note})
            claims.append({"text": text.strip(), "evidence": links})
        return claims

    def _write_claims(self, analysis_id: int, claims: list[dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM evidence_links WHERE claim_id IN"
                        " (SELECT id FROM claims WHERE analysis_id=?)", (analysis_id,))
        self.db.execute("DELETE FROM claims WHERE analysis_id=?", (analysis_id,))
        for pos, claim in enumerate(claims, start=1):
            cur = self.db.execute(
                "INSERT INTO claims(analysis_id,position,text) VALUES (?,?,?)",
                (analysis_id, pos, claim["text"]),
            )
            for ev in claim["evidence"]:
                self.db.execute(
                    "INSERT INTO evidence_links(claim_id,kind,target_type,target_id,note)"
                    " VALUES (?,?,?,?,?)",
                    (cur.lastrowid, ev["kind"], ev["target_type"], ev["target_id"], ev["note"]),
                )

    def create_analysis(self, event_id: str, payload: dict[str, Any], *,
                        actor: str, team: str) -> dict[str, Any]:
        self.get_event(event_id)
        title = _require(payload, "title", "分析标题")
        body = _require(payload, "body", "分析正文")
        self._guard_trading(title, body)
        claims = self._normalize_claims(event_id, payload.get("claims"))
        now = utcnow_iso()
        cur = self.db.execute(
            "INSERT INTO analyses(event_id,revision_of_id,root_id,seq,title,body,"
            "owner_team,status,base_version,rev,created_at,created_by)"
            " VALUES (?,?,?,?,?,?,?,'draft',NULL,1,?,?)",
            (event_id, None, -1, 1, title.strip(), body, team, now, actor),
        )
        analysis_id = cur.lastrowid
        self.db.execute("UPDATE analyses SET root_id=id WHERE id=?", (analysis_id,))
        self._write_claims(analysis_id, claims)
        self._audit(actor, team, "create_analysis", "analysis", analysis_id,
                    {"event_id": event_id})
        self.db.commit()
        return self.get_analysis(analysis_id, actor=actor, team=team)

    def _visible_row(self, row: Any, team: str) -> None:
        if row["status"] == "draft" and row["owner_team"] != team:
            raise NotFound("分析不存在或尚未发布（草稿仅所属团队可见）")

    def get_analysis(self, analysis_id: int, *, actor: str | None = None,
                     team: str | None = None) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM analyses WHERE id=?", (analysis_id,))
        if row is None:
            raise NotFound(f"分析不存在：{analysis_id}")
        if team is not None:
            self._visible_row(row, team)
        out = dict(row)
        out.pop("watermark", None)
        out["claims"] = self._claims_of(analysis_id)
        if row["status"] == "published":
            out["watermark"] = self._published_watermark(analysis_id)
        return out

    def _claims_of(self, analysis_id: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for crow in self.db.rows(
                "SELECT * FROM claims WHERE analysis_id=? ORDER BY position", (analysis_id,)):
            links = [dict(r) for r in self.db.rows(
                "SELECT id,kind,target_type,target_id,note FROM evidence_links"
                " WHERE claim_id=? ORDER BY id", (crow["id"],))]
            for link in links:
                link["kind_cn"] = EVIDENCE_KIND_CN[link["kind"]]
            result.append({"position": crow["position"], "text": crow["text"], "evidence": links})
        return result

    def list_analyses(self, event_id: str, *, team: str) -> dict[str, Any]:
        self.get_event(event_id)
        rows = self.db.rows(
            "SELECT * FROM analyses WHERE event_id=? ORDER BY root_id, seq", (event_id,))
        chains: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            if r["status"] == "draft" and r["owner_team"] != team:
                continue  # 他人团队草稿在列表层面也不可见
            chains.setdefault(r["root_id"], []).append(
                {"id": r["id"], "seq": r["seq"], "title": r["title"],
                 "status": r["status"], "owner_team": r["owner_team"],
                 "revision_of_id": r["revision_of_id"],
                 "published_at": r["published_at"], "rev": r["rev"]})
        return {"event_id": event_id, "chains": [chains[k] for k in sorted(chains)]}

    def update_draft(self, analysis_id: int, payload: dict[str, Any], *,
                     actor: str, team: str) -> dict[str, Any]:
        """更新草稿。必须携带 expected_rev；不一致一律 409，拒绝静默覆盖。"""

        row = self.db.one("SELECT * FROM analyses WHERE id=?", (analysis_id,))
        if row is None:
            raise NotFound(f"分析不存在：{analysis_id}")
        if row["owner_team"] != team:
            if row["status"] == "draft":
                # 写入路径给出明确 403；读取路径仍返回 404 以避免草稿存在性泄漏
                raise PermissionDenied("草稿仅所属团队可修改")
            raise PermissionDenied("已发布版本不可修改；请发起修订形成新版本")
        if row["status"] == "published":
            raise ConflictError("该版本已发给投委会，不可改写；请基于它发起修订（新版本）")
        expected_rev = payload.get("expected_rev")
        if not isinstance(expected_rev, int):
            raise ValidationError("必须携带 expected_rev（草稿当前修订号），不允许无条件覆盖")
        title = _require(payload, "title", "分析标题")
        body = _require(payload, "body", "分析正文")
        self._guard_trading(title, body)
        claims = self._normalize_claims(row["event_id"], payload.get("claims"))
        # 原子乐观锁：只有 rev 未变才写入；并发下恰好一方成功，另一方得到 409。
        cur = self.db.execute(
            "UPDATE analyses SET title=?, body=?, rev=rev+1"
            " WHERE id=? AND status='draft' AND owner_team=? AND rev=?",
            (title.strip(), body, analysis_id, team, expected_rev),
        )
        if cur.rowcount == 0:
            latest = self.db.one("SELECT rev FROM analyses WHERE id=?", (analysis_id,))
            raise ConflictError(
                "草稿已被他人修改；请重新拉取、人工合并后以最新修订号提交，或明确放弃远端修改",
                details={
                    "current_rev": latest["rev"] if latest else None,
                    "submitted_rev": expected_rev,
                    "current_title": row["title"],
                    "current_body": self.db.one(
                        "SELECT body FROM analyses WHERE id=?", (analysis_id,))["body"],
                    "current_claims": self._claims_of(analysis_id),
                },
            )
        self._write_claims(analysis_id, claims)
        self._audit(actor, team, "update_draft", "analysis", analysis_id,
                    {"new_rev": expected_rev + 1, "resolution": "explicit_merge"})
        self.db.commit()
        return self.get_analysis(analysis_id, actor=actor, team=team)

    def publish_analysis(self, analysis_id: int, *, actor: str, team: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM analyses WHERE id=?", (analysis_id,))
        if row is None:
            raise NotFound(f"分析不存在：{analysis_id}")
        if row["owner_team"] != team:
            raise PermissionDenied("只有所属团队可以发布该草稿")
        self._visible_row(row, team)
        if row["status"] == "published":
            raise ConflictError("该版本已发布，发布不可重复")
        if not self.db.one("SELECT 1 FROM expectations WHERE event_id=? AND frozen_at IS NOT NULL",
                           (row["event_id"],)):
            raise ConflictError("发布前必须已冻结政策预期，否则结论无法对照“公布前共识”")
        watermark = self.current_watermark(row["event_id"])
        now = utcnow_iso()
        self.db.execute(
            "UPDATE analyses SET status='published', published_at=?, watermark=? WHERE id=?",
            (now, dumps(watermark), analysis_id),
        )
        self._audit(actor, team, "publish_analysis", "analysis", analysis_id,
                    {"event_id": row["event_id"], "seq": row["seq"]})
        self.db.commit()
        return self.get_analysis(analysis_id, actor=actor, team=team)

    def revise_analysis(self, analysis_id: int, *, actor: str, team: str) -> dict[str, Any]:
        """基于已发布版本（或同团队草稿）发起修订：创建链上下一个草稿，不触碰旧版本。"""

        row = self.db.one("SELECT * FROM analyses WHERE id=?", (analysis_id,))
        if row is None:
            raise NotFound(f"分析不存在：{analysis_id}")
        self._visible_row(row, team)
        if row["status"] != "published":
            raise ConflictError("只能基于已发布版本发起修订（更正只能形成新分析）")
        if self.db.one(
                "SELECT 1 FROM analyses WHERE root_id=? AND status='draft'",
                (row["root_id"],)):
            raise ConflictError("该版本链已存在未发布的修订草稿，请先完成或放弃它")
        now = utcnow_iso()
        cur = self.db.execute(
            "INSERT INTO analyses(event_id,revision_of_id,root_id,seq,title,body,"
            "owner_team,status,base_version,rev,created_at,created_by)"
            " VALUES (?,?,?,?,?,?,?,'draft',?,1,?,?)",
            (row["event_id"], row["id"], row["root_id"], row["seq"] + 1,
             row["title"], row["body"], team, row["id"], now, actor),
        )
        new_id = cur.lastrowid
        # 复制判断与证据关联作为修订起点（证据仍指向同一批材料编号）。
        self._write_claims(new_id, self._claims_payload(row["id"]))
        self._audit(actor, team, "revise_analysis", "analysis", new_id,
                    {"base_version": row["id"], "root_id": row["root_id"]})
        self.db.commit()
        return self.get_analysis(new_id, actor=actor, team=team)

    def _claims_payload(self, analysis_id: int) -> list[dict[str, Any]]:
        return [
            {"text": c["text"],
             "evidence": [{"kind": e["kind"], "target_type": e["target_type"],
                           "target_id": e["target_id"], "note": e["note"]}
                          for e in c["evidence"]]}
            for c in self._claims_of(analysis_id)
        ]

    # -- 版本对比：新材料改变了哪段判断 ---------------------------------------

    def diff_analyses(self, from_id: int, to_id: int, *, team: str) -> dict[str, Any]:
        a = self.db.one("SELECT * FROM analyses WHERE id=?", (from_id,))
        b = self.db.one("SELECT * FROM analyses WHERE id=?", (to_id,))
        if a is None or b is None:
            raise NotFound("参与对比的分析版本不存在")
        self._visible_row(a, team)
        self._visible_row(b, team)
        if a["root_id"] != b["root_id"]:
            raise ValidationError("只能对比同一条版本链上的分析")
        ca, cb = self._claims_of(from_id), self._claims_of(to_id)
        changed: list[dict[str, Any]] = []
        for i in range(max(len(ca), len(cb))):
            left, right = (ca[i] if i < len(ca) else None,
                           cb[i] if i < len(cb) else None)
            if left is None:
                changed.append({"position": i + 1, "change": "added", "to": right})
            elif right is None:
                changed.append({"position": i + 1, "change": "removed", "from": left})
            elif left["text"] != right["text"] or _links(left) != _links(right):
                changed.append({"position": i + 1, "change": "modified",
                                "from": left, "to": right})
        wm_a = _loads(a["watermark"]) if a["watermark"] else None
        wm_b = _loads(b["watermark"]) if b["watermark"] else None
        watermark_change = _watermark_diff(wm_a, wm_b)
        return {
            "from_version": from_id, "to_version": to_id,
            "title_change": None if a["title"] == b["title"]
            else {"from": a["title"], "to": b["title"]},
            "body_changed": a["body"] != b["body"],
            "claim_changes": changed,
            "watermark_change": watermark_change,
        }


def _links(claim: dict[str, Any]) -> set[tuple[str, str, int, str | None]]:
    return {(e["kind"], e["target_type"], e["target_id"], e["note"])
            for e in claim["evidence"]}


def _watermark_diff(wm_a: dict[str, Any] | None, wm_b: dict[str, Any] | None) -> dict[str, Any]:
    """标出两个发布水位之间新增/修订了哪些材料。

    观测按 (series, label) 对齐：水位中同一逻辑序列的最新版本，在新版里
    通常是“新 id 经 replaces_id 接续旧链”，所以不能直接按 id 比较。
    """

    if wm_a is None or wm_b is None:
        return {"available": False, "说明": "对比双方均需为已发布版本（含固化水位）"}
    obs_a = {(o["series"], o["label"]): o for o in wm_a.get("observations_latest", [])}
    obs_b = {(o["series"], o["label"]): o for o in wm_b.get("observations_latest", [])}
    added = [obs_b[k] for k in sorted(set(obs_b) - set(obs_a))]
    revised = [
        obs_b[k] for k in sorted(set(obs_a) & set(obs_b))
        if obs_a[k]["id"] != obs_b[k]["id"]
        or obs_a[k]["source_version"] != obs_b[k]["source_version"]
        or obs_a[k]["value"] != obs_b[k]["value"]
    ]
    dec_a = {d["id"] for d in wm_a.get("decisions", [])}
    dec_b = {d["id"]: d for d in wm_b.get("decisions", [])}
    decisions_added = [dec_b[i] for i in sorted(set(dec_b) - dec_a)]
    return {
        "available": True,
        "observations_added": [_brief(o) for o in added],
        "observations_revised": [_brief(o) for o in revised],
        "decisions_added": [{"id": d["id"], "action": d["action"],
                             "basis_points": d["basis_points"],
                             "source_version": d["source_version"]}
                            for d in decisions_added],
        "expectations_frozen_a": len(wm_a.get("expectations", [])),
        "expectations_frozen_b": len(wm_b.get("expectations", [])),
    }


def _brief(obs: dict[str, Any]) -> dict[str, Any]:
    return {"id": obs["id"], "series": obs["series"], "label": obs["label"],
            "value": obs["value"], "currency": obs["currency"],
            "observed_at": obs["observed_at"], "source_version": obs["source_version"],
            "replaces_id": obs["replaces_id"]}


def _loads(raw: str | None) -> Any:
    import json
    return json.loads(raw) if raw else {}
