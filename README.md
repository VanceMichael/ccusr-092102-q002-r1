# 黄金市场事件归因账本

投委会复盘政策事件（如美联储加息 25bp 后现货金短跌重上 4300、纽约期金破 4400）用的
**后端归因账本**。它不预测、不下单，只把一次事件下的预期、实际决定、跨市场材料与
彼此竞争的研究解释，按"当时的数据水位"钉成可审计、可还原、可对比的版本记录。

## 核心不变量

1. **公布前冻结**：事件 `open → frozen → decided`。冻结时刻 `frozen_at` 一旦写入不可
   重置；冻结后补录/修改任何来源的预期概率或采集时刻都会被拒绝并写入 `rejected_writes`。
2. **只追加（WORM）**：证据、分析修订、发布记录、审计日志全部 INSERT-only。
   - 来源更正不是覆盖：显式传 `revision_of_version` + `revision_note`，挂成同链新版本
     （`supersedes_id` 指回旧版）；次日新观察默认是独立链，不被误当成更正。
   - 已发投委会的版本永不改写；更正只能形成新修订并再次发布，旧发布版派生为
     `superseded`，原文随时可原样取回。每个修订带 `doc_hash`，读取时重算校验，
     绕过应用直接改库会在读取时报错；审计日志另有 SHA-256 哈希链。
3. **原币种、原时区、来源版本**：所有市场时间必须携带来源原始 UTC 偏移（naive
   datetime 直接拒绝），跨市场价格保留 ISO 4217 币种（LBMA=USD、SGE=CNY 各存各的），
   窗口时间同样保留原时区；内部排序只用 UTC 的入账时刻 `recorded_at`。
4. **竞争解释**：每个分析系列（团队归属）包含多条主张，每条主张按
   **支持 / 相反 / 不确定**三种立场钉到具体的证据**版本 id**；系列间可显式声明竞争。
5. **并发显式合并或拒绝**：保存草稿带 `base_revision` 乐观锁；落后于 head 时抛
   `ConflictError`，必须三选一：`ours`（己方覆盖，留痕）、`theirs`（拒绝己方，不留修订）、
   `merge`（逐主张 ours/theirs/merged/dropped，必须覆盖双方每个 local_id）。
6. **草稿权限**：未发布草稿只有所属团队能读、能写、能发布；其他团队只能看到已发布版本。
7. **数据水位还原**：每个修订保存时钉住 `data_watermark_at`。结论页返回该水位内可见的
   全部证据、水位后才到达的材料、以及每条被引证据"引用版本是否已被来源修订、修订是否
   晚于水位"。`diff_revisions` 逐主张显示新增/删除/文本变更/立场证据增减，并把水位窗口
   内的新资料映射到"它实际改变了哪条主张"，新资料未被引用也单列。
8. **禁止自动交易指令**：服务 API 不存在任何下单/执行方法；标题、摘要、主张、证据备注
   与 payload 文本字段经中英文双层启发式扫描（指令式/点位/数量/下单 API 硬命中；裸
   "买入黄金/buy gold" 拦截但"央行买入黄金/central banks bought"等事实陈述放行），
   命中即拒绝并独立事务留痕。

## 模块

- `src/gold_attribution/models.py`：枚举与 dataclass、时区/币种/概率校验、领域异常。
- `src/gold_attribution/storage.py`：SQLite schema 与只追加仓储（WAL、外键、
  `BEGIN IMMEDIATE` 写事务）。
- `src/gold_attribution/service.py`：`AttributionService` 领域服务（生命周期、证据修订链、
  草稿/乐观锁/发布、权限、水位视图、版本 diff、哈希链审计）。
- `src/gold_attribution/guard.py`：交易指令护栏。

## 事件与证据类型

事件：`open → frozen → decided`（可选 `locked`）。

证据：`policy_decision`（实际决定）、`usd_window`、`real_rate_window`、`spot_gold`、
`futures_gold`、`etf_holding`、`central_bank_buy`、`risk_material`（地缘/财政信用）；
公布前预期走独立的 `expectations` 表。价格类必须带币种，窗口字段必须带原时区 ISO 时间。

## 最小用法

```python
from datetime import datetime, timezone, timedelta
from src.gold_attribution.storage import Ledger
from src.gold_attribution.service import AttributionService, Actor
from src.gold_attribution.models import EvidenceKind, Claim, Stance

svc = AttributionService(Ledger("attribution.db"))
ny = timezone(timedelta(hours=-4))
alice = Actor(team="macro", user="alice")

svc.create_event("fed-2026-09-16", "9月议息", datetime(2026,9,16,14,tzinfo=ny), alice)
svc.record_expectation("fed-2026-09-16", "FedWatch", "v1",
                       datetime(2026,9,16,9,tzinfo=timezone.utc),
                       {"25bp": 0.97, "hold": 0.03}, alice)
svc.freeze_expectations("fed-2026-09-16", datetime(2026,9,16,14,tzinfo=ny), alice)

decision = svc.record_decision("fed-2026-09-16", "FOMC声明", "stmt-v1",
    datetime(2026,9,16,14,tzinfo=ny), {"basis_points": 25, "vote": "12-0"},
    "America/New_York", alice)
spot = svc.attach_evidence("fed-2026-09-16", EvidenceKind.SPOT_GOLD, "LBMA", "PM-fix",
    datetime(2026,9,16,15,tzinfo=ny), {"price": 4305.2},
    "America/New_York", alice, currency="USD")

svc.create_analysis_series("rates", "fed-2026-09-16", "macro", "利率解释", alice)
svc.save_draft("rates", "利率单因解释", "25bp已被提前定价",
    [Claim("c1", "加息充分定价，短跌后修复", {Stance.SUPPORTING: (decision, spot)})],
    alice, base_revision=0)
svc.publish("rates", alice)
page = svc.get_revision("rates", 1, alice)      # 结论页：按发布水位还原
```

来源事后更正（旧版不覆盖）：

```python
svc.attach_evidence(..., source_version="PM-fix-REV1",
                    revision_of_version="PM-fix",
                    revision_note="定盘价事后下修0.30美元", ...)
```

并发冲突：

```python
try:
    svc.save_draft("rates", ..., base_revision=1)
except ConflictError as e:  # head 已被同队同事推进
    svc.resolve_conflict("rates", ..., base_revision=1, choice="merge",
                         claim_resolution={"c1": "merged", "c2": "theirs"})
```

## 运行检查

```bash
python3 -m unittest discover -s tests -v
```

39 个用例覆盖：冻结时序与拒绝留痕、原币种/原时区、显式修订链与次日独立观察、
草稿权限、WORM 发布与库层篡改检测、并发 ours/theirs/merge、水位还原、
新资料→主张变更 diff、交易指令拦截与事实陈述放行、审计哈希链与文件库持久化重开。

健康检查服务仍可启动：

```bash
python3 -m src.gold_attribution  # GET /health
```
