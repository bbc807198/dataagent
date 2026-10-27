"""阶段 07.5 回归测试：SQL 结果 → 趋势分析的数据链路。

全部使用合成数据库，不调用真实模型 API。
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
from dataagent.analysis_runner import ExecutionStatus, QueryExecution, run_analysis
from dataagent.query_builder import build_queries
from dataagent.query_service import QueryService

SYNTH_DIR = Path(__file__).parent / "fixtures" / "synthetic"
DATA_RANGE = (date(2024, 1, 2), date(2024, 1, 12))
FULL_RANGE = (date(2024, 1, 2), date(2024, 1, 12))


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory) -> Path:
    db_path = tmp_path_factory.mktemp("s75") / "chain.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
    assert all(r.ok for r in reports)
    shibor.create_fill_view(con)
    con.close()
    return db_path


def _plan(*, start=date(2024, 1, 2), end=date(2024, 1, 12), group_by=(),
          metrics=("purchase_amount", "redeem_amount"), filters=()):
    return AnalysisPlan(
        question="分析申购和赎回趋势", intent="趋势",
        analysis_type=AnalysisType.TIME_TREND,
        metrics=[MetricSpec(metric_id=m) for m in metrics],
        time_range=TimeRangeSpec(
            type=TimeRangeType.EXPLICIT_DATES,
            resolved_start_date=start, resolved_end_date=end,
            resolution_status="resolved"),
        group_by=list(group_by), filters=list(filters),
        statistics=[StatOperation.SUM])


def _run(plan_obj, db_path, data_range=DATA_RANGE):
    with QueryService(db_path) as qs:
        execution = run_analysis(plan_obj, qs, data_range=data_range)
        analysis = analytics.analyze_execution_result(execution, plan_obj)
    return execution, analysis


class TestTrendDataChain:
    def test_two_metrics_many_dates_succeeds(self, synth_db):
        """① 两个指标在多个日期均有有效数值 → 趋势分析成功。"""
        ex, an = _run(_plan(), synth_db)
        assert ex.status == ExecutionStatus.OK
        assert an.status == analytics.AnalysisStatus.OK
        assert len(an.statistics.series) == 2
        purchase = next(s for s in an.statistics.series
                        if s.metric_id == "purchase_amount")
        redeem = next(s for s in an.statistics.series
                      if s.metric_id == "redeem_amount")
        assert len(purchase.points) == 11
        assert len(redeem.points) == 11

    def test_one_metric_all_null_not_silently_ignored(self, synth_db):
        """② 一个指标有效、另一个整列为空 → 不得静默忽略（明确数据不足）。"""
        # 构造：SQL 正常执行但赎回列全为 NULL
        plan_obj = _plan()
        with QueryService(synth_db) as qs:
            execution = run_analysis(plan_obj, qs, data_range=DATA_RANGE)
        q = execution.queries[0]
        nulled = QueryExecution(
            name=q.name, status="ok", columns=q.columns,
            rows=[(r[0], r[1], None) for r in q.rows], row_count=len(q.rows),
            window_start=q.window_start, window_end=q.window_end)
        from dataagent.analysis_runner import AnalysisExecutionResult
        ex2 = AnalysisExecutionResult(status=ExecutionStatus.OK, plan_summary={},
                                      queries=[nulled])
        an = analytics.analyze_execution_result(ex2, plan_obj)
        assert an.status == analytics.AnalysisStatus.INSUFFICIENT_DATA
        assert "redeem_amount" in (an.status_reason or "")

    def test_both_metrics_empty_returns_empty_result(self, synth_db):
        """③ 两个指标均无有效数值 → empty_result。"""
        plan_obj = _plan()
        with QueryService(synth_db) as qs:
            execution = run_analysis(plan_obj, qs, data_range=DATA_RANGE)
        q = execution.queries[0]
        nulled = QueryExecution(
            name=q.name, status="ok", columns=q.columns,
            rows=[(r[0], None, None) for r in q.rows], row_count=len(q.rows))
        from dataagent.analysis_runner import AnalysisExecutionResult
        ex2 = AnalysisExecutionResult(status=ExecutionStatus.OK, plan_summary={},
                                      queries=[nulled])
        an = analytics.analyze_execution_result(ex2, plan_obj)
        assert an.status == analytics.AnalysisStatus.EMPTY_RESULT

    def test_single_valid_date_no_daily_change(self, synth_db):
        """④ 仅有一个有效日期 → 不计算日环比（insufficient_data）。"""
        plan_obj = _plan(start=date(2024, 1, 12), end=date(2024, 1, 12))
        ex, an = _run(plan_obj, synth_db)
        assert an.status == analytics.AnalysisStatus.INSUFFICIENT_DATA
        assert "少于 2 个" in (an.status_reason or "")
        # 不得计算日环比（变化列表为空，结论中不出现日环比）
        assert all(len(s.daily_changes) == 0 for s in an.statistics.series)
        assert not any("日环比" in c.text for c in an.conclusions)

    def test_narrow_filter_insufficient_data(self, synth_db):
        """⑤ 日期范围过滤后不足两个有效日期 → 明确 insufficient_data。"""
        plan_obj = _plan(start=date(2024, 1, 12), end=date(2024, 1, 12))
        ex, an = _run(plan_obj, synth_db)
        assert ex.status == ExecutionStatus.OK
        assert an.status == analytics.AnalysisStatus.INSUFFICIENT_DATA
        # 诊断信息包含各序列有效点数
        assert "有效点数" in (an.status_reason or "")

    def test_group_by_date_does_not_break_trend(self, synth_db):
        """⑥ 日期范围内有足够数据时，不得错误返回 insufficient_data。

        回归 07.5 根因：group_by 含 report_date 时，时间轴被当作分组键，
        每组只剩 1 个点 → 11 行有效数据被误判为数据不足。
        """
        plan_obj = _plan(group_by=("report_date",))
        ex, an = _run(plan_obj, synth_db)
        assert ex.queries[0].row_count == 11        # SQL 仍按模型计划执行
        assert an.status == analytics.AnalysisStatus.OK
        assert len(an.statistics.series) == 2
        assert all(len(s.points) == 11 for s in an.statistics.series)
        # 并且明确提示已忽略日期分组
        assert any("忽略按日期分组" in w for w in an.warnings)

    def test_sql_dates_match_analysis_dates(self, synth_db):
        """⑦ SQL 返回日期与分析结果使用的日期一致。"""
        plan_obj = _plan()
        ex, an = _run(plan_obj, synth_db)
        sql_dates = sorted({str(r[0]) for r in ex.queries[0].rows})
        analysis_dates = sorted({p.date for p in an.statistics.series[0].points})
        assert sql_dates == analysis_dates
        assert len(analysis_dates) == 11

    def test_values_and_change_rate_match_manual_calc(self, synth_db):
        """⑧ 分析结果的指标值、变化率与独立计算一致。"""
        plan_obj = _plan()
        ex, an = _run(plan_obj, synth_db)
        rows = {str(r[0]): (r[1], r[2]) for r in ex.queries[0].rows}
        purchase = next(s for s in an.statistics.series
                        if s.metric_id == "purchase_amount")
        for point in purchase.points:
            assert point.value == rows[point.date][0]
        changes = {c.date: c for c in purchase.daily_changes}
        dates = sorted(rows)
        for prev, cur in zip(dates, dates[1:]):
            pv, cv = rows[prev][0], rows[cur][0]
            ch = changes[cur]
            assert ch.abs_diff == pytest.approx(cv - pv)
            if pv == 0:
                assert ch.change_rate is None
            else:
                assert ch.change_rate == pytest.approx((cv - pv) / pv)

    def test_group_by_city_still_supported(self, synth_db):
        """分组维度仍生效（城市分组不受修复影响）。"""
        plan_obj = _plan(group_by=("city",))
        ex, an = _run(plan_obj, synth_db)
        assert an.status == analytics.AnalysisStatus.OK
        cities = {s.group.get("city") for s in an.statistics.series}
        assert {"6081949", "6281949", "6301949"} <= cities

    def test_no_zero_filling(self, synth_db):
        """缺失值不得自动填零（缺失日期不产生点）。"""
        plan_obj = _plan()
        ex, an = _run(plan_obj, synth_db)
        purchase = next(s for s in an.statistics.series
                        if s.metric_id == "purchase_amount")
        sql_dates = {str(r[0]) for r in ex.queries[0].rows}
        assert {p.date for p in purchase.points} == sql_dates
        # 全部观测均为真实聚合值（无人为 0 填充的额外日期）
        assert all(p.value is not None for p in purchase.points)
