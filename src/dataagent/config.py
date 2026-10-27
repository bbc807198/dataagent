"""dataagent 全局配置与路径解析。

路径解析规则（不硬编码任何个人 Windows 用户目录）：
1. 项目根目录默认由本文件位置推导：``src/dataagent/config.py`` 的上两级。
2. 可用环境变量 ``DATAAGENT_HOME`` 覆盖项目根目录。
3. 原始数据目录可用 ``DATAAGENT_DATA_DIR`` 覆盖，默认为 ``<项目根>/Purchase Redemption Data``。
4. 数据库路径可用 ``DATAAGENT_DB_PATH`` 覆盖，默认为 ``<项目根>/data/processed/dataagent_demo.duckdb``。
5. 数据库重建策略可用 ``DATAAGENT_DB_REBUILD`` 提示，但命令行参数优先。

所有路径在导入时即解析为绝对路径，便于脚本与测试共用。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---- 项目根目录：优先环境变量，否则按包位置推导 ----
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(os.environ.get("DATAAGENT_HOME", _PROJECT_ROOT)).resolve()

# ---- 原始数据目录（只读，不修改、不复制、不移动）----
DATA_DIR = Path(os.environ.get("DATAAGENT_DATA_DIR", PROJECT_ROOT / "Purchase Redemption Data")).resolve()

# ---- 本地生成物目录 ----
PROCESSED_DIR = Path(os.environ.get("DATAAGENT_PROCESSED_DIR", PROJECT_ROOT / "data" / "processed")).resolve()
# ---- 演示数据库（仓库自带 Parquet → scripts/build_demo_db.py 构建）----
# 默认数据源是**仓库随附的演示数据库**（相对项目根解析，不含任何个人绝对路径）。
# 可用环境变量 DATAAGENT_DB_PATH 指向自己的本地 DuckDB。
DEMO_DB_PATH = PROCESSED_DIR / "dataagent_demo.duckdb"
DB_PATH = Path(os.environ.get("DATAAGENT_DB_PATH", DEMO_DB_PATH)).resolve()
DEMO_DATA_DIR = PROJECT_ROOT / "demo_data"

# ---- 四张业务表的源文件（comp_predict_table.csv 是比赛提交模板，故意不导入）----
RAW_FILES = {
    "user_balance_table": DATA_DIR / "user_balance_table.csv",
    "user_profile_table": DATA_DIR / "user_profile_table.csv",
    "mfd_day_share_interest": DATA_DIR / "mfd_day_share_interest.csv",
    "mfd_bank_shibor": DATA_DIR / "mfd_bank_shibor.csv",
}

# ---- DuckDB 连接与行为 ----
DUCKDB_THREADS = int(os.environ.get("DATAAGENT_DUCKDB_THREADS", "4"))
DUCKDB_MEMORY_LIMIT = os.environ.get("DATAAGENT_DUCKDB_MEMORY_LIMIT", "2GB")


def raw_data_available() -> bool:
    """本地是否具备真实原始数据（用于集成测试的跳过判断）。"""
    return all(p.is_file() for p in RAW_FILES.values())


def ensure_processed_dir() -> Path:
    """确保本地生成物目录存在并返回。"""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    return PROCESSED_DIR


def as_str_dict() -> dict:
    """导出当前生效的路径配置（用于日志与报告）。"""
    return {
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "DATA_DIR": str(DATA_DIR),
        "PROCESSED_DIR": str(PROCESSED_DIR),
        "DB_PATH": str(DB_PATH),
    }


def demo_db_available() -> bool:
    """仓库随附的演示数据库是否已构建（未构建时可离线一键构建）。"""
    return DEMO_DB_PATH.is_file()


def demo_data_available() -> bool:
    """仓库随附的演示数据文件（Parquet）是否齐全。"""
    if not DEMO_DATA_DIR.is_dir():
        return False
    needed = ("user_balance_table", "user_profile_table",
              "mfd_day_share_interest", "mfd_bank_shibor")
    return all((DEMO_DATA_DIR / f"{t}.parquet").is_file() for t in needed)
