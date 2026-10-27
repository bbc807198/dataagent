"""CSV 导入模块：原始 CSV → DuckDB 业务表。

职责：
1. 导入前校验：表头与期望列一致、逐行字段数一致、日期可解析、金额可解析。
2. 导入：DuckDB ``read_csv`` 高速读取 + 显式类型转换（日期规范化为 DATE，金额整数元 BIGINT）。
3. 导入后检查：行数、关键列 NULL、日期范围、负值扫描。
4. 可重复运行：表已存在且非空时默认拒绝重复导入（防止重复累积），由调用方显式决定重建。

字段处理规则（依据阶段一审计结论）：
- 四张业务表保留全部原始字段；``comp_predict_table.csv`` 是比赛提交模板，**不导入**。
- ``report_date`` / ``mfd_date`` 由 YYYYMMDD 整数字符串规范化为 DATE。
- 金额字段按已审计的整数元口径使用 BIGINT；出现非整数将报错而非静默截断。
- ``category1..4`` 的空值具有已审计的业务特征（consume=0 时为空），空字符串转为 NULL。
- ``city`` 是类别编码，按 VARCHAR 保留，不做数值运算。
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from . import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 期望列（顺序须与源文件一致）
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "user_balance_table": [
        "user_id", "report_date", "tBalance", "yBalance",
        "total_purchase_amt", "direct_purchase_amt", "purchase_bal_amt", "purchase_bank_amt",
        "total_redeem_amt", "consume_amt", "transfer_amt", "tftobal_amt", "tftocard_amt",
        "share_amt", "category1", "category2", "category3", "category4",
    ],
    "user_profile_table": ["user_id", "sex", "city", "constellation"],
    "mfd_day_share_interest": ["mfd_date", "mfd_daily_yield", "mfd_7daily_yield"],
    "mfd_bank_shibor": [
        "mfd_date", "Interest_O_N", "Interest_1_W", "Interest_2_W", "Interest_1_M",
        "Interest_3_M", "Interest_6_M", "Interest_9_M", "Interest_1_Y",
    ],
}

DATE_COLUMNS: dict[str, str] = {
    "user_balance_table": "report_date",
    "user_profile_table": None,  # type: ignore[dict-item]
    "mfd_day_share_interest": "mfd_date",
    "mfd_bank_shibor": "mfd_date",
}
DATE_COLUMNS.pop("user_profile_table", None)

BIGINT_COLUMNS: dict[str, list[str]] = {
    "user_balance_table": [
        "user_id", "tBalance", "yBalance", "total_purchase_amt", "direct_purchase_amt",
        "purchase_bal_amt", "purchase_bank_amt", "total_redeem_amt", "consume_amt",
        "transfer_amt", "tftobal_amt", "tftocard_amt", "share_amt",
        "category1", "category2", "category3", "category4",
    ],
    "user_profile_table": ["user_id", "sex"],
}

DOUBLE_COLUMNS: dict[str, list[str]] = {
    "mfd_day_share_interest": ["mfd_daily_yield", "mfd_7daily_yield"],
    "mfd_bank_shibor": [
        "Interest_O_N", "Interest_1_W", "Interest_2_W", "Interest_1_M",
        "Interest_3_M", "Interest_6_M", "Interest_9_M", "Interest_1_Y",
    ],
}

VARCHAR_COLUMNS: dict[str, list[str]] = {
    "user_profile_table": ["city", "constellation"],
}

# 金额/余额列（用于负值扫描）
AMOUNT_COLUMNS: dict[str, list[str]] = {
    "user_balance_table": [
        "tBalance", "yBalance", "total_purchase_amt", "direct_purchase_amt",
        "purchase_bal_amt", "purchase_bank_amt", "total_redeem_amt", "consume_amt",
        "transfer_amt", "tftobal_amt", "tftocard_amt", "share_amt",
    ],
}


# ---------------------------------------------------------------------------
# 导入前校验（流式，内存友好）
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    csv_path: str
    table: str
    header_ok: bool = False
    expected_columns: list[str] = field(default_factory=list)
    actual_columns: list[str] = field(default_factory=list)
    row_count: int = 0
    field_count_errors: int = 0
    date_format_errors: int = 0
    amount_parse_errors: int = 0
    first_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.header_ok
            and self.field_count_errors == 0
            and self.date_format_errors == 0
            and self.amount_parse_errors == 0
        )


def _looks_like_int(s: str) -> bool:
    if s == "":
        return True  # 空值单独处理
    try:
        int(s)
        return True
    except ValueError:
        return False


def validate_raw_csv(table: str, csv_path: Path) -> ValidationResult:
    """流式校验原始 CSV：表头、字段数、日期格式、金额可解析性。"""
    expected = EXPECTED_COLUMNS[table]
    res = ValidationResult(csv_path=str(csv_path), table=table, expected_columns=expected)
    date_col = DATE_COLUMNS.get(table)
    amount_cols = BIGINT_COLUMNS.get(table, [])
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            res.first_errors.append("文件为空")
            return res
        res.actual_columns = header
        res.header_ok = header == expected
        if not res.header_ok:
            res.first_errors.append(f"表头不匹配: 期望 {expected}, 实际 {header}")
            return res
        for i, row in enumerate(reader, start=2):  # 第 1 行是表头
            res.row_count += 1
            if len(row) != len(expected):
                res.field_count_errors += 1
                if len(res.first_errors) < 5:
                    res.first_errors.append(f"第 {i} 行字段数 {len(row)} != {len(expected)}")
                continue
            rec = dict(zip(expected, row))
            if date_col:
                v = rec[date_col]
                if len(v) != 8 or not v.isdigit():
                    res.date_format_errors += 1
                    if len(res.first_errors) < 5:
                        res.first_errors.append(f"第 {i} 行日期无法解析: {v!r}")
            for c in amount_cols:
                if not _looks_like_int(rec[c]):
                    res.amount_parse_errors += 1
                    if len(res.first_errors) < 5:
                        res.first_errors.append(f"第 {i} 行 {c} 非整数: {rec[c]!r}")
    return res


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def _select_expr(table: str, col: str) -> str:
    if col == DATE_COLUMNS.get(table):
        return f"CAST(strptime(NULLIF({col}, ''), '%Y%m%d') AS DATE)"
    if col in BIGINT_COLUMNS.get(table, []):
        return f"TRY_CAST(NULLIF({col}, '') AS BIGINT)"
    if col in DOUBLE_COLUMNS.get(table, []):
        return f"TRY_CAST(NULLIF({col}, '') AS DOUBLE)"
    return f"NULLIF({col}, '')"  # VARCHAR 列


@dataclass
class ImportReport:
    table: str
    csv_path: str
    rows_inserted: int = 0
    duration_s: float = 0.0
    ok: bool = False
    error: str | None = None
    validation: ValidationResult | None = None


def import_table(
    con: duckdb.DuckDBPyConnection,
    table: str,
    csv_path: Path,
    allow_nonempty: bool = False,
) -> ImportReport:
    """导入单张表。表已存在且含数据时默认拒绝（除非 allow_nonempty=True，需显式重建后调用）。"""
    rep = ImportReport(table=table, csv_path=str(csv_path))
    t0 = time.time()

    # 策略性检查放在 try 之外：表非空时的拒绝必须向上抛出，由调用方显式处理，
    # 不能被转成"导入失败报告"而静默继续。
    from .db import table_exists  # 延迟导入避免环

    if table_exists(con, table):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if n > 0 and not allow_nonempty:
            rep.duration_s = round(time.time() - t0, 2)
            raise RuntimeError(
                f"表 {table} 已存在且包含 {n} 行数据；默认拒绝重复导入。"
                "如需重建，请先显式调用 rebuild_tables() 或使用 --rebuild 参数。"
            )

    try:
        rep.validation = validate_raw_csv(table, csv_path)
        if not rep.validation.ok:
            raise ValueError(f"原始 CSV 校验失败: {rep.validation.first_errors}")

        cols = EXPECTED_COLUMNS[table]
        select_sql = ", ".join(f"{_select_expr(table, c)} AS {c}" for c in cols)
        con.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"SELECT {select_sql} FROM read_csv(?, header=true, all_varchar=true)",
            [str(csv_path)],
        )
        rep.rows_inserted = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        rep.ok = True
    except Exception as e:  # noqa: BLE001 - 需要把任何导入失败转成明确报告
        rep.ok = False
        rep.error = f"{type(e).__name__}: {e}"
        log.exception("import %s failed", table)
    finally:
        rep.duration_s = round(time.time() - t0, 2)
    return rep


def import_all(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path | None = None,
    tables: list[str] | None = None,
    allow_nonempty: bool = False,
) -> list[ImportReport]:
    """按 IMPORT_ORDER 导入四张业务表。"""
    d = Path(data_dir) if data_dir else config.DATA_DIR
    names = tables or config.RAW_FILES.keys()
    reports = []
    for t in names:
        path = d / f"{t}.csv"
        reports.append(import_table(con, t, path, allow_nonempty=allow_nonempty))
    return reports


# ---------------------------------------------------------------------------
# 导入后检查
# ---------------------------------------------------------------------------

def post_import_checks(con: duckdb.DuckDBPyConnection) -> dict:
    """导入后的基本质量检查（行数、关键列 NULL、日期范围、负值扫描）。"""
    out: dict = {}
    for t in config.RAW_FILES:
        if t not in EXPECTED_COLUMNS:
            continue
        row = con.execute(
            f"SELECT count(*) FROM {t}"
        ).fetchone()
        n = row[0] if row else 0
        info: dict = {"row_count": n}
        date_col = DATE_COLUMNS.get(t)
        if date_col:
            lo, hi = con.execute(f"SELECT min({date_col}), max({date_col}) FROM {t}").fetchone()
            info["date_min"] = str(lo)
            info["date_max"] = str(hi)
            info["null_dates"] = con.execute(
                f"SELECT count(*) FROM {t} WHERE {date_col} IS NULL"
            ).fetchone()[0]
        # 关键 BIGINT 列 NULL 检查（category 除外——其空值为设计特征，单独报告）
        big_cols = [c for c in BIGINT_COLUMNS.get(t, []) if not c.startswith("category")]
        if big_cols:
            cond = " OR ".join(f"{c} IS NULL" for c in big_cols)
            info["null_key_or_amount"] = con.execute(
                f"SELECT count(*) FROM {t} WHERE {cond}"
            ).fetchone()[0]
        # 负值扫描
        amt = AMOUNT_COLUMNS.get(t, [])
        if amt:
            cond = " OR ".join(f"{c} < 0" for c in amt)
            info["negative_amount_rows"] = con.execute(
                f"SELECT count(*) FROM {t} WHERE {cond}"
            ).fetchone()[0]
        out[t] = info
    return out
