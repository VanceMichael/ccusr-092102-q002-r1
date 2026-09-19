# 黄金市场事件归因账本

把一次政策事件（如 2026-09-16 美联储加息 25bp）前后的**预期、实际决定、跨市场行情与研究解释**挂在同一事件下管理的后端账本。目标是让投委会看到的任何结论页面，都能还原发布当时的数据水位，并清楚标出后来的新资料改变了哪段判断。

## 设计原则

1. **会前冻结预期**：各来源（FedWatch、一级交易商调查等）的概率分布在公布前登记，到达冻结截止后整体冻结，不能补录或改动；冻结时刻不得晚于计划公布时刻。
2. **跨市场材料原样留存**：实际决定、美元/实际利率窗口、现货金、纽约期金、ETF 持仓、央行购金、地缘与财政信用风险材料同挂一个事件；记录保留**原币种、原时区（带偏移的 ISO 8601）、来源、来源版本、采集时刻**。
3. **修订只能追加**：数据商更正或官方勘误通过 `replaces_id` 形成单链（禁止分叉），旧版本永不删除；水位按序列+标签沿链折叠到最新。
4. **竞争解释并存**：不同团队可就同一事件各自创建分析，逐条判断（claims）显式关联**支持 / 相反 / 不确定**三类证据，证据引用限定在同一事件内。
5. **WORM 分析版本**：草稿仅所属团队可见；一旦发布给投委会即**不可改写**，任何更正只能基于已发布版本发起修订，生成版本链（root, seq）上的新版本。
6. **并发必须显式合并或拒绝**：草稿更新需携带 `expected_rev`；更新以原子条件 `UPDATE … WHERE rev=?` 执行，冲突返回 409 并回带当前内容，由人合并后以最新 rev 重提，系统不做静默合并，也不允许无条件覆盖。
7. **数据水位可还原**：发布时把事件全部预期、全部决定版本、各序列最新观测固化为 JSON 快照；`GET 已发布版本` 永远还原当时水位；两版本对比（`/diff/`）逐条列出判断变更，并列出水位间新增/修订的材料。
8. **不产出交易指令**：标题、正文、判断与证据备注中的交易指令性表述（买入/卖出/加仓/go long 等）一律 422 拦截；「央行购金」「储备购买」等描述性叙述不受影响。

## 数据模型（SQLite）

| 表 | 作用 |
|---|---|
| `events` | 政策事件、计划公布时刻、冻结截止、冻结标志 |
| `expectations` | 来源×结果×采集时刻的概率；`frozen_at` 非空即冻结 |
| `decisions` | 实际决定版本；`replaces_id` 构成官方勘误链 |
| `observations` | 七类跨市场序列；币种/单位/原时区/来源版本/采集时刻/`replaces_id`/附加 payload |
| `analyses` | 分析版本链：`root_id, seq`、草稿或已发布、草稿乐观锁 `rev`、发布水位 `watermark` |
| `claims` / `evidence_links` | 逐条判断及其支持/相反/不确定证据关联 |
| `audit_log` | 全部写操作留痕，支持按事件回溯 |

观测序列：`spot_gold`、`gold_futures`、`dollar_index`、`real_yield`、`etf_holdings`、`central_bank_purchase`、`risk_material`。

## 运行

```bash
pip install -e .          # 或：export PYTHONPATH=src
python3 -m gold_attribution --db data/ledger.db --port 8080
```

健康检查：`GET /health`。所有 `/api/*` 请求需携带请求头 `X-Actor`（操作人）与 `X-Team`（团队）。

## API 概览

| 方法与路径 | 说明 |
|---|---|
| `POST /api/events` | 建事件（含计划公布时刻与冻结截止） |
| `POST /api/events/{id}/expectations` | 登记来源预期（采集时刻须早于冻结截止） |
| `POST /api/events/{id}/freeze` | 会前冻结全部预期（不可逆） |
| `POST /api/events/{id}/decisions` | 录入实际决定；带 `replaces_id` 为官方勘误 |
| `POST /api/events/{id}/observations` | 录入跨市场观测；带 `replaces_id` 为数据更正 |
| `GET  /api/events/{id}/observations?series=spot_gold` | 列观测（可按序列过滤，含全部历史版本） |
| `GET  /api/events/{id}/watermark` | 当前数据水位 |
| `POST /api/events/{id}/analyses` | 建草稿（claims+三类证据关联） |
| `GET  /api/events/{id}/analyses` | 版本链列表（自动隐藏他团队草稿） |
| `GET  /api/analyses/{id}` | 取分析；已发布版本内嵌发布时水位 |
| `PUT  /api/analyses/{id}` | 更新草稿，必须带 `expected_rev`（409=并发冲突） |
| `POST /api/analyses/{id}/publish` | 发布并固化水位（前置：预期已冻结） |
| `POST /api/analyses/{id}/revise` | 基于已发布版本建下一版草稿（复制判断作起点） |
| `GET  /api/analyses/{a}/diff/{b}` | 同链两版对比：判断逐条变化 + 水位材料变化 |
| `GET  /api/audit?event_id=...` | 审计日志 |

主要错误码：422 校验失败（含朴素时间戳、交易指令）、403 非所属团队写操作、404 资源不存在或他团队草稿（防存在性泄漏）、409 已冻结/已发布不可变/乐观锁冲突（响应体回带当前版本供人工合并）。

## 端到端示例（2026-09-16 场景）

```bash
python3 -m gold_attribution.seed fixtures/fed_2026_09_16_scenario.json --db data/demo.db
```

该 fixture 覆盖：4 条会前预期（FedWatch + 交易商调查）在 13:30 冻结、14:00 加息 25bp 及官方勘误链、LBMA 瞬时低点（含数据商更正）与尾盘收复、COMEX 期金次日破 4400、DXY/TIPS 窗口、伦敦时区披露的 GLD 持仓、央行月度购金、新加坡时区地缘材料与财政信用材料，以及 macro / geopolitics 两组**彼此竞争且各自带支持/相反/不确定证据**的已发布分析。

## 测试

```bash
python3 -m pytest tests/ -q
# 或（无需第三方依赖）
python3 -m unittest discover -s tests -v
```

测试覆盖：冻结后拒写、朴素时间戳拒绝、修订链不可分叉、原币种/原时区留存、水位折叠与发布快照不可变、版本 diff、跨团队草稿隔离、乐观锁并发（含双线程恰好一成一败）、交易指令护栏、HTTP 端到端。

## 边界说明

- 服务不接入行情商 API，所有材料由请求方登记；服务负责校验、留痕、版本与可见性。
- 团队身份来自 `X-Team` 请求头（研究内网的简化模型）；跨网部署需在网关层替换为认证主体。
- 不提供、也永远不会提供自动交易指令接口。
