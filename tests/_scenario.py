"""测试用场景构造：2026-09-16 FOMC + 黄金跨市场归因。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.gold_attribution.models import Claim, EvidenceKind, Stance
from src.gold_attribution.service import Actor, AttributionService
from src.gold_attribution.storage import Ledger

NY = timezone(timedelta(hours=-4))
SH = timezone(timedelta(hours=8))
UTC = timezone.utc

EVENT_ID = "fed-2026-09-16"
DUE = datetime(2026, 9, 16, 14, 0, tzinfo=NY)


class FakeClock:
    """可控时钟：测试中精确安排证据入账先后（水位）。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 16, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: int = 1) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def build_decided_event(svc: AttributionService, macro: Actor | None = None) -> SimpleNamespace:
    """构造一个走到 decided 的事件，返回各证据 id 句柄。"""
    macro = macro or Actor("macro", "alice")
    svc.create_event(EVENT_ID, "美联储2026年9月议息", DUE, macro)

    svc.record_expectation(
        EVENT_ID, "FedWatch", "2026-09-16T09:00Z",
        datetime(2026, 9, 16, 9, 0, tzinfo=UTC),
        {"25bp": 0.97, "hold": 0.03}, macro,
        note="会前隐含加息概率",
    )
    svc.record_expectation(
        EVENT_ID, "投行调查-均值", "2026-09-15",
        datetime(2026, 9, 15, 17, 0, tzinfo=NY),
        {"25bp": 0.80, "50bp": 0.05, "hold": 0.15}, macro,
    )

    svc.freeze_expectations(EVENT_ID, DUE, macro)

    decision = svc.record_decision(
        EVENT_ID, "FOMC声明", "stmt-2026-09-16-v1", DUE,
        {"basis_points": 25, "vote": "12-0", "statement": "通胀仍处高位，点阵图维持年内路径"},
        "America/New_York", macro,
    )
    dxy = svc.attach_evidence(
        EVENT_ID, EvidenceKind.USD_WINDOW, "ICE-DXY", "2026-09-16-window",
        datetime(2026, 9, 16, 14, 30, tzinfo=NY),
        {"index_level": 98.42,
         "window_start": "2026-09-16T13:30:00-04:00",
         "window_end": "2026-09-16T16:00:00-04:00",
         "change_pct": -0.35},
        "America/New_York", macro,
    )
    real_rate = svc.attach_evidence(
        EVENT_ID, EvidenceKind.REAL_RATE_WINDOW, "US-TIPS-10Y", "2026-09-16-window",
        datetime(2026, 9, 16, 16, 0, tzinfo=NY),
        {"yield_pct": 1.62, "change_bp": -6,
         "window_start": "2026-09-16T13:30:00-04:00",
         "window_end": "2026-09-16T16:00:00-04:00"},
        "America/New_York", macro,
    )
    spot = svc.attach_evidence(
        EVENT_ID, EvidenceKind.SPOT_GOLD, "LBMA", "PM-fix-2026-09-16",
        datetime(2026, 9, 16, 15, 0, tzinfo=NY),
        {"price": 4305.20, "intraday_low": 4278.10, "note": "短跌后重上4300"},
        "America/New_York", macro, currency="USD",
    )
    futures = svc.attach_evidence(
        EVENT_ID, EvidenceKind.FUTURES_GOLD, "COMEX", "GCZ6-2026-09-16",
        datetime(2026, 9, 16, 17, 30, tzinfo=NY),
        {"price": 4402.50, "contract": "GCZ6", "note": "纽约期金随后突破4400"},
        "America/New_York", macro, currency="USD",
    )
    sge = svc.attach_evidence(
        EVENT_ID, EvidenceKind.SPOT_GOLD, "SGE", "Au9999-2026-09-17",
        datetime(2026, 9, 17, 15, 30, tzinfo=SH),
        {"price": 980.10, "note": "上海金下午盘，原币种人民币计价"},
        "Asia/Shanghai", macro, currency="CNY", stream_key="Au9999",
    )
    etf = svc.attach_evidence(
        EVENT_ID, EvidenceKind.ETF_HOLDING, "GLD-holdings", "2026-09-16",
        datetime(2026, 9, 16, 20, 0, tzinfo=UTC),
        {"tonnes": 945.3, "daily_change_tonnes": 6.8},
        "UTC", macro,
    )
    cb = svc.attach_evidence(
        EVENT_ID, EvidenceKind.CENTRAL_BANK_BUY, "官方部门月报", "2026-08",
        datetime(2026, 8, 31, tzinfo=UTC),
        {"tonnes": 120.0, "note": "新兴市场央行连续第15个月净购入"},
        "UTC", macro,
    )
    geo = svc.attach_evidence(
        EVENT_ID, EvidenceKind.RISK_MATERIAL, "地缘风险监测", "2026-09-16",
        datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        {"summary": "中东航运冲突升级，能源走廊风险溢价上行", "risk": "geopolitical"},
        "UTC", macro, stream_key="geo-middle-east",
    )
    fiscal = svc.attach_evidence(
        EVENT_ID, EvidenceKind.RISK_MATERIAL, "财政信用跟踪", "2026-09-16",
        datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        {"summary": "主要经济体赤字率与利息负担抬升，主权信用边际走弱",
         "risk": "fiscal_credibility"},
        "UTC", macro, stream_key="fiscal-credibility",
    )

    return SimpleNamespace(
        svc=svc, macro=macro, decision=decision, dxy=dxy, real_rate=real_rate,
        spot=spot, futures=futures, sge=sge, etf=etf, cb=cb, geo=geo, fiscal=fiscal,
    )


def claim(local_id: str, text: str, support=(), against=(), uncertain=()) -> Claim:
    return Claim(
        local_id,
        text,
        {
            Stance.SUPPORTING: tuple(support),
            Stance.CONTRADICTING: tuple(against),
            Stance.UNCERTAINTY: tuple(uncertain),
        },
    )


def make_service(path: str = ":memory:") -> tuple[AttributionService, FakeClock]:
    clock = FakeClock()
    return AttributionService(Ledger(path), clock=clock), clock
