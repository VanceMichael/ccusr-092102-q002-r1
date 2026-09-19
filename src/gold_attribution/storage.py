"""归因账本的持久化层。

只依赖标准库（sqlite3）。所有跨市场记录均保留：

* 原币种 / 原单位 / 原时区（时间戳以 ISO 8601 带偏移量存储）；
* 来源标识、来源版本、采集时刻；
* 修订链：后来的更正只能追加新记录，并通过 ``replaces_id`` 指向被替代的记录。

分析报告采用 WORM（Write Once Read Many）语义：版本一经发布即不可修改，
更正只能形成新的分析版本；草稿仅所属团队可见。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class Storage:
    """封装 SQLite 连接与建表/查询辅助。"""

    def __init__(self, path: str | Path = ":memory:", *, initialize: bool = True) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        # 内存库始终自建表；文件库由首个连接建表，后续请求连接只设 PRAGMA。
        if initialize or self.path == ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self._migrate()

    # -- 基础辅助 -----------------------------------------------------------

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- 政策事件（如 2026-09-16 美联储议息），一切材料挂在其下 ----------
            CREATE TABLE IF NOT EXISTS events (
                event_id           TEXT PRIMARY KEY,
                title              TEXT NOT NULL,
                policy_body        TEXT NOT NULL,          -- 例如 Federal Reserve
                scheduled_at       TEXT NOT NULL,          -- 原时区 ISO 8601
                freeze_at          TEXT NOT NULL,          -- 预期冻结截止（原时区）
                expectations_frozen INTEGER NOT NULL DEFAULT 0,
                created_at         TEXT NOT NULL,
                created_by         TEXT NOT NULL
            );

            -- 政策公布前登记、到期冻结的各来源预期概率 -------------------------
            CREATE TABLE IF NOT EXISTS expectations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT NOT NULL REFERENCES events(event_id),
                source           TEXT NOT NULL,           -- Fedwatch / 投行调查 ...
                source_version   TEXT,
                outcome          TEXT NOT NULL,           -- hike25 / hold / cut25 ...
                probability      REAL NOT NULL,           -- 0..1
                collected_at     TEXT NOT NULL,           -- 采集时刻（原时区）
                frozen_at        TEXT,                    -- 冻结时刻；NULL=未冻结
                UNIQUE(event_id, source, outcome, collected_at)
            );

            -- 实际政策决定：来源修订时追加新版本，旧版本保留 -------------------
            CREATE TABLE IF NOT EXISTS decisions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT NOT NULL REFERENCES events(event_id),
                basis_points     INTEGER NOT NULL,
                action           TEXT NOT NULL,           -- hike / hold / cut
                statement_summary TEXT,
                decided_at       TEXT NOT NULL,           -- 决定公布时刻（原时区）
                source           TEXT NOT NULL,
                source_version   TEXT NOT NULL,
                recorded_at      TEXT NOT NULL,
                replaces_id      INTEGER REFERENCES decisions(id),
                recorded_by      TEXT NOT NULL
            );

            -- 跨市场观测：现货/期货、美元、实际利率、ETF 持仓、官方购金、风险材料
            CREATE TABLE IF NOT EXISTS observations (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT NOT NULL REFERENCES events(event_id),
                series           TEXT NOT NULL,
                -- spot_gold / gold_futures / dollar_index / real_yield /
                -- etf_holdings / central_bank_purchase / risk_material
                label            TEXT NOT NULL,
                value            REAL,
                currency         TEXT,                    -- 原币种（NULL=无量纲）
                unit             TEXT,                    -- ounce / tonne / bps ...
                market_tz        TEXT,                    -- 原时区（America/New_York ...）
                observed_at      TEXT NOT NULL,           -- 行情/材料对应时刻（原时区）
                source           TEXT NOT NULL,
                source_version   TEXT NOT NULL,
                collected_at     TEXT NOT NULL,           -- 采集时刻（原时区）
                replaces_id      INTEGER REFERENCES observations(id),
                payload          TEXT NOT NULL DEFAULT '{}',  -- 附加结构化字段
                recorded_by      TEXT NOT NULL
            );

            -- 分析报告：同一标题下形成版本链；草稿仅本团队，发布后不可变 --------
            CREATE TABLE IF NOT EXISTS analyses (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id         TEXT NOT NULL REFERENCES events(event_id),
                revision_of_id   INTEGER REFERENCES analyses(id),  -- 修订的旧版本
                root_id          INTEGER NOT NULL,        -- 版本链根（首版=自身 id）
                seq              INTEGER NOT NULL,        -- 链内序号，从 1 起
                title            TEXT NOT NULL,
                body             TEXT NOT NULL,
                owner_team       TEXT NOT NULL,
                status           TEXT NOT NULL CHECK (status IN ('draft','published')),
                base_version     INTEGER,                 -- 编辑所依据的版本（乐观锁）
                rev              INTEGER NOT NULL DEFAULT 1,  -- 草稿修订号（乐观锁）
                watermark        TEXT,                    -- 发布时的数据水位快照
                created_at       TEXT NOT NULL,
                created_by       TEXT NOT NULL,
                published_at     TEXT,
                UNIQUE(root_id, seq)
            );

            -- 分析中的逐条判断 ------------------------------------------------
            CREATE TABLE IF NOT EXISTS claims (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id      INTEGER NOT NULL REFERENCES analyses(id),
                position         INTEGER NOT NULL,
                text             TEXT NOT NULL
            );

            -- 判断与证据的关联：supporting / contradicting / uncertainty --------
            CREATE TABLE IF NOT EXISTS evidence_links (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id         INTEGER NOT NULL REFERENCES claims(id),
                kind             TEXT NOT NULL
                                 CHECK (kind IN ('supporting','contradicting','uncertainty')),
                target_type      TEXT NOT NULL
                                 CHECK (target_type IN ('observation','decision','expectation')),
                target_id        INTEGER NOT NULL,
                note             TEXT
            );

            -- 全部写操作的审计日志 -------------------------------------------
            CREATE TABLE IF NOT EXISTS audit_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,
                actor            TEXT NOT NULL,
                team             TEXT,
                action           TEXT NOT NULL,
                entity           TEXT NOT NULL,
                entity_id        TEXT,
                detail           TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        self.conn.commit()

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        self.conn.close()


def utcnow_iso() -> str:
    """统一的审计时间戳（UTC，带偏移量）。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
