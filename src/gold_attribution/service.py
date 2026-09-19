"""归因账本服务层。

职责边界：
- 账本只记录“发生了什么、谁在什么水位下得出了什么结论、证据如何关联”，
  不提供任何下单/调仓接口；自由文本经 guard 扫描，命中交易指令即拒绝并留痕。
- 所有写库内容只追加；事件状态推进受生命周期约束，并写入哈希链审计日志。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from .guard import assert_no_trading_instruction
from .models import (
    AnalysisStatus,
    Claim,
    ConflictError,
    Evidence,
    EvidenceKind,
    Event,
    EventState,
    Expectation,
    ForbiddenError,
    LifecycleError,
    NotFoundError,
    Stance,
    ValidationError,
    _POST_DECISION_KINDS,
    iso,
    require_aware,
    require_currency,
    require_tz_name,
    utc,
    validate_probabilities,
)
from .storage import Ledger, dumps

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


# 各类证据的最小 payload 契约
_PAYLOAD_CONTRACT: dict[EvidenceKind, dict[str, type]] = {
    EvidenceKind.POLICY_DECISION: {"basis_points": int},
    EvidenceKind.USD_WINDOW: {"index_level": (int, float)},  # type: ignore[dict-item]
    EvidenceKind.REAL_RATE_WINDOW: {"yield_pct": (int, float)},  # type: ignore[dict-item]
    EvidenceKind.SPOT_GOLD: {"price": (int, float)},  # type: ignore[dict-item]
    EvidenceKind.FUTURES_GOLD: {"price": (int, float)},  # type: ignore[dict-item]
    EvidenceKind.ETF_HOLDING: {"tonnes": (int, float)},  # type: ignore[dict-item]
    EvidenceKind.CENTRAL_BANK_BUY: {"tonnes": (int, float)},  # type: ignore[dict-item]
    EvidenceKind.RISK_MATERIAL: {"summary": str},
}

_CURRENCY_REQUIRED = {EvidenceKind.SPOT_GOLD, EvidenceKind.FUTURES_GOLD}


class Actor:
    def __init__(self, team: str, user: str) -> None:
        if not team or not user:
            raise ValidationError("actor 必须同时给出 team 与 user")
        self.team = team
        self.user = user


class AttributionService:
    def __init__(self, ledger: Ledger, clock: Clock = _default_clock) -> None:
        self.db = ledger
        self.clock = clock

    # ============================================================
    # 事件生命周期
    # ============================================================

    def create_event(
        self, event_id: str, title: str, decision_due_at: datetime, actor: Actor
    ) -> Event:
        require_aware(decision_due_at, "decision_due_at")
        self._guard(actor, "event_title", [("title", title)])
        now = self.clock()
        with self.db.tx() as c:
            if self.db.get_event_row(c, event_id) is not None:
                raise ValidationError(f"事件已存在：{event_id}")
            row = {
                "event_id": event_id,
                "title": title,
                "state": EventState.OPEN.value,
                "decision_due_at": iso(decision_due_at),
                "expectation_frozen_at": None,
                "decided_at": None,
                "created_at": iso(now),
            }
            self.db.insert_event(c, row)
            self._audit(c, actor, "event_create", "event", event_id, {"title": title})
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> Event:
        with self.db.tx() as c:
            r = self.db.get_event_row(c, event_id)
            if r is None:
                raise NotFoundError(f"事件不存在：{event_id}")
            return self._row_to_event(r)

    @staticmethod
    def _row_to_event(r: Any) -> Event:
        return Event(
            event_id=r["event_id"],
            title=r["title"],
            state=EventState(r["state"]),
            decision_due_at=datetime.fromisoformat(r["decision_due_at"]) if r["decision_due_at"] else None,
            expectation_frozen_at=datetime.fromisoformat(r["expectation_frozen_at"]) if r["expectation_frozen_at"] else None,
            decided_at=datetime.fromisoformat(r["decided_at"]) if r["decided_at"] else None,
            created_at=datetime.fromisoformat(r["created_at"]),
        )

    def freeze_expectations(self, event_id: str, frozen_at: datetime, actor: Actor) -> Event:
        """公布前冻结：此后任何来源的预期概率/采集时刻均不可再写入或修改。"""
        require_aware(frozen_at, "frozen_at")
        with self.db.tx() as c:
            r = self._require_event(c, event_id)
            if r["state"] != EventState.OPEN.value:
                raise LifecycleError(
                    f"只有 open 事件可以冻结预期，当前状态 {r['state']}"
                )
            count = len(self.db.list_expectations(c, event_id))
            self.db.update_event_state(
                c, event_id, state=EventState.FROZEN.value, expectation_frozen_at=iso(frozen_at)
            )
            self._audit(
                c, actor, "expectations_freeze", "event", event_id,
                {"frozen_at": iso(frozen_at), "sources_frozen": count},
            )
        return self.get_event(event_id)

    def record_expectation(
        self,
        event_id: str,
        source: str,
        source_version: str,
        collected_at: datetime,
        probabilities: dict[str, float],
        actor: Actor,
        note: str = "",
    ) -> int:
        require_aware(collected_at, "collected_at")
        validate_probabilities(probabilities)
        self._guard(actor, "expectation_note", [("note", note)])
        with self.db.tx() as c:
            r = self._require_event(c, event_id)
            frozen = r["state"] != EventState.OPEN.value
        if frozen:
            self._reject_separate(
                actor, "frozen_expectation_write",
                {"event_id": event_id, "source": source, "probabilities": probabilities},
            )
            raise LifecycleError(
                "预期已冻结（或事件已公布），不得补录/修改预期；"
                "请在公布后把更新作为证据或新分析记录"
            )
        with self.db.tx() as c:
            try:
                row_id = self.db.insert_expectation(c, {
                    "event_id": event_id,
                    "source": source,
                    "source_version": source_version,
                    "collected_at": iso(collected_at),
                    "probabilities": dumps(probabilities),
                    "note": note,
                    "recorded_at": iso(self.clock()),
                })
            except Exception as exc:  # UNIQUE 冲突等
                raise ValidationError(f"预期版本已存在：{source}@{source_version}") from exc
            self._audit(c, actor, "expectation_record", "expectation", str(row_id),
                        {"event_id": event_id, "source": source,
                         "source_version": source_version, "collected_at": iso(collected_at)})
            return row_id

    def list_expectations(self, event_id: str) -> list[Expectation]:
        with self.db.tx() as c:
            self._require_event(c, event_id)
            rows = self.db.list_expectations(c, event_id)
        return [
            Expectation(
                id=r["id"], event_id=r["event_id"], source=r["source"],
                source_version=r["source_version"],
                collected_at=datetime.fromisoformat(r["collected_at"]),
                probabilities=json.loads(r["probabilities"]), note=r["note"],
            )
            for r in rows
        ]

    # ============================================================
    # 公布后：实际决定与跨市场证据
    # ============================================================

    def record_decision(
        self,
        event_id: str,
        source: str,
        source_version: str,
        observed_at: datetime,
        payload: dict[str, Any],
        market_tz: str,
        actor: Actor,
        revision_note: str = "",
        currency: str | None = None,
    ) -> int:
        """录入实际决定；仅 FROZEN→DECIDED 时发生状态跃迁。

        后续更正（如声明措辞修订）以同一证据链的新版本追加。
        """
        return self._attach(
            event_id, EvidenceKind.POLICY_DECISION, source, source_version,
            observed_at, payload, actor, market_tz=market_tz,
            revision_note=revision_note, currency=currency,
            stream_key="", revision_of_version=None, decide=True,
        )

    def attach_evidence(
        self,
        event_id: str,
        kind: EvidenceKind | str,
        source: str,
        source_version: str,
        observed_at: datetime,
        payload: dict[str, Any],
        market_tz: str,
        actor: Actor,
        currency: str | None = None,
        stream_key: str = "",
        revision_note: str = "",
        revision_of_version: str | None = None,
    ) -> int:
        """挂载公布后材料。

        - 默认：每个 source_version 是一条独立观察（例如 ETF 每日持仓、
          每日美元窗口），各自成链，seq=1。
        - revision_of_version：显式声明“本记录是对同一来源旧版本的更正”，
          会挂到旧版本所在链上并置 supersedes 指针；必须填写 revision_note。
          旧记录永不覆盖。
        - stream_key：同一来源下并存的逻辑流标识（如 SGE 的不同合约）。
        """
        kind = kind if isinstance(kind, EvidenceKind) else EvidenceKind(kind)
        if kind == EvidenceKind.POLICY_DECISION:
            raise LifecycleError("实际决定请使用 record_decision 录入")
        if kind == EvidenceKind.EXPECTATION:
            raise LifecycleError("预期只能在冻结前通过 record_expectation 登记")
        return self._attach(
            event_id, kind, source, source_version, observed_at, payload, actor,
            market_tz=market_tz, revision_note=revision_note,
            currency=currency, stream_key=stream_key,
            revision_of_version=revision_of_version, decide=False,
        )

    def _attach(
        self,
        event_id: str,
        kind: EvidenceKind,
        source: str,
        source_version: str,
        observed_at: datetime,
        payload: dict[str, Any],
        actor: Actor,
        *,
        market_tz: str,
        revision_note: str,
        currency: str | None,
        stream_key: str,
        revision_of_version: str | None,
        decide: bool,
    ) -> int:
        require_aware(observed_at, "observed_at")
        require_tz_name(market_tz)
        self._validate_payload(kind, payload, currency)
        self._guard(
            actor, f"evidence_{kind.value}",
            [("revision_note", revision_note), *_iter_payload_text_fields(payload)],
        )
        with self.db.tx() as c:
            r = self._require_event(c, event_id)
            state = r["state"]
            if decide:
                if state not in (EventState.FROZEN.value, EventState.DECIDED.value):
                    raise LifecycleError(
                        f"实际决定只能在预期冻结后录入，当前状态 {state}"
                    )
            else:
                if kind in _POST_DECISION_KINDS and state != EventState.DECIDED.value:
                    raise LifecycleError(
                        f"{kind.value} 属于公布后材料：事件必须先录入实际决定，当前状态 {state}"
                    )

            chain_id: int
            seq: int
            supersedes_id: int | None = None

            if decide:
                # 实际决定：每事件每来源一条修订链（声明修订挂后续 seq）
                chain_key = ""
                chain_row = self.db.get_chain(c, event_id, kind.value, source, chain_key)
                if chain_row is None:
                    chain_id = self.db.insert_chain(c, {
                        "event_id": event_id, "kind": kind.value, "source": source,
                        "stream_key": "", "created_at": iso(self.clock()),
                    })
                    seq = 1
                else:
                    chain_id, seq, supersedes_id = self._append_to_chain_head(
                        c, chain_row, source_version, revision_note
                    )
            elif revision_of_version is not None:
                # 显式来源更正：定位旧版本所在链
                old = self._find_evidence_version(
                    c, event_id, kind.value, source, revision_of_version
                )
                if old is None:
                    raise ValidationError(
                        f"被更正的来源版本不存在：{source}@{revision_of_version}"
                    )
                chain_key = old["stream_key"]
                chain_row = self.db.get_chain(c, event_id, kind.value, source, chain_key)
                assert chain_row is not None
                chain_id, seq, supersedes_id = self._append_to_chain_head(
                    c, chain_row, source_version, revision_note,
                    expect_supersedes=int(old["id"]),
                )
            else:
                # 独立观察：stream_key 默认取 source_version，避免把
                # “次日新数据”误挂成“同日数据的更正”
                chain_key = stream_key or source_version
                chain_row = self.db.get_chain(c, event_id, kind.value, source, chain_key)
                if chain_row is not None:
                    head = self.db.latest_in_chain(c, int(chain_row["chain_id"]))
                    raise ValidationError(
                        f"来源流 {source}[{chain_key}] 已有记录（版本 {head['source_version']}）。"
                        "若本条是对旧版本的更正，请显式传 revision_of_version 并附 revision_note；"
                        "若是新的逻辑数据流，请使用不同 stream_key"
                    )
                chain_id = self.db.insert_chain(c, {
                    "event_id": event_id, "kind": kind.value, "source": source,
                    "stream_key": chain_key, "created_at": iso(self.clock()),
                })
                seq = 1

            evidence_id = self.db.insert_evidence(c, {
                "event_id": event_id, "chain_id": chain_id, "seq": seq,
                "kind": kind.value, "source": source, "stream_key": chain_key,
                "source_version": source_version, "observed_at": iso(observed_at),
                "recorded_at": iso(self.clock()),
                "currency": currency, "market_tz": market_tz,
                "payload": dumps(payload), "revision_note": revision_note,
                "supersedes_id": supersedes_id,
            })

            if decide and state == EventState.FROZEN.value:
                self.db.update_event_state(
                    c, event_id, state=EventState.DECIDED.value, decided_at=iso(self.clock())
                )
            self._audit(
                c, actor,
                "decision_record" if decide else "evidence_attach",
                "evidence", str(evidence_id),
                {"event_id": event_id, "kind": kind.value, "source": source,
                 "source_version": source_version, "chain_id": chain_id, "seq": seq,
                 "supersedes_id": supersedes_id, "currency": currency,
                 "market_tz": market_tz, "observed_at": iso(observed_at)},
            )
            return evidence_id

    def _append_to_chain_head(
        self, c, chain_row, source_version: str, revision_note: str,
        *, expect_supersedes: int | None = None,
    ) -> tuple[int, int, int]:
        """在修订链头部追加新版本，返回 (chain_id, seq, supersedes_id)。"""
        chain_id = int(chain_row["chain_id"])
        head = self.db.latest_in_chain(c, chain_id)
        assert head is not None
        if head["source_version"] == source_version:
            raise ValidationError(
                f"来源版本 {source_version} 已在该链上且不可覆盖；请使用新版本号"
            )
        if expect_supersedes is not None and int(head["id"]) != expect_supersedes:
            raise ValidationError(
                f"链头已推进到 id={head['id']}（版本 {head['source_version']}），"
                f"与待更正版本 id={expect_supersedes} 不一致；请基于最新链头修订"
            )
        if not revision_note:
            raise ValidationError("追加来源修订必须填写 revision_note 说明改了什么")
        return chain_id, int(head["seq"]) + 1, int(head["id"])

    def _find_evidence_version(self, c, event_id: str, kind: str,
                               source: str, source_version: str):
        return c.execute(
            """SELECT * FROM evidence
               WHERE event_id=? AND kind=? AND source=? AND source_version=?
               ORDER BY id DESC LIMIT 1""",
            (event_id, kind, source, source_version),
        ).fetchone()

    @staticmethod
    def _validate_payload(kind: EvidenceKind, payload: Any, currency: str | None) -> None:
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("payload 必须是非空对象")
        contract = _PAYLOAD_CONTRACT[kind]
        for key, expected_type in contract.items():
            if key not in payload:
                raise ValidationError(f"{kind.value} 缺少必需字段 {key!r}")
            if not isinstance(payload[key], expected_type) or isinstance(payload[key], bool):
                raise ValidationError(
                    f"{kind.value}.{key} 类型错误，应为 {expected_type}"
                )
        if kind in _CURRENCY_REQUIRED:
            require_currency(currency or "")
        elif currency is not None:
            require_currency(currency)
        for win_key in ("window_start", "window_end"):
            if win_key in payload:
                try:
                    dt = datetime.fromisoformat(str(payload[win_key]))
                except ValueError as exc:
                    raise ValidationError(f"{win_key} 必须是带时区的 ISO 时间") from exc
                if dt.tzinfo is None:
                    raise ValidationError(f"{win_key} 必须携带来源原始时区")

    def list_evidence_chains(self, event_id: str) -> list[Evidence]:
        """每个证据流的最新版本（供浏览用）。"""
        with self.db.tx() as c:
            self._require_event(c, event_id)
            rows = self.db.list_chains(c, event_id)
        return [self._row_to_evidence(r) for r in rows]

    @staticmethod
    def _row_to_evidence(r: Any) -> Evidence:
        return Evidence(
            id=r["id"], event_id=r["event_id"], kind=EvidenceKind(r["kind"]),
            source=r["source"], source_version=r["source_version"],
            observed_at=datetime.fromisoformat(r["observed_at"]),
            recorded_at=datetime.fromisoformat(r["recorded_at"]),
            currency=r["currency"], market_tz=r["market_tz"],
            payload=json.loads(r["payload"]), revision_note=r["revision_note"],
            supersedes_id=r["supersedes_id"],
        )

    # ============================================================
    # 竞争解释：分析系列、草稿、乐观锁、发布
    # ============================================================

    def create_analysis_series(
        self, series_id: str, event_id: str, owner_team: str, title: str, actor: Actor
    ) -> None:
        if actor.team != owner_team:
            raise ForbiddenError("只能由所属团队创建自己的分析系列")
        self._guard(actor, "series_title", [("title", title)])
        with self.db.tx() as c:
            ev = self._require_event(c, event_id)
            if EventState(ev["state"]) not in (EventState.DECIDED.value, EventState.LOCKED.value):
                raise LifecycleError("分析只能在实际决定录入后创建")
            if self.db.get_series(c, series_id) is not None:
                raise ValidationError(f"分析系列已存在：{series_id}")
            self.db.insert_series(c, {
                "series_id": series_id, "event_id": event_id,
                "owner_team": owner_team, "title": title,
                "created_at": iso(self.clock()),
            })
            self._audit(c, actor, "series_create", "analysis_series", series_id,
                        {"event_id": event_id, "owner_team": owner_team})

    def save_draft(
        self,
        series_id: str,
        title: str,
        summary: str,
        claims: Iterable[Claim | dict[str, Any]],
        actor: Actor,
        *,
        base_revision: int,
        competing_series: Iterable[str] = (),
    ) -> int:
        """保存草稿修订。

        base_revision 必须等于系列当前 head；否则抛 ConflictError，
        调用方必须 resolve_conflict 显式选择 ours/theirs/merge。
        """
        self._guard_analysis_text(actor, title, summary, claims)
        return self._write_revision(
            series_id, title, summary, claims, actor,
            base_revision=base_revision, competing_series=competing_series,
            mode="normal",
        )

    def resolve_conflict(
        self,
        series_id: str,
        title: str,
        summary: str,
        claims: Iterable[Claim | dict[str, Any]],
        actor: Actor,
        *,
        base_revision: int,
        choice: str,
        competing_series: Iterable[str] = (),
        claim_resolution: dict[str, str] | None = None,
    ) -> int:
        """并发冲突的显式处置。

        choice:
        - "theirs"：拒绝己方变更，不产生修订（返回当前 head，留痕）。
        - "ours"：以当前 head 为基底整体覆盖为己方内容，产生新修订。
        - "merge"：逐主张合并；claim_resolution 必须覆盖双方出现过的每个
          local_id，取值 ours/theirs/merged/dropped。
        """
        with self.db.tx() as c:
            s = self._require_series(c, series_id)
            self._assert_owner(s, actor)
            head = int(s["head_revision"])
            if base_revision == head:
                raise LifecycleError("系列并未发生并发修改，无需冲突处置")
            if choice == "theirs":
                self._audit(c, actor, "draft_conflict_reject_ours", "analysis_series",
                            series_id, {"base_revision": base_revision, "kept_head": head})
                return head

        if choice == "ours":
            self._guard_analysis_text(actor, title, summary, claims)
            return self._write_revision(
                series_id, title, summary, claims, actor,
                base_revision=head, competing_series=competing_series,
                mode="override", overridden_head=head, stale_base=base_revision,
            )
        if choice == "merge":
            self._guard_analysis_text(actor, title, summary, claims)
            return self._write_merged(
                series_id, title, summary, claims, actor,
                competing_series=competing_series, stale_base=base_revision,
                claim_resolution=claim_resolution or {},
            )
        raise ValidationError(f"未知冲突处置 {choice!r}，可选 ours/theirs/merge")

    def _write_merged(
        self, series_id, title, summary, claims, actor, *,
        competing_series, stale_base, claim_resolution,
    ) -> int:
        norm_claims = [self._normalize_claim(cl) for cl in claims]
        with self.db.tx() as c:
            s = self._require_series(c, series_id)
            self._assert_owner(s, actor)
            head = int(s["head_revision"])
            ours_claims = {cl["local_id"]: cl for cl in self._load_claim_map(c, series_id, stale_base)}
            theirs_claims = {cl["local_id"]: cl for cl in self._load_claim_map(c, series_id, head)}
            all_ids = set(ours_claims) | set(theirs_claims)
            if set(claim_resolution) != all_ids:
                raise ValidationError(
                    "claim_resolution 必须显式覆盖双方出现过的每个主张，"
                    f"期望 {sorted(all_ids)}，收到 {sorted(claim_resolution)}"
                )
            bad = {k: v for k, v in claim_resolution.items()
                   if v not in ("ours", "theirs", "merged", "dropped")}
            if bad:
                raise ValidationError(f"非法的逐主张处置：{bad}")
            final_by_id = {cl["local_id"]: cl for cl in norm_claims}
            merged: list[dict[str, Any]] = []
            for local_id in sorted(all_ids):
                decision = claim_resolution[local_id]
                if decision == "ours":
                    merged.append(ours_claims[local_id])
                elif decision == "theirs":
                    merged.append(theirs_claims[local_id])
                elif decision == "merged":
                    if local_id not in final_by_id:
                        raise ValidationError(f"标记为 merged 的主张 {local_id} 缺少合并稿")
                    merged.append(final_by_id[local_id])
                # dropped：显式放弃，不进入新修订
            # 允许全新增的主张（双方都没有的 id 必须显式以 merged 稿之外的新增传入？）
            new_ids = set(final_by_id) - all_ids
            for local_id in sorted(new_ids):
                merged.append(final_by_id[local_id])
            revision = self._persist_revision(
                c, s, title, summary, merged, actor,
                base=head, competing_series=competing_series,
                mode="merge", extra={
                    "stale_base": stale_base, "merged_with_head": head,
                    "claim_resolution": claim_resolution,
                    "added_new_claims": sorted(new_ids),
                },
            )
            return revision

    def _write_revision(
        self, series_id, title, summary, claims, actor, *,
        base_revision, competing_series, mode, overridden_head=None, stale_base=None,
    ) -> int:
        norm_claims = [self._normalize_claim(cl) for cl in claims]
        with self.db.tx() as c:
            s = self._require_series(c, series_id)
            self._assert_owner(s, actor)
            head = int(s["head_revision"])
            if base_revision != head:
                raise ConflictError(
                    f"草稿基于修订 {base_revision}，但系列 head 已到 {head}；"
                    "请先拉取并用 resolve_conflict 显式合并(ours/theirs/merge)",
                    series_id=series_id, current_head=head, base_revision=base_revision,
                )
            revision = self._persist_revision(
                c, s, title, summary, norm_claims, actor,
                base=head, competing_series=competing_series,
                mode=mode, extra={"overridden_head": overridden_head, "stale_base": stale_base},
            )
            return revision

    def _persist_revision(
        self, c, s, title, summary, norm_claims, actor, *,
        base, competing_series, mode, extra,
    ) -> int:
        event_id = s["event_id"]
        competing = list(competing_series)
        for other in competing:
            row = self.db.get_series(c, other)
            if row is None or row["event_id"] != event_id:
                raise ValidationError(f"竞争解释系列不存在或不属于同一事件：{other}")
        # 主张 local_id 唯一、有序；证据必须属于同一事件
        seen: set[str] = set()
        for cl in norm_claims:
            if cl["local_id"] in seen:
                raise ValidationError(f"主张 local_id 重复：{cl['local_id']}")
            seen.add(cl["local_id"])
            for stance_name, ev_ids in cl["stances"].items():
                Stance(stance_name)
                for ev_id in ev_ids:
                    ev_row = self.db.get_evidence(c, ev_id)
                    if ev_row is None:
                        raise ValidationError(f"证据不存在：id={ev_id}")
                    if ev_row["event_id"] != event_id:
                        raise ValidationError(
                            f"证据 {ev_id} 属于其他事件，不能挂到本分析"
                        )

        revision = int(s["head_revision"]) + 1
        series_id = s["series_id"]
        watermark = iso(self.clock())
        canonical = self._canonical_doc(title, summary, norm_claims, competing)
        doc_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.db.insert_revision(c, {
            "series_id": series_id, "revision": revision, "event_id": event_id,
            "owner_team": s["owner_team"], "author": actor.user,
            "status": AnalysisStatus.DRAFT.value, "base_revision": base if base else None,
            "title": title, "summary": summary, "competing_series": dumps(competing),
            "data_watermark_at": watermark, "doc_hash": doc_hash,
            "created_at": watermark,
        })
        for ordinal, cl in enumerate(norm_claims):
            self.db.insert_claim(c, {
                "series_id": series_id, "revision": revision,
                "local_id": cl["local_id"], "ordinal": ordinal, "text": cl["text"],
            })
            for stance_name, ev_ids in cl["stances"].items():
                for ev_id in ev_ids:
                    self.db.insert_claim_evidence(c, {
                        "series_id": series_id, "revision": revision,
                        "local_id": cl["local_id"], "stance": stance_name,
                        "evidence_id": ev_id,
                    })
        self.db.bump_head(c, series_id, revision)
        self._audit(c, actor, f"draft_{mode}", "analysis_revision",
                    f"{series_id}#{revision}",
                    {"base_revision": base, "watermark": watermark,
                     "doc_hash": doc_hash, **{k: v for k, v in extra.items() if v is not None}})
        return revision

    @staticmethod
    def _normalize_claim(cl: Claim | dict[str, Any]) -> dict[str, Any]:
        if isinstance(cl, Claim):
            d = {
                "local_id": cl.local_id, "text": cl.text,
                "stances": {k.value: list(v) for k, v in cl.stances.items()},
            }
        else:
            try:
                d = {
                    "local_id": str(cl["local_id"]),
                    "text": str(cl["text"]),
                    "stances": {str(k): list(v) for k, v in (cl.get("stances") or {}).items()},
                }
            except (KeyError, TypeError) as exc:
                raise ValidationError("主张必须含 local_id/text/stances") from exc
        if not d["local_id"] or not d["text"].strip():
            raise ValidationError("主张 local_id 与 text 均不能为空")
        # 唯一的立场规范化口径：丢弃空立场、证据 id 去重排序。
        # doc_hash 的 canonical 必须与读取路径 (_load_claim_map) 完全一致。
        normalized_stances: dict[str, list[int]] = {}
        for key, value in d["stances"].items():
            ids = sorted({int(v) for v in value})
            if ids:
                normalized_stances[key] = ids
        d["stances"] = normalized_stances
        return d

    @staticmethod
    def _canonical_doc(title, summary, claims, competing) -> str:
        return dumps({
            "title": title, "summary": summary, "competing": competing,
            "claims": [
                {"local_id": cl["local_id"], "text": cl["text"],
                 "stances": {k: sorted(v) for k, v in sorted(cl["stances"].items())}}
                for cl in claims
            ],
        })

    def publish(self, series_id: str, actor: Actor) -> int:
        """把系列最新修订发给投委会。

        WORM：修订行永不 UPDATE；发布动作只向 publications 追加一条记录。
        已发版本因此被永久钉住，后来更正只能形成新分析（新修订→新发布），
        旧发布版本在视图中派生为 superseded，但行本身不变。
        """
        with self.db.tx() as c:
            s = self._require_series(c, series_id)
            self._assert_owner(s, actor)
            revision = int(s["head_revision"])
            if revision == 0:
                raise LifecycleError("系列尚无草稿可发布")
            prior_pub = self.db.latest_publication(c, series_id)
            if prior_pub is not None and int(prior_pub["revision"]) == revision:
                raise LifecycleError(f"修订 #{revision} 已发布，不可重复发布或改写")
            at = iso(self.clock())
            self.db.insert_publication(c, {
                "series_id": series_id, "revision": revision, "published_at": at,
                "actor_team": actor.team, "actor": actor.user,
                "supersedes_rev": int(prior_pub["revision"]) if prior_pub else None,
            })
            self.db.mark_published(c, series_id, revision)
            self._audit(c, actor, "analysis_publish", "publication",
                        f"{series_id}#{revision}",
                        {"supersedes": int(prior_pub["revision"]) if prior_pub else None,
                         "published_at": at})
            return revision

    def _effective_status(self, c, series_id: str, revision: int) -> AnalysisStatus:
        pub = self.db.latest_publication(c, series_id)
        if pub is None:
            return AnalysisStatus.DRAFT
        latest = int(pub["revision"])
        if revision == latest:
            return AnalysisStatus.PUBLISHED
        published_revs = {int(r["revision"]) for r in self.db.list_publications(c, series_id)}
        if revision in published_revs:
            return AnalysisStatus.SUPERSEDED
        return AnalysisStatus.DRAFT

    # ============================================================
    # 读取：水位还原 / 权限 / diff
    # ============================================================

    def list_event_series(self, event_id: str, actor: Actor) -> list[dict[str, Any]]:
        """非 owner 团队只能看到已有发布版本的系列（草稿不可见）。"""
        with self.db.tx() as c:
            self._require_event(c, event_id)
            rows = self.db.list_series_for_event(c, event_id)
            result = []
            for r in rows:
                is_owner = r["owner_team"] == actor.team
                published = r["published_revision"]
                if not is_owner and published is None:
                    continue  # 未发布草稿仅所属团队可读
                item = {
                    "series_id": r["series_id"], "event_id": r["event_id"],
                    "owner_team": r["owner_team"],
                    "head_revision": r["head_revision"],
                    "published_revision": published,
                    "visible_title": r["title"] if is_owner else None,
                }
                if published is not None:
                    pr = self.db.get_revision(c, r["series_id"], int(published))
                    item["published_title"] = pr["title"]
                result.append(item)
            return result

    def get_revision(self, series_id: str, revision: int, actor: Actor) -> dict[str, Any]:
        with self.db.tx() as c:
            s = self._require_series(c, series_id)
            r = self.db.get_revision(c, series_id, revision)
            if r is None:
                raise NotFoundError(f"修订不存在：{series_id}#{revision}")
            effective = self._effective_status(c, series_id, revision)
            if effective == AnalysisStatus.DRAFT and s["owner_team"] != actor.team:
                raise ForbiddenError("未发布草稿只能由所属团队读取")
            view = self._build_revision_view(c, s, r)
            view["status"] = effective.value
            self._verify_doc_hash(c, r, view)
            return view

    def _verify_doc_hash(self, c, r: Any, view: dict[str, Any]) -> None:
        """按库内当前内容重算文档哈希，与发布时钉住的 doc_hash 比对。"""
        canonical = self._canonical_doc(
            r["title"], r["summary"],
            self._load_claim_map(c, r["series_id"], r["revision"]),
            json.loads(r["competing_series"]),
        )
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual != r["doc_hash"]:
            raise LifecycleError(
                f"修订 {r['series_id']}#{r['revision']} 的内容与发布时钉住的 "
                f"doc_hash 不一致（库内行可能被直接篡改）"
            )

    def _build_revision_view(self, c, s, r) -> dict[str, Any]:
        claims_view = []
        watermark = r["data_watermark_at"]
        for cl_row in self.db.list_claims(c, r["series_id"], r["revision"]):
            stances: dict[str, list[dict[str, Any]]] = {}
            for link in self.db.list_claim_evidence(c, r["series_id"], r["revision"]):
                if link["local_id"] != cl_row["local_id"]:
                    continue
                ev = self.db.get_evidence(c, int(link["evidence_id"]))
                assert ev is not None
                chain_head = self.db.latest_in_chain(c, int(ev["chain_id"]))
                superseded = chain_head["id"] != ev["id"]
                stances.setdefault(link["stance"], []).append({
                    "evidence": self._row_to_evidence(ev),
                    "pinned_version": ev["source_version"],
                    "is_current_version": not superseded,
                    "current_chain_head_seq": chain_head["seq"],
                    # 该引用版本是否已被修订；以及修订到达时点是否晚于本版水位
                    "pinned_version_superseded": superseded,
                    "correction_arrived_after_watermark": (
                        superseded and chain_head["recorded_at"] > watermark
                    ),
                    "current_head_version": chain_head["source_version"],
                })
            claims_view.append({"local_id": cl_row["local_id"], "text": cl_row["text"],
                                "stances": stances})

        # 水位清单：发布/保存时可见的全部证据；以及之后才到达的材料
        visible_at_mark = self.db.list_evidence_for_event(
            c, s["event_id"], as_of_utc=watermark
        )
        later_rows = [
            row for row in self.db.list_evidence_for_event(c, s["event_id"])
            if row["recorded_at"] > watermark
        ]
        return {
            "series_id": r["series_id"],
            "revision": r["revision"],
            "status": r["status"],
            "owner_team": r["owner_team"],
            "author": r["author"],
            "title": r["title"],
            "summary": r["summary"],
            "base_revision": r["base_revision"],
            "competing_series": json.loads(r["competing_series"]),
            "data_watermark_at": watermark,
            "doc_hash": r["doc_hash"],
            "created_at": r["created_at"],
            "claims": claims_view,
            "evidence_watermark": {
                "as_of_utc": watermark,
                "visible_evidence": [self._watermark_entry(row) for row in visible_at_mark],
                "later_evidence": [
                    {**self._watermark_entry(row), "recorded_at": row["recorded_at"]}
                    for row in later_rows
                ],
            },
        }

    def _watermark_entry(self, row: Any) -> dict[str, Any]:
        return {
            "evidence_id": row["id"], "kind": row["kind"], "source": row["source"],
            "stream_key": row["stream_key"], "source_version": row["source_version"],
            "seq": row["seq"], "currency": row["currency"], "market_tz": row["market_tz"],
            "observed_at": row["observed_at"], "recorded_at": row["recorded_at"],
        }

    def diff_revisions(self, series_id: str, rev_a: int, rev_b: int, actor: Actor) -> dict[str, Any]:
        """逐主张对比两版，并标出水位间新到的资料改变了哪段判断。"""
        with self.db.tx() as c:
            s = self._require_series(c, series_id)
            ra = self.db.get_revision(c, series_id, rev_a)
            rb = self.db.get_revision(c, series_id, rev_b)
            if ra is None or rb is None:
                raise NotFoundError(f"修订不存在：{series_id}#{rev_a} 或 #{rev_b}")
            for rr in (ra, rb):
                if (self._effective_status(c, series_id, rr["revision"]) == AnalysisStatus.DRAFT
                        and s["owner_team"] != actor.team):
                    raise ForbiddenError("未发布草稿只能由所属团队读取")
            a_map = {cl["local_id"]: cl for cl in self._load_claim_map(c, series_id, rev_a)}
            b_map = {cl["local_id"]: cl for cl in self._load_claim_map(c, series_id, rev_b)}
            added, removed, changed, unchanged = [], [], [], []
            for local_id in sorted(set(a_map) | set(b_map)):
                ca, cb = a_map.get(local_id), b_map.get(local_id)
                if ca is None:
                    added.append(local_id); continue
                if cb is None:
                    removed.append(local_id); continue
                if ca == cb:
                    unchanged.append(local_id); continue
                text_changed = ca["text"] != cb["text"]
                stance_changes = {}
                for stance in set(ca["stances"]) | set(cb["stances"]):
                    old_ids = set(ca["stances"].get(stance, []))
                    new_ids = set(cb["stances"].get(stance, []))
                    if old_ids != new_ids:
                        stance_changes[stance] = {
                            "linked": sorted(new_ids - old_ids),
                            "unlinked": sorted(old_ids - new_ids),
                        }
                changed.append({
                    "local_id": local_id, "text_changed": text_changed,
                    "stance_changes": stance_changes,
                })

            # 水位间新到证据
            lo, hi = sorted([ra["data_watermark_at"], rb["data_watermark_at"]])
            new_evidence = [
                self._watermark_entry(row)
                for row in self.db.list_evidence_for_event(c, s["event_id"])
                if lo < row["recorded_at"] <= hi
            ]
            new_ids = {e["evidence_id"] for e in new_evidence}
            # 新资料是否被新版主张实际引用 → 改变了哪段判断
            b_links: dict[int, list[str]] = {}
            for cl in b_map.values():
                for ids in cl["stances"].values():
                    for ev_id in ids:
                        b_links.setdefault(ev_id, []).append(cl["local_id"])
            for e in new_evidence:
                e["changed_claims"] = sorted(b_links.get(e["evidence_id"], []))
            return {
                "series_id": series_id, "from_revision": rev_a, "to_revision": rev_b,
                "watermark_window": {"start_utc": lo, "end_utc": hi},
                "claims_added": added, "claims_removed": removed,
                "claims_unchanged": unchanged, "claims_changed": changed,
                "new_evidence_in_window": new_evidence,
                "new_evidence_not_used": sorted(
                    e["evidence_id"] for e in new_evidence if not e["changed_claims"]
                ),
                "summary_changed": ra["summary"] != rb["summary"],
                "title_changed": ra["title"] != rb["title"],
            }

    def _load_claim_map(self, c, series_id: str, revision: int) -> list[dict[str, Any]]:
        if revision == 0:
            return []
        r = self.db.get_revision(c, series_id, revision)
        if r is None:
            raise NotFoundError(f"修订不存在：{series_id}#{revision}")
        claims: dict[str, dict[str, Any]] = {}
        for cl in self.db.list_claims(c, series_id, revision):
            claims[cl["local_id"]] = {
                "local_id": cl["local_id"], "text": cl["text"], "stances": {},
            }
        for link in self.db.list_claim_evidence(c, series_id, revision):
            claims[link["local_id"]]["stances"].setdefault(link["stance"], []).append(
                int(link["evidence_id"])
            )
        for cl in claims.values():
            cl["stances"] = {k: sorted(v) for k, v in cl["stances"].items()}
        return list(claims.values())

    # ============================================================
    # 审计
    # ============================================================

    def verify_audit_chain(self) -> bool:
        with self.db.tx() as c:
            prev = "GENESIS"
            for row in self.db.list_audit(c):
                if row["prev_hash"] != prev:
                    return False
                if row["hash"] != self._audit_hash(row, row["prev_hash"]):
                    return False
                prev = row["hash"]
            return True

    def list_audit(self) -> list[dict[str, Any]]:
        with self.db.tx() as c:
            return [dict(r) for r in self.db.list_audit(c)]

    def list_rejected_writes(self) -> list[dict[str, Any]]:
        with self.db.tx() as c:
            return [dict(r) for r in self.db.list_rejected(c)]

    # ---------- 内部工具 ----------

    def _guard_analysis_text(self, actor, title, summary, claims) -> None:
        fields: list[tuple[str, str]] = [("title", title), ("summary", summary)]
        for cl in claims:
            text = cl.text if isinstance(cl, Claim) else str(cl.get("text", ""))
            fields.append(("claim", text))
        self._guard(actor, "analysis_text", fields)

    def _guard(self, actor: Actor, reason: str, fields: list[tuple[str, str]]) -> None:
        """交易指令扫描；命中时以独立事务留痕后抛出（不随业务事务回滚）。"""
        try:
            assert_no_trading_instruction(*fields)
        except ValidationError:
            with self.db.tx() as rc:
                self.db.insert_rejected(rc, {
                    "at": iso(self.clock()), "actor_team": actor.team, "actor": actor.user,
                    "reason": reason,
                    "content": dumps({name: text for name, text in fields}),
                })
            raise

    def _audit(self, c, actor: Actor, action: str, entity: str, entity_id: str,
               details: dict[str, Any]) -> None:
        prev = self.db.last_audit_hash(c)
        at = iso(self.clock())
        material_row = {
            "at": at, "actor_team": actor.team, "actor": actor.user,
            "action": action, "entity": entity, "entity_id": entity_id,
            "details": dumps(details),
        }
        self.db.insert_audit(c, {
            **material_row, "prev_hash": prev,
            "hash": self._audit_hash(material_row, prev),
        })

    @staticmethod
    def _audit_hash(row: Any, prev_hash: str) -> str:
        def g(key: str) -> Any:
            return row[key] if isinstance(row, dict) or hasattr(row, "keys") else getattr(row, key)
        material = dumps({
            "at": g("at"), "actor_team": g("actor_team"), "actor": g("actor"),
            "action": g("action"), "entity": g("entity"), "entity_id": g("entity_id"),
            "details": g("details"), "prev_hash": prev_hash,
        })
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _reject_separate(self, actor: Actor, reason: str, content: dict[str, Any]) -> None:
        """以独立事务记录被拒写入，确保拒绝本身不随业务事务回滚。"""
        with self.db.tx() as rc:
            self.db.insert_rejected(rc, {
                "at": iso(self.clock()), "actor_team": actor.team, "actor": actor.user,
                "reason": reason, "content": dumps(content),
            })

    def _require_event(self, c, event_id: str) -> Any:
        r = self.db.get_event_row(c, event_id)
        if r is None:
            raise NotFoundError(f"事件不存在：{event_id}")
        return r

    def _require_series(self, c, series_id: str) -> Any:
        r = self.db.get_series(c, series_id)
        if r is None:
            raise NotFoundError(f"分析系列不存在：{series_id}")
        return r

    @staticmethod
    def _assert_owner(s: Any, actor: Actor) -> None:
        if s["owner_team"] != actor.team:
            raise ForbiddenError("未发布草稿只能由所属团队读取/修改")


def _iter_payload_text_fields(payload: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """递归取出 payload 中的字符串字段用于交易指令扫描。"""
    def walk(obj: Any, path: str) -> Iterable[tuple[str, str]]:
        if isinstance(obj, str):
            yield (f"payload.{path}", obj)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                yield from walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from walk(v, f"{path}[{i}]")
    yield from walk(payload, "")
