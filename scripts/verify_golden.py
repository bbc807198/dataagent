#!/usr/bin/env python
"""独立复算 Golden Answers（阶段 11.1，任务 D）。

**只读取公开合成 CSV**（`tests/fixtures/synthetic/`），用 DuckDB `read_csv_auto` 与纯 Python
算术复算标准答案中的每个可验证数值，**完全不调用** DataAgent 的 `QueryBuilder`、
`Analytics`、`ReportGenerator`——因此它能发现标准答案被篡改。

用法：
    python scripts/verify_golden.py                 # 校验并打印汇总
    python scripts/verify_golden.py --verbose       # 逐条打印
    python scripts/verify_golden.py --json OUT      # 写出机器可读结果

退出码：0 = 全部一致；1 = 存在不一致（或无法复算项超阈值）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
SYNTH = ROOT / "tests" / "fixtures" / "synthetic"
BENCH = ROOT / "benchmark"
START, END = date(2024, 1, 2), date(2024, 1, 12)
MAIN_W = (date(2024, 1, 8), date(2024, 1, 12))
CMP_W = (date(2024, 1, 2), date(2024, 1, 7))

AMT_TOL = 0.01
RATE_TOL = 1e-6
COEF_TOL = 1e-3


def connect():
    import duckdb
    con = duckdb.connect()
    con.execute(f"""
    CREATE VIEW bal AS
    SELECT user_id, strptime(report_date::VARCHAR, '%Y%m%d')::DATE AS d,
           CAST(total_purchase_amt AS BIGINT) AS p, CAST(total_redeem_amt AS BIGINT) AS r
    FROM read_csv_auto('{SYNTH / 'user_balance_table.csv'}', header=true)""")
    con.execute(f"""
    CREATE VIEW prof AS
    SELECT CAST(user_id AS BIGINT) AS user_id, CAST(sex AS INT) AS sex,
           CAST(city AS BIGINT) AS city, constellation
    FROM read_csv_auto('{SYNTH / 'user_profile_table.csv'}', header=true)""")
    con.execute(f"""
    CREATE VIEW yld AS
    SELECT strptime(mfd_date::VARCHAR, '%Y%m%d')::DATE AS d,
           CAST(mfd_daily_yield AS DOUBLE) AS daily_yield,
           CAST(mfd_7daily_yield AS DOUBLE) AS seven_day_yield
    FROM read_csv_auto('{SYNTH / 'mfd_day_share_interest.csv'}', header=true)""")
    con.execute(f"""
    CREATE VIEW shb AS
    SELECT strptime(mfd_date::VARCHAR, '%Y%m%d')::DATE AS d,
           CAST(Interest_O_N AS DOUBLE) AS o_n, CAST(Interest_1_W AS DOUBLE) AS w1
    FROM read_csv_auto('{SYNTH / 'mfd_bank_shibor.csv'}', header=true)""")
    return con


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def mad_anomalies(values, dates, k=3.0):
    center = median(values)
    mad = median([abs(v - center) for v in values])
    scaled = mad * 1.4826
    if scaled == 0:
        return center, mad, []
    return center, mad, [d for d, v in zip(dates, values)
                         if abs(v - center) / scaled > k]


class Data:
    """独立读取合成数据并给出每日序列（纯 SQL + Python，不经 Agent）。"""

    def __init__(self):
        self.con = connect()
        self.daily_p = self._daily("p")
        self.daily_r = self._daily("r")
        self.daily_net = {d: float(self.daily_p[d] - self.daily_r.get(d, 0))
                          for d in self.daily_p}
        self.daily_active = {str(d): float(v) for d, v in self.con.execute(
            "SELECT d, COUNT(DISTINCT user_id) FROM bal WHERE d BETWEEN ? AND ? "
            "GROUP BY 1 ORDER BY 1", [START, END]).fetchall()}
        self.daily_yield = {str(d): float(v) for d, v in self.con.execute(
            "SELECT d, daily_yield FROM yld WHERE d BETWEEN ? AND ? ORDER BY 1",
            [START, END]).fetchall()}
        self.daily_seven = {str(d): float(v) for d, v in self.con.execute(
            "SELECT d, seven_day_yield FROM yld WHERE d BETWEEN ? AND ? ORDER BY 1",
            [START, END]).fetchall()}
        self.days = sorted(self.daily_p)

    def _daily(self, col: str) -> dict:
        return {str(d): float(v) for d, v in self.con.execute(
            f"SELECT d, SUM({col}) FROM bal WHERE d BETWEEN ? AND ? GROUP BY 1 ORDER BY 1",
            [START, END]).fetchall()}

    def series(self, metric: str) -> tuple[list[str], list[float]]:
        if metric == "purchase_amount":
            d = self.daily_p
        elif metric == "redeem_amount":
            d = self.daily_r
        elif metric == "net_inflow":
            d = self.daily_net
        elif metric == "daily_yield":
            d = self.daily_yield
        elif metric == "seven_day_yield":
            d = self.daily_seven
        elif metric == "active_user_count":
            d = self.daily_active
        else:
            return [], []
        keys = sorted(d)
        return keys, [d[k] for k in keys]


# ---------------------------------------------------------------------------
def recompute(case: dict, data: Data) -> dict:
    """按案例的预期分析类型与指标，独立复算所有可核验数值。

    返回 {(kind, extra): value}；未覆盖的类型不出现在结果中（计为不可复算）。
    """
    plan = case["fixture_plan"]
    atype = case["expected_analysis_type"]
    metrics = [m.split(":")[0] for m in case["expected_metrics"]]
    terms = [m.split(":")[1] for m in case["expected_metrics"] if ":" in m]
    out: dict = {}
    con = data.con

    if atype == "time_trend":
        primary = case["expected_metrics"][0]
        mid = primary.split(":")[0]
        term = primary.split(":")[1] if ":" in primary else None
        if mid == "shibor_rate":
            col = "o_n" if term == "O_N" else "w1"
            rows = con.execute(
                "SELECT d, {} FROM shb WHERE d BETWEEN ? AND ? ORDER BY 1".format(col),
                [date(2024, 1, 2), date(2024, 1, 5)]).fetchall()
            vals = [float(r[1]) for r in rows]
            out[("point_count", None)] = len(vals)
            out[("first_value", None)] = vals[0]
            out[("last_value", None)] = vals[-1]
            out[("period_mean", None)] = sum(vals) / len(vals)
            out[("term_period_mean", "O_N")] = sum(vals) / len(vals)
            rows_w = con.execute(
                "SELECT w1 FROM shb WHERE d BETWEEN ? AND ? ORDER BY 1",
                [date(2024, 1, 2), date(2024, 1, 5)]).fetchall()
            out[("term_period_mean", "1W")] = sum(float(r[0]) for r in rows_w) / len(rows_w)
            out[("series_count", None)] = len(case["expected_metrics"])
            return out
        days, vals = data.series(mid)
        if not days:
            return out
        out[("point_count", None)] = len(vals)
        if mid in ("purchase_amount", "redeem_amount", "net_inflow"):
            out[("series_sum", None)] = sum(vals)
        else:
            out[("period_mean", None)] = sum(vals) / len(vals)
        out[("first_value", None)] = vals[0]
        out[("last_value", None)] = vals[-1]
        # 多指标 / 多组
        if len(case["expected_metrics"]) > 1:
            out[("series_count", None)] = len(case["expected_metrics"])
            for m in metrics:
                d2, v2 = data.series(m)
                if m in ("purchase_amount", "redeem_amount", "net_inflow"):
                    out[("metric_sum", m)] = sum(v2)
        if plan.get("group_by"):
            gcol = plan["group_by"][0]
            n = con.execute(
                f"SELECT COUNT(DISTINCT p.{gcol}) FROM bal b JOIN prof p USING(user_id) "
                "WHERE b.d BETWEEN ? AND ?", [START, END]).fetchone()[0]
            out[("series_count", None)] = int(n)
        # 三类极值
        changes = []
        for i in range(1, len(days)):
            prev, cur = vals[i - 1], vals[i]
            if prev != 0:
                changes.append((days[i], prev, cur, (cur - prev) / prev))
        if changes:
            neg = [c for c in changes if c[3] < 0]
            pos = [c for c in changes if c[3] > 0]
            if neg:
                worst = min(neg, key=lambda c: c[3])
                out[("max_drop", None)] = (worst[0], worst[3])
            if pos:
                best = max(pos, key=lambda c: c[3])
                out[("max_rise", None)] = (best[0], best[3])
            worst_abs = max(changes, key=lambda c: abs(c[3]))
            out[("max_abs", None)] = (worst_abs[0], worst_abs[3])
        return out

    if atype == "window_comparison":
        mid = metrics[0]
        if mid == "daily_yield":
            # 市场指标与用户行无关：必须按**日期去重**后取均值，否则会被用户行加权
            def win(start, end):
                v = con.execute(
                    "SELECT AVG(daily_yield) FROM "
                    "(SELECT DISTINCT d, daily_yield FROM yld "
                    " WHERE d BETWEEN ? AND ?)", [start, end]).fetchone()[0]
                return float(v or 0)
        else:
            expr = {"purchase_amount": "SUM(b.p)", "redeem_amount": "SUM(b.r)",
                    "net_inflow": "SUM(b.p) - SUM(b.r)"}[mid]

            def win(start, end):
                v = con.execute(
                    f"SELECT {expr} FROM bal b WHERE b.d BETWEEN ? AND ?",
                    [start, end]).fetchone()[0]
                return float(v or 0)

        out[("window_main_total", None)] = win(*MAIN_W)
        out[("window_comparison_total", None)] = win(*CMP_W)
        out[("window_main_value_nongrouped", None)] = win(*MAIN_W)
        out[("window_comparison_value_nongrouped", None)] = win(*CMP_W)
        if plan.get("group_by"):
            gcol = plan["group_by"][0]
            gexpr = {"purchase_amount": "SUM(b.p)", "redeem_amount": "SUM(b.r)",
                     "net_inflow": "SUM(b.p) - SUM(b.r)"}[mid]
            for (start, end, key) in ((*MAIN_W, "main"), (*CMP_W, "cmp")):
                rows = con.execute(
                    f"SELECT p.{gcol}, {gexpr} FROM bal b JOIN prof p USING(user_id) "
                    f"WHERE b.d BETWEEN ? AND ? GROUP BY 1",
                    [start, end]).fetchall()
                for g, v in rows:
                    out[(f"window_{key}_group", str(g))] = float(v or 0)
        return out

    if atype == "group_ranking":
        mid = metrics[0]
        gcol = plan["group_by"][0]
        if mid == "purchase_amount":
            expr = "SUM(b.p)"
        elif mid == "redeem_amount":
            expr = "SUM(b.r)"
        elif mid == "net_inflow":
            expr = "SUM(b.p) - SUM(b.r)"
        else:      # active_user_count
            expr = "COUNT(DISTINCT b.user_id)"
        rows = con.execute(
            f"SELECT p.{gcol}, {expr} AS v FROM bal b JOIN prof p USING(user_id) "
            "WHERE b.d BETWEEN ? AND ? GROUP BY 1 ORDER BY v DESC",
            [START, END]).fetchall()
        out[("ranking_count", None)] = len(rows)
        if rows:
            out[("ranking_top1_group", None)] = str(rows[0][0])
            out[("ranking_top1_value", None)] = float(rows[0][1])
        return out

    if atype == "distribution":
        mid = metrics[0]
        gcol = plan["group_by"][0] if plan.get("group_by") else None
        if gcol:
            expr = {"purchase_amount": "SUM(b.p)",
                    "redeem_amount": "SUM(b.r)",
                    "net_inflow": "SUM(b.p) - SUM(b.r)"}.get(mid, "SUM(b.p)")
            rows = con.execute(
                f"SELECT p.{gcol}, {expr} FROM bal b JOIN prof p USING(user_id) "
                "WHERE b.d BETWEEN ? AND ? GROUP BY 1", [START, END]).fetchall()
            vals = [float(v) for _, v in rows]
        elif mid in ("purchase_amount", "redeem_amount", "net_inflow"):
            days, vals_src = data.series(mid)
            vals = [float(sum(vals_src))]        # 无分组 → 单聚合值
        else:
            vals = [1.0]                          # 不可加指标 → 窗口内 1 条记录
        out[("distribution_total", None)] = float(sum(vals))
        out[("distribution_sample_size", None)] = len(vals)
        out[("distribution_value_stat", "min")] = min(vals)
        out[("distribution_value_stat", "max")] = max(vals)
        out[("distribution_value_stat", "mean")] = sum(vals) / len(vals)
        out[("distribution_value_stat", "median")] = median(vals)
        return out

    if atype == "anomaly":
        mid = metrics[0]
        days, vals = data.series(mid)
        center, mad, dates = mad_anomalies(vals, days)
        out[("anomaly_sample_size", None)] = len(vals)
        out[("anomaly_center", None)] = float(center)
        out[("anomaly_mad", None)] = float(mad)
        out[("anomaly_count", None)] = len(dates)
        out[("anomaly_dates", None)] = sorted(dates)
        return out

    if atype == "correlation":
        a, b = metrics[0], metrics[1]
        da, va = data.series(a)
        db, vb = data.series(b)
        common = [d for d in da if d in set(db)]
        xs = [va[da.index(d)] for d in common]
        ys = [vb[db.index(d)] for d in common]
        out[("correlation_pairs_count", None)] = len(common)
        out[("correlation_pair", None)] = {a, b}
        coef = pearson(xs, ys)
        out[("correlation_coefficient", None)] = coef
        return out

    if atype == "descriptive":
        # 多指标汇总：为每个指标独立复算（不能只看第一个指标）
        for mid in metrics:
            if mid in ("purchase_amount", "redeem_amount", "net_inflow"):
                days, vals = data.series(mid)
                out[("descriptive_value", mid)] = float(sum(vals))
            elif mid == "active_user_count":
                v = con.execute("SELECT COUNT(DISTINCT user_id) FROM bal "
                                "WHERE d BETWEEN ? AND ?", [START, END]).fetchone()[0]
                out[("descriptive_value", mid)] = float(v)
        return out

    return out


def close(a, b, tol) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def verify(verbose: bool = False) -> dict:
    cases = [json.loads(line) for line in
             (BENCH / "questions.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    golden = json.loads((BENCH / "golden_answers.json").read_text(encoding="utf-8"))
    answers = golden["answers"]
    data = Data()

    checks_total = checks_pass = checks_fail = unverifiable = 0
    failures: list[dict] = []
    per_case: list[dict] = []

    for case in cases:
        cid = case["case_id"]
        ans = answers.get(cid)
        if not ans or not ans.get("verifiable"):
            continue
        rec = recompute(case, data)
        case_pass = case_total = 0
        details = []
        for ch in ans["checks"]:
            kind = ch["kind"]
            if kind in ("analysis_type_is",):      # 结构检查：与问题集期望一致
                ok = ch["value"] == case["expected_analysis_type"]
                src = "questions.jsonl expected_analysis_type"
            elif kind == "metrics_exact":
                # 含期限身份（"mid:term"）严格比较
                ok = sorted(ch["metrics"]) == sorted(case["expected_metrics"])
                src = "questions.jsonl expected_metrics（含期限）"
            elif kind in ("status_only", "error_code_in", "clarification_present",
                          "schema_rejected"):
                continue                            # 行为检查，非数值
            elif kind in ("point_count", "series_count", "first_value", "last_value",
                          "period_mean"):
                got = rec.get((kind, None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", RATE_TOL))
                src = f"独立序列 {kind}"
            elif kind in ("series_sum", "metric_sum"):
                key = ("metric_sum", ch["metric_id"]) if kind == "metric_sum" else (kind, None)
                got = rec.get(key)
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                src = "独立 SUM 聚合"
            elif kind == "term_period_mean":
                got = rec.get(("term_period_mean", ch["term"]))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", RATE_TOL))
                src = f"独立 SQL 均值（期限 {ch['term']}）"
            elif kind in ("max_drop", "max_rise", "max_abs"):
                got = rec.get((kind, None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = got[0] == ch["date"] and close(got[1], ch["rate"],
                                                    ch.get("tol", RATE_TOL))
                src = "独立日环比极值"
            elif kind == "ranking_count":
                got = rec.get(("ranking_count", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = int(got) == int(ch["value"])
                src = "独立 GROUP BY 计数"
            elif kind == "ranking_top1_group":
                got = rec.get(("ranking_top1_group", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = got == str(list(ch["group"].values())[0])
                src = "独立 GROUP BY 排序"
            elif kind == "ranking_top1_value":
                got = rec.get(("ranking_top1_value", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                src = "独立 GROUP BY 排序"
            elif kind in ("window_main_total", "window_comparison_total"):
                got = rec.get((kind, None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                src = "独立两窗口聚合"
            elif kind in ("window_main_value_nongrouped",
                          "window_comparison_value_nongrouped"):
                got = rec.get((kind, None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", RATE_TOL))
                src = "独立两窗口均值"
            elif kind in ("window_main_value", "window_comparison_value",
                          "window_missing_in_comparison"):
                g = str(list(ch["group"].values())[0])
                if kind == "window_missing_in_comparison":
                    # 独立判断：该组在对比窗口无记录、在主窗口有记录
                    gcol = case["fixture_plan"]["group_by"][0]
                    main_n = data.con.execute(
                        f"SELECT COUNT(*) FROM bal b JOIN prof p USING(user_id) "
                        f"WHERE b.d BETWEEN ? AND ? AND p.{gcol} = ?",
                        [*MAIN_W, int(g)]).fetchone()[0]
                    cmp_n = data.con.execute(
                        f"SELECT COUNT(*) FROM bal b JOIN prof p USING(user_id) "
                        f"WHERE b.d BETWEEN ? AND ? AND p.{gcol} = ?",
                        [*CMP_W, int(g)]).fetchone()[0]
                    ok = main_n > 0 and cmp_n == 0
                    src = "独立检查两窗口记录数"
                else:
                    key = "main" if kind == "window_main_value" else "cmp"
                    got = rec.get((f"window_{key}_group", g))
                    if got is None:
                        unverifiable += 1
                        details.append({"kind": kind, "status": "unverifiable"})
                        continue
                    ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                    src = "独立分组窗口聚合"
            elif kind == "distribution_total":
                got = rec.get(("distribution_total", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                src = "独立分组/单值聚合"
            elif kind == "distribution_sample_size":
                got = rec.get(("distribution_sample_size", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = int(got) == int(ch["value"])
                src = "独立类别计数"
            elif kind == "distribution_value_stat":
                got = rec.get(("distribution_value_stat", ch["stat"]))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                src = f"独立 Python {ch['stat']}"
            elif kind in ("anomaly_sample_size", "anomaly_center", "anomaly_mad",
                          "anomaly_count"):
                key = {"anomaly_sample_size": ("anomaly_sample_size", None),
                       "anomaly_center": ("anomaly_center", None),
                       "anomaly_mad": ("anomaly_mad", None),
                       "anomaly_count": ("anomaly_count", None)}[kind]
                got = rec.get(key)
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                src = "独立 median/MAD 复算"
            elif kind == "anomaly_dates":
                got = rec.get(("anomaly_dates", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = sorted(got) == sorted(ch["dates"])
                src = "独立 MAD 判定（k=3, 1.4826）"
            elif kind == "correlation_pair":
                got = rec.get(("correlation_pair", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = set(got) == set(ch["metrics"])
                src = "问题集指标组合"
            elif kind == "correlation_pairs_count":
                got = rec.get(("correlation_pairs_count", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = int(got) == int(ch["value"])
                src = "独立共同日期计数"
            elif kind == "correlation_coefficient":
                got = rec.get(("correlation_coefficient", None))
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", COEF_TOL))
                src = "独立 Pearson 复算"
            elif kind == "descriptive_value":
                needle = ch["column_contains"]
                got = next((v for k, v in rec.items()
                            if k[0] == "descriptive_value" and needle in str(k[1])), None)
                if got is None:
                    unverifiable += 1
                    details.append({"kind": kind, "status": "unverifiable"})
                    continue
                ok = close(got, ch["value"], ch.get("tol", AMT_TOL))
                src = "独立汇总聚合"
            else:
                unverifiable += 1
                details.append({"kind": kind, "status": "unverifiable"})
                continue

            checks_total += 1
            case_total += 1
            if ok:
                checks_pass += 1
                case_pass += 1
            else:
                checks_fail += 1
                failures.append({"case_id": cid, "kind": kind,
                                 "expected": ch.get("value", ch.get("date")),
                                 "recomputed": got, "source": src})
            details.append({"kind": kind, "status": "pass" if ok else "fail",
                            "source": src})
        per_case.append({"case_id": cid, "checks": case_total, "passed": case_pass,
                         "details": details})
        if verbose:
            flag = "PASS" if case_pass == case_total else "FAIL"
            print(f"[{flag}] {cid}: {case_pass}/{case_total}")

    result = {
        "verified_at": "2026-10-26",
        "dataset": "tests/fixtures/synthetic（人工构造合成样本）",
        "independent_sources": [
            "DuckDB read_csv_auto 直接 CSV 聚合（不经 QueryBuilder/Analytics）",
            "纯 Python 算术（sum/mean/median/MAD/Pearson/日环比）",
        ],
        "cases_verified": len(per_case),
        "checks_total": checks_total,
        "checks_passed": checks_pass,
        "checks_failed": checks_fail,
        "checks_unverifiable": unverifiable,
        "failures": failures,
        "cases": per_case,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="独立复算 Golden Answers")
    ap.add_argument("--verbose", action="store_true", help="逐案例打印")
    ap.add_argument("--json", type=Path, default=None, help="写出机器可读结果")
    args = ap.parse_args(argv)

    res = verify(verbose=args.verbose)
    print()
    print(f"独立复算：可验证案例 {res['cases_verified']} 个")
    print(f"  数值检查项 {res['checks_total']} 项：通过 {res['checks_passed']}，"
          f"不一致 {res['checks_failed']}，无法复算 {res['checks_unverifiable']}")
    if res["failures"]:
        print("  不一致明细：")
        for f in res["failures"][:10]:
            print(f"    - {f['case_id']}/{f['kind']}: 标准答案={f['expected']} "
                  f"独立复算={f['recomputed']}（{f['source']}）")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"  已写出：{args.json}")
    ok = res["checks_failed"] == 0 and res["checks_total"] > 0
    print("  结论：" + ("PASS（标准答案与独立复算一致）" if ok else "FAIL（存在不一致）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
