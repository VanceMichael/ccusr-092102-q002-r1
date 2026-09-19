"""SQLite 只追加账本。

持久化约定：
- 证据（evidence）、分析修订（analysis_revisions/claims/...）、审计日志
  一律 INSERT-only，不提供 UPDATE/DELETE 路径；旧版本通过修订指针回溯。
- 事件行（events）的状态字段会随生命周期推进（open→frozen→decided），
  每次状态跃迁都写入带哈希链的 audit_log，冻结时刻不可被重置。
- 时间统一以 ISO 字符串存储：市场时间保留来源原始偏移；
  recorded_at / 审计时间使用 UTC。
- 并发控制：analysis_series.head_revision 作为草稿的乐观锁版本号。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    event_id               TEXT PRIMARY KEY,
    title                  TEXT NOT NULL,
    state                  TEXT NOT NULL,
    decision_due_at        TEXT,
    expectation_frozen_at  TEXT,
    decided_at             TEXT,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expectations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL REFERENCES events(event_id),
    source          TEXT NOT NULL,
    source_version  TEXT NOT NULL,
    collected_at    TEXT NOT NULL,
    probabilities   TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    recorded_at     TEXT NOT NULL,
    UNIQUE(event_id, source, source_version)
);

CREATE TABLE IF NOT EXISTS evidence_chains (
    chain_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL REFERENCES events(event_id),
    kind       TEXT NOT NULL,
    source     TEXT NOT NULL,
    stream_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(event_id, kind, source, stream_key)
);

CREATE TABLE IF NOT EXISTS evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL REFERENCES events(event_id),
    chain_id        INTEGER NOT NULL REFERENCES evidence_chains(chain_id),
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    source          TEXT NOT NULL,
    stream_key      TEXT NOT NULL,
    source_version  TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    currency        TEXT,
    market_tz       TEXT NOT NULL,
    payload         TEXT NOT NULL,
    revision_note   TEXT NOT NULL DEFAULT '',
    supersedes_id   INTEGER REFERENCES evidence(id),
    UNIQUE(chain_id, seq),
    UNIQUE(chain_id, source_version)
);
CREATE INDEX IF NOT EXISTS idx_evidence_event ON evidence(event_id, recorded_at);

CREATE TABLE IF NOT EXISTS analysis_series (
    series_id           TEXT PRIMARY KEY,
    event_id            TEXT NOT NULL REFERENCES events(event_id),
    owner_team          TEXT NOT NULL,
    title               TEXT NOT NULL,
    head_revision       INTEGER NOT NULL DEFAULT 0,
    published_revision  INTEGER,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_revisions (
    series_id         TEXT NOT NULL REFERENCES analysis_series(series_id),
    revision          INTEGER NOT NULL,
    event_id          TEXT NOT NULL REFERENCES events(event_id),
    owner_team        TEXT NOT NULL,
    author            TEXT NOT NULL,
    status            TEXT NOT NULL,
    base_revision     INTEGER,
    title             TEXT NOT NULL,
    summary           TEXT NOT NULL,
    competing_series  TEXT NOT NULL DEFAULT '[]',
    data_watermark_at TEXT,
    doc_hash          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (series_id, revision)
);

CREATE TABLE IF NOT EXISTS claims (
    series_id  TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    local_id   TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    PRIMARY KEY (series_id, revision, local_id),
    FOREIGN KEY (series_id, revision) REFERENCES analysis_revisions(series_id, revision)
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    series_id   TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    local_id    TEXT NOT NULL,
    stance      TEXT NOT NULL,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    PRIMARY KEY (series_id, revision, local_id, stance, evidence_id),
    FOREIGN KEY (series_id, revision, local_id) REFERENCES claims(series_id, revision, local_id)
);

CREATE TABLE IF NOT EXISTS publications (
    series_id       TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    published_at    TEXT NOT NULL,
    actor_team      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    supersedes_rev  INTEGER,
    PRIMARY KEY (series_id, revision),
    FOREIGN KEY (series_id, revision) REFERENCES analysis_revisions(series_id, revision)
);

-- 只追加审计日志，附加哈希链以防静默篡改
CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor_team TEXT,
    actor      TEXT,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}',
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_writes (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor_team TEXT,
    actor      TEXT,
    reason     TEXT NOT NULL,
    content    TEXT NOT NULL
);
"""


class Ledger:
    """SQLite 仓储。所有写操作在调用方给定的事务内完成。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务：BEGIN IMMEDIATE 让乐观锁冲突在提交前显式暴露。"""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # ---------- 事件 ----------

    def insert_event(self, c: sqlite3.Connection, row: dict[str, Any]) -> None:
        c.execute(
            """INSERT INTO events(event_id, title, state, decision_due_at,
                                  expectation_frozen_at, decided_at, created_at)
               VALUES(:event_id,:title,:state,:decision_due_at,
                      :expectation_frozen_at,:decided_at,:created_at)""",
            row,
        )

    def get_event_row(self, c: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()

    def update_event_state(
        self,
        c: sqlite3.Connection,
        event_id: str,
        *,
        state: str,
        expectation_frozen_at: str | None = None,
        decided_at: str | None = None,
    ) -> None:
        c.execute(
            """UPDATE events SET state=:state,
                   expectation_frozen_at=COALESCE(:frozen, expectation_frozen_at),
                   decided_at=COALESCE(:decided, decided_at)
               WHERE event_id=:id""",
            {"state": state, "frozen": expectation_frozen_at, "decided": decided_at, "id": event_id},
        )

    # ---------- 预期 ----------

    def insert_expectation(self, c: sqlite3.Connection, row: dict[str, Any]) -> int:
        cur = c.execute(
            """INSERT INTO expectations(event_id, source, source_version,
                                        collected_at, probabilities, note, recorded_at)
               VALUES(:event_id,:source,:source_version,:collected_at,
                      :probabilities,:note,:recorded_at)""",
            row,
        )
        return int(cur.lastrowid)

    def list_expectations(self, c: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM expectations WHERE event_id=? ORDER BY recorded_at, id", (event_id,)
        ).fetchall()

    # ---------- 证据 ----------

    def get_chain(
        self,
        c: sqlite3.Connection,
        event_id: str,
        kind: str,
        source: str,
        stream_key: str = "",
    ) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM evidence_chains WHERE event_id=? AND kind=? AND source=? AND stream_key=?",
            (event_id, kind, source, stream_key),
        ).fetchone()

    def insert_chain(self, c: sqlite3.Connection, row: dict[str, Any]) -> int:
        cur = c.execute(
            """INSERT INTO evidence_chains(event_id, kind, source, stream_key, created_at)
               VALUES(:event_id,:kind,:source,:stream_key,:created_at)""",
            row,
        )
        return int(cur.lastrowid)

    def latest_in_chain(self, c: sqlite3.Connection, chain_id: int) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM evidence WHERE chain_id=? ORDER BY seq DESC, id DESC LIMIT 1",
            (chain_id,),
        ).fetchone()

    def find_chain(
        self, c: sqlite3.Connection, event_id: str, kind: str, source: str
    ) -> sqlite3.Row | None:
        return self.get_chain(c, event_id, kind, source, "")

    def insert_evidence(self, c: sqlite3.Connection, row: dict[str, Any]) -> int:
        cur = c.execute(
            """INSERT INTO evidence(event_id, chain_id, seq, kind, source, stream_key,
                                    source_version, observed_at, recorded_at, currency, market_tz,
                                    payload, revision_note, supersedes_id)
               VALUES(:event_id,:chain_id,:seq,:kind,:source,:stream_key,:source_version,
                      :observed_at,:recorded_at,:currency,:market_tz,
                      :payload,:revision_note,:supersedes_id)""",
            row,
        )
        return int(cur.lastrowid)

    def get_evidence(self, c: sqlite3.Connection, evidence_id: int) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()

    def list_evidence_for_event(
        self, c: sqlite3.Connection, event_id: str, *, as_of_utc: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM evidence WHERE event_id=?"
        params: list[Any] = [event_id]
        if as_of_utc is not None:
            sql += " AND recorded_at<=?"
            params.append(as_of_utc)
        sql += " ORDER BY recorded_at, id"
        return c.execute(sql, params).fetchall()

    def list_chains(self, c: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
        return c.execute(
            """SELECT e.* FROM evidence e
               JOIN (SELECT chain_id, MAX(seq) AS mseq FROM evidence
                     WHERE event_id=? GROUP BY chain_id) h
               ON e.chain_id=h.chain_id AND e.seq=h.mseq
               ORDER BY e.recorded_at, e.id""",
            (event_id,),
        ).fetchall()

    # ---------- 分析系列 / 修订 ----------

    def insert_series(self, c: sqlite3.Connection, row: dict[str, Any]) -> None:
        c.execute(
            """INSERT INTO analysis_series(series_id, event_id, owner_team, title,
                                           head_revision, published_revision, created_at)
               VALUES(:series_id,:event_id,:owner_team,:title,0,NULL,:created_at)""",
            row,
        )

    def get_series(self, c: sqlite3.Connection, series_id: str) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM analysis_series WHERE series_id=?", (series_id,)
        ).fetchone()

    def list_series_for_event(self, c: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM analysis_series WHERE event_id=? ORDER BY created_at, series_id",
            (event_id,),
        ).fetchall()

    def bump_head(self, c: sqlite3.Connection, series_id: str, revision: int) -> None:
        c.execute(
            "UPDATE analysis_series SET head_revision=? WHERE series_id=?",
            (revision, series_id),
        )

    def mark_published(self, c: sqlite3.Connection, series_id: str, revision: int) -> None:
        c.execute(
            "UPDATE analysis_series SET published_revision=? WHERE series_id=?",
            (revision, series_id),
        )

    def insert_publication(self, c: sqlite3.Connection, row: dict[str, Any]) -> None:
        c.execute(
            """INSERT INTO publications(series_id, revision, published_at,
                                        actor_team, actor, supersedes_rev)
               VALUES(:series_id,:revision,:published_at,:actor_team,:actor,:supersedes_rev)""",
            row,
        )

    def latest_publication(
        self, c: sqlite3.Connection, series_id: str
    ) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM publications WHERE series_id=? ORDER BY revision DESC LIMIT 1",
            (series_id,),
        ).fetchone()

    def list_publications(self, c: sqlite3.Connection, series_id: str) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM publications WHERE series_id=? ORDER BY revision", (series_id,)
        ).fetchall()

    def insert_revision(self, c: sqlite3.Connection, row: dict[str, Any]) -> None:
        c.execute(
            """INSERT INTO analysis_revisions(series_id, revision, event_id, owner_team,
                    author, status, base_revision, title, summary, competing_series,
                    data_watermark_at, doc_hash, created_at)
               VALUES(:series_id,:revision,:event_id,:owner_team,:author,:status,
                      :base_revision,:title,:summary,:competing_series,
                      :data_watermark_at,:doc_hash,:created_at)""",
            row,
        )

    def get_revision(
        self, c: sqlite3.Connection, series_id: str, revision: int
    ) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM analysis_revisions WHERE series_id=? AND revision=?",
            (series_id, revision),
        ).fetchone()

    def list_claims(
        self, c: sqlite3.Connection, series_id: str, revision: int
    ) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM claims WHERE series_id=? AND revision=? ORDER BY ordinal",
            (series_id, revision),
        ).fetchall()

    def list_claim_evidence(
        self, c: sqlite3.Connection, series_id: str, revision: int
    ) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM claim_evidence WHERE series_id=? AND revision=?",
            (series_id, revision),
        ).fetchall()

    def insert_claim(self, c: sqlite3.Connection, row: dict[str, Any]) -> None:
        c.execute(
            """INSERT INTO claims(series_id, revision, local_id, ordinal, text)
               VALUES(:series_id,:revision,:local_id,:ordinal,:text)""",
            row,
        )

    def insert_claim_evidence(self, c: sqlite3.Connection, row: dict[str, Any]) -> None:
        c.execute(
            """INSERT INTO claim_evidence(series_id, revision, local_id, stance, evidence_id)
               VALUES(:series_id,:revision,:local_id,:stance,:evidence_id)""",
            row,
        )

    # ---------- 审计 / 拒绝留痕 ----------

    def last_audit_hash(self, c: sqlite3.Connection) -> str:
        row = c.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        return row["hash"] if row else "GENESIS"

    def insert_audit(self, c: sqlite3.Connection, row: dict[str, Any]) -> int:
        cur = c.execute(
            """INSERT INTO audit_log(at, actor_team, actor, action, entity, entity_id,
                                     details, prev_hash, hash)
               VALUES(:at,:actor_team,:actor,:action,:entity,:entity_id,
                      :details,:prev_hash,:hash)""",
            row,
        )
        return int(cur.lastrowid)

    def list_audit(self, c: sqlite3.Connection) -> list[sqlite3.Row]:
        return c.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()

    def insert_rejected(self, c: sqlite3.Connection, row: dict[str, Any]) -> None:
        c.execute(
            """INSERT INTO rejected_writes(at, actor_team, actor, reason, content)
               VALUES(:at,:actor_team,:actor,:reason,:content)""",
            row,
        )

    def list_rejected(self, c: sqlite3.Connection) -> list[sqlite3.Row]:
        return c.execute("SELECT * FROM rejected_writes ORDER BY seq").fetchall()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
