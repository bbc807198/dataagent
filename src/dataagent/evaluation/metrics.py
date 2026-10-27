"""评测指标：check 比对与汇总统计（阶段 11）。

标准答案来自 `benchmark/golden_answers.json`（独立核算），本模块只负责
**从被测系统输出中提取同名字段并比对**，不参与标准答案生成。

比较规则：
- 数值：显式绝对容差（金额 0.01 元；比率/收益率 1e-6）；
- 日期 / 指标 ID / 期限 / 状态：**严格**相等；
- 缺失值：期望为数值而实际缺失 → 判失败（不得当作通过）。
"""

from __future__ import annotations

import statistics
from typing import Any

from .models import CaseResult, CheckResult, EvaluationSummary, TokenUsage


# ---------------------------------------------------------------------------
# 提取（从 AnalysisResult 读取被测系统自身的输出）
# ---------------------------------------------------------------------------
def _trend_series(analysis) -> list:
    """只返回真正的趋势序列（按 discriminator kind 判定）。

    注意：AnomalyStats 也有 `points` 字段，不能用 hasattr 判断，
    否则会被误认成趋势序列（阶段 12 A3 修复）。
    """
    stats = getattr(analysis, "statistics", None)
    if stats is None:
        return []
    kind = getattr(stats, "kind", None)
    if kind == "trend_set":
        return list(getattr(stats, "series", []) or [])
    if kind == "trend":
        return [stats]
    return []


def _pick_series(series: list, metric_id: str | None = None,
                 term: str | None = None):
    for s in series:
        if metric_id is not None and getattr(s, "metric_id", None) != metric_id:
            continue
        if term is not None and getattr(s, "term", None) != term:
            continue
        return s
    return None


def _extremum(series, kind: str):
    changes = [c for c in series.daily_changes if c.change_rate is not None]
    if not changes:
        return None
    if kind == "max_drop":
        neg = [c for c in changes if c.change_rate < 0]
        return min(neg, key=lambda c: c.change_rate) if neg else None
    if kind == "max_rise":
        pos = [c for c in changes if c.change_rate > 0]
        return max(pos, key=lambda c: c.change_rate) if pos else None
    return max(changes, key=lambda c: abs(c.change_rate))


def _num_eq(actual, expected, tol: float) -> bool:
    if actual is None or expected is None:
        return False
    try:
        return abs(float(actual) - float(expected)) <= tol
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 单个 check
# ---------------------------------------------------------------------------
def compare_check(spec: dict, result: CaseResult, analysis,
                  agent_status: str, error_code: str | None,
                  clarification: list[str], schema_rejected: bool) -> CheckResult:
    kind = spec["kind"]
    tol = spec.get("tol")

    if kind == "status_only":
        # 仅用于"执行成功/行为"检查；**不作为数值正确性证据**（不计入数值正确率）
        ok = bool(result.analysis_succeeded)
        return CheckResult(kind, ok, "analysis_succeeded",
                           f"{result.status}/analysis={result.analysis_succeeded}",
                           detail="仅验证执行成功（非数值正确性）", tolerance=tol)

    if kind == "error_code_in":
        ok = (error_code or "") in spec["values"] or result.status in spec["values"]
        return CheckResult(kind, ok, spec["values"], error_code or result.status,
                           detail=f"status={result.status}")

    if kind == "clarification_present":
        ok = bool(clarification) or result.status == "needs_clarification"
        return CheckResult(kind, ok, ">=1 澄清问题", len(clarification),
                           detail=f"status={result.status}")

    if kind == "schema_rejected":
        ok = schema_rejected
        return CheckResult(kind, ok, True, schema_rejected,
                           detail=f"status={result.status}, error_code={error_code}")

    # ---- 口径与身份检查（严格比较）----
    if kind == "analysis_type_is":
        actual = getattr(analysis, "analysis_type", None)
        return CheckResult(kind, actual == spec["value"], spec["value"], actual,
                           detail="分析类型必须与标准答案一致")

    if kind == "metrics_exact":
        # A3-1：同时比较 metric_id **与期限**（"mid:term" 形式），
        # 避免只比 metric_id 而遗漏 SHIBOR 等参数化指标的期限差异
        def _split(x: str):
            parts = x.split(":")
            return (parts[0], parts[1] if len(parts) > 1 else None)

        expected = sorted(_split(x) for x in spec["metrics"])
        series = _trend_series(analysis)
        if series:
            actual = sorted({(getattr(s_, "metric_id", None), getattr(s_, "term", None))
                             for s_ in series})
        else:
            actual = sorted({(m.metric_id, None)
                             for m in (getattr(analysis, "metrics", None) or [])})
        ok = actual == expected
        return CheckResult(kind, ok,
                           [f"{m}{':' + t if t else ''}" for m, t in expected],
                           [f"{m}{':' + t if t else ''}" for m, t in actual])

    if kind == "metric_terms_exact":
        # 期限一致性：比较 (metric_id, term) 集合，避免只比 metric_id 遗漏期限
        expected = {(m, t) for m, t in
                    ((x.split(":") + [None])[:2] for x in spec["metrics"])}
        series = _trend_series(analysis)
        if series:
            actual = {(getattr(s_, "metric_id", None), getattr(s_, "term", None))
                      for s_ in series}
        else:
            actual = {(m.metric_id, None)
                      for m in (getattr(analysis, "metrics", None) or [])}
        ok = actual == expected
        return CheckResult(kind, ok, sorted(str(x) for x in expected),
                           sorted(str(x) for x in actual))

    if kind == "term_present":
        terms = {getattr(x, "term", None) for x in _trend_series(analysis)}
        terms.discard(None)
        present = spec["term"] in terms
        return CheckResult(kind, present == spec.get("present", True),
                           spec["term"], sorted(terms))

    # ---- 分布核验 ----
    if kind == "distribution_total":
        stats = getattr(analysis, "statistics", None)
        actual = getattr(stats, "total_count", None)
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, tolerance=tol)

    if kind == "distribution_sample_size":
        stats = getattr(analysis, "statistics", None)
        actual = getattr(stats, "sample_size", None)
        return CheckResult(kind, actual == spec["value"], spec["value"], actual)

    if kind == "distribution_value_stat":
        stats = getattr(analysis, "statistics", None)
        vs = getattr(stats, "value_stats", None) or {}
        actual = vs.get(spec["stat"])
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, detail=f"value_stats[{spec['stat']}]",
                           tolerance=tol)

    # ---- MAD 异常核验 ----
    if kind == "anomaly_sample_size":
        stats = getattr(analysis, "statistics", None)
        actual = getattr(stats, "sample_size", None)
        return CheckResult(kind, actual == spec["value"], spec["value"], actual)

    if kind == "anomaly_center":
        stats = getattr(analysis, "statistics", None)
        actual = getattr(stats, "center", None)
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, detail="稳健中心（中位数）", tolerance=tol)

    if kind == "anomaly_mad":
        stats = getattr(analysis, "statistics", None)
        actual = getattr(stats, "mad", None)
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, detail="MAD", tolerance=tol)

    if kind == "anomaly_count":
        stats = getattr(analysis, "statistics", None)
        actual = len(getattr(stats, "anomalies", None) or [])
        return CheckResult(kind, actual == spec["value"], spec["value"], actual)

    if kind == "anomaly_dates":
        stats = getattr(analysis, "statistics", None)
        actual = sorted(p.date for p in (getattr(stats, "anomalies", None) or []))
        expected = sorted(spec["dates"])
        return CheckResult(kind, actual == expected, expected, actual)

    # ---- 关联核验 ----
    if kind == "correlation_pair":
        stats = getattr(analysis, "statistics", None)
        actual = {getattr(stats, "metric_a", None), getattr(stats, "metric_b", None)}
        expected = set(spec["metrics"])
        return CheckResult(kind, actual == expected, sorted(expected), sorted(actual))

    if kind == "correlation_coefficient":
        stats = getattr(analysis, "statistics", None)
        actual = getattr(stats, "coefficient", None)
        method = getattr(stats, "method", None)
        ok = _num_eq(actual, spec["value"], tol) and method == spec.get("method", "pearson")
        return CheckResult(kind, ok, {"coefficient": spec["value"],
                                      "method": spec.get("method", "pearson")},
                           {"coefficient": actual, "method": method}, tolerance=tol)

    if kind == "correlation_pairs_count":
        stats = getattr(analysis, "statistics", None)
        actual = getattr(stats, "n_pairs", None)
        return CheckResult(kind, actual == spec["value"], spec["value"], actual)

    series = _trend_series(analysis)

    if kind == "point_count":
        s = series[0] if series else None
        actual = len(s.points) if s else None
        return CheckResult(kind, actual == spec["value"], spec["value"], actual)

    if kind == "series_count":
        actual = len(series)
        return CheckResult(kind, actual == spec["value"], spec["value"], actual)

    if kind == "series_sum":
        s = series[0] if series else None
        if not s:
            return CheckResult(kind, False, spec["value"], None, "无趋势序列")
        actual = s.total if s.total is not None else sum(p.value for p in s.points)
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, tolerance=tol)

    if kind == "metric_sum":
        s = _pick_series(series, metric_id=spec["metric_id"])
        if not s:
            return CheckResult(kind, False, spec["value"], None,
                               f"缺少指标 {spec['metric_id']}")
        actual = s.total if s.total is not None else sum(p.value for p in s.points)
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, tolerance=tol)

    if kind == "period_mean":
        s = series[0] if series else None
        if not s:
            return CheckResult(kind, False, spec["value"], None, "无趋势序列")
        if spec["value"] is None:      # 仅要求存在期间均值
            ok = s.period_mean is not None
            return CheckResult(kind, ok, "not None", s.period_mean)
        return CheckResult(kind, _num_eq(s.period_mean, spec["value"], tol),
                           spec["value"], s.period_mean, tolerance=tol)

    if kind == "term_period_mean":
        s = _pick_series(series, term=spec["term"])
        if not s:
            return CheckResult(kind, False, spec["value"], None,
                               f"缺少期限 {spec['term']}")
        return CheckResult(kind, _num_eq(s.period_mean, spec["value"], tol),
                           spec["value"], s.period_mean, tolerance=tol)

    if kind in ("first_value", "last_value"):
        s = series[0] if series else None
        if not s or not s.points:
            return CheckResult(kind, False, spec["value"], None, "无数据点")
        actual = (s.points[0].value if kind == "first_value"
                  else s.points[-1].value)
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, tolerance=tol)

    if kind in ("max_drop", "max_rise", "max_abs"):
        s = _pick_series(series, metric_id=spec.get("metric_id")) or (series[0] if series else None)
        if not s:
            return CheckResult(kind, False, spec, None, "无趋势序列")
        c = _extremum(s, kind)
        if c is None:
            return CheckResult(kind, False, spec, None, "未计算该极值（可能无有效变化率）")
        ok = c.date == spec["date"] and _num_eq(c.change_rate, spec["rate"], tol)
        return CheckResult(kind, ok, {"date": spec["date"], "rate": spec["rate"]},
                           {"date": c.date, "rate": c.change_rate}, tolerance=tol)

    if kind.startswith("ranking_"):
        stats = getattr(analysis, "statistics", None)
        entries = getattr(stats, "entries", None)
        if not entries:
            return CheckResult(kind, False, spec.get("value", spec.get("group")),
                               None, "无排名结果")
        if kind == "ranking_count":
            return CheckResult(kind, len(entries) == spec["value"], spec["value"],
                               len(entries))
        top = entries[0]
        if kind == "ranking_top1_group":
            actual = {k: str(v) for k, v in top.group.items()}
            expected = {k: str(v) for k, v in spec["group"].items()}
            ok = all(actual.get(k) == v for k, v in expected.items())
            return CheckResult(kind, ok, expected, actual)
        actual = top.value
        return CheckResult(kind, _num_eq(actual, spec["value"], tol), spec["value"],
                           actual, tolerance=tol)

    if kind.startswith("window_"):
        stats = getattr(analysis, "statistics", None)
        pairs = getattr(stats, "pairs", None)
        if not pairs:
            return CheckResult(kind, False, spec.get("value"), None, "无窗口对比结果")

        if kind in ("window_main_total", "window_comparison_total"):
            key = "main_value" if kind == "window_main_total" else "comparison_value"
            actual = sum(getattr(p, key) for p in pairs)
            return CheckResult(kind, _num_eq(actual, spec["value"], tol),
                               spec["value"], actual, tolerance=tol)

        if kind in ("window_main_value_nongrouped", "window_comparison_value_nongrouped"):
            key = ("main_value" if kind == "window_main_value_nongrouped"
                   else "comparison_value")
            actual = sum(getattr(p, key) for p in pairs) / len(pairs)
            return CheckResult(kind, _num_eq(actual, spec["value"], tol),
                               spec["value"], actual, tolerance=tol)

        if kind == "window_missing_in_comparison":
            # 组出现在主窗口但对比窗口无记录（"缺组不视为 0" 的正确表达）
            expected_group = {k: str(v) for k, v in spec["group"].items()}
            missing = getattr(stats, "missing_in_comparison", None) or []
            for g in missing:
                actual_group = {k: str(v) for k, v in g.items()}
                if all(actual_group.get(k) == v for k, v in expected_group.items()):
                    return CheckResult(kind, True, expected_group, actual_group,
                                       detail="该组仅在主窗口出现（未按 0 处理）")
            return CheckResult(kind, False, expected_group, missing,
                               detail="missing_in_comparison 未包含该组")

        key = ("main_value" if kind == "window_main_value" else "comparison_value")
        expected_group = {k: str(v) for k, v in spec["group"].items()}
        for p in pairs:
            actual_group = {k: str(v) for k, v in p.group.items()}
            if all(actual_group.get(k) == v for k, v in expected_group.items()):
                actual = getattr(p, key)
                return CheckResult(kind, _num_eq(actual, spec["value"], tol),
                                   spec["value"], actual, tolerance=tol)
        return CheckResult(kind, False, spec["value"], None,
                           f"未找到分组 {expected_group}")

    if kind == "descriptive_value":
        stats = getattr(analysis, "statistics", None)
        values = getattr(stats, "values", None) or {}
        needle = spec["column_contains"]
        for col, val in values.items():
            if needle in col.lower():
                return CheckResult(kind, _num_eq(val, spec["value"], tol),
                                   spec["value"], val, detail=col, tolerance=tol)
        return CheckResult(kind, False, spec["value"], None,
                           f"未找到包含 {needle} 的列（实际列：{list(values)}）")

    return CheckResult(kind, False, None, None, "未知 check 类型")


# ---------------------------------------------------------------------------
# 分位数与汇总
# ---------------------------------------------------------------------------
def percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数（p ∈ [0,100]）；空列表返回 None。"""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    xs = sorted(float(v) for v in values)
    k = (len(xs) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(results: list[CaseResult], run_id: str, model_mode: str,
              data_mode: str, environment: dict,
              extra_verifiable_ids: set[str] | None = None) -> EvaluationSummary:
    s = EvaluationSummary(run_id=run_id, model_mode=model_mode,
                          data_mode=data_mode, environment=environment)
    s.total_cases = len(results)
    valid = [r for r in results if r.expected_behavior == "success"]
    neg = [r for r in results if r.expected_behavior != "success"]
    s.valid_cases = len(valid)
    s.negative_cases = len(neg)

    # 查询成功率：有效案例中 SQL 与统计都成功
    s.query_success = sum(1 for r in valid if r.query_executed and r.analysis_succeeded)
    s.query_success_rate = (s.query_success / len(valid)) if valid else None

    # 报告生成成功率（单独口径）
    s.report_generated = sum(1 for r in valid if r.report_generated)
    s.report_success_rate = (s.report_generated / len(valid)) if valid else None

    # ---- 答案正确率：分母由标准答案**事前**声明（固定），不因执行失败缩小 ----
    # numeric_denominator_ids 由 runner 传入（verifiable=True 的有效案例）
    denom_ids = set(extra_verifiable_ids or ())
    if denom_ids:
        denom = [r for r in valid if r.case_id in denom_ids
                 or getattr(r, "verifiable_expected", False)]
    else:
        denom = [r for r in valid if getattr(r, "verifiable_expected", False)]
    s.verifiable_cases = len(denom)                      # 固定分母
    s.verifiable_evaluated = sum(1 for r in denom if r.answers_verified)
    s.execution_only_cases = sum(1 for r in valid
                                 if not getattr(r, "verifiable_expected", False))
    s.answers_correct = sum(1 for r in denom if r.answers_correct is True)
    s.answer_accuracy = (s.answers_correct / len(denom)) if denom else None

    # ---- 行为正确率（全部案例，与成功率/正确率并列展示）----
    s.behavior_accuracy = (sum(1 for r in results if r.behavior_correct) / len(results)
                           if results else None)

    # 正确拒绝率 / 误拒绝率
    s.negative_correct = sum(1 for r in neg if r.behavior_correct)
    s.negative_accuracy = (s.negative_correct / len(neg)) if neg else None
    s.false_rejections = sum(1 for r in valid if not r.report_generated)
    s.false_rejection_rate = (s.false_rejections / len(valid)) if valid else None

    # 响应时间（端到端，perf_counter 计时）
    lat = [r.latency_ms for r in results]
    if lat:
        s.latency_avg_ms = round(statistics.fmean(lat), 3)
        s.latency_p50_ms = round(percentile(lat, 50) or 0.0, 3)
        s.latency_p95_ms = round(percentile(lat, 95) or 0.0, 3)
        s.latency_min_ms = round(min(lat), 3)
        s.latency_max_ms = round(max(lat), 3)
    ok_lat = [r.latency_ms for r in results if r.behavior_correct]
    bad_lat = [r.latency_ms for r in results if r.behavior_correct is False]
    s.latency_avg_success_ms = round(statistics.fmean(ok_lat), 3) if ok_lat else None
    s.latency_avg_failure_ms = round(statistics.fmean(bad_lat), 3) if bad_lat else None

    # Token / 成本（离线不适用）
    tokens = [r.token_usage.total_tokens for r in results
              if r.token_usage.applicable and r.token_usage.total_tokens is not None]
    s.token_calls = sum(r.token_usage.calls for r in results if r.token_usage.applicable)
    s.token_incomplete_cases = sum(
        1 for r in results if getattr(r, "usage_complete", True) is False)
    s.token_total = sum(tokens) if tokens else None
    costs = [r.cost.estimated_cost for r in results
             if r.cost.estimated_cost is not None]
    s.cost_total = round(sum(costs), 6) if costs else None
    if model_mode == "mock":
        s.cost_note = "不适用：离线/Mock 模式未调用真实 API（Token 与费用记为 N/A）"
    elif s.cost_total is None:
        s.cost_note = "未计算：API 未返回 token 用量或未配置可信价格表"
    else:
        s.cost_note = "按配置价格表估算（见 benchmark/pricing.json）"

    # 分组表现
    def bucket(key_fn):
        out: dict[str, dict] = {}
        for r in results:
            k = key_fn(r)
            b = out.setdefault(k, {"total": 0, "behavior_correct": 0,
                                   "answers_verified": 0, "answers_correct": 0})
            b["total"] += 1
            if r.behavior_correct:
                b["behavior_correct"] += 1
            if r.answers_verified:
                b["answers_verified"] += 1
                if r.answers_correct:
                    b["answers_correct"] += 1
        return out

    s.by_category = bucket(lambda r: r.category)
    s.by_analysis_type = bucket(lambda r: (r.analysis_type or r.expected_analysis_type))

    s.failures = [
        {"case_id": r.case_id, "category": r.category, "status": r.status,
         "error_code": r.error_code, "expected_behavior": r.expected_behavior,
         "expected_status": r.expected_status,
         "failure_reason": r.failure_reason,
         "failed_checks": [c.kind for c in r.checks if not c.passed]}
        for r in results if r.behavior_correct is False or r.answers_correct is False
    ]
    return s
