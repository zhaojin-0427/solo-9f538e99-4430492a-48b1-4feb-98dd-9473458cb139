"""SQLite 连接、模式与事务助手。

设计：单进程单连接 + 进程级写锁。所有写路径在 BEGIN IMMEDIATE ...
COMMIT 的单个事务中执行，因此原文、校时依据、分析供数状态同次生效；
查询与（崩溃后）重启都不会看到半次重算的中间态。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB_PATH = "./buoy.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_points (
    boot_segment TEXT NOT NULL,      -- 开机段
    counter      INTEGER NOT NULL,   -- 段内毫秒计数
    utc_ms       INTEGER NOT NULL,   -- 对应 UTC 毫秒
    PRIMARY KEY (boot_segment, counter)
);

CREATE TABLE IF NOT EXISTS observations (
    obs_id         TEXT PRIMARY KEY, -- 观测 ID
    boot_segment   TEXT NOT NULL,    -- 原文：开机段
    counter        INTEGER NOT NULL, -- 原文：段内计数
    tide_level     REAL NOT NULL,    -- 原文：潮位（非有限值以 0.0 + 标记列保存）
    tide_level_nonfinite INTEGER NOT NULL DEFAULT 0, -- 1 表示原文潮位非有限
    utc_ms         INTEGER,          -- 换算结果；NULL 表示待校时
    status         TEXT NOT NULL,    -- ok / pending_calibration / conflict
    reason         TEXT,             -- 状态原因
    cal_lo_counter INTEGER,          -- 校时依据：下界校时点
    cal_lo_utc_ms  INTEGER,
    cal_hi_counter INTEGER,          -- 校时依据：上界校时点
    cal_hi_utc_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_observations_utc_ms ON observations(utc_ms);
CREATE INDEX IF NOT EXISTS idx_observations_segment ON observations(boot_segment);

-- 潮段：同一开机段内相邻的一对实测高/低拐点。
-- 由可用观测（status=ok）派生，提交时按受影响开机段增量重算。
CREATE TABLE IF NOT EXISTS tide_segments (
    boot_segment     TEXT NOT NULL,
    direction        TEXT NOT NULL,   -- rising / falling
    start_utc_ms     INTEGER NOT NULL, -- 较早拐点时刻
    start_tide_level REAL NOT NULL,
    start_obs_id     TEXT NOT NULL,
    end_utc_ms       INTEGER NOT NULL, -- 较晚拐点时刻（查询按此筛选）
    end_tide_level   REAL NOT NULL,
    end_obs_id       TEXT NOT NULL,
    tidal_range      REAL NOT NULL,    -- 潮差：|end - start|
    PRIMARY KEY (boot_segment, start_obs_id, end_obs_id)
);

CREATE INDEX IF NOT EXISTS idx_tide_segments_end ON tide_segments(end_utc_ms);
CREATE INDEX IF NOT EXISTS idx_tide_segments_segment ON tide_segments(boot_segment);

-- 初始化标记（旧卷首次升级时用于判定是否需要补齐潮段）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    """返回进程内共享连接（惰性创建并建表、迁移旧卷、补齐潮段）。"""
    global _conn
    if _conn is None:
        with _lock:  # 并发首次访问时只建立一个连接
            if _conn is None:
                path = os.environ.get("BUOY_DB", DEFAULT_DB_PATH)
                if path != ":memory:":
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.executescript(SCHEMA)
                _migrate(conn)
                _conn = conn
                # 旧 SQLite 卷首次启用时补齐潮段（见 core.ensure_tide_segments）
                from . import core

                core.ensure_tide_segments(conn)
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧卷的 observations 补上非有限潮位标记列（旧库潮位均为有限数）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(observations)")}
    if "tide_level_nonfinite" not in cols:
        conn.execute(
            "ALTER TABLE observations ADD COLUMN tide_level_nonfinite "
            "INTEGER NOT NULL DEFAULT 0"
        )


def lock() -> threading.RLock:
    """读写共用的进程级锁：写事务进行中，查询不会读到中间态。"""
    return _lock


@contextmanager
def transaction(conn: sqlite3.Connection):
    """单事务包裹一批写入与重算；异常即整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def reset_for_tests() -> None:
    """测试用：丢弃当前连接（下次 get_conn 重新建库）。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
