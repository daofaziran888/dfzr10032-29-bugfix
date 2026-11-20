"""SQLite 访问层：迁移库的建表、连接与仓储方法。

库文件路径由 app.config.settings.migration_db 给出，进程重启后仍能读出同一份
已确认数据。所有写方法自行提交；跨多步的业务事务由 service 层用一次连接完成，
避免在切换窗口里把已确认的修改覆盖回初始种子。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings

SCHEMA_VERSION = 1


def utc_now() -> str:
    """统一的 UTC 时间戳，便于重启后按时间还原事件顺序。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开迁移库连接：外键约束开启、行工厂为字典、WAL 降低并发读写冲突。"""
    path = Path(db_path or settings.migration_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 源端记录：从内存样本表 datasource 迁移，之后只在缺号时插入，不覆盖已确认修改
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT NOT NULL UNIQUE,          -- 原源端编号（兼容窗口内可查）
    legacy_id INTEGER,                          -- 内存样本表里的原 id
    connection_type TEXT,
    auth_mode TEXT,
    sample_cycle TEXT,
    timeout_seconds TEXT,
    retry_limit TEXT,
    owner TEXT,
    current_address_version INTEGER NOT NULL DEFAULT 1,
    confirmed_at TEXT,                          -- 当前地址最近一次确认时间
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 接入地址版本：每次地址更正生成新版本；计划/采样快照的是确认时的版本
CREATE TABLE IF NOT EXISTS source_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_code TEXT NOT NULL,
    version INTEGER NOT NULL,
    address TEXT NOT NULL,
    change_note TEXT,
    confirmed_at TEXT NOT NULL,
    UNIQUE(source_code, version),
    FOREIGN KEY(source_code) REFERENCES sources(source_code)
);

-- 采集登记迁移对账数据集：每条旧计划一条对账记录
CREATE TABLE IF NOT EXISTS reconciliation_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_version INTEGER NOT NULL,
    plan_code TEXT NOT NULL,                    -- 原计划编号（兼容窗口内可查）
    legacy_id INTEGER,
    related_task TEXT NOT NULL,                 -- 旧表“关联任务”原文
    schedule_cycle TEXT,
    start_at TEXT,
    end_at TEXT,
    legacy_status TEXT,                         -- 原状态快照（兼容窗口内可查）
    binding_status TEXT NOT NULL,              -- LANDED 已落库 / REVIEW 待核对
    binding_method TEXT,                        -- AUTO_EXACT / EXPLICIT / NONE
    bound_source_code TEXT,
    bound_address_version INTEGER,
    review_reason TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0,       -- 已确认的绑定不允许重建覆盖
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dataset_version, plan_code)
);

-- 明确的任务与来源对照：旧记录无法直接匹配时由值班员提供
CREATE TABLE IF NOT EXISTS task_source_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    related_task TEXT NOT NULL UNIQUE,
    source_code TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(source_code) REFERENCES sources(source_code)
);

-- 可核对的待执行计划：只从对账数据集中的有效绑定（LANDED、已确认）生成
CREATE TABLE IF NOT EXISTS dispatch_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_code TEXT NOT NULL UNIQUE,
    source_code TEXT NOT NULL,
    address_version INTEGER NOT NULL,
    address_snapshot TEXT NOT NULL,             -- 生成时确认的地址，之后不变
    schedule_cycle TEXT,
    related_task TEXT,
    origin TEXT NOT NULL,                       -- MIGRATION 迁移生成 / INTAKE 新送达
    status TEXT NOT NULL,                       -- PENDING 待执行 / CLAIMED 已领取 / CLOSED
    accepted INTEGER NOT NULL DEFAULT 1,
    acceptance_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_code) REFERENCES sources(source_code)
);

-- 采样领取记录：记录领取时使用的是哪一次确认的地址
CREATE TABLE IF NOT EXISTS sampling_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id INTEGER NOT NULL,
    operator TEXT NOT NULL,
    address_version INTEGER NOT NULL,
    address_used TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    FOREIGN KEY(dispatch_id) REFERENCES dispatch_plans(id)
);

-- 旧登记处报送记录：切换窗口内是否继续向旧登记处报送排期
CREATE TABLE IF NOT EXISTS legacy_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_code TEXT,
    related_task TEXT,
    payload TEXT NOT NULL,
    kind TEXT NOT NULL,                         -- STATUS 状态 / SCHEDULE 排期
    reported_at TEXT NOT NULL
);

-- 关键事件流水：地址更正、计划送达、受理结论等都留痕，重启后可追
CREATE TABLE IF NOT EXISTS migration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    ref_code TEXT,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 运行期开关与口径
CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class MigrationDB:
    """迁移库仓储：封装常用读写，复杂查询保留显式 SQL 方便对账。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(Path(db_path or settings.migration_db))
        self._init_schema()

    # ---- 基础 ----
    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(DDL)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            conn.execute(
                "INSERT OR IGNORE INTO settings_kv(key, value, updated_at) "
                "VALUES ('legacy_report_enabled', '1', ?)",
                (utc_now(),),
            )

    # ---- 设置 ----
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings_kv WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings_kv(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, utc_now()),
            )

    # ---- 事件 ----
    def add_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        detail: dict[str, Any],
        ref_code: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO migration_events(event_type, ref_code, detail, created_at) "
            "VALUES (?, ?, ?, ?)",
            (event_type, ref_code, json.dumps(detail, ensure_ascii=False), utc_now()),
        )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM migration_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out

    # ---- 源端 ----
    def get_source(self, conn: sqlite3.Connection, source_code: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM sources WHERE source_code = ?", (source_code,)
        ).fetchone()

    def list_sources(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    def insert_source(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        now = utc_now()
        conn.execute(
            "INSERT INTO sources(source_code, legacy_id, connection_type, auth_mode, "
            "sample_cycle, timeout_seconds, retry_limit, owner, "
            "current_address_version, confirmed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                row["source_code"],
                row.get("legacy_id"),
                row.get("connection_type"),
                row.get("auth_mode"),
                row.get("sample_cycle"),
                row.get("timeout_seconds"),
                row.get("retry_limit"),
                row.get("owner"),
                now,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO source_addresses(source_code, version, address, change_note, "
            "confirmed_at) VALUES (?, 1, ?, ?, ?)",
            (row["source_code"], row["address"], "迁移初始种子地址", now),
        )

    def list_addresses(self, source_code: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM source_addresses WHERE source_code = ? ORDER BY version",
                (source_code,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---- 任务与来源对照 ----
    def get_mapping(self, conn: sqlite3.Connection, related_task: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM task_source_mappings WHERE related_task = ?", (related_task,)
        ).fetchone()

    def list_mappings(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_source_mappings ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    def upsert_mapping(
        self,
        conn: sqlite3.Connection,
        related_task: str,
        source_code: str,
        note: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO task_source_mappings(related_task, source_code, note, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(related_task) DO UPDATE SET "
            "source_code = excluded.source_code, note = excluded.note",
            (related_task, source_code, note, utc_now()),
        )

    # ---- 对账数据集 ----
    def max_dataset_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(dataset_version), 0) AS v FROM reconciliation_entries"
        ).fetchone()
        return int(row["v"])

    def get_recon_entry(
        self, conn: sqlite3.Connection, plan_code: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM reconciliation_entries WHERE plan_code = ? "
            "ORDER BY dataset_version DESC LIMIT 1",
            (plan_code,),
        ).fetchone()

    def list_recon_entries(
        self, binding_status: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reconciliation_entries WHERE 1=1"
        params: list[Any] = []
        if binding_status:
            sql += " AND binding_status = ?"
            params.append(binding_status)
        sql += " ORDER BY dataset_version DESC, id"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def insert_recon_entry(
        self, conn: sqlite3.Connection, row: dict[str, Any], version: int
    ) -> None:
        now = utc_now()
        conn.execute(
            "INSERT INTO reconciliation_entries(dataset_version, plan_code, legacy_id, "
            "related_task, schedule_cycle, start_at, end_at, legacy_status, "
            "binding_status, binding_method, bound_source_code, bound_address_version, "
            "review_reason, confirmed, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                row["plan_code"],
                row.get("legacy_id"),
                row["related_task"],
                row.get("schedule_cycle"),
                row.get("start_at"),
                row.get("end_at"),
                row.get("legacy_status"),
                row["binding_status"],
                row.get("binding_method"),
                row.get("bound_source_code"),
                row.get("bound_address_version"),
                row.get("review_reason"),
                1 if row["binding_status"] == "LANDED" else 0,
                now,
                now,
            ),
        )

    def confirm_recon_entry(self, conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
        conn.execute(
            "UPDATE reconciliation_entries SET confirmed = 1, updated_at = ? WHERE id = ?",
            (utc_now(), entry_id),
        )
        return conn.execute(
            "SELECT * FROM reconciliation_entries WHERE id = ?", (entry_id,)
        ).fetchone()

    def bind_recon_entry(
        self,
        conn: sqlite3.Connection,
        entry_id: int,
        source_code: str,
        address_version: int,
        method: str,
    ) -> sqlite3.Row | None:
        conn.execute(
            "UPDATE reconciliation_entries SET binding_status = 'LANDED', "
            "binding_method = ?, bound_source_code = ?, bound_address_version = ?, "
            "review_reason = NULL, confirmed = 1, updated_at = ? WHERE id = ?",
            (method, source_code, address_version, utc_now(), entry_id),
        )
        return conn.execute(
            "SELECT * FROM reconciliation_entries WHERE id = ?", (entry_id,)
        ).fetchone()

    # ---- 待执行计划 ----
    def get_dispatch(self, conn: sqlite3.Connection, plan_code: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM dispatch_plans WHERE plan_code = ?", (plan_code,)
        ).fetchone()

    def get_dispatch_by_id(
        self, conn: sqlite3.Connection, dispatch_id: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM dispatch_plans WHERE id = ?", (dispatch_id,)
        ).fetchone()

    def list_dispatch_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dispatch_plans WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def insert_dispatch_plan(
        self, conn: sqlite3.Connection, row: dict[str, Any]
    ) -> int:
        now = utc_now()
        cur = conn.execute(
            "INSERT INTO dispatch_plans(plan_code, source_code, address_version, "
            "address_snapshot, schedule_cycle, related_task, origin, status, accepted, "
            "acceptance_note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
            (
                row["plan_code"],
                row["source_code"],
                row["address_version"],
                row["address_snapshot"],
                row.get("schedule_cycle"),
                row.get("related_task"),
                row["origin"],
                row.get("status", "PENDING"),
                1 if row.get("accepted", True) else 0,
                row.get("acceptance_note"),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def update_dispatch_status(
        self, conn: sqlite3.Connection, dispatch_id: int, status: str
    ) -> None:
        conn.execute(
            "UPDATE dispatch_plans SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), dispatch_id),
        )

    def insert_claim(
        self,
        conn: sqlite3.Connection,
        dispatch_id: int,
        operator: str,
        address_version: int,
        address_used: str,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO sampling_claims(dispatch_id, operator, address_version, "
            "address_used, claimed_at) VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, operator, address_version, address_used, utc_now()),
        )
        return int(cur.lastrowid)

    def list_claims(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sampling_claims ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]

    # ---- 旧登记处报送 ----
    def insert_legacy_report(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: dict[str, Any],
        plan_code: str | None,
        related_task: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO legacy_reports(plan_code, related_task, payload, kind, "
            "reported_at) VALUES (?, ?, ?, ?, ?)",
            (
                plan_code,
                related_task,
                json.dumps(payload, ensure_ascii=False),
                kind,
                utc_now(),
            ),
        )

    def list_legacy_reports(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM legacy_reports ORDER BY id"
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out
