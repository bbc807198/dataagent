"""阶段 07.5.1 回归测试：重复聚合列与指标映射（合成数据，不调用真实 API）。"""

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
from dataagent.query_builder import build_queries
from dataagent.query_service import QueryService

SYNTH_DIR = Path(__file__).parent / "fixtures" / "synthetic"
DATA_RANGE = (date(2024, 1, 2), date(2024, 1, 12))


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory) -> Path:
    db_path = tmp_path_factory.mktemp("s751") / "dup.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
    assert all(r.ok for r in reports)
    shibor.create_fill_view(con)
    con.close()
    return db_path


def _plan(metrics=None, group_by=("report_date",),
          start=date(2024, 1, 2), end=date(2024, 1, 12)):
    return AnalysisPlan(
        question="分析2024年1月2日至2024年1月12日的申购和赎回趋势",
        intent="趋势", analysis_type=AnalysisType.TIME_TREND,
        metrics=[MetricSpec(metric_id=m) for m in
                 (metrics or ("purchase_amount", "redeem_amount"))],
        time_range=TimeRangeSpec(
            type=TimeRangeType.EXPLICIT_DATES,
            resolved_start_date=start, resolved_end_date=end,
            resolution_status="resolved"),
        group_by=list(group_by), filters=[],
        statistics=[StatOperation.SUM])


class TestDuplicateAggregateColumns:
    def test_duplicate_metrics_deduped_in_plan(self):
        """① 同一指标重复出现 → 计划层去重（保留首个）并记录说明。"""
        p = _plan(metrics=("purchase_amount", "purchase_amount", "redeem_amount"))
        assert [m.metric_id for m in p.metrics] == ["purchase_amount", "redeem_amount"]
        assert any("已合并重复指标" in n for n in p.notes)

    def test_different_terms_are_kept(self):
        """同一指标的不同 term 视为不同口径，不合并。"""
        p = AnalysisPlan(
            question="q", intent="i", analysis_type=AnalysisType.TIME_TREND,
            metrics=[MetricSpec(metric_id="shibor_rate", term="O_N"),
                     MetricSpec(metric_id="shibor_rate", term="1W")],
            time_range=TimeRangeSpec(
                type=TimeRangeType.EXPLICIT_DATES,
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 12),
                resolution_status="resolved"),
            group_by=[], filters=[], statistics=[StatOperation.AVG])
        assert len(p.metrics) == 2
        assert {m.term for m in p.metrics} == {"O_N", "1W"}

    def test_sql_has_no_duplicate_aggregate_expression(self, synth_db):
        """② SQL 不生成重复聚合表达式（每个指标只有一个聚合输出列）。"""
        p = _plan(metrics=("purchase_amount", "purchase_amount", "redeem_amount"))
        bundle = build_queries(p, data_range=DATA_RANGE)
        sql = " ".join(bundle.queries[0].sql.split())
        aggs = [seg.strip() for seg in sql.split("FROM")[0].split(",")
                if "SUM(" in seg or "AVG(" in seg or "COUNT(" in seg]
        assert len(aggs) == len(set(aggs))          # 无重复聚合表达式
        assert len(aggs) == 2                       # 两个指标各一列
        assert bundle.queries[0].columns.count("purchase_amount_sum") == 1

    def test_result_columns_unique_per_metric(self, synth_db):
        """③ 查询结果中两个指标各有唯一、明确的聚合列。"""
        p = _plan(metrics=("purchase_amount", "purchase_amount", "redeem_amount"))
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        assert ex.status == ExecutionStatus.OK
        cols = ex.queries[0].columns
        assert len(cols) == len(set(cols))          # 无 _1 后缀列
        assert "purchase_amount_sum" in cols
        assert "redeem_amount_sum" in cols

    def test_report_date_used_for_time_grouping(self, synth_db):
        """④ report_date 正确用于时间分组（不被当作额外指标列）。"""
        p = _plan()
        bundle = build_queries(p, data_range=DATA_RANGE)
        sql = " ".join(bundle.queries[0].sql.split())
        assert "GROUP BY b.report_date" in sql
        assert "report_date AS report_date" in sql
        assert "report_date_sum" not in bundle.queries[0].columns

    def test_trend_succeeds_with_two_metrics(self, synth_db):
        """⑤ 两指标均有多个有效日期 → 趋势分析成功。"""
        p = _plan()
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        assert len(an.statistics.series) == 2
        assert all(len(s.points) == 11 for s in an.statistics.series)

    def test_expected_date_series(self, synth_db):
        """⑥ 2024-01-02~01-12 合成演示生成预期日期序列。"""
        p = _plan()
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        dates = [pt.date for pt in an.statistics.series[0].points]
        assert dates[0] == "2024-01-02"
        assert dates[-1] == "2024-01-12"
        assert len(dates) == 11

    def test_daily_change_matches_manual(self, synth_db):
        """⑦ 日环比与独立计算一致。"""
        p = _plan()
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        rows = {str(r[0]): r[1] for r in ex.queries[0].rows}
        dates = sorted(rows)
        purchase = next(s for s in an.statistics.series
                        if s.metric_id == "purchase_amount")
        for ch in purchase.daily_changes:
            idx = dates.index(ch.date)
            prev_value = rows[dates[idx - 1]]
            assert ch.previous_value == prev_value
            assert ch.abs_diff == pytest.approx(rows[ch.date] - prev_value)
            if prev_value:
                assert ch.change_rate == pytest.approx(
                    (rows[ch.date] - prev_value) / prev_value)

    def test_genuinely_ambiguous_columns_still_rejected(self, synth_db):
        """⑧ 真正存在多个不同聚合口径且无法消歧 → 仍返回 incompatible_metric。"""
        p = _plan()
        # 构造两列映射到同一指标的结果（模拟历史缺陷/外部数据）
        q = QueryExecution(
            name="time_trend_report_date", status="ok",
            columns=["report_date", "purchase_amount_sum", "purchase_amount_sum_1",
                     "redeem_amount_sum"],
            rows=[(date(2024, 1, 2), 100.0, 100.0, 50.0),
                  (date(2024, 1, 3), 120.0, 120.0, 60.0)],
            row_count=2)
        ex = AnalysisExecutionResult(status=ExecutionStatus.OK, plan_summary={},
                                      queries=[q])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.INCOMPATIBLE_METRIC
        assert "多个聚合口径列" in (an.status_reason or "")
        assert an.conclusions == []

    def test_city_grouping_not_regressed(self, synth_db):
        """⑨ 原有城市分组不受影响。"""
        p = _plan(group_by=("city",))
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        cities = {s.group.get("city") for s in an.statistics.series}
        assert {"6081949", "6281949", "6301949"} <= cities

    def test_insufficient_data_protection_still_active(self, synth_db):
        """⑩ insufficient_data 保护继续生效（单日范围）。"""
        p = _plan(start=date(2024, 1, 12), end=date(2024, 1, 12))
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.INSUFFICIENT_DATA
