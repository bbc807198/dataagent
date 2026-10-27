"""阶段 07.5.3 回归测试：参数化指标的完整性检查（按 metric_id + term）。

合成数据 + 固定计划，不调用真实模型 API。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dataagent import analytics, db, ingestion, shibor
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
    TimeRangeType,
)
from dataagent.analysis_runner import (
    AnalysisExecutionResult,
    ExecutionStatus,
    QueryExecution,
    run_analysis,
)
from dataagent.query_service import QueryService

SYNTH_DIR = Path(__file__).parent / "fixtures" / "synthetic"
DATA_RANGE = (date(2024, 1, 2), date(2024, 1, 12))


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory) -> Path:
    db_path = tmp_path_factory.mktemp("s753") / "complete.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
    assert all(r.ok for r in reports)
    shibor.create_fill_view(con)
    con.close()
    return db_path


def _plan(terms=("O_N", "1W"), metrics=None):
    specs = metrics if metrics is not None else [
        MetricSpec(metric_id="shibor_rate", term=t) for t in terms]
    return AnalysisPlan(
        question="分析 SHIBOR 利率趋势", intent="利率趋势",
        analysis_type=AnalysisType.TIME_TREND, metrics=specs,
        time_range=TimeRangeSpec(
            type=TimeRangeType.EXPLICIT_DATES,
            resolved_start_date=date(2024, 1, 2),
            resolved_end_date=date(2024, 1, 12),
            resolution_status="resolved"),
        group_by=[], filters=[], statistics=[StatOperation.AVG])


def _exec(columns, rows):
    q = QueryExecution(name="time_trend_mfd_date", status="ok",
                       columns=columns, rows=rows, row_count=len(rows))
    return AnalysisExecutionResult(status=ExecutionStatus.OK, plan_summary={},
                                   queries=[q])


class TestTermCompleteness:
    def test_both_terms_present_succeeds(self, synth_db):
        """① 同时请求 O_N 与 1W，结果含两列 → 分析成功。"""
        p = _plan()
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        assert {s.term for s in an.statistics.series} == {"O_N", "1W"}

    def test_missing_second_term_rejected(self, synth_db):
        """② 请求 O_N + 1W，结果仅含 O_N → incompatible_metric。"""
        p = _plan()
        ex = _exec(["mfd_date", "shibor_rate_o_n_avg"],
                   [(date(2024, 1, 2), 3.8), (date(2024, 1, 3), 3.6)])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.INCOMPATIBLE_METRIC
        reason = an.status_reason or ""
        assert "1W" in reason and "期限" in reason
        assert an.conclusions == []

    def test_missing_first_term_rejected(self, synth_db):
        """③ 请求 O_N + 1W，结果仅含 1W → incompatible_metric。"""
        p = _plan()
        ex = _exec(["mfd_date", "shibor_rate_1w_avg"],
                   [(date(2024, 1, 2), 4.3), (date(2024, 1, 3), 4.1)])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.INCOMPATIBLE_METRIC
        assert "O_N" in (an.status_reason or "")
        assert an.conclusions == []

    def test_column_order_does_not_matter(self, synth_db):
        """④ 结果列顺序与计划相反 → 仍正确映射。"""
        p = _plan()
        ex = _exec(["mfd_date", "shibor_rate_1w_avg", "shibor_rate_o_n_avg"],
                   [(date(2024, 1, 2), 4.3, 3.8), (date(2024, 1, 3), 4.1, 3.6)])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        by_term = {s.term: s for s in an.statistics.series}
        assert by_term["O_N"].points[0].value == 3.8
        assert by_term["1W"].points[0].value == 4.3

    def test_duplicate_same_term_request_not_rejected(self, synth_db):
        """⑤ 同一指标同一期限的重复请求 → 不因重复计划而错误拒绝。"""
        p = _plan(metrics=[MetricSpec(metric_id="shibor_rate", term="O_N"),
                           MetricSpec(metric_id="shibor_rate", term="O_N")])
        assert len(p.metrics) == 1          # 计划层已规范化去重
        ex = _exec(["mfd_date", "shibor_rate_o_n_avg"],
                   [(date(2024, 1, 2), 3.8), (date(2024, 1, 3), 3.6)])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        stats = an.statistics
        series = getattr(stats, "series", None)
        if series is not None:
            assert len(series) == 1
        else:                      # 单指标 → TrendStats
            assert len(stats.points) == 2

    def test_plain_metrics_not_regressed(self, synth_db):
        """⑥ 普通 purchase_amount / redeem_amount 查询不回归。"""
        p = AnalysisPlan(
            question="分析申购和赎回趋势", intent="趋势",
            analysis_type=AnalysisType.TIME_TREND,
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="redeem_amount")],
            time_range=TimeRangeSpec(
                type=TimeRangeType.EXPLICIT_DATES,
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 12),
                resolution_status="resolved"),
            group_by=[], filters=[], statistics=[StatOperation.SUM])
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        assert {s.metric_id for s in an.statistics.series} == {
            "purchase_amount", "redeem_amount"}

    def test_ambiguous_columns_still_rejected(self, synth_db):
        """⑦ 真正无法消歧的多聚合列 → 继续拒绝。"""
        p = _plan(terms=("O_N",))
        ex = _exec(["mfd_date", "shibor_rate_o_n_avg", "shibor_rate_o_n_avg_1"],
                   [(date(2024, 1, 2), 3.8, 3.8), (date(2024, 1, 3), 3.6, 3.6)])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.INCOMPATIBLE_METRIC
        assert an.conclusions == []

    def test_missing_term_no_fake_series(self, synth_db):
        """⑧ 缺失期限时不生成成功结论或虚假趋势序列。"""
        p = _plan()
        ex = _exec(["mfd_date", "shibor_rate_o_n_avg"],
                   [(date(2024, 1, 2), 3.8), (date(2024, 1, 3), 3.6)])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.INCOMPATIBLE_METRIC
        assert an.statistics is None or not getattr(an.statistics, "series", [])
        assert an.conclusions == []
        assert not any("1W" in c.text for c in an.conclusions)
