"""数据契约：把阶段一审计结论固化为可执行的断言。

分层设计：
- ``check_identities``：业务恒等式，**与数据规模无关**，可在合成数据与真实数据上运行；
  全部在原始金额精度（BIGINT 元）下验证，不用四舍五入后的亿元金额。
- ``check_scale`` / ``check_joins``：规模与关联断言，期望值来自已审核的阶段一审计报告
  （AUDIT_BASELINE）；在合成数据上运行时应传入定制期望。
- ``check_balance_flow_diagnostic``：余额变动诊断。已知存在单个 100 元残差且
  tBalance/yBalance 语义未确认，**仅作诊断，不作为必须零违例的核心断言**。

纪律：若实际运行结果与审计基线不同，契约失败并原样报告差异；
不得通过修改原始数据或放宽断言来让测试通过。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import duckdb

# ---------------------------------------------------------------------------
# 审计基线（来源：已审核的阶段一审计报告，2026-10-24）
# ---------------------------------------------------------------------------

AUDIT_BASELINE: dict = {
    "user_balance_table": {
        "rows": 2_840_421,
        "users": 28_041,
        "dates": 427,
        "date_min": "2013-07-01",
        "date_max": "2014-08-31",
        "duplicate_keys": 0,
        "negative_amount_rows": 0,
    },
    "user_profile_table": {"rows": 28_041, "duplicate_user_ids": 0},
    "mfd_day_share_interest": {"rows": 427, "duplicate_dates": 0},
    "mfd_bank_shibor": {"rows": 294, "duplicate_dates": 0},
    "joins": {
        "balance_users_not_in_profile": 0,
        "profile_users_not_in_balance": 0,
        "balance_dates_not_in_yield": 0,
    },
    # 诊断性基线（不作为核心断言）：连续日余额流违例 1 例（100 元残差）
    "balance_flow_diagnostic": {"expected_violations": 1},
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    expected: str
    actual: str
    detail: str = ""
    fatal: bool = True

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
            "fatal": self.fatal,
        }


@dataclass
class ContractReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def fatal_failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.fatal and not r.passed]

    @property
    def ok(self) -> bool:
        return not self.fatal_failures

    def add(self, r: CheckResult) -> None:
        self.results.append(r)


# ---------------------------------------------------------------------------
# 业务恒等式（规模无关；原始金额精度）
# ---------------------------------------------------------------------------

IDENTITY_SQL: dict[str, str] = {
    "identity_total_redeem": """
        SELECT count(*) FROM user_balance_table
        WHERE total_redeem_amt IS DISTINCT FROM consume_amt + transfer_amt
    """,
    "identity_transfer_split": """
        SELECT count(*) FROM user_balance_table
        WHERE transfer_amt IS DISTINCT FROM tftobal_amt + tftocard_amt
    """,
    "identity_direct_purchase": """
        SELECT count(*) FROM user_balance_table
        WHERE direct_purchase_amt IS DISTINCT FROM purchase_bal_amt + purchase_bank_amt
    """,
    "identity_consume_categories": """
        SELECT count(*) FROM user_balance_table
        WHERE consume_amt IS DISTINCT FROM
              COALESCE(category1, 0) + COALESCE(category2, 0)
            + COALESCE(category3, 0) + COALESCE(category4, 0)
    """,
    "identity_total_purchase": """
        SELECT count(*) FROM user_balance_table
        WHERE total_purchase_amt IS DISTINCT FROM direct_purchase_amt + share_amt
    """,
}

IDENTITY_DESC: dict[str, str] = {
    "identity_total_redeem": "总赎回 = 消费 + 转出",
    "identity_transfer_split": "转出 = 到余额 + 到银行卡",
    "identity_direct_purchase": "直接购买 = 余额购 + 银行卡购",
    "identity_consume_categories": "消费 = category1+2+3+4（category 空值按 0 处理，设计性空值）",
    "identity_total_purchase": "总申购 = 直接购买 + 收益(share_amt)",
}


def check_identities(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    results = []
    total = con.execute("SELECT count(*) FROM user_balance_table").fetchone()[0]
    for key, sql in IDENTITY_SQL.items():
        violations = con.execute(sql).fetchone()[0]
        results.append(CheckResult(
            name=key,
            passed=violations == 0,
            expected="0 违例",
            actual=f"{violations} 违例",
            detail=f"{IDENTITY_DESC[key]}；检查行数 {total:,}",
        ))
    return results


# ---------------------------------------------------------------------------
# 规模与唯一性断言（期望值来自审计基线；合成数据可传定制期望）
# ---------------------------------------------------------------------------

def check_scale(
    con: duckdb.DuckDBPyConnection,
    expectations: dict | None = None,
) -> list[CheckResult]:
    exp = expectations or AUDIT_BASELINE
    results: list[CheckResult] = []

    def scalar(sql: str):
        return con.execute(sql).fetchone()[0]

    bal = exp["user_balance_table"]
    checks = [
        ("balance_row_count", "SELECT count(*) FROM user_balance_table", bal["rows"]),
        ("balance_user_count", "SELECT count(DISTINCT user_id) FROM user_balance_table", bal["users"]),
        ("balance_date_count", "SELECT count(DISTINCT report_date) FROM user_balance_table", bal["dates"]),
        ("balance_duplicate_keys", """
            SELECT count(*) FROM (
                SELECT user_id, report_date FROM user_balance_table
                GROUP BY user_id, report_date HAVING count(*) > 1
            )""", bal["duplicate_keys"]),
        ("balance_negative_amounts", """
            SELECT count(*) FROM user_balance_table
            WHERE total_purchase_amt < 0 OR total_redeem_amt < 0
               OR consume_amt < 0 OR transfer_amt < 0 OR share_amt < 0
               OR tBalance < 0 OR yBalance < 0""", bal["negative_amount_rows"]),
        ("balance_amount_columns_no_null", """
            SELECT count(*) FROM user_balance_table
            WHERE total_purchase_amt IS NULL OR total_redeem_amt IS NULL
               OR direct_purchase_amt IS NULL OR share_amt IS NULL""", 0),
        ("profile_row_count", "SELECT count(*) FROM user_profile_table", exp["user_profile_table"]["rows"]),
        ("profile_duplicate_user_ids", """
            SELECT count(*) FROM (
                SELECT user_id FROM user_profile_table
                GROUP BY user_id HAVING count(*) > 1
            )""", exp["user_profile_table"]["duplicate_user_ids"]),
        ("yield_row_count", "SELECT count(*) FROM mfd_day_share_interest", exp["mfd_day_share_interest"]["rows"]),
        ("yield_duplicate_dates", """
            SELECT count(*) FROM (
                SELECT mfd_date FROM mfd_day_share_interest
                GROUP BY mfd_date HAVING count(*) > 1
            )""", exp["mfd_day_share_interest"]["duplicate_dates"]),
        ("shibor_row_count", "SELECT count(*) FROM mfd_bank_shibor", exp["mfd_bank_shibor"]["rows"]),
        ("shibor_duplicate_dates", """
            SELECT count(*) FROM (
                SELECT mfd_date FROM mfd_bank_shibor
                GROUP BY mfd_date HAVING count(*) > 1
            )""", exp["mfd_bank_shibor"]["duplicate_dates"]),
    ]
    for name, sql, expected in checks:
        actual = scalar(sql)
        results.append(CheckResult(
            name=name,
            passed=actual == expected,
            expected=str(expected),
            actual=str(actual),
        ))

    lo, hi = con.execute(
        "SELECT min(report_date), max(report_date) FROM user_balance_table"
    ).fetchone()
    results.append(CheckResult(
        name="balance_date_range",
        passed=(str(lo) == bal["date_min"] and str(hi) == bal["date_max"]),
        expected=f"{bal['date_min']} ~ {bal['date_max']}",
        actual=f"{lo} ~ {hi}",
    ))
    return results


# ---------------------------------------------------------------------------
# 关联断言
# ---------------------------------------------------------------------------

def check_joins(
    con: duckdb.DuckDBPyConnection,
    expectations: dict | None = None,
) -> list[CheckResult]:
    exp = (expectations or AUDIT_BASELINE)["joins"]
    results: list[CheckResult] = []

    def scalar(sql: str):
        return con.execute(sql).fetchone()[0]

    checks = [
        ("join_balance_users_not_in_profile", """
            SELECT count(*) FROM (SELECT DISTINCT user_id FROM user_balance_table) b
            WHERE NOT EXISTS (SELECT 1 FROM user_profile_table p WHERE p.user_id = b.user_id)
        """, exp["balance_users_not_in_profile"]),
        ("join_profile_users_not_in_balance", """
            SELECT count(*) FROM (SELECT DISTINCT user_id FROM user_profile_table) p
            WHERE NOT EXISTS (SELECT 1 FROM user_balance_table b WHERE b.user_id = p.user_id)
        """, exp["profile_users_not_in_balance"]),
        ("join_balance_dates_not_in_yield", """
            SELECT count(*) FROM (SELECT DISTINCT report_date FROM user_balance_table) b
            WHERE NOT EXISTS (SELECT 1 FROM mfd_day_share_interest y WHERE y.mfd_date = b.report_date)
        """, exp["balance_dates_not_in_yield"]),
    ]
    for name, sql, expected in checks:
        actual = scalar(sql)
        results.append(CheckResult(
            name=name, passed=actual == expected,
            expected=str(expected), actual=str(actual),
        ))
    return results


# ---------------------------------------------------------------------------
# 诊断：连续日余额流（已知 1 例 100 元残差；tBalance/yBalance 语义未确认）
# ---------------------------------------------------------------------------

def check_balance_flow_diagnostic(
    con: duckdb.DuckDBPyConnection,
    expected_violations: int | None = None,
) -> CheckResult:
    sql = """
        WITH x AS (
            SELECT user_id, report_date,
                   lag(report_date) OVER w AS prev_date,
                   tBalance - lag(tBalance) OVER w AS tbal_diff,
                   total_purchase_amt - total_redeem_amt AS flow
            FROM user_balance_table
            WINDOW w AS (PARTITION BY user_id ORDER BY report_date)
        )
        SELECT
            count(*) FILTER (WHERE prev_date IS NOT NULL AND report_date = prev_date + 1),
            count(*) FILTER (WHERE prev_date IS NOT NULL AND report_date = prev_date + 1
                             AND tbal_diff IS DISTINCT FROM flow)
        FROM x
    """
    transitions, violations = con.execute(sql).fetchone()
    if expected_violations is None:
        expected_violations = AUDIT_BASELINE["balance_flow_diagnostic"]["expected_violations"]
    return CheckResult(
        name="diag_balance_flow",
        passed=violations == expected_violations,
        expected=f"{expected_violations} 违例（审计基线）",
        actual=f"{violations} 违例 / {transitions:,} 个连续日过渡",
        detail=(
            "诊断项，非核心断言：tBalance(t)-tBalance(t-1) = 申购-赎回。"
            "审计已知 1 例 100 元残差；tBalance/yBalance 语义未确认，待数据字典。"
        ),
        fatal=False,
    )


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------

def run_contracts(
    con: duckdb.DuckDBPyConnection,
    expectations: dict | None = None,
    include_scale: bool = True,
) -> ContractReport:
    """运行全部契约。include_scale=False 时只跑规模无关的恒等式（合成数据默认）。"""
    report = ContractReport()
    for r in check_identities(con):
        report.add(r)
    if include_scale:
        for r in check_scale(con, expectations):
            report.add(r)
        for r in check_joins(con, expectations):
            report.add(r)
    report.add(check_balance_flow_diagnostic(con) if include_scale
               else CheckResult(
                   name="diag_balance_flow", passed=True,
                   expected="跳过（合成数据）", actual="跳过",
                   detail="余额流诊断仅在真实数据规模下运行", fatal=False))
    return report
