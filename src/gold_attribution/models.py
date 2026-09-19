"""领域模型：枚举、dataclass 与基础校验。

设计要点：
- 所有时间必须携带原始时区偏移（offset-aware），系统不做隐式时区转换，
  展示/还原时保留来源原始偏移；内部比较时使用 UTC。
- 跨市场数据保留原币种（currency 为 ISO 4217 三字母码）。
- 证据按修订链（revision chain）追加，旧记录永不覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class DomainError(Exception):
    """所有领域校验错误的基类。"""


class ValidationError(DomainError):
    """输入不满足领域约束。"""


class NotFoundError(DomainError):
    """实体不存在。"""


class ForbiddenError(DomainError):
    """访问被拒绝：草稿仅所属团队可读/可写。"""


class LifecycleError(DomainError):
    """操作不满足事件生命周期（如冻结后再登记预期、未公布先挂行情）。"""


class ConflictError(DomainError):
    """乐观锁冲突：草稿已被他人推进，必须显式合并或拒绝。"""

    def __init__(self, message: str, *, series_id: str, current_head: int, base_revision: int):
        super().__init__(message)
        self.series_id = series_id
        self.current_head = current_head
        self.base_revision = base_revision


class EventState(str, Enum):
    OPEN = "open"                 # 公布前：可登记预期
    FROZEN = "frozen"             # 预期已冻结，等待实际决定
    DECIDED = "decided"           # 已录入实际决定，可挂公布后材料、写分析
    LOCKED = "locked"             # 事件归档（可选的终态）


class EvidenceKind(str, Enum):
    POLICY_DECISION = "policy_decision"   # 实际决定（幅度、票数、声明）
    USD_WINDOW = "usd_window"             # 美元指数窗口
    REAL_RATE_WINDOW = "real_rate_window"  # 实际利率窗口（如 TIPS）
    SPOT_GOLD = "spot_gold"               # 现货黄金
    FUTURES_GOLD = "futures_gold"         # 纽约期金等
    ETF_HOLDING = "etf_holding"           # ETF 持仓
    CENTRAL_BANK_BUY = "central_bank_buy"  # 官方/央行购金
    RISK_MATERIAL = "risk_material"       # 地缘/财政信用等风险材料
    EXPECTATION = "expectation"           # 公布前预期（特殊：受冻结保护）


# 公布之后才允许挂载的证据
_POST_DECISION_KINDS = {
    EvidenceKind.USD_WINDOW,
    EvidenceKind.REAL_RATE_WINDOW,
    EvidenceKind.SPOT_GOLD,
    EvidenceKind.FUTURES_GOLD,
    EvidenceKind.ETF_HOLDING,
    EvidenceKind.CENTRAL_BANK_BUY,
    EvidenceKind.RISK_MATERIAL,
}


class Stance(str, Enum):
    SUPPORTING = "supporting"      # 支持证据
    CONTRADICTING = "contradicting"  # 相反证据
    UNCERTAINTY = "uncertainty"    # 不确定性


class AnalysisStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")
# 宽松的 IANA / 固定偏移时区标识，如 Asia/Shanghai、UTC、UTC-05:00、+08:00
_TZ_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")


def require_aware(dt: datetime, field_name: str = "时间") -> datetime:
    """时间必须携带固定 UTC 偏移（来源原时区），拒绝 naive datetime。"""
    if not isinstance(dt, datetime):
        raise ValidationError(f"{field_name}必须是 datetime，收到 {type(dt)!r}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationError(f"{field_name}缺少时区信息：所有市场时间必须携带原始时区")
    return dt


def require_currency(code: str) -> str:
    if not isinstance(code, str) or not _ISO_CURRENCY.match(code):
        raise ValidationError(f"币种必须是 ISO 4217 三字母码（如 USD/CNY），收到 {code!r}")
    return code


def require_tz_name(name: str) -> str:
    if not isinstance(name, str) or not _TZ_NAME.match(name):
        raise ValidationError(f"非法时区标识：{name!r}")
    return name


def utc(dt: datetime) -> datetime:
    """转 UTC 用于内部排序/比较；展示仍用原始对象。"""
    return require_aware(dt).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """保留原始偏移的 ISO 表示。"""
    return require_aware(dt).isoformat()


@dataclass(frozen=True)
class Event:
    event_id: str
    title: str
    state: EventState
    decision_due_at: datetime | None       # 公布时刻（原时区）
    expectation_frozen_at: datetime | None  # 冻结时刻（原时区）
    decided_at: datetime | None            # 实际决定录入时刻（原时区）
    created_at: datetime


@dataclass(frozen=True)
class Expectation:
    """公布前某来源对政策结果的预期概率分布。"""
    id: int | None
    event_id: str
    source: str                       # 来源，如 FedWatch / 投行研报
    source_version: str               # 来源版本标识
    collected_at: datetime            # 采集时刻（原时区）
    probabilities: dict[str, float]   # outcome(bp) -> 概率，和须为 1
    note: str = ""


@dataclass(frozen=True)
class Evidence:
    """市场材料的一个不可变版本；修订即新增一行并指向 supersedes_id。"""
    id: int | None
    event_id: str
    kind: EvidenceKind
    source: str
    source_version: str
    observed_at: datetime             # 市场时间（原时区）
    recorded_at: datetime             # 入账时刻（UTC）
    currency: str | None              # 原币种；无量纲材料可为 None
    market_tz: str                    # 市场/来源时区标识
    payload: dict                     # 结构化内容（价格、窗口、持仓量等）
    revision_note: str = ""           # 本版本相对上一版的说明
    supersedes_id: int | None = None


@dataclass(frozen=True)
class Claim:
    """分析中的一条可竞争主张。"""
    local_id: str                     # 系列内稳定标识，如 claim-1；修订间靠它对齐
    text: str
    stances: dict[Stance, tuple[int, ...]] = field(default_factory=dict)
    # stance -> 证据 id 列表（在 service 层解析为 Evidence 版本）


@dataclass(frozen=True)
class AnalysisRevision:
    series_id: str
    revision: int
    event_id: str
    owner_team: str
    author: str
    status: AnalysisStatus
    base_revision: int | None         # 派生自哪个修订（diff 用）
    created_at: datetime
    title: str
    summary: str
    claims: tuple[Claim, ...]
    competing_series: tuple[str, ...] = ()  # 明确竞争的其他解释系列


@dataclass(frozen=True)
class AnalysisSeries:
    series_id: str
    event_id: str
    owner_team: str
    latest_revision: int
    status: AnalysisStatus
    published_revision: int | None


def validate_probabilities(probs: dict[str, float]) -> None:
    if not probs:
        raise ValidationError("预期概率分布不能为空")
    if any(p < 0 or p > 1 for p in probs.values()):
        raise ValidationError("概率必须落在 [0, 1] 区间")
    total = sum(probs.values())
    if abs(total - 1.0) > 1e-6:
        raise ValidationError(f"概率之和必须为 1，当前为 {total}")
