"""阶段 07.5.2 回归测试：参数化指标（SHIBOR 期限）的 SQL 去重与分析映射。

全部使用合成数据库与固定计划，不调用真实模型 API。
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
from dataagent.query_builder import build_queries
from dataagent.query_service import QueryService

SYNTH_DIR = Path(__file__).parent / "fixtures" / "synthetic"
DATA_RANGE = (date(2024, 1, 2), date(2024, 1, 12))


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory) -> Path:
    db_path = tmp_path_factory.mktemp("s752") / "term.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
    assert all(r.ok for r in reports)
    shibor.create_fill_view(con)
    con.close()
    return db_path


def _shibor_plan(terms=("O_N", "1W"), metrics=None, stats=(StatOperation.AVG,)):
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
        group_by=[], filters=[], statistics=list(stats))


class TestTermAwareAggregation:
    def test_two_terms_produce_two_columns(self, synth_db):
        """① O_N 与 1W 生成两列不同的 SQL 聚合输出。"""
        p = _shibor_plan()
        bundle = build_queries(p, data_range=DATA_RANGE)
        cols = bundle.queries[0].columns
        assert "shibor_rate_o_n_avg" in cols
        assert "shibor_rate_1w_avg" in cols
        sql = " ".join(bundle.queries[0].sql.split())
        assert "AS shibor_rate_o_n_avg" in sql
        assert "AS shibor_rate_1w_avg" in sql

    def test_columns_reference_distinct_fields(self, synth_db):
        """② 两列分别引用正确的底层利率字段。"""
        p = _shibor_plan()
        bundle = build_queries(p, data_range=DATA_RANGE)
        sql = " ".join(bundle.queries[0].sql.split())
        assert "AVG(s.Interest_O_N) AS shibor_rate_o_n_avg" in sql
        assert "AVG(s.Interest_1_W) AS shibor_rate_1w_avg" in sql

    def test_aliases_unique_no_auto_suffix(self, synth_db):
        """③ 两列别名唯一，不出现自动生成的 `_1` 后缀。"""
        p = _shibor_plan()
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        cols = ex.queries[0].columns
        assert len(cols) == len(set(cols))
        assert not any(c.endswith("_1") or c.endswith("_2") for c in cols)

    def test_identical_metric_term_deduped(self, synth_db):
        """④ 完全相同的 (metric_id, term) 重复出现时仍然去重。"""
        p = _shibor_plan(terms=("O_N", "O_N"))
        bundle = build_queries(p, data_range=DATA_RANGE)
        cols = bundle.queries[0].columns
        assert cols.count("shibor_rate_o_n_avg") == 1
        sql = " ".join(bundle.queries[0].sql.split())
        assert sql.count("AVG(s.Interest_O_N)") == 1

    def test_different_terms_not_deduped(self, synth_db):
        """⑤ 同一指标、不同期限时不去重。"""
        p = _shibor_plan(terms=("O_N", "1W", "2W"))
        bundle = build_queries(p, data_range=DATA_RANGE)
        cols = bundle.queries[0].columns
        assert {"shibor_rate_o_n_avg", "shibor_rate_1w_avg",
                "shibor_rate_2w_avg"} <= set(cols)

    def test_two_terms_values_independent(self, synth_db):
        """⑥ 两个期限的查询结果数值保持独立，不互换、不覆盖。"""
        p = _shibor_plan()
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        assert ex.status == ExecutionStatus.OK
        idx = {c: i for i, c in enumerate(ex.queries[0].columns)}
        for row in ex.queries[0].rows:
            on = row[idx["shibor_rate_o_n_avg"]]
            one_w = row[idx["shibor_rate_1w_avg"]]
            assert on is not None and one_w is not None
            assert on != one_w          # 不同期限数值不同（夹具中 O_N < 1W）
            assert on < one_w

    def test_analysis_series_identify_terms(self, synth_db):
        """⑦ 分析结果中两条序列均能识别对应期限。"""
        p = _shibor_plan()
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        terms = {s.term for s in an.statistics.series}
        assert terms == {"O_N", "1W"}
        # 结论文本带期限，可区分
        texts = " ".join(c.text for c in an.conclusions)
        assert "O_N" in texts and "1W" in texts

    def test_purchase_redeem_dedup_still_effective(self, synth_db):
        """⑧ 申购/赎回重复指标的原修复继续有效。"""
        p = AnalysisPlan(
            question="分析申购和赎回趋势", intent="趋势",
            analysis_type=AnalysisType.TIME_TREND,
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="redeem_amount")],
            time_range=TimeRangeSpec(
                type=TimeRangeType.EXPLICIT_DATES,
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 12),
                resolution_status="resolved"),
            group_by=[], filters=[], statistics=[StatOperation.SUM])
        bundle = build_queries(p, data_range=DATA_RANGE)
        cols = bundle.queries[0].columns
        assert cols.count("purchase_amount_sum") == 1
        assert "purchase_amount_sum_1" not in cols
        with QueryService(synth_db) as qs:
            ex = run_analysis(p, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.OK
        assert {s.metric_id for s in an.statistics.series} == {
            "purchase_amount", "redeem_amount"}

    def test_ambiguous_columns_still_rejected(self, synth_db):
        """⑨ 真正无法消歧的多聚合列仍返回 incompatible_metric。"""
        p = _shibor_plan(terms=("O_N",))
        q = QueryExecution(
            name="time_trend_mfd_date", status="ok",
            columns=["mfd_date", "shibor_rate_o_n_avg", "shibor_rate_o_n_avg_1"],
            rows=[(date(2024, 1, 2), 3.8, 3.8), (date(2024, 1, 3), 3.6, 3.6)],
            row_count=2)
        ex = AnalysisExecutionResult(status=ExecutionStatus.OK, plan_summary={},
                                      queries=[q])
        an = analytics.analyze_execution_result(ex, p)
        assert an.status == analytics.AnalysisStatus.INCOMPATIBLE_METRIC
        assert an.conclusions == []

    def test_insufficient_data_protection_not_regressed(self, synth_db):
        """⑩ insufficient_data 与日期趋势保护不回归。"""
        p = _shibor_plan()
        p2 = p.model_copy(update={"time_range": TimeRangeSpec(
            type=TimeRangeType.EXPLICIT_DATES,
            resolved_start_date=date(2024, 1, 12),
            resolved_end_date=date(2024, 1, 12),
            resolution_status="resolved")})
        with QueryService(synth_db) as qs:
            ex = run_analysis(p2, qs, data_range=DATA_RANGE)
        an = analytics.analyze_execution_result(ex, p2)
        assert an.status == analytics.AnalysisStatus.INSUFFICIENT_DATA
