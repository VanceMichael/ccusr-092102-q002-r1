"""交易指令护栏。

本系统是归因账本，不提供任何下单/调仓接口；写入账本的自由文本（标题、
摘要、主张、证据备注、payload 文本字段）还要经过两层保守扫描：

第一层（硬性命中，必定拦截）：
- 指令式标记 + 交易动词：建议/应当/立即/recommend/should/trade idea …
- 风控/点位语言：止损、止盈、目标价、stop-loss、target $4500 …
- 数量语言 + 工具：N contracts/lots of gold …
- 下单 API 语言：execute/place/cancel order、执行/撤销订单 …

第二层（裸“动词+品种”）：缺少指令标记时，只有当动词前后的语境里**没有**
描述性主语（央行/官方/ETF/基金/投资者/资金/central bank/investor…）或
英文过去式时才放行——因为归因研究需要陈述“央行买了黄金”这类事实，
而裸“买入黄金 / buy gold”在给投委会的结论里属于指令式表述，拦截。

这是启发式防线，宁可误拦也不漏放；被拦内容写入 rejected_writes 留痕。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------------------------------------------------------ 参数

_ZH_DIRECTIVE = (
    r"建议|建議|推荐|推薦|应当|應該|应|須|务必|務必|立即|立刻|马上|馬上|"
    r"即刻|赶紧|趕緊|抓紧|抓緊|考虑|考慮|不妨|操作建议|操作建議|交易建议|交易建議|"
    r"策略上|观点上|觀點上"
)
_ZH_VERB = (
    r"买入|買入|卖出|賣出|加仓|加倉|减仓|減倉|清仓|清倉|平仓|平倉|"
    r"建仓|建倉|做多|做空|抄底|逃顶|逃頂|增持|減持|入手|出手"
)
_ZH_TARGET = (
    r"黄金|黃金|金价|金價|现货金|現貨金|期金|金矿股|金礦股|"
    r"黄金ETF|黃金ETF|GLD|XAU(?:USD)?|多单|多單|空单|空單|仓位|倉位"
)
_ZH_SUBJECT = (
    r"央行|中央银行|中央銀行|官方|储备|儲備|各国|各國|新兴市场|新興市場|"
    r"ETF|基金|投资者|投資者|投机者|投機者|资金|資金|机构|機構|银行|銀行|"
    r"交易员|交易員|散户|散戶|他们|他們|其|报告|報告|数据|數據|显示|顯示"
)

_EN_DIRECTIVE = (
    r"recommend(?:ed|s)?|suggest(?:ed|s)?|should|must|ought\s+to|had\s+better|"
    r"trade\s+idea|trading\s+idea|action(?:able)?\s*[:\-]|consider(?:ing)?|"
    r"we\s+(?:think|believe|view)\s+it\s+is\s+time|it\s+is\s+time\s+to"
)
_EN_VERB_BASE = (
    r"buy(?:ing)?|sell(?:ing)?|short(?:ing)?|accumulat(?:e|ing)|add(?:ing)?|"
    r"trim(?:ming)?|reduc(?:e|ing)|exit(?:ing)?|liquidat(?:e|ing)|unwind(?:ing)?|"
    r"cover(?:ing)?|enter(?:ing)?|position(?:ing)?|go(?:ing)?\s+long|"
    r"go(?:ing)?\s+short|put(?:ting)?\s+on\s+a"
)
_EN_VERB_PAST = (
    r"bought|sold|shorted|accumulated|added|trimmed|reduced|exited|"
    r"liquidated|unwound|covered|entered|positioned|went\s+long|went\s+short"
)
_EN_TARGET = (
    r"gold|xau(?:usd)?|spot\s+gold|gold\s+futures|comex\s+gold|\bgld\b|"
    r"gold\s+etf|gold\s+position|a\s+position"
)
_EN_SUBJECT = (
    r"central\s+banks?|monetary\s+authorit(?:y|ies)|official\s+sector|"
    r"reserve\s+managers?|pbo[cc]|norges\s+bank|etf\s+(?:in|out)?flows?|"
    r"\betfs?\b|funds?|investors?|speculators?|managers?|traders?|"
    r"they|inflows?|outflows?|officials?|reported(?:ly)?|according\s+to"
)

_ZH_HARD_RULES: tuple[tuple[str, str], ...] = (
    # 指令标记 + 交易动词（标记与动词间隔很小）
    ("zh-directive-verb", rf"(?:{_ZH_DIRECTIVE})[^。；;\n]{{0,12}}(?:{_ZH_VERB})"),
    # 点位/风控语言
    ("zh-risk-level", r"(?:止损|止損|止盈|目标价|目標價|目标位|目標位)\s*[0-9]"),
    (r"zh-risk-level-2", r"[0-9][0-9,.]*\s*(?:点止损|點止損|止盈|止損|止损)"),
    # 下单 API
    ("zh-order-api", r"(?:执行|執行|提交|撤销|撤銷|下达|下達|挂出|掛出)\s{0,4}(?:订单|訂單|交易指令|挂单|掛單|市价单|市價單)"),
)

_EN_HARD_RULES: tuple[tuple[str, str], ...] = (
    ("en-directive-verb",
     rf"(?:{_EN_DIRECTIVE})\W{{0,24}}(?:{_EN_VERB_BASE})\W{{0,10}}(?:{_EN_TARGET})"),
    ("en-risk-level",
     r"(?:stop\s*[-–—]?\s*loss|take\s*[-–—]?\s*profit|SL/TP|target(?:ing)?\s+(?:a\s+)?\$?\d)"),
    ("en-qty",
     rf"\d[\d,]*\.?\d*\s*(?:contracts?|lots?|oz|ounces?)\W{{0,12}}(?:{_EN_TARGET})"),
    ("en-order-api",
     r"(?:execute|place|submit|cancel|fill)\s+[a-z\s]{0,12}(?:order|trade|fill)"),
)

_ZH_BARE = re.compile(rf"(?P<verb>{_ZH_VERB}).{{0,6}}?(?P<target>{_ZH_TARGET})", re.IGNORECASE)
_EN_BARE = re.compile(rf"(?<![A-Za-z])(?P<verb>{_EN_VERB_BASE})\W{{0,6}}(?P<target>{_EN_TARGET})",
                      re.IGNORECASE)
_EN_PAST_BARE = re.compile(rf"(?<![A-Za-z])(?:{_EN_VERB_PAST})\W{{0,6}}(?:{_EN_TARGET})",
                           re.IGNORECASE)
_ZH_DIRECTIVE_RE = re.compile(_ZH_DIRECTIVE)
_ZH_SUBJECT_RE = re.compile(_ZH_SUBJECT)
_EN_DIRECTIVE_RE = re.compile(_EN_DIRECTIVE, re.IGNORECASE)
_EN_SUBJECT_RE = re.compile(_EN_SUBJECT, re.IGNORECASE)
_HARD = tuple(
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in (*_ZH_HARD_RULES, *_EN_HARD_RULES)
)


@dataclass(frozen=True)
class GuardHit:
    field_name: str
    pattern: str
    excerpt: str


def _excerpt(text: str, start: int, end: int) -> str:
    return text[max(0, start - 16):min(len(text), end + 16)]


def scan_text(text: str, field_name: str = "text") -> list[GuardHit]:
    """扫描一段文本，返回命中项（去重）。空/非字符串返回空。"""
    if not isinstance(text, str) or not text:
        return []
    hits: list[GuardHit] = []
    seen: set[str] = set()

    for name, pattern in _HARD:
        m = pattern.search(text)
        if m and name not in seen:
            seen.add(name)
            hits.append(GuardHit(field_name, name, _excerpt(text, m.start(), m.end())))

    # 中文裸“动词+品种”：语境中出现指令标记→硬规则已覆盖；
    # 出现描述性主语则视为事实陈述放行，否则拦截。
    for m in _ZH_BARE.finditer(text):
        prefix = text[max(0, m.start() - 24):m.start()]
        window = text[max(0, m.start() - 24):m.end()]
        if _ZH_SUBJECT_RE.search(window) and not _ZH_DIRECTIVE_RE.search(window):
            continue
        if "zh-bare" not in seen:
            seen.add("zh-bare")
            hits.append(GuardHit(field_name, "zh-bare", _excerpt(text, m.start(), m.end())))

    # 英文裸“动词+品种”：过去式或描述性主语放行
    for m in _EN_BARE.finditer(text):
        window = text[max(0, m.start() - 32):m.end()]
        if _EN_PAST_BARE.search(text[max(0, m.start() - 2):m.end() + 2]):
            continue
        if _EN_SUBJECT_RE.search(window) and not _EN_DIRECTIVE_RE.search(window):
            continue
        if "en-bare" not in seen:
            seen.add("en-bare")
            hits.append(GuardHit(field_name, "en-bare", _excerpt(text, m.start(), m.end())))

    return hits


def assert_no_trading_instruction(*fields: tuple[str, str]) -> None:
    """校验若干 (字段名, 文本)，命中即抛 ValidationError。"""
    from .models import ValidationError

    all_hits = [hit for name, text in fields for hit in scan_text(text, name)]
    if all_hits:
        detail = "; ".join(f"[{h.field_name}:{h.pattern}] …{h.excerpt}…" for h in all_hits)
        raise ValidationError(
            "检测到交易指令式表述：归因账本不得产出或存储自动交易指令，"
            f"请改写为因果/事实陈述。命中：{detail}"
        )
