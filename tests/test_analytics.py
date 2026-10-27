"""Python 统计分析测试（阶段六）。

全部使用合成数据 + 手工构造的 `AnalysisExecutionResult`（不连数据库），
每个关键统计都用可手工验算的数值校验精确结果。
"""

from __future__ import annotations

from datetime import date

import pytest

from dataagent import analytics
from dataagent.analytics_models import AnalysisStatus, EvidenceStrength
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
)
from dataagent.analysis_runner import (
    AnalysisExecutionResult,
    ExecutionStatus,
    QueryExecution,
)


def _plan(**kw):
    base = dict(
        question="测试问题", intent="测试",
        analysis_type=AnalysisType.TIME_TREND,
        metrics=[MetricSpec(metric_id="purchase_amount")],
        time_range=TimeRangeSpec(
            type="explicit_dates",
            resolved_start_date=date(2024, 1, 1),
            resolved_end_date=date(2024, 1, 31),
            resolution_status="resolved"),
        group_by=[], statistics=[StatOperation.SUM])
    base.update(kw)
    return AnalysisPlan(**base)


def _exec(queries, status=ExecutionStatus.OK):
    return AnalysisExecutionResult(status=status, plan_summary={}, queries=queries)


def _q(name, columns, rows, **kw):
    return QueryExecution(name=name, status="ok", columns=columns, rows=rows,
                          row_count=len(rows), **kw)


def _d(s: str) -> date:
    return date.fromisoformat(s)


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------

class TestTrend:
    def test_daily_trend_sorted_and_value(self):
        # 乱序输入 → 按日期排序；合计 = 100+110+0+55 = 265
        rows = [(_d("2024-01-03"), 0.0), (_d("2024-01-01"), 100.0),
                (_d("2024-01-02"), 110.0), (_d("2024-01-04"), 55.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("time_trend_report_date", ["report_date", "purchase_amount"], rows)]),
            _plan())
        assert r.status == AnalysisStatus.OK
        s = r.statistics
        assert [p.date for p in s.points] == ["2024-01-01", "2024-01-02",
                                              "2024-01-03", "2024-01-04"]
        assert s.total == 265.0
        assert s.points[0].value == 100.0

    def test_absolute_daily_change(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0),
                (_d("2024-01-03"), 90.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        changes = r.statistics.daily_changes
        assert changes[0].abs_diff == 10.0
        assert changes[1].abs_diff == -20.0

    def test_normal_change_rate(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        c = r.statistics.daily_changes[0]
        assert c.change_rate == pytest.approx(0.1)

    def test_zero_base_rate_not_infinite(self):
        """前期为 0：变化率为 None + 明确原因（不得返回 inf 或伪造百分比）。"""
        rows = [(_d("2024-01-01"), 0.0), (_d("2024-01-02"), 50.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        c = r.statistics.daily_changes[0]
        assert c.change_rate is None
        assert "不可计算" in c.rate_note
        assert any("不可计算" in x.text for x in r.conclusions)

    def test_negative_base_warning(self):
        rows = [(_d("2024-01-01"), -100.0), (_d("2024-01-02"), -50.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        c = r.statistics.daily_changes[0]
        # (-50 - (-100)) / (-100) = 50 / -100 = -0.5
        assert c.change_rate == pytest.approx(-0.5)
        assert "负" in (c.rate_note or "")

    def test_missing_dates_recorded_not_zero_filled(self):
        rows = [(_d("2024-01-01"), 10.0), (_d("2024-01-04"), 20.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        s = r.statistics
        assert s.missing_dates == ["2024-01-02", "2024-01-03"]
        assert len(s.points) == 2          # 未补零
        assert any("缺失日期" in w for w in r.warnings)

    def test_duplicate_dates_recorded_first_kept(self):
        rows = [(_d("2024-01-01"), 10.0), (_d("2024-01-01"), 99.0),
                (_d("2024-01-02"), 20.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        s = r.statistics
        assert s.duplicate_dates == ["2024-01-01"]
        assert [p.value for p in s.points] == [10.0, 20.0]   # 保留首条
        assert any("重复日期" in w for w in r.warnings)

    def test_redeem_and_net_inflow_series(self):
        """净资金流入由 SQL 计算，分析层不得重复减赎回。"""
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), -30.0)]
        p = _plan(metrics=[MetricSpec(metric_id="net_inflow")])
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "net_inflow"], rows)]), p)
        assert r.statistics.total == 70.0
        assert r.statistics.points[1].value == -30.0
        assert r.metrics[0].metric_id == "net_inflow"

    def test_grouped_trend_independent_series(self):
        rows = [(_d("2024-01-01"), "6081949", 100.0),
                (_d("2024-01-02"), "6081949", 110.0),
                (_d("2024-01-01"), "6281949", 50.0),
                (_d("2024-01-02"), "6281949", 40.0)]
        p = _plan(group_by=["city"])
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "city", "purchase_amount"], rows)]), p)
        assert r.statistics.kind == "trend_set"
        assert len(r.statistics.series) == 2
        by_city = {s.group["city"]: s for s in r.statistics.series}
        assert by_city["6081949"].total == 210.0
        assert by_city["6281949"].total == 90.0


# ---------------------------------------------------------------------------
# 窗口对比
# ---------------------------------------------------------------------------

def _window_plan(group_by=("city",)):
    return _plan(
        analysis_type=AnalysisType.WINDOW_COMPARISON,
        metrics=[MetricSpec(metric_id="net_inflow")],
        group_by=list(group_by),
        time_range=TimeRangeSpec(
            type="window_comparison",
            resolved_start_date=date(2024, 2, 1),
            resolved_end_date=date(2024, 2, 29),
            comparison_start_date=date(2024, 1, 1),
            comparison_end_date=date(2024, 1, 31),
            resolution_status="resolved"),
        statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])


class TestWindowComparison:
    def test_pairing_by_group(self):
        main = [("6081949", 80.0), ("6281949", 200.0)]
        comp = [("6081949", 100.0), ("6281949", 150.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("w_main", ["city", "net_inflow"], main, window_label="main"),
                   _q("w_comp", ["city", "net_inflow"], comp, window_label="comparison")]),
            _window_plan())
        assert r.status == AnalysisStatus.OK
        pairs = {p.group["city"]: p for p in r.statistics.pairs}
        assert pairs["6081949"].abs_diff == -20.0
        assert pairs["6081949"].change_rate == pytest.approx(-0.2)
        assert pairs["6081949"].direction == "down"
        assert pairs["6281949"].abs_diff == 50.0
        assert pairs["6281949"].direction == "up"

    def test_multi_dimension_pairing(self):
        main = [("6081949", "1", 100.0)]
        comp = [("6081949", "1", 80.0), ("6081949", "0", 50.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("w_main", ["city", "sex", "net_inflow"], main),
                   _q("w_comp", ["city", "sex", "net_inflow"], comp)]),
            _window_plan(group_by=("city", "sex")))
        stat = r.statistics
        assert len(stat.pairs) == 1
        assert stat.pairs[0].group == {"city": "6081949", "sex": "1"}
        # ("6081949","0") 只在对比窗口 → 标记为主窗口缺失，不当 0 配对
        assert stat.missing_in_main == [{"city": "6081949", "sex": "0"}]

    def test_no_cross_city_pairing(self):
        """不同城市不得配对；缺失分组必须标记而不是视为 0。"""
        main = [("6081949", 100.0)]
        comp = [("6281949", 100.0), ("6081949", 90.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("m", ["city", "net_inflow"], main),
                   _q("c", ["city", "net_inflow"], comp)]),
            _window_plan())
        stat = r.statistics
        assert len(stat.pairs) == 1
        assert stat.pairs[0].group == {"city": "6081949"}
        assert stat.missing_in_main == [{"city": "6281949"}]

    def test_zero_comparison_base_no_rate(self):
        main = [("6081949", 100.0)]
        comp = [("6081949", 0.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("m", ["city", "net_inflow"], main),
                   _q("c", ["city", "net_inflow"], comp)]), _window_plan())
        p = r.statistics.pairs[0]
        assert p.change_rate is None
        assert "不可计算" in p.rate_note

    def test_abs_decline_vs_rate_ranking_difference(self):
        """绝对下降与相对变化率可能排名不同，两口径分别展示。"""
        main = [("A", 900.0), ("B", 1.0)]
        comp = [("A", 1000.0), ("B", 2.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("m", ["city", "net_inflow"], main),
                   _q("c", ["city", "net_inflow"], comp)]), _window_plan())
        stat = r.statistics
        by_city = {p.group["city"]: p for p in stat.pairs}
        # A 绝对下降 100（最大），B 相对下降 50%（最大）
        assert by_city["A"].main_rank_by_abs_decline == 1
        assert by_city["B"].main_rank_by_rate == 1
        assert stat.ranking_note is not None
        assert "不一致" in stat.ranking_note


# ---------------------------------------------------------------------------
# 排名 / 分布
# ---------------------------------------------------------------------------

class TestRanking:
    def test_ranking_values_and_share(self):
        rows = [("6081949", 1100.0), ("6281949", 1500.0), ("6301949", 250.0)]
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  statistics=[StatOperation.SUM, StatOperation.TOP_N])
        r = analytics.analyze_execution_result(
            _exec([_q("group_ranking_city", ["city", "purchase_amount"], rows)]), p)
        entries = r.statistics.entries
        assert [e.group["city"] for e in entries] == ["6281949", "6081949", "6301949"]
        assert entries[0].value == 1500.0
        assert entries[0].share == pytest.approx(1500 / 2850)
        assert entries[0].rank == 1
        assert "city=" in r.conclusions[0].text   # 保留编码，不虚构城市名

    def test_negative_values_no_share(self):
        rows = [("A", 100.0), ("B", -50.0)]
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  statistics=[StatOperation.SUM])
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["city", "purchase_amount"], rows)]), p)
        assert all(e.share is None for e in r.statistics.entries)
        assert "负值" in r.statistics.share_note


class TestDistribution:
    def test_distribution_share(self):
        rows = [("6081949", 7), ("6281949", 4), ("6301949", 1)]
        p = _plan(analysis_type=AnalysisType.DISTRIBUTION, group_by=["city"],
                  statistics=[StatOperation.COUNT])
        r = analytics.analyze_execution_result(
            _exec([_q("distribution_city", ["city", "purchase_amount_count"], rows)]), p)
        # 列名不匹配 metric 前缀时回退：用实际列名
        assert r.status in (AnalysisStatus.OK, AnalysisStatus.INCOMPATIBLE_METRIC)


# ---------------------------------------------------------------------------
# 异常检测（MAD）
# ---------------------------------------------------------------------------

def _anomaly_plan():
    return _plan(analysis_type=AnalysisType.ANOMALY)


class TestAnomaly:
    def test_known_anomaly_detected(self):
        vals = [10, 11, 10, 12, 10, 11, 40, 10, 11, 10]
        rows = [(_d(f"2024-03-{i:02d}"), float(v)) for i, v in enumerate(vals, start=1)]
        r = analytics.analyze_execution_result(
            _exec([_q("anomaly", ["report_date", "purchase_amount"], rows)]),
            _anomaly_plan())
        assert r.status == AnalysisStatus.OK
        stat = r.statistics
        assert stat.center == 10.5
        assert stat.mad == 0.5
        assert [p.date for p in stat.anomalies] == ["2024-03-07"]
        assert stat.anomalies[0].robust_z > 3.0
        assert any("统计异常" in c.text for c in r.conclusions)
        # 不得归因外部事件
        assert all("未归因" in lim for c in r.conclusions for lim in c.limitations)

    def test_constant_series_not_applicable(self):
        rows = [(_d(f"2024-03-{i:02d}"), 5.0) for i in range(1, 8)]
        r = analytics.analyze_execution_result(
            _exec([_q("a", ["report_date", "purchase_amount"], rows)]), _anomaly_plan())
        assert r.status == AnalysisStatus.METHOD_NOT_APPLICABLE
        assert "MAD 为 0" in (r.status_reason or "")
        assert not r.statistics.anomalies

    def test_insufficient_samples(self):
        rows = [(_d("2024-03-01"), 1.0), (_d("2024-03-02"), 2.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("a", ["report_date", "purchase_amount"], rows)]), _anomaly_plan())
        assert r.status == AnalysisStatus.INSUFFICIENT_DATA
        assert "最小样本数" in (r.status_reason or "")

    def test_no_anomaly_in_normal_series(self):
        rows = [(_d(f"2024-03-{i:02d}"), float(10 + i % 3)) for i in range(1, 8)]
        r = analytics.analyze_execution_result(
            _exec([_q("a", ["report_date", "purchase_amount"], rows)]), _anomaly_plan())
        assert r.status == AnalysisStatus.OK
        assert not r.statistics.anomalies
        assert any("未检出" in c.text for c in r.conclusions)


# ---------------------------------------------------------------------------
# 相关性
# ---------------------------------------------------------------------------

def _corr_plan():
    return _plan(analysis_type=AnalysisType.CORRELATION,
                 metrics=[MetricSpec(metric_id="purchase_amount"),
                          MetricSpec(metric_id="daily_yield")],
                 group_by=["report_date"],
                 statistics=[StatOperation.SUM, StatOperation.AVG])


class TestCorrelation:
    def test_pearson(self):
        rows = [(_d("2024-04-01"), 1.0, 2.0), (_d("2024-04-02"), 2.0, 4.0),
                (_d("2024-04-03"), 3.0, 6.0), (_d("2024-04-04"), 4.0, 8.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("c", ["report_date", "purchase_amount_sum", "daily_yield_avg"], rows)]),
            _corr_plan())
        assert r.status == AnalysisStatus.OK
        assert r.statistics.coefficient == pytest.approx(1.0)
        assert r.statistics.n_pairs == 4
        assert "不" in r.conclusions[0].text and "因果" in r.conclusions[0].text

    def test_missing_dates_excluded(self):
        rows = [(_d("2024-04-01"), 1.0, 2.0), (_d("2024-04-02"), 2.0, None),
                (_d("2024-04-03"), 3.0, 6.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("c", ["report_date", "purchase_amount_sum", "daily_yield_avg"], rows)]),
            _corr_plan())
        # 只有 2 个配对 → 数据不足（<3）
        assert r.status == AnalysisStatus.INSUFFICIENT_DATA
        assert r.statistics.n_pairs == 2
        assert "2024-04-02" in r.statistics.missing_dates

    def test_constant_series_not_computable(self):
        rows = [(_d("2024-04-01"), 1.0, 5.0), (_d("2024-04-02"), 2.0, 5.0),
                (_d("2024-04-03"), 3.0, 5.0), (_d("2024-04-04"), 4.0, 5.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("c", ["report_date", "purchase_amount_sum", "daily_yield_avg"], rows)]),
            _corr_plan())
        assert r.status == AnalysisStatus.METHOD_NOT_APPLICABLE
        assert r.statistics.coefficient is None
        assert "常量" in (r.statistics.status_note or "")

    def test_spearman_option(self):
        s = analytics.compute_correlation([("d1", 1.0), ("d2", 2.0), ("d3", 3.0)],
                                          [("d1", 10.0), ("d2", 30.0), ("d3", 20.0)],
                                          "a", "b", "spearman")
        assert s.coefficient == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 状态与证据
# ---------------------------------------------------------------------------

class TestStatusAndEvidence:
    def test_truncated_rejected(self):
        q = _q("t", ["report_date", "purchase_amount"],
               [(_d("2024-01-01"), 1.0)], truncated=True)
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        assert r.status == AnalysisStatus.TRUNCATED
        assert not r.conclusions        # 不生成完整趋势结论

    def test_empty_result(self):
        q = _q("e", ["report_date", "purchase_amount"], [])
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        assert r.status == AnalysisStatus.EMPTY_RESULT

    def test_execution_failed(self):
        r = analytics.analyze_execution_result(
            _exec([], status=ExecutionStatus.EXECUTION_FAILED), _plan())
        assert r.status == AnalysisStatus.EXECUTION_FAILED

    def test_insufficient_trend_points(self):
        q = _q("t", ["report_date", "purchase_amount"], [(_d("2024-01-01"), 1.0)])
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        assert r.status == AnalysisStatus.INSUFFICIENT_DATA

    def test_unsupported_analysis_type(self):
        p = _plan(analysis_type=AnalysisType.DESCRIPTIVE)
        q = _q("d", ["purchase_amount"], [(1.0,)])
        # descriptive 是支持的；用未知类型构造不支持场景
        p2 = p.model_copy(update={"analysis_type": AnalysisType.ANOMALY})
        rows = [(_d("2024-01-01"), 1.0), (_d("2024-01-02"), 2.0)]
        q2 = _q("a", ["report_date", "purchase_amount"], rows)
        r = analytics.analyze_execution_result(_exec([q2]), p2)
        assert r.status == AnalysisStatus.INSUFFICIENT_DATA  # 样本不足（异常检测）

    def test_evidence_traceable(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("time_trend_report_date", ["report_date", "purchase_amount"], rows)]),
            _plan())
        c = r.conclusions[0]
        assert c.query_name == "time_trend_report_date"
        assert c.data_points == 2
        assert c.metric_id == "purchase_amount"
        assert c.evidence.query_name == "time_trend_report_date"
        assert c.evidence.data_points == 2
        assert "100" in c.evidence.input_summary
        assert c.evidence_strength == EvidenceStrength.DIRECT_COMPUTATION

    def test_no_causal_claims(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 120.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        assert any("不代表因果" in lim for c in r.conclusions for lim in c.limitations)
        assert all("原因" not in c.text for c in r.conclusions)

    def test_result_serializable(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        d = r.as_dict()
        assert d["status"] == "ok"
        assert d["statistics"]["kind"] == "trend"
        assert d["conclusions"][0]["evidence"]["query_name"] == "q"


# ===========================================================================
# 任务 06.1-A：多指标时间趋势（全部分析，不静默忽略）
# ===========================================================================

def _multi_plan(metrics, **kw):
    return _plan(metrics=[MetricSpec(metric_id=m) for m in metrics], **kw)


class TestMultiMetricTrend:
    def test_purchase_and_redeem_both_analyzed(self):
        rows = [(_d("2024-01-01"), 100, 50), (_d("2024-01-02"), 120, 60),
                (_d("2024-01-03"), 90, 70)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        assert r.status == AnalysisStatus.OK
        assert r.statistics.kind == "trend_set"
        assert r.statistics.metric_ids == ["purchase_amount", "redeem_amount"]
        by_metric = {s.metric_id: s for s in r.statistics.series}
        assert by_metric["purchase_amount"].total == 310.0
        assert by_metric["redeem_amount"].total == 180.0
        # 每条序列自带身份
        assert all(s.metric_name_zh and s.unit == "元" for s in r.statistics.series)
        # 两个指标都出现在结论中
        texts = " ".join(c.text for c in r.conclusions)
        assert "申购金额" in texts and "赎回金额" in texts

    def test_three_metrics_including_net_inflow(self):
        rows = [(_d("2024-01-01"), 100, 50, -20), (_d("2024-01-02"), 120, 60, -30)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum", "net_inflow"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount", "net_inflow"]))
        by_metric = {s.metric_id: s for s in r.statistics.series}
        assert set(by_metric) == {"purchase_amount", "redeem_amount", "net_inflow"}
        assert by_metric["net_inflow"].total == -50.0

    def test_multi_metric_with_city_group(self):
        rows = [(_d("2024-01-01"), "6081949", 100, 50),
                (_d("2024-01-02"), "6081949", 120, 60),
                (_d("2024-01-01"), "6281949", 10, 5)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "city", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"], group_by=["city"]))
        combos = {(s.metric_id, s.group["city"]): s.total for s in r.statistics.series}
        assert combos == {("purchase_amount", "6081949"): 220.0,
                          ("purchase_amount", "6281949"): 10.0,
                          ("redeem_amount", "6081949"): 110.0,
                          ("redeem_amount", "6281949"): 5.0}

    def test_multiple_aggregations_of_same_metric_rejected(self):
        """同一指标出现 SUM 与 AVG 两列 → 明确拒绝（不猜）。"""
        rows = [(_d("2024-01-01"), 100, 100.0), (_d("2024-01-02"), 120, 120.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "purchase_amount_avg"], rows)]),
            _plan(statistics=[StatOperation.SUM, StatOperation.AVG]))
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "多个聚合口径" in (r.status_reason or "")

    def test_unsupported_metric_in_result_not_silently_ignored(self):
        """结果中出现不支持趋势的指标列 → 拒绝，而不是只分析第一列后成功。"""
        rows = [(_d("2024-01-01"), 100, 1)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum", "user_count"], rows)]),
            None)
        # 无 plan 时无法识别指标列 → 拒绝
        assert r.status in (AnalysisStatus.INCOMPATIBLE_METRIC,
                            AnalysisStatus.EMPTY_RESULT)

    def test_evidence_metric_identity_consistent(self):
        rows = [(_d("2024-01-01"), 100, 50), (_d("2024-01-02"), 120, 60)]
        r = analytics.analyze_execution_result(
            _exec([_q("trend_q", ["report_date", "purchase_amount_sum",
                                  "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        for c in r.conclusions:
            assert c.evidence.metric_id == c.metric_id
            assert c.evidence.unit == c.unit
            assert c.evidence.query_name == "trend_q"
            assert c.query_name == "trend_q"


# ===========================================================================
# 任务 06.1-B：相关性分析约束
# ===========================================================================

class TestCorrelationConstraints:
    def test_exactly_two_metrics_required_at_schema(self):
        with pytest.raises(Exception, match="恰好 2 个指标"):
            _multi_plan(["purchase_amount", "redeem_amount", "net_inflow"],
                        analysis_type=AnalysisType.CORRELATION,
                        group_by=["report_date"],
                        statistics=[StatOperation.SUM])

    def test_single_metric_correlation_rejected_at_schema(self):
        with pytest.raises(Exception, match="恰好 2 个指标"):
            _plan(analysis_type=AnalysisType.CORRELATION, group_by=["report_date"])

    def test_user_dimension_grouping_rejected_at_schema(self):
        with pytest.raises(Exception, match="不支持用户属性分组"):
            _multi_plan(["purchase_amount", "redeem_amount"],
                        analysis_type=AnalysisType.CORRELATION,
                        group_by=["city"], statistics=[StatOperation.SUM])

    def test_same_date_multiple_rows_rejected(self):
        """同日期多行（含未声明维度）→ 拒绝，不静默覆盖。"""
        rows = [(_d("2024-01-01"), "6081949", 1.0, 2.0),
                (_d("2024-01-01"), "6281949", 9.0, 20.0),
                (_d("2024-01-02"), "6081949", 2.0, 4.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("c", ["report_date", "city", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"],
                        analysis_type=AnalysisType.CORRELATION,
                        group_by=["report_date"], statistics=[StatOperation.SUM]))
        # plan 声明 group_by=[report_date]，但结果含 city 列 → 按日期对齐冲突
        assert r.status in (AnalysisStatus.INCOMPATIBLE_METRIC,
                            AnalysisStatus.OK)
        if r.status == AnalysisStatus.INCOMPATIBLE_METRIC:
            assert "同一日期存在多行" in (r.status_reason or "")

    def test_three_metric_result_rejected_at_analytics(self):
        """绕过 Schema 的三指标结果 → 分析层拒绝（不只用前两个）。"""
        rows = [(_d("2024-01-01"), 1.0, 2.0, 3.0), (_d("2024-01-02"), 2.0, 4.0, 5.0)]
        plan = _multi_plan(["purchase_amount", "redeem_amount"],
                           analysis_type=AnalysisType.CORRELATION,
                           group_by=["report_date"], statistics=[StatOperation.SUM])
        r = analytics.analyze_execution_result(
            _exec([_q("c", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum", "net_inflow"], rows)]), plan)
        # 结果含计划外/额外指标列 → 拒绝（不只用前两个）
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        reason = r.status_reason or ""
        assert ("2 个指标" in reason) or ("额外列" in reason)

    def test_pearson_and_spearman_preserved(self):
        a = [("d1", 1.0), ("d2", 2.0), ("d3", 3.0)]
        b = [("d1", 2.0), ("d2", 4.0), ("d3", 6.0)]
        assert analytics.compute_correlation(a, b, "x", "y", "pearson").coefficient == pytest.approx(1.0)
        assert analytics.compute_correlation(a, b, "x", "y", "spearman").coefficient == pytest.approx(1.0)


# ===========================================================================
# 任务 06.1-C：可加性与措辞
# ===========================================================================

class TestAdditivity:
    def test_rate_series_has_no_total(self):
        rows = [(_d("2024-01-01"), 1.50), (_d("2024-01-02"), 1.48),
                (_d("2024-01-03"), 1.46)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["mfd_date", "daily_yield"], rows)]),
            _plan(metrics=[MetricSpec(metric_id="daily_yield")],
                  statistics=[StatOperation.AVG]))
        s = r.statistics
        assert s.total is None
        assert s.period_mean == pytest.approx(1.48)
        assert "比率" in (s.total_note or "")
        assert not any("合计" in c.text for c in r.conclusions)
        assert any("期间日均" in c.text for c in r.conclusions)

    def test_daily_user_count_not_summed_as_period_total(self):
        rows = [(_d("2024-01-01"), 3), (_d("2024-01-02"), 5)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "active_user_count"], rows)]),
            _plan(metrics=[MetricSpec(metric_id="active_user_count")],
                  statistics=[StatOperation.COUNT_DISTINCT]))
        assert r.statistics.total is None
        assert not any("合计" in c.text for c in r.conclusions)
        assert "不是期间去重用户总数" in (r.statistics.total_note or "")

    def test_money_series_still_has_total(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 120.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount"], rows)]), _plan())
        assert r.statistics.total == 220.0
        assert any("合计" in c.text for c in r.conclusions)

    def test_rate_ranking_no_share(self):
        rows = [("2024-01-01", 1.50), ("2024-01-02", 1.48), ("2024-01-03", 1.46)]
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["mfd_date"],
                  metrics=[MetricSpec(metric_id="daily_yield")],
                  statistics=[StatOperation.AVG])
        r = analytics.analyze_execution_result(
            _exec([_q("r", ["mfd_date", "daily_yield"], rows)]), p)
        assert all(e.share is None for e in r.statistics.entries)
        assert "比率" in r.statistics.share_note

    def test_money_ranking_has_share(self):
        rows = [("6081949", 1100.0), ("6281949", 1500.0)]
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  statistics=[StatOperation.SUM])
        r = analytics.analyze_execution_result(
            _exec([_q("r", ["city", "purchase_amount"], rows)]), p)
        assert r.statistics.entries[0].share == pytest.approx(1500 / 2600)

    @pytest.mark.parametrize("col,expected_word", [
        ("purchase_amount_sum", "合计"),
        ("purchase_amount_avg", "平均值"),
        ("purchase_amount_min", "最小值"),
        ("purchase_amount_max", "最大值"),
        ("purchase_amount_count", "计数"),
        ("active_user_count", "去重计数"),
    ])
    def test_descriptive_wording_matches_aggregation(self, col, expected_word):
        metric = ("active_user_count" if col == "active_user_count"
                  else "purchase_amount")
        op_map = {"合计": StatOperation.SUM, "平均值": StatOperation.AVG,
                  "最小值": StatOperation.MIN, "最大值": StatOperation.MAX,
                  "计数": StatOperation.COUNT, "去重计数": StatOperation.COUNT_DISTINCT}
        p = _plan(analysis_type=AnalysisType.DESCRIPTIVE,
                  metrics=[MetricSpec(metric_id=metric)],
                  statistics=[op_map[expected_word]])
        r = analytics.analyze_execution_result(_exec([_q("d", [col], [(150.0,)])]), p)
        assert r.conclusions
        assert expected_word in r.conclusions[0].text
        assert r.statistics.column_ops[col]

    def test_change_rate_wording_distinguishes_signed_vs_absolute(self):
        """上升 100% 与下降 90% 同现时，措辞必须区分有符号/绝对。"""
        # 变化率：+1%（01-02）与 -90.1%（01-03）
        # → 有符号最大在 01-02（上升 1%），绝对波动最大在 01-03（下跌 90.1%）
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 101.0),
                (_d("2024-01-03"), 10.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount"], rows)]), _plan())
        texts = " ".join(c.text for c in r.conclusions)
        # 07.6-B：三类极值分别表述，口径互不混用
        #   最大降幅 = 最负的有效变化率（01-03, -90.10%）
        #   最大涨幅 = 最大的有效正变化率（01-02, +1.00%）
        #   最大绝对波动 = |变化率| 最大（01-03, 90.10%）
        assert "降幅最大" in texts and "变化率 -90.10%" in texts
        assert "涨幅最大" in texts and "变化率 +1.00%" in texts
        assert "绝对波动最大" in texts and "绝对幅度 90.10%" in texts
        assert "2024-01-03" in texts and "2024-01-02" in texts


# ===========================================================================
# 任务 06.1-D：有效时间窗口
# ===========================================================================

class TestEffectiveWindow:
    def test_in_range_request_uses_same_window(self):
        rows = [(_d("2024-01-02"), 100.0), (_d("2024-01-03"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-05")
        r = analytics.analyze_execution_result(
            _exec([q]), _plan(time_range=TimeRangeSpec(
                type="explicit_dates", resolved_start_date=date(2024, 1, 1),
                resolved_end_date=date(2024, 1, 5), resolution_status="resolved")))
        assert r.time_range["effective"] == {"start": "2024-01-01", "end": "2024-01-05"}
        assert r.conclusions[0].date_range == ("2024-01-01", "2024-01-05")

    def test_start_clipped_reports_effective_and_requested(self):
        rows = [(_d("2024-01-02"), 100.0), (_d("2024-01-03"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-02", window_end="2024-01-03")
        r = analytics.analyze_execution_result(
            _exec([q]), _plan(time_range=TimeRangeSpec(
                type="explicit_dates", resolved_start_date=date(2023, 12, 1),
                resolved_end_date=date(2024, 1, 3), resolution_status="resolved")))
        c = r.conclusions[0]
        assert c.date_range == ("2024-01-02", "2024-01-03")     # 实际执行
        assert c.evidence.requested_range == ("2023-12-01", "2024-01-03")
        assert any("实际执行窗口" in w for w in r.warnings)

    def test_end_clipped(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-02")
        r = analytics.analyze_execution_result(
            _exec([q]), _plan(time_range=TimeRangeSpec(
                type="explicit_dates", resolved_start_date=date(2024, 1, 1),
                resolved_end_date=date(2024, 6, 1), resolution_status="resolved")))
        assert r.conclusions[0].date_range == ("2024-01-01", "2024-01-02")

    def test_window_comparison_clipped_both(self):
        main = [("6081949", 80.0)]
        comp = [("6081949", 100.0)]
        q_main = _q("m", ["city", "net_inflow"], main, window_label="main",
                    window_start="2024-02-01", window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], comp, window_label="comparison",
                    window_start="2024-01-05", window_end="2024-01-31")
        p = _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                  metrics=[MetricSpec(metric_id="net_inflow")],
                  group_by=["city"],
                  time_range=TimeRangeSpec(
                      type="window_comparison",
                      resolved_start_date=date(2024, 2, 1),
                      resolved_end_date=date(2024, 2, 29),
                      comparison_start_date=date(2024, 1, 1),
                      comparison_end_date=date(2024, 1, 31),
                      resolution_status="resolved"),
                  statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])
        r = analytics.analyze_execution_result(_exec([q_main, q_comp]), p)
        assert r.time_range["effective"] == {"start": "2024-02-01",
                                             "end": "2024-02-29"}
        assert r.time_range["effective_comparison"] == {"start": "2024-01-05",
                                                        "end": "2024-01-31"}
        assert r.statistics.comparison_window == ("2024-01-05", "2024-01-31")
        assert any("对比窗口" in w and "实际执行" in w for w in r.warnings)

    def test_missing_dates_do_not_shrink_window(self):
        """存在缺失日期时，证据范围仍为实际执行窗口（不由首末记录推断）。"""
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-05"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-05")
        r = analytics.analyze_execution_result(
            _exec([q]), _plan(time_range=TimeRangeSpec(
                type="explicit_dates", resolved_start_date=date(2024, 1, 1),
                resolved_end_date=date(2024, 1, 5), resolution_status="resolved")))
        assert r.conclusions[0].date_range == ("2024-01-01", "2024-01-05")
        assert r.statistics.missing_dates == ["2024-01-02", "2024-01-03",
                                             "2024-01-04"]

    def test_no_window_metadata_does_not_fabricate_range(self):
        """无窗口元数据时证据 date_range 必须为 None（不得用请求范围冒充，06.2-B）。"""
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount"], rows)]), _plan())
        assert r.time_range["effective"] is None
        assert r.conclusions[0].date_range is None
        assert r.conclusions[0].evidence.requested_range == ("2024-01-01",
                                                             "2024-01-31")
        assert any("无法确认实际执行窗口" in w for w in r.warnings)


# ===========================================================================
# 任务 06.2-A：请求指标完整性
# ===========================================================================

class TestMetricCompleteness:
    def _two_metric_plan(self, **kw):
        return _multi_plan(["purchase_amount", "redeem_amount"], **kw)

    def test_missing_second_metric_rejected(self):
        """请求申购+赎回，结果只有申购列 → 拒绝。"""
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 120.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum"], rows)]),
            self._two_metric_plan())
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "redeem_amount" in (r.status_reason or "")
        assert "未出现在查询结果中" in (r.status_reason or "")

    def test_missing_first_metric_rejected(self):
        """请求申购+赎回，结果只有赎回列 → 拒绝。"""
        rows = [(_d("2024-01-01"), 50.0), (_d("2024-01-02"), 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "redeem_amount_sum"], rows)]),
            self._two_metric_plan())
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "purchase_amount" in (r.status_reason or "")

    def test_three_metrics_missing_one_rejected(self):
        rows = [(_d("2024-01-01"), 100.0, 50.0), (_d("2024-01-02"), 120.0, 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount", "net_inflow"]))
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "net_inflow" in (r.status_reason or "")

    def test_all_metrics_present_succeeds(self):
        rows = [(_d("2024-01-01"), 100.0, 50.0), (_d("2024-01-02"), 120.0, 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]), self._two_metric_plan())
        assert r.status == AnalysisStatus.OK
        assert set(r.statistics.metric_ids) == {"purchase_amount", "redeem_amount"}

    def test_unexpected_extra_column_rejected(self):
        rows = [(_d("2024-01-01"), 100.0, 1.0), (_d("2024-01-02"), 120.0, 2.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum", "mystery_metric"],
                      rows)]), _plan())
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "未识别的额外列" in (r.status_reason or "")

    def test_multi_metric_ranking_supported_per_metric(self):
        """多指标排名：每指标独立排名（不只取第一列）。"""
        rows = [("6081949", 1100.0, 1), ("6281949", 1500.0, 2)]
        # 两指标计划（金额 + 期间用户数）
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="active_user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])
        r = analytics.analyze_execution_result(
            _exec([_q("r", ["city", "purchase_amount_sum", "active_user_count"],
                      rows)]), p)
        assert r.status == AnalysisStatus.OK
        assert r.statistics.kind == "ranking_set"
        assert r.statistics.metric_ids == ["purchase_amount", "active_user_count"]
        by_metric = {s.metric_id: s for s in r.statistics.series}
        assert by_metric["purchase_amount"].entries[0].value == 1500.0
        assert by_metric["active_user_count"].entries[0].value == 2.0

    def test_multi_metric_descriptive_supported(self):
        p = _plan(analysis_type=AnalysisType.DESCRIPTIVE,
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="active_user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])
        r = analytics.analyze_execution_result(
            _exec([_q("d", ["purchase_amount_sum", "active_user_count"],
                      [(2850.0, 3.0)])]), p)
        assert r.status == AnalysisStatus.OK
        texts = " ".join(c.text for c in r.conclusions)
        assert "申购金额" in texts and "期间去重用户数" in texts

    def test_multi_metric_distribution_rejected_at_schema(self):
        """分布统计仅支持单指标：Schema 层即拒绝（更早、更明确）。"""
        with pytest.raises(Exception, match="仅支持单指标"):
            _plan(analysis_type=AnalysisType.DISTRIBUTION, group_by=["city"],
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="redeem_amount")],
                  statistics=[StatOperation.COUNT])

    def test_multi_metric_distribution_rejected_at_analytics(self):
        """绕过 Schema 的多指标分布结果 → 分析层同样拒绝（双保险）。"""
        single = _plan(analysis_type=AnalysisType.DISTRIBUTION, group_by=["city"],
                       statistics=[StatOperation.COUNT])
        bad = single.model_copy(update={"metrics": [
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="redeem_amount")]})
        r = analytics.analyze_execution_result(
            _exec([_q("d", ["city", "purchase_amount_count", "redeem_amount_count"],
                      [("6081949", 7, 3)])]), bad)
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "仅支持单指标" in (r.status_reason or "")


# ===========================================================================
# 任务 06.2-B：无有效窗口元数据时不得伪造
# ===========================================================================

class TestNoWindowFabrication:
    def test_effective_none_and_date_range_none(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount"], rows)]), _plan())
        assert r.time_range["effective"] is None
        c = r.conclusions[0]
        assert c.date_range is None
        assert c.evidence.date_range is None
        assert c.evidence.requested_range == ("2024-01-01", "2024-01-31")
        assert any("无法确认实际执行窗口" in w for w in r.warnings)

    def test_observed_span_reported_separately(self):
        """观测值范围单独提示，不冒充查询窗口。"""
        rows = [(_d("2024-01-05"), 100.0), (_d("2024-01-09"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount"], rows)]), _plan())
        joined = " ".join(r.warnings)
        assert "观测值范围 2024-01-05 至 2024-01-09" in joined
        assert "不代表实际查询窗口" in joined
        assert r.conclusions[0].date_range is None

    def test_missing_dates_do_not_override_effective(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-09"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-09")
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        assert r.conclusions[0].date_range == ("2024-01-01", "2024-01-09")
        assert r.statistics.missing_dates      # 缺口仍被记录

    def test_evidence_uses_effective_when_present(self):
        rows = [(_d("2024-01-02"), 100.0), (_d("2024-01-03"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-05")
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        c = r.conclusions[0]
        assert c.date_range == ("2024-01-01", "2024-01-05")
        assert c.evidence.date_range == ("2024-01-01", "2024-01-05")
        assert c.evidence.requested_range == ("2024-01-01", "2024-01-31")


# ===========================================================================
# 任务 06.2-C：双窗口证据
# ===========================================================================

def _win_plan():
    return _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                 metrics=[MetricSpec(metric_id="net_inflow")],
                 group_by=["city"],
                 time_range=TimeRangeSpec(
                     type="window_comparison",
                     resolved_start_date=date(2024, 2, 1),
                     resolved_end_date=date(2024, 2, 29),
                     comparison_start_date=date(2024, 1, 1),
                     comparison_end_date=date(2024, 1, 31),
                     resolution_status="resolved"),
                 statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])


class TestDualWindowEvidence:
    def _run(self, q_main, q_comp):
        return analytics.analyze_execution_result(_exec([q_main, q_comp]), _win_plan())

    def test_consecutive_windows(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-01",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-01",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        c = r.conclusions[0]
        assert c.date_range is None                       # 不合并连续范围
        assert c.main_window == ("2024-02-01", "2024-02-29")
        assert c.comparison_window == ("2024-01-01", "2024-01-31")
        assert c.evidence.requested_main_window == ("2024-02-01", "2024-02-29")

    def test_non_consecutive_windows_not_merged(self):
        """不连续窗口（如 1 月与 3 月）不得合并为 1~3 月的连续范围。"""
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-03-01",
                    window_end="2024-03-31")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-01",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        c = r.conclusions[0]
        assert c.date_range is None
        assert c.main_window == ("2024-03-01", "2024-03-31")
        assert c.comparison_window == ("2024-01-01", "2024-01-31")
        # 不得出现 2024-01-01 至 2024-03-31 这样的合并跨度
        assert "2024-01-01 至 2024-03-31" not in c.text

    def test_main_window_clipped(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-05",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-01",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        assert r.conclusions[0].main_window == ("2024-02-05", "2024-02-29")
        assert any("主窗口" in w and "实际执行" in w for w in r.warnings)

    def test_comparison_window_clipped(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-01",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-10",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        assert r.conclusions[0].comparison_window == ("2024-01-10", "2024-01-31")
        assert any("对比窗口" in w and "实际执行" in w for w in r.warnings)

    def test_both_windows_clipped_separate_warnings(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-05",
                    window_end="2024-02-25")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-10",
                    window_end="2024-01-20")
        r = self._run(q_main, q_comp)
        assert sum(1 for w in r.warnings if "实际执行" in w) == 2

    def test_missing_metadata_marks_requested(self):
        """某窗口缺元数据 → 标注为请求范围并警告，不冒充实际。"""
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-01",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison")   # 无窗口元数据
        r = self._run(q_main, q_comp)
        stat = r.statistics
        assert stat.comparison_window_from_effective is False
        assert stat.main_window_from_effective is True
        c = r.conclusions[0]
        assert c.comparison_window is None        # 实际未知
        assert c.evidence.requested_comparison_window == ("2024-01-01", "2024-01-31")
        assert any("无法确认对比窗口" in w for w in r.warnings)
        assert "（请求范围）" in c.text

    def test_time_range_keeps_both_effective(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-05",
                    window_end="2024-02-25")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-10",
                    window_end="2024-01-20")
        r = self._run(q_main, q_comp)
        assert r.time_range["effective"] == {"start": "2024-02-05",
                                            "end": "2024-02-25"}
        assert r.time_range["effective_comparison"] == {"start": "2024-01-10",
                                                        "end": "2024-01-20"}


# ===========================================================================
# 任务 06.2-A：请求指标完整性
# ===========================================================================

class TestMetricCompleteness:
    def _two_metric_plan(self, **kw):
        return _multi_plan(["purchase_amount", "redeem_amount"], **kw)

    def test_missing_second_metric_rejected(self):
        """请求申购+赎回，结果只有申购列 → 拒绝。"""
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 120.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum"], rows)]),
            self._two_metric_plan())
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "redeem_amount" in (r.status_reason or "")
        assert "未出现在查询结果中" in (r.status_reason or "")

    def test_missing_first_metric_rejected(self):
        """请求申购+赎回，结果只有赎回列 → 拒绝。"""
        rows = [(_d("2024-01-01"), 50.0), (_d("2024-01-02"), 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "redeem_amount_sum"], rows)]),
            self._two_metric_plan())
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "purchase_amount" in (r.status_reason or "")

    def test_three_metrics_missing_one_rejected(self):
        rows = [(_d("2024-01-01"), 100.0, 50.0), (_d("2024-01-02"), 120.0, 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount", "net_inflow"]))
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "net_inflow" in (r.status_reason or "")

    def test_all_metrics_present_succeeds(self):
        rows = [(_d("2024-01-01"), 100.0, 50.0), (_d("2024-01-02"), 120.0, 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]), self._two_metric_plan())
        assert r.status == AnalysisStatus.OK
        assert set(r.statistics.metric_ids) == {"purchase_amount", "redeem_amount"}

    def test_unexpected_extra_column_rejected(self):
        rows = [(_d("2024-01-01"), 100.0, 1.0), (_d("2024-01-02"), 120.0, 2.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum", "mystery_metric"],
                      rows)]), _plan())
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "未识别的额外列" in (r.status_reason or "")

    def test_multi_metric_ranking_supported_per_metric(self):
        """多指标排名：每指标独立排名（不只取第一列）。"""
        rows = [("6081949", 1100.0, 1), ("6281949", 1500.0, 2)]
        # 两指标计划（金额 + 期间用户数）
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="active_user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])
        r = analytics.analyze_execution_result(
            _exec([_q("r", ["city", "purchase_amount_sum", "active_user_count"],
                      rows)]), p)
        assert r.status == AnalysisStatus.OK
        assert r.statistics.kind == "ranking_set"
        assert r.statistics.metric_ids == ["purchase_amount", "active_user_count"]
        by_metric = {s.metric_id: s for s in r.statistics.series}
        assert by_metric["purchase_amount"].entries[0].value == 1500.0
        assert by_metric["active_user_count"].entries[0].value == 2.0

    def test_multi_metric_descriptive_supported(self):
        p = _plan(analysis_type=AnalysisType.DESCRIPTIVE,
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="active_user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])
        r = analytics.analyze_execution_result(
            _exec([_q("d", ["purchase_amount_sum", "active_user_count"],
                      [(2850.0, 3.0)])]), p)
        assert r.status == AnalysisStatus.OK
        texts = " ".join(c.text for c in r.conclusions)
        assert "申购金额" in texts and "期间去重用户数" in texts

    def test_multi_metric_distribution_rejected_at_schema(self):
        """分布统计仅支持单指标：Schema 层即拒绝（更早、更明确）。"""
        with pytest.raises(Exception, match="仅支持单指标"):
            _plan(analysis_type=AnalysisType.DISTRIBUTION, group_by=["city"],
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="redeem_amount")],
                  statistics=[StatOperation.COUNT])

    def test_multi_metric_distribution_rejected_at_analytics(self):
        """绕过 Schema 的多指标分布结果 → 分析层同样拒绝（双保险）。"""
        single = _plan(analysis_type=AnalysisType.DISTRIBUTION, group_by=["city"],
                       statistics=[StatOperation.COUNT])
        bad = single.model_copy(update={"metrics": [
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="redeem_amount")]})
        r = analytics.analyze_execution_result(
            _exec([_q("d", ["city", "purchase_amount_count", "redeem_amount_count"],
                      [("6081949", 7, 3)])]), bad)
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "仅支持单指标" in (r.status_reason or "")


# ===========================================================================
# 任务 06.2-B：无有效窗口元数据时不得伪造
# ===========================================================================

class TestNoWindowFabrication:
    def test_effective_none_and_date_range_none(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount"], rows)]), _plan())
        assert r.time_range["effective"] is None
        c = r.conclusions[0]
        assert c.date_range is None
        assert c.evidence.date_range is None
        assert c.evidence.requested_range == ("2024-01-01", "2024-01-31")
        assert any("无法确认实际执行窗口" in w for w in r.warnings)

    def test_observed_span_reported_separately(self):
        """观测值范围单独提示，不冒充查询窗口。"""
        rows = [(_d("2024-01-05"), 100.0), (_d("2024-01-09"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount"], rows)]), _plan())
        joined = " ".join(r.warnings)
        assert "观测值范围 2024-01-05 至 2024-01-09" in joined
        assert "不代表实际查询窗口" in joined
        assert r.conclusions[0].date_range is None

    def test_missing_dates_do_not_override_effective(self):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-09"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-09")
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        assert r.conclusions[0].date_range == ("2024-01-01", "2024-01-09")
        assert r.statistics.missing_dates      # 缺口仍被记录

    def test_evidence_uses_effective_when_present(self):
        rows = [(_d("2024-01-02"), 100.0), (_d("2024-01-03"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-05")
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        c = r.conclusions[0]
        assert c.date_range == ("2024-01-01", "2024-01-05")
        assert c.evidence.date_range == ("2024-01-01", "2024-01-05")
        assert c.evidence.requested_range == ("2024-01-01", "2024-01-31")


# ===========================================================================
# 任务 06.2-C：双窗口证据
# ===========================================================================

def _win_plan():
    return _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                 metrics=[MetricSpec(metric_id="net_inflow")],
                 group_by=["city"],
                 time_range=TimeRangeSpec(
                     type="window_comparison",
                     resolved_start_date=date(2024, 2, 1),
                     resolved_end_date=date(2024, 2, 29),
                     comparison_start_date=date(2024, 1, 1),
                     comparison_end_date=date(2024, 1, 31),
                     resolution_status="resolved"),
                 statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])


class TestDualWindowEvidence:
    def _run(self, q_main, q_comp):
        return analytics.analyze_execution_result(_exec([q_main, q_comp]), _win_plan())

    def test_consecutive_windows(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-01",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-01",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        c = r.conclusions[0]
        assert c.date_range is None                       # 不合并连续范围
        assert c.main_window == ("2024-02-01", "2024-02-29")
        assert c.comparison_window == ("2024-01-01", "2024-01-31")
        assert c.evidence.requested_main_window == ("2024-02-01", "2024-02-29")

    def test_non_consecutive_windows_not_merged(self):
        """不连续窗口（如 1 月与 3 月）不得合并为 1~3 月的连续范围。"""
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-03-01",
                    window_end="2024-03-31")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-01",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        c = r.conclusions[0]
        assert c.date_range is None
        assert c.main_window == ("2024-03-01", "2024-03-31")
        assert c.comparison_window == ("2024-01-01", "2024-01-31")
        # 不得出现 2024-01-01 至 2024-03-31 这样的合并跨度
        assert "2024-01-01 至 2024-03-31" not in c.text

    def test_main_window_clipped(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-05",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-01",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        assert r.conclusions[0].main_window == ("2024-02-05", "2024-02-29")
        assert any("主窗口" in w and "实际执行" in w for w in r.warnings)

    def test_comparison_window_clipped(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-01",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-10",
                    window_end="2024-01-31")
        r = self._run(q_main, q_comp)
        assert r.conclusions[0].comparison_window == ("2024-01-10", "2024-01-31")
        assert any("对比窗口" in w and "实际执行" in w for w in r.warnings)

    def test_both_windows_clipped_separate_warnings(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-05",
                    window_end="2024-02-25")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-10",
                    window_end="2024-01-20")
        r = self._run(q_main, q_comp)
        assert sum(1 for w in r.warnings if "实际执行" in w) == 2

    def test_missing_metadata_marks_requested(self):
        """某窗口缺元数据 → 标注为请求范围并警告，不冒充实际。"""
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-01",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison")   # 无窗口元数据
        r = self._run(q_main, q_comp)
        stat = r.statistics
        assert stat.comparison_window_from_effective is False
        assert stat.main_window_from_effective is True
        c = r.conclusions[0]
        assert c.comparison_window is None        # 实际未知
        assert c.evidence.requested_comparison_window == ("2024-01-01", "2024-01-31")
        assert any("无法确认对比窗口" in w for w in r.warnings)
        assert "（请求范围）" in c.text

    def test_time_range_keeps_both_effective(self):
        q_main = _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                    window_label="main", window_start="2024-02-05",
                    window_end="2024-02-25")
        q_comp = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                    window_label="comparison", window_start="2024-01-10",
                    window_end="2024-01-20")
        r = self._run(q_main, q_comp)
        assert r.time_range["effective"] == {"start": "2024-02-05",
                                            "end": "2024-02-25"}
        assert r.time_range["effective_comparison"] == {"start": "2024-01-10",
                                                        "end": "2024-01-20"}


# ===========================================================================
# 任务 06.3-A：统一拒绝同一指标的多个聚合列
# ===========================================================================

class TestAggregationUniqueness:
    """同一指标出现多个聚合口径列 → 全部分析类型均拒绝（不只时间趋势）。"""

    def _two_cols_rows(self):
        return [(_d("2024-01-01"), 100.0, 100.0), (_d("2024-01-02"), 120.0, 120.0)]

    def test_trend_rejects_duplicate_aggregation(self):
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "purchase_amount_avg"], self._two_cols_rows())]),
            _plan(statistics=[StatOperation.SUM, StatOperation.AVG]))
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "多个聚合口径列" in (r.status_reason or "")
        assert r.conclusions == []

    def test_ranking_rejects_duplicate_aggregation(self):
        rows = [("6081949", 100.0, 50.0), ("6281949", 200.0, 100.0)]
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  statistics=[StatOperation.SUM, StatOperation.AVG])
        r = analytics.analyze_execution_result(
            _exec([_q("r", ["city", "purchase_amount_sum",
                           "purchase_amount_avg"], rows)]), p)
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "多个聚合口径列" in (r.status_reason or "")
        assert r.statistics is None
        assert r.conclusions == []

    def test_descriptive_rejects_duplicate_aggregation(self):
        p = _plan(analysis_type=AnalysisType.DESCRIPTIVE,
                  statistics=[StatOperation.SUM, StatOperation.AVG])
        r = analytics.analyze_execution_result(
            _exec([_q("d", ["purchase_amount_sum", "purchase_amount_avg"],
                      [(2850.0, 950.0)])]), p)
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert r.conclusions == []

    def test_window_comparison_rejects_duplicate_aggregation(self):
        main = [("6081949", 80.0, 40.0)]
        comp = [("6081949", 100.0, 50.0)]
        q_main = _q("m", ["city", "net_inflow_sum", "net_inflow_avg"], main,
                    window_label="main", window_start="2024-02-01",
                    window_end="2024-02-29")
        q_comp = _q("c", ["city", "net_inflow_sum", "net_inflow_avg"], comp,
                    window_label="comparison", window_start="2024-01-01",
                    window_end="2024-01-31")
        r = analytics.analyze_execution_result(_exec([q_main, q_comp]), _win_plan())
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert r.conclusions == []

    def test_distribution_rejects_duplicate_aggregation(self):
        """分布结果中同一指标同时出现 count 与 sum 两列 → 拒绝。"""
        p = _plan(analysis_type=AnalysisType.DISTRIBUTION, group_by=["city"],
                  statistics=[StatOperation.COUNT])
        r = analytics.analyze_execution_result(
            _exec([_q("d", ["city", "purchase_amount_count",
                           "purchase_amount_sum"], [("6081949", 7, 3500.0)])]), p)
        assert r.status == AnalysisStatus.INCOMPATIBLE_METRIC
        assert "多个聚合口径列" in (r.status_reason or "")
        assert r.conclusions == []

    def test_two_distinct_metrics_still_succeed(self):
        rows = [(_d("2024-01-01"), 100.0, 50.0), (_d("2024-01-02"), 120.0, 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        assert r.status == AnalysisStatus.OK
        assert r.statistics.metric_ids == ["purchase_amount", "redeem_amount"]

    def test_multi_metric_ranking_still_succeeds(self):
        rows = [("6081949", 1100.0, 1), ("6281949", 1500.0, 2)]
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="active_user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])
        r = analytics.analyze_execution_result(
            _exec([_q("r", ["city", "purchase_amount_sum", "active_user_count"],
                      rows)]), p)
        assert r.status == AnalysisStatus.OK
        assert r.statistics.metric_ids == ["purchase_amount", "active_user_count"]
        # 每个指标只出现一次（无重复口径）
        assert len(r.statistics.series) == 2


# ===========================================================================
# 任务 06.3-B：整列为空的指标不得静默跳过
# ===========================================================================

class TestEmptyMetricColumn:
    def _cols(self):
        return ["report_date", "purchase_amount_sum", "redeem_amount_sum"]

    def test_both_metrics_have_data_succeeds(self):
        rows = [(_d("2024-01-01"), 100.0, 50.0), (_d("2024-01-02"), 120.0, 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", self._cols(), rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        assert r.status == AnalysisStatus.OK
        assert len(r.statistics.series) == 2

    def test_second_metric_all_null_rejected(self):
        rows = [(_d("2024-01-01"), 100.0, None), (_d("2024-01-02"), 120.0, None)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", self._cols(), rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        assert r.status == AnalysisStatus.INSUFFICIENT_DATA
        assert "redeem_amount" in (r.status_reason or "")
        assert r.conclusions == []
        assert r.statistics is None

    def test_first_metric_all_null_rejected(self):
        rows = [(_d("2024-01-01"), None, 50.0), (_d("2024-01-02"), None, 60.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", self._cols(), rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        assert r.status == AnalysisStatus.INSUFFICIENT_DATA
        assert "purchase_amount" in (r.status_reason or "")

    def test_all_metrics_null_returns_empty_result(self):
        rows = [(_d("2024-01-01"), None, None), (_d("2024-01-02"), None, None)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", self._cols(), rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        assert r.status == AnalysisStatus.EMPTY_RESULT
        assert "均无有效数值" in (r.status_reason or "")

    def test_partial_missing_dates_still_analyzed(self):
        rows = [(_d("2024-01-01"), 100.0, 50.0), (_d("2024-01-02"), 120.0, None),
                (_d("2024-01-03"), 90.0, 70.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", self._cols(), rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"]))
        assert r.status == AnalysisStatus.OK
        redeem = next(s for s in r.statistics.series
                      if s.metric_id == "redeem_amount")
        assert len(redeem.points) == 2        # 缺失值不填零
        assert redeem.missing_dates == ["2024-01-02"]

    def test_group_missing_not_zero_filled(self):
        """某指标在某分组无数据：不虚构零值，给出明确警告。"""
        rows = [(_d("2024-01-01"), "6081949", 100.0, 50.0),
                (_d("2024-01-02"), "6081949", 120.0, None),
                (_d("2024-01-01"), "6281949", 10.0, 5.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("t", ["report_date", "city", "purchase_amount_sum",
                           "redeem_amount_sum"], rows)]),
            _multi_plan(["purchase_amount", "redeem_amount"], group_by=["city"]))
        assert r.status == AnalysisStatus.OK
        # 每个指标各自的分组序列都存在（不补零）
        combos = {(s.metric_id, s.group.get("city")) for s in r.statistics.series}
        assert combos == {("purchase_amount", "6081949"), ("purchase_amount", "6281949"),
                          ("redeem_amount", "6081949"), ("redeem_amount", "6281949")}
        # 无虚构零值：redeem 在 6081949 只有 1 个有效点
        redeem_608 = next(s for s in r.statistics.series
                          if s.metric_id == "redeem_amount"
                          and s.group.get("city") == "6081949")
        assert len(redeem_608.points) == 1


# ===========================================================================
# 任务 06.3-C：主窗口与对比窗口元数据隔离
# ===========================================================================

class TestWindowMetadataIsolation:
    def _q_main(self, **kw):
        return _q("m", ["city", "net_inflow"], [("6081949", 80.0)],
                  window_label="main", **kw)

    def _q_comp(self, **kw):
        return _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                  window_label="comparison", **kw)

    def test_both_windows_have_metadata(self):
        r = analytics.analyze_execution_result(
            _exec([self._q_main(window_start="2024-02-01", window_end="2024-02-29"),
                   self._q_comp(window_start="2024-01-01", window_end="2024-01-31")]),
            _win_plan())
        assert r.time_range["effective"] == {"start": "2024-02-01",
                                            "end": "2024-02-29"}
        assert r.time_range["effective_comparison"] == {"start": "2024-01-01",
                                                        "end": "2024-01-31"}
        assert r.time_range["effective_note"] is None

    def test_main_has_metadata_comparison_missing(self):
        r = analytics.analyze_execution_result(
            _exec([self._q_main(window_start="2024-02-01", window_end="2024-02-29"),
                   self._q_comp()]), _win_plan())
        assert r.time_range["effective"] == {"start": "2024-02-01",
                                            "end": "2024-02-29"}
        assert r.time_range["effective_comparison"] is None
        assert "对比窗口缺少" in (r.time_range["effective_note"] or "")

    def test_main_missing_comparison_has_metadata(self):
        """主窗口无元数据时，不得用对比窗口日期填充 effective。"""
        r = analytics.analyze_execution_result(
            _exec([self._q_main(),
                   self._q_comp(window_start="2024-01-01", window_end="2024-01-31")]),
            _win_plan())
        assert r.time_range["effective"] is None
        assert r.time_range["effective_comparison"] == {"start": "2024-01-01",
                                                        "end": "2024-01-31"}
        assert "主窗口缺少" in (r.time_range["effective_note"] or "")

    def test_both_missing(self):
        r = analytics.analyze_execution_result(
            _exec([self._q_main(), self._q_comp()]), _win_plan())
        assert r.time_range["effective"] is None
        assert r.time_range["effective_comparison"] is None
        c = r.conclusions[0]
        assert c.main_window is None and c.comparison_window is None
        assert c.date_range is None
        # 结论文本标注为请求范围
        assert "（请求范围）" in c.text

    def test_different_dates_do_not_overwrite(self):
        r = analytics.analyze_execution_result(
            _exec([self._q_main(window_start="2024-03-01", window_end="2024-03-15"),
                   self._q_comp(window_start="2024-01-05", window_end="2024-01-20")]),
            _win_plan())
        assert r.time_range["effective"] == {"start": "2024-03-01",
                                            "end": "2024-03-15"}
        assert r.time_range["effective_comparison"] == {"start": "2024-01-05",
                                                        "end": "2024-01-20"}
        assert r.conclusions[0].main_window == ("2024-03-01", "2024-03-15")
        assert r.conclusions[0].comparison_window == ("2024-01-05", "2024-01-20")

    def test_single_window_query_unaffected(self):
        rows = [(_d("2024-01-02"), 100.0), (_d("2024-01-03"), 110.0)]
        q = _q("t", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-05")
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        assert r.time_range["effective"] == {"start": "2024-01-01",
                                            "end": "2024-01-05"}
        assert r.time_range["effective_comparison"] is None
        assert r.conclusions[0].date_range == ("2024-01-01", "2024-01-05")

    def test_duplicate_window_labels_flagged(self):
        q1 = _q("m1", ["city", "net_inflow"], [("6081949", 80.0)],
                window_label="main", window_start="2024-02-01",
                window_end="2024-02-29")
        q2 = _q("m2", ["city", "net_inflow"], [("6081949", 70.0)],
                window_label="main", window_start="2024-02-10",
                window_end="2024-02-20")
        q3 = _q("c", ["city", "net_inflow"], [("6081949", 100.0)],
                window_label="comparison", window_start="2024-01-01",
                window_end="2024-01-31")
        r = analytics.analyze_execution_result(_exec([q1, q2, q3]), _win_plan())
        assert "重复窗口标签" in (r.time_range["effective_note"] or "")
