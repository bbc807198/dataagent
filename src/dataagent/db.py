"""DuckDB 数据底座：连接管理、Schema 定义与重建策略。

设计原则：
- 数据库文件是本地生成物（``data/processed/dataagent_demo.duckdb``），不提交 Git。
- 表名与字段名保留源数据名称，便于后续自然语言查询与数据字典对照。
- 日期字段规范化为 DuckDB ``DATE``；金额按已审计的整数元口径使用 ``BIGINT``。
- 已有数据库不得被静默覆盖：``init_schema`` 只建不存在的表；重建必须显式调用
  ``rebuild_tables`` 或使用 ``--rebuild`` 参数。
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema：四张业务表（保留全部原始字段；comp_predict_table 是比赛模板，不建表）
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS user_balance_table (
        user_id           BIGINT  NOT NULL,
        report_date       DATE    NOT NULL,
        tBalance          BIGINT,
        yBalance          BIGINT,
        total_purchase_amt BIGINT,
        direct_purchase_amt BIGINT,
        purchase_bal_amt  BIGINT,
        purchase_bank_amt BIGINT,
        total_redeem_amt  BIGINT,
        consume_amt       BIGINT,
        transfer_amt      BIGINT,
        tftobal_amt       BIGINT,
        tftocard_amt      BIGINT,
        share_amt         BIGINT,
        category1         BIGINT,
        category2         BIGINT,
        category3         BIGINT,
        category4         BIGINT,
        PRIMARY KEY (user_id, report_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_profile_table (
        user_id      BIGINT       NOT NULL PRIMARY KEY,
        sex          BIGINT,
        city         VARCHAR(16),          -- 类别编码（非数值），无官方名称映射
        constellation VARCHAR(16)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mfd_day_share_interest (
        mfd_date         DATE NOT NULL PRIMARY KEY,
        mfd_daily_yield  DOUBLE,
        mfd_7daily_yield DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mfd_bank_shibor (
        mfd_date     DATE NOT NULL PRIMARY KEY,
        Interest_O_N DOUBLE,
        Interest_1_W DOUBLE,
        Interest_2_W DOUBLE,
        Interest_1_M DOUBLE,
        Interest_3_M DOUBLE,
        Interest_6_M DOUBLE,
        Interest_9_M DOUBLE,
        Interest_1_Y DOUBLE
    )
    """,
]

BUSINESS_TABLES = [
    "user_balance_table",
    "user_profile_table",
    "mfd_day_share_interest",
    "mfd_bank_shibor",
]

# 导入顺序：先维表再事实表（本阶段无强制外键，顺序仅用于日志可读性）
IMPORT_ORDER = list(BUSINESS_TABLES)


class DatabaseExistsError(RuntimeError):
    """数据库文件已存在且包含业务表时的默认拒绝（防止静默覆盖）。"""


def connect(db_path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """建立 DuckDB 连接。

    - 自动创建父目录；
    - ``read_only=True`` 用于审计/查询，避免误写；
    - 通过 PRAGMA 设置线程与内存上限，参数来自 config。
    """
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute(f"SET threads={config.DUCKDB_THREADS}")
    if config.DUCKDB_MEMORY_LIMIT:
        con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
    return con


def table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchone()
    return bool(row and row[0] > 0)


def existing_business_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [t for t in BUSINESS_TABLES if table_exists(con, t)]


def init_schema(con: duckdb.DuckDBPyConnection) -> list[str]:
    """创建不存在的表；返回本次新建的表名。"""
    created = []
    for stmt in SCHEMA_STATEMENTS:
        # 从 DDL 中提取表名（CREATE TABLE IF NOT EXISTS <name>）
        name = stmt.split("IF NOT EXISTS")[1].split("(")[0].strip()
        if not table_exists(con, name):
            con.execute(stmt)
            created.append(name)
            log.info("created table %s", name)
    return created


def rebuild_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    """显式重建：DROP 后 CREATE。仅在调用方明确要求时使用。"""
    dropped = []
    for t in reversed(BUSINESS_TABLES):
        if table_exists(con, t):
            con.execute(f"DROP TABLE {t}")
            dropped.append(t)
    init_schema(con)
    log.warning("rebuilt tables (dropped: %s)", dropped or "none")
    return dropped
