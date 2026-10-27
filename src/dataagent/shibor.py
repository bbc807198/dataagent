"""SHIBOR 前向填充视图与验证。

视图 ``v_mfd_bank_shibor_daily_filled``：
- 日历脊柱范围由 ``user_balance_table`` 的实际最小/最大日期动态推导，**不硬编码年份**；
- 原始表 ``mfd_bank_shibor`` 保持原样（294 行真实观测，不为周末/节假日改原始数据）；
- 缺失日期用 DuckDB ``ASOF LEFT JOIN``（``sh.mfd_date <=  spine.d`` 的最近一条观测）填充，
  语义等价于 LOCF，且不会使用未来观测、不会使用累计最大值；
- 携带 ``source_obs_date``（填充值来源的原始观测日期）与 ``is_filled``（是否发生填充）；
- 2014-08-30/31（数据截止后两天）由最后一个有效观测日 2014-08-29 承接，
  该截止事实在 docs/data_dictionary.md 与 docs/data_quality.md 中披露。

验证：``verify_fill_view`` 用纯 Python 从原始表独立重算 LOCF，与视图逐格比对。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import duckdb

log = logging.getLogger(__name__)

FILL_VIEW_NAME = "v_mfd_bank_shibor_daily_filled"

SHIBOR_VALUE_COLUMNS = [
    "Interest_O_N", "Interest_1_W", "Interest_2_W", "Interest_1_M",
    "Interest_3_M", "Interest_6_M", "Interest_9_M", "Interest_1_Y",
]

CREATE_FILL_VIEW_SQL = f"""
CREATE OR REPLACE VIEW {FILL_VIEW_NAME} AS
WITH RECURSIVE spine(d) AS (
    SELECT (SELECT min(report_date) FROM user_balance_table)
    UNION ALL
    SELECT d + 1 FROM spine
    WHERE d < (SELECT max(report_date) FROM user_balance_table)
)
SELECT
    s.d                                        AS mfd_date,
    sh.mfd_date                                AS source_obs_date,
    (sh.mfd_date IS NULL OR sh.mfd_date <> s.d) AS is_filled,
    sh.Interest_O_N, sh.Interest_1_W, sh.Interest_2_W, sh.Interest_1_M,
    sh.Interest_3_M, sh.Interest_6_M, sh.Interest_9_M, sh.Interest_1_Y
FROM spine s
ASOF LEFT JOIN mfd_bank_shibor sh ON sh.mfd_date <= s.d
ORDER BY s.d
"""


def create_fill_view(con: duckdb.DuckDBPyConnection) -> str:
    """创建（或替换）前向填充分析视图。"""
    con.execute(CREATE_FILL_VIEW_SQL)
    return FILL_VIEW_NAME


# ---------------------------------------------------------------------------
# 验证：视图 vs 独立 LOCF 重算
# ---------------------------------------------------------------------------

@dataclass
class FillVerification:
    spine_days: int = 0
    filled_rows: int = 0
    original_rows: int = 0
    value_mismatches: int = 0
    source_mismatches: int = 0
    flag_mismatches: int = 0
    first_mismatch: str | None = None
    special_dates: dict | None = None
    last_obs_date: str | None = None

    @property
    def ok(self) -> bool:
        return (self.value_mismatches == 0
                and self.source_mismatches == 0
                and self.flag_mismatches == 0)

    def as_dict(self) -> dict:
        return {
            "spine_days": self.spine_days,
            "filled_rows": self.filled_rows,
            "original_rows": self.original_rows,
            "value_mismatches": self.value_mismatches,
            "source_mismatches": self.source_mismatches,
            "flag_mismatches": self.flag_mismatches,
            "first_mismatch": self.first_mismatch,
            "special_dates": self.special_dates,
            "last_obs_date": self.last_obs_date,
            "ok": self.ok,
        }


def _expected_locf(con: duckdb.DuckDBPyConnection):
    """从原始表独立重算 LOCF（纯 Python，不依赖 pandas）。"""
    raw = con.execute(
        f"SELECT mfd_date, {', '.join(SHIBOR_VALUE_COLUMNS)} "
        "FROM mfd_bank_shibor ORDER BY mfd_date"
    ).fetchall()
    d0, d1 = con.execute(
        "SELECT min(report_date), max(report_date) FROM user_balance_table"
    ).fetchone()
    last_row = None
    last_date = None
    expected: list[tuple] = []
    cur = d0
    raw_iter = iter(raw)
    nxt = next(raw_iter, None)
    while cur <= d1:
        while nxt is not None and nxt[0] <= cur:
            last_row = nxt
            last_date = nxt[0]
            nxt = next(raw_iter, None)
        if last_row is None:
            expected.append((cur, None, True, tuple([None] * len(SHIBOR_VALUE_COLUMNS))))
        else:
            is_filled = last_date != cur
            expected.append((cur, last_date, is_filled, tuple(last_row[1:])))
        cur += dt.timedelta(days=1)
    return expected, str(raw[-1][0]) if raw else None


def verify_fill_view(con: duckdb.DuckDBPyConnection) -> FillVerification:
    """逐格比对视图输出与独立 LOCF 重算结果。"""
    v = FillVerification()
    expected, v.last_obs_date = _expected_locf(con)
    view = con.execute(
        f"SELECT mfd_date, source_obs_date, is_filled, "
        f"{', '.join(SHIBOR_VALUE_COLUMNS)} FROM {FILL_VIEW_NAME} ORDER BY mfd_date"
    ).fetchall()
    v.spine_days = len(view)
    v.original_rows = sum(1 for row in view if not row[2])
    v.filled_rows = sum(1 for row in view if row[2])
    if len(view) != len(expected):
        v.first_mismatch = f"行数不一致: 视图 {len(view)} vs 期望 {len(expected)}"
        return v
    for got, exp in zip(view, expected):
        d, src, flag = got[0], got[1], got[2]
        e_d, e_src, e_flag, e_vals = exp
        if src != e_src:
            v.source_mismatches += 1
            v.first_mismatch = v.first_mismatch or f"{d}: source {src} != {e_src}"
        if bool(flag) != bool(e_flag):
            v.flag_mismatches += 1
            v.first_mismatch = v.first_mismatch or f"{d}: is_filled {flag} != {e_flag}"
        for i, col in enumerate(SHIBOR_VALUE_COLUMNS):
            g, e = got[3 + i], e_vals[i]
            if (g is None) != (e is None) or (g is not None and abs(g - e) > 1e-9):
                v.value_mismatches += 1
                v.first_mismatch = v.first_mismatch or f"{d} {col}: {g} != {e}"
    # 数据截止后特殊日期（若脊柱覆盖到最后观测日之后）
    v.special_dates = {}
    for row in view:
        if row[0] > dt.date.fromisoformat(v.last_obs_date) if v.last_obs_date else False:
            v.special_dates[str(row[0])] = {
                "source_obs_date": str(row[1]), "is_filled": bool(row[2]),
            }
    return v
