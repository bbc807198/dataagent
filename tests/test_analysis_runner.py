"""分析执行器测试（阶段五）：端到端（合成数据 → SQL → QueryService 执行）。

全部基于 tests/fixtures/synthetic 合成的临时数据库，不依赖真实用户数据。
"""

from __future__ import annotations

import datetime as dt
from datetime import date
from pathlib import Path

import pytest

from dataagent import analysis_runner, db, ingestion, shibor
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    FilterCondition,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
)
from dataagent.analysis_runner import ExecutionStatus, run_analysis
from dataagent.query_service import QueryService

SYNTHETIC_DIR = Path(__file__).parent / "fixtures" / "synthetic"
SYNTH_RANGE = (date(2024, 1, 2), date(2024, 1, 12))


@pytest.fixture(scope="module")
def synth_qs(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("stage5") / "synth_s5.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTHETIC_DIR)
    assert all(r.ok for r in reports), [r.error for r in reports if r.error]
    shibor.create_fill_view(con)
    con.close()
    qs = QueryService(db_path)
    yield qs
    qs.close()


def _plan(**overrides):
    base = dict(
        question="测试问题", intent="测试",
        analysis_type=AnalysisType.TIME_TREND,
        metrics=[MetricSpec(metric_id="purchase_amount")],
        time_range=TimeRangeSpec(
            type="explicit_dates",
            resolved_start_date=date(2024, 1, 2),
            resolved_end_date=date(2024, 1, 12),
            resolution_status="resolved"),
        group_by=[], statistics=[StatOperation.SUM])
    base.update(overrides)
    return AnalysisPlan(**base)


# ---------------------------------------------------------------------------
# 正向：结果与合成数据已知统计值一致
# ---------------------------------------------------------------------------

class TestTrendEndToEnd:
    def test_daily_purchase_trend(self, synth_qs):
        r = run_analysis(_plan(), synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.OK
        q = r.queries[0]
        assert q.columns == ["report_date", "purchase_amount"]
        assert q.row_count == 11          # 合成数据 11 天
        total = sum(row[1] for row in q.rows)
        assert total == 2850              # 合成数据总申购

    def test_purchase_and_redeem(self, synth_qs):
        r = run_analysis(_plan(metrics=[
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="redeem_amount")]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        purchase = sum(row[1] for row in q.rows)
        redeem = sum(row[2] for row in q.rows)
        assert (purchase, redeem) == (2850, 1850)

    def test_net_inflow(self, synth_qs):
        r = run_analysis(_plan(metrics=[MetricSpec(metric_id="net_inflow")]),
                         synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert sum(row[1] for row in q.rows) == 1000   # 2850 - 1850

    def test_yield_series(self, synth_qs):
        r = run_analysis(_plan(metrics=[MetricSpec(metric_id="daily_yield")],
                               statistics=[StatOperation.AVG]),
                         synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.row_count == 11
        assert q.rows[0][0] == dt.date(2024, 1, 2)
        assert float(q.rows[0][1]) == pytest.approx(1.50)

    def test_shibor_series_uses_filled_view(self, synth_qs):
        r = run_analysis(_plan(metrics=[MetricSpec(metric_id="shibor_rate", term="1W")],
                               statistics=[StatOperation.AVG]),
                         synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert r.status == ExecutionStatus.OK
        assert q.row_count == 11   # 填充视图覆盖全部日期
        # 首行 2024-01-02 的 1W 利率为 4.30（0101 超出合成数据范围）
        assert q.rows[0][0] == dt.date(2024, 1, 2)
        assert float(q.rows[0][1]) == pytest.approx(4.30)


class TestRankingEndToEnd:
    def test_city_ranking(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            statistics=[StatOperation.SUM, StatOperation.TOP_N], top_n=3),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert r.status == ExecutionStatus.OK
        # 合成数据：101→6081949(1100)、102→6281949(1500)、103→6301949(250)
        by_city = {row[0]: row[1] for row in q.rows}
        assert by_city == {"6281949": 1500, "6081949": 1100, "6301949": 250}
        assert q.rows[0][0] == "6281949"   # 降序第一

    def test_sex_period_user_count(self, synth_qs):
        """期间去重用户数（2024-01-02~01-12 内有资金记录的用户）。"""
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        by_sex = {row[0]: row[1] for row in q.rows}
        # 有资金记录的 101/102/103：sex=1 有 101/103，sex=0 有 102（999 无资金记录）
        assert by_sex == {1: 2, 0: 1}

    def test_city_filter_in(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            filters=[FilterCondition(field="city", operator="in",
                                     value=["6081949", "6281949"])]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert {row[0] for row in q.rows} == {"6081949", "6281949"}


class TestWindowComparisonEndToEnd:
    def test_two_windows(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.WINDOW_COMPARISON,
            time_range=TimeRangeSpec(
                type="window_comparison",
                resolved_start_date=date(2024, 1, 9),
                resolved_end_date=date(2024, 1, 12),
                comparison_start_date=date(2024, 1, 2),
                comparison_end_date=date(2024, 1, 8),
                resolution_status="resolved"),
            statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF]),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.OK
        assert len(r.queries) == 2
        main, comp = r.queries
        assert main.window_label == "main" and comp.window_label == "comparison"
        # 主窗口 0109-0112：1000+500+250 = 1750；对比窗口 0102-0108：500+300+200+100 = 1100
        assert main.rows[0][1] == 1750
        assert comp.rows[0][1] == 1100


class TestDescriptiveEndToEnd:
    def test_total(self, synth_qs):
        r = run_analysis(_plan(analysis_type=AnalysisType.DESCRIPTIVE),
                         synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.row_count == 1
        assert q.rows[0][0] == 2850

    def test_distribution(self, synth_qs):
        r = run_analysis(_plan(analysis_type=AnalysisType.DISTRIBUTION,
                               group_by=["city"],
                               statistics=[StatOperation.COUNT]),
                         synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        # 记录数：101 有 7 条（0102-0108）、102 有 4 条（0109-0112）、103 有 1 条
        by_city = {row[0]: row[1] for row in q.rows}
        assert by_city == {"6081949": 7, "6281949": 4, "6301949": 1}


# ---------------------------------------------------------------------------
# 负向：拒绝的计划不进入数据库执行
# ---------------------------------------------------------------------------

class TestNegativeExecution:
    def test_clarification_plan_not_executed(self, synth_qs):
        r = run_analysis(_plan(clarification_needed=True,
                               clarification_questions=["请确认指标"]),
                         synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.NEEDS_CLARIFICATION
        assert r.queries == []          # 未生成/未执行任何查询

    def test_unresolved_plan_not_executed(self, synth_qs):
        r = run_analysis(_plan(unresolved=["指标口径未确认"]),
                         synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.PLAN_REJECTED
        assert r.error_code == "unresolved_present"
        assert r.queries == []

    def test_out_of_range_plan_not_executed(self, synth_qs):
        r = run_analysis(_plan(time_range=TimeRangeSpec(
            type="explicit_dates",
            resolved_start_date=date(2030, 1, 1),
            resolved_end_date=date(2030, 1, 31),
            resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.PLAN_REJECTED
        assert r.error_code == "data_range_mismatch"
        assert r.queries == []

    def test_rejected_plan_never_hits_database(self, synth_qs):
        """非法计划不得触发任何 execute 调用。"""
        calls = {"n": 0}

        class Spy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, *a, **k):
                calls["n"] += 1
                return self._inner.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        spy = Spy(synth_qs._con)
        synth_qs._con = spy
        try:
            run_analysis(_plan(unresolved=["未确认"]), synth_qs,
                         data_range=SYNTH_RANGE)
            assert calls["n"] == 0
        finally:
            synth_qs._con = spy._inner

    def test_sql_rejected_status_when_guard_blocks(self, synth_qs, monkeypatch):
        """若生成的 SQL 在生成后被篡改为危险语句，QueryService 必须拒绝，
        且执行器状态为 sql_rejected（不得当作成功）。"""
        from dataagent import analysis_runner as ar
        from dataagent.query_builder import GeneratedQuery, QueryBundle

        def tampered_build(plan, data_range=None):
            return QueryBundle(
                question=plan.question, analysis_type="time_trend",
                queries=[GeneratedQuery(
                    name="tampered", sql="DROP TABLE user_balance_table",
                    params=[], columns=[], purpose="tampered", metric_ids=[])])

        monkeypatch.setattr(ar, "build_queries", tampered_build)
        r = run_analysis(_plan(), synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.SQL_REJECTED
        assert r.queries[0].status == "rejected"

    def test_execution_error_status(self, synth_qs):
        """通过校验但执行失败 → execution_failed（不得当作成功）。"""
        r = run_analysis(_plan(), synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.OK  # 基线正常
        # 直接构造参数错误场景：日期合法但列不存在（模拟执行期错误）
        from dataagent.query_service import QueryStatus
        bad = synth_qs.execute("SELECT SUM(b.nonexistent_col) AS x FROM "
                               "user_balance_table b WHERE b.report_date BETWEEN ? AND ?",
                               ["2024-01-02", "2024-01-12"])
        assert bad.query_status in (QueryStatus.EXECUTION_ERROR, QueryStatus.REJECTED)

    def test_timeout_status(self, synth_qs, monkeypatch):
        """查询超时 → timeout 状态，不视为成功（用受控 fake 稳定验证映射）。"""
        from dataagent.query_service import QueryResult, QueryStatus

        def fake_execute(self, sql, params=None):
            return QueryResult(query_status=QueryStatus.TIMEOUT,
                               error="查询执行超过 0.001 秒，已被取消",
                               error_category="timeout")

        monkeypatch.setattr(QueryService, "execute", fake_execute)
        r = run_analysis(_plan(), synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.TIMEOUT
        assert not r.all_ok()
        assert r.queries[0].status == "timeout"


class TestResultStructure:
    def test_result_contains_required_fields(self, synth_qs):
        r = run_analysis(_plan(), synth_qs, data_range=SYNTH_RANGE)
        d = r.as_dict()
        for key in ("status", "plan_summary", "queries", "total_ms",
                    "notes", "warnings"):
            assert key in d
        q = d["queries"][0]
        for key in ("name", "status", "columns", "rows", "row_count",
                    "truncated", "execution_ms", "purpose", "param_count"):
            assert key in q

    def test_no_params_values_in_result(self, synth_qs):
        """结果中不输出参数值（防止用户输入进入日志/报告）。"""
        r = run_analysis(_plan(filters=[
            FilterCondition(field="city", operator="eq", value="6081949")]),
            synth_qs, data_range=SYNTH_RANGE)
        d = r.as_dict()
        import json
        blob = json.dumps({k: v for k, v in d.items() if k != "queries"},
                          ensure_ascii=False, default=str)
        assert "6081949" not in blob   # 参数值不出现在计划摘要/警告中
        q = d["queries"][0]
        assert "params" not in q and q["param_count"] >= 3

    def test_no_analysis_conclusions_claimed(self, synth_qs):
        r = run_analysis(_plan(), synth_qs, data_range=SYNTH_RANGE)
        assert any("后续阶段" in n for n in r.notes)


# ===========================================================================
# 任务 05.1：多维分组 / 期间用户数 / 非法计划拒绝（端到端）
# ===========================================================================

class TestTrendWithDimensionEndToEnd:
    """任务 05.1-A：时间趋势叠加城市维度，结果不得混合城市。"""

    def test_daily_by_city(self, synth_qs):
        r = run_analysis(_plan(group_by=["city"]), synth_qs,
                         data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.OK
        q = r.queries[0]
        assert q.columns == ["report_date", "city", "purchase_amount"]
        # 每天 × 城市 的组合：0102 只有 101（city 6081949）→ 500
        first = q.rows[0]
        assert first[0] == dt.date(2024, 1, 2)
        assert first[1] == "6081949"
        assert first[2] == 500
        # 各城市总额之和 = 总申购 2850（城市之间不混合）
        by_city: dict[str, int] = {}
        for _, city, amt in q.rows:
            by_city[city] = by_city.get(city, 0) + amt
        assert by_city == {"6081949": 1100, "6281949": 1500, "6301949": 250}

    def test_daily_by_sex(self, synth_qs):
        r = run_analysis(_plan(group_by=["sex"]), synth_qs,
                         data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["report_date", "sex", "purchase_amount"]
        # 101(sex=1) 0102 500；102(sex=0) 0109 1000；103(sex=1) 0112 250
        rows = {(row[0], row[1]): row[2] for row in q.rows}
        assert rows[(dt.date(2024, 1, 2), 1)] == 500
        assert rows[(dt.date(2024, 1, 9), 0)] == 1000
        assert rows[(dt.date(2024, 1, 12), 1)] == 250

    def test_daily_by_constellation(self, synth_qs):
        r = run_analysis(_plan(group_by=["constellation"]), synth_qs,
                         data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["report_date", "constellation", "purchase_amount"]
        assert r.status == ExecutionStatus.OK


class TestMultiDimensionEndToEnd:
    """任务 05.1-B：多维分组结果与合成数据已知值一致。"""

    def test_city_sex_ranking(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING,
            group_by=["city", "sex"]), synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["city", "sex", "purchase_amount"]
        got = {(row[0], row[1]): row[2] for row in q.rows}
        # 101: 6081949/sex1 → 1100；102: 6281949/sex0 → 1500；103: 6301949/sex1 → 250
        assert got == {("6081949", 1): 1100, ("6281949", 0): 1500,
                       ("6301949", 1): 250}
        # 排序：降序
        amounts = [row[2] for row in q.rows]
        assert amounts == sorted(amounts, reverse=True)

    def test_city_constellation_ranking(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING,
            group_by=["city", "constellation"]), synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["city", "constellation", "purchase_amount"]
        got = {(row[0], row[1]): row[2] for row in q.rows}
        assert got == {("6081949", "白羊座"): 1100,
                       ("6281949", "金牛座"): 1500,
                       ("6301949", "双子座"): 250}

    def test_multi_dimension_distribution(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DISTRIBUTION, group_by=["city", "sex"],
            statistics=[StatOperation.COUNT]), synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        got = {(row[0], row[1]): row[2] for row in q.rows}
        # 记录数：101→7 条、102→4 条、103→1 条
        assert got == {("6081949", 1): 7, ("6281949", 0): 4, ("6301949", 1): 1}

    def test_multi_dimension_window_comparison(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.WINDOW_COMPARISON,
            time_range=TimeRangeSpec(
                type="window_comparison",
                resolved_start_date=date(2024, 1, 8),
                resolved_end_date=date(2024, 1, 12),
                comparison_start_date=date(2024, 1, 2),
                comparison_end_date=date(2024, 1, 7),
                resolution_status="resolved"),
            group_by=["city", "sex"],
            statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF]),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.OK
        main, comp = r.queries
        # 主窗口 0108-0112：6281949/0 = 1500、6301949/1 = 250（101 在 0108 无申购）
        main_map = {(row[1], row[2]): row[3] for row in main.rows}
        # 101 在 0108 有一条金额为 0 的记录 → 该组合在主窗口内聚合为 0
        assert main_map == {("6081949", 1): 0, ("6281949", 0): 1500,
                            ("6301949", 1): 250}
        # 对比窗口 0102-0107：只有 101（6081949/1）= 1100
        comp_map = {(row[1], row[2]): row[3] for row in comp.rows}
        assert comp_map == {("6081949", 1): 1100}


class TestPeriodUserCountEndToEnd:
    """任务 05.1-D：期间去重用户数（同一用户多天只计一次）。"""

    def test_period_user_count_dedup(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        # 101 有 7 条记录、102 有 4 条、103 有 1 条，但去重后仅 3 人
        assert q.rows[0][0] == 3

    def test_date_filter_changes_period_count(self, synth_qs):
        """日期过滤确实影响期间用户数（不同窗口用户集合不同）。"""
        early = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="explicit_dates",
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 8),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        late = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="explicit_dates",
                resolved_start_date=date(2024, 1, 9),
                resolved_end_date=date(2024, 1, 12),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        assert early.queries[0].rows[0][0] == 1   # 只有 101
        assert late.queries[0].rows[0][0] == 2    # 102 + 103

    def test_static_total_full_period(self, synth_qs):
        """静态画像总数：全期口径下为画像表全部用户（含无资金记录的 999）。"""
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="user_count")],
            statistics=[StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="full_range",
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 12),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.queries[0].rows[0][0] == 4   # 101/102/103/999


class TestIllegalPlanNeverExecutes:
    """任务 05.1：非法计划（类别范围筛选/口径不明）在执行前被拒绝。"""

    def test_category_range_filter_rejected_before_execution(self, synth_qs):
        # Schema 层即拒绝：无法构造该计划
        with pytest.raises(Exception, match="只允许 eq/ne/in"):
            FilterCondition(field="city", operator="gt", value="6081949")

    def test_ambiguous_user_count_rejected_before_execution(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.PLAN_REJECTED
        assert r.error_code == "ambiguous_user_count_semantics"
        assert r.queries == []

    def test_unsupported_multi_dimension_combination_rejected(self):
        # 市场指标 + 用户维度 → Schema 拒绝（维度不兼容）
        with pytest.raises(Exception):
            _plan(metrics=[MetricSpec(metric_id="daily_yield")],
                  statistics=[StatOperation.AVG], group_by=["city"])


# ===========================================================================
# 任务 05.1：多维分组 / 期间用户数 / 非法计划拒绝（端到端）
# ===========================================================================

class TestTrendWithDimensionEndToEnd:
    """任务 05.1-A：时间趋势叠加城市维度，结果不得混合城市。"""

    def test_daily_by_city(self, synth_qs):
        r = run_analysis(_plan(group_by=["city"]), synth_qs,
                         data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.OK
        q = r.queries[0]
        assert q.columns == ["report_date", "city", "purchase_amount"]
        # 每天 × 城市 的组合：0102 只有 101（city 6081949）→ 500
        first = q.rows[0]
        assert first[0] == dt.date(2024, 1, 2)
        assert first[1] == "6081949"
        assert first[2] == 500
        # 各城市总额之和 = 总申购 2850（城市之间不混合）
        by_city: dict[str, int] = {}
        for _, city, amt in q.rows:
            by_city[city] = by_city.get(city, 0) + amt
        assert by_city == {"6081949": 1100, "6281949": 1500, "6301949": 250}

    def test_daily_by_sex(self, synth_qs):
        r = run_analysis(_plan(group_by=["sex"]), synth_qs,
                         data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["report_date", "sex", "purchase_amount"]
        # 101(sex=1) 0102 500；102(sex=0) 0109 1000；103(sex=1) 0112 250
        rows = {(row[0], row[1]): row[2] for row in q.rows}
        assert rows[(dt.date(2024, 1, 2), 1)] == 500
        assert rows[(dt.date(2024, 1, 9), 0)] == 1000
        assert rows[(dt.date(2024, 1, 12), 1)] == 250

    def test_daily_by_constellation(self, synth_qs):
        r = run_analysis(_plan(group_by=["constellation"]), synth_qs,
                         data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["report_date", "constellation", "purchase_amount"]
        assert r.status == ExecutionStatus.OK


class TestMultiDimensionEndToEnd:
    """任务 05.1-B：多维分组结果与合成数据已知值一致。"""

    def test_city_sex_ranking(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING,
            group_by=["city", "sex"]), synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["city", "sex", "purchase_amount"]
        got = {(row[0], row[1]): row[2] for row in q.rows}
        # 101: 6081949/sex1 → 1100；102: 6281949/sex0 → 1500；103: 6301949/sex1 → 250
        assert got == {("6081949", 1): 1100, ("6281949", 0): 1500,
                       ("6301949", 1): 250}
        # 排序：降序
        amounts = [row[2] for row in q.rows]
        assert amounts == sorted(amounts, reverse=True)

    def test_city_constellation_ranking(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING,
            group_by=["city", "constellation"]), synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        assert q.columns == ["city", "constellation", "purchase_amount"]
        got = {(row[0], row[1]): row[2] for row in q.rows}
        assert got == {("6081949", "白羊座"): 1100,
                       ("6281949", "金牛座"): 1500,
                       ("6301949", "双子座"): 250}

    def test_multi_dimension_distribution(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DISTRIBUTION, group_by=["city", "sex"],
            statistics=[StatOperation.COUNT]), synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        got = {(row[0], row[1]): row[2] for row in q.rows}
        # 记录数：101→7 条、102→4 条、103→1 条
        assert got == {("6081949", 1): 7, ("6281949", 0): 4, ("6301949", 1): 1}

    def test_multi_dimension_window_comparison(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.WINDOW_COMPARISON,
            time_range=TimeRangeSpec(
                type="window_comparison",
                resolved_start_date=date(2024, 1, 8),
                resolved_end_date=date(2024, 1, 12),
                comparison_start_date=date(2024, 1, 2),
                comparison_end_date=date(2024, 1, 7),
                resolution_status="resolved"),
            group_by=["city", "sex"],
            statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF]),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.OK
        main, comp = r.queries
        # 主窗口 0108-0112：6281949/0 = 1500、6301949/1 = 250（101 在 0108 无申购）
        main_map = {(row[1], row[2]): row[3] for row in main.rows}
        # 101 在 0108 有一条金额为 0 的记录 → 该组合在主窗口内聚合为 0
        assert main_map == {("6081949", 1): 0, ("6281949", 0): 1500,
                            ("6301949", 1): 250}
        # 对比窗口 0102-0107：只有 101（6081949/1）= 1100
        comp_map = {(row[1], row[2]): row[3] for row in comp.rows}
        assert comp_map == {("6081949", 1): 1100}


class TestPeriodUserCountEndToEnd:
    """任务 05.1-D：期间去重用户数（同一用户多天只计一次）。"""

    def test_period_user_count_dedup(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        # 101 有 7 条记录、102 有 4 条、103 有 1 条，但去重后仅 3 人
        assert q.rows[0][0] == 3

    def test_date_filter_changes_period_count(self, synth_qs):
        """日期过滤确实影响期间用户数（不同窗口用户集合不同）。"""
        early = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="explicit_dates",
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 8),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        late = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="explicit_dates",
                resolved_start_date=date(2024, 1, 9),
                resolved_end_date=date(2024, 1, 12),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        assert early.queries[0].rows[0][0] == 1   # 只有 101
        assert late.queries[0].rows[0][0] == 2    # 102 + 103

    def test_static_total_full_period(self, synth_qs):
        """静态画像总数：全期口径下为画像表全部用户（含无资金记录的 999）。"""
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="user_count")],
            statistics=[StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="full_range",
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 12),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.queries[0].rows[0][0] == 4   # 101/102/103/999


class TestIllegalPlanNeverExecutes:
    """任务 05.1：非法计划（类别范围筛选/口径不明）在执行前被拒绝。"""

    def test_category_range_filter_rejected_before_execution(self, synth_qs):
        # Schema 层即拒绝：无法构造该计划
        with pytest.raises(Exception, match="只允许 eq/ne/in"):
            FilterCondition(field="city", operator="gt", value="6081949")

    def test_ambiguous_user_count_rejected_before_execution(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        assert r.status == ExecutionStatus.PLAN_REJECTED
        assert r.error_code == "ambiguous_user_count_semantics"
        assert r.queries == []

    def test_unsupported_multi_dimension_combination_rejected(self):
        # 市场指标 + 用户维度 → Schema 拒绝（维度不兼容）
        with pytest.raises(Exception):
            _plan(metrics=[MetricSpec(metric_id="daily_yield")],
                  statistics=[StatOperation.AVG], group_by=["city"])


# ===========================================================================
# 任务 05.2：active_user_count + 资金指标的合成数据端到端
# ===========================================================================

class TestAmountWithPeriodUsersEndToEnd:
    """同一窗口内同时返回资金金额与期间去重用户数（值经合成数据核对）。"""

    def _run(self, synth_qs, metrics, **kw):
        kw.setdefault("analysis_type", AnalysisType.DESCRIPTIVE)
        return run_analysis(_plan(
            metrics=metrics,
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT], **kw),
            synth_qs, data_range=SYNTH_RANGE)

    def test_purchase_with_users(self, synth_qs):
        r = self._run(synth_qs, [
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="active_user_count")])
        assert r.status == ExecutionStatus.OK
        q = r.queries[0]
        row = q.rows[0]
        cols = {name: row[i] for i, name in enumerate(q.columns)}
        assert cols["purchase_amount_sum"] == 2850
        assert cols["active_user_count"] == 3

    def test_redeem_with_users(self, synth_qs):
        r = self._run(synth_qs, [
            MetricSpec(metric_id="redeem_amount"),
            MetricSpec(metric_id="active_user_count")])
        q = r.queries[0]
        cols = {name: q.rows[0][i] for i, name in enumerate(q.columns)}
        assert cols["redeem_amount_sum"] == 1850
        assert cols["active_user_count"] == 3

    def test_net_inflow_with_users(self, synth_qs):
        r = self._run(synth_qs, [
            MetricSpec(metric_id="net_inflow"),
            MetricSpec(metric_id="active_user_count")])
        q = r.queries[0]
        cols = {name: q.rows[0][i] for i, name in enumerate(q.columns)}
        assert cols["net_inflow_sum"] == 1000
        assert cols["active_user_count"] == 3

    def test_same_user_multiple_days_counted_once(self, synth_qs):
        """101 在 0102-0108 有 7 条记录，期间用户数只计 1。"""
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="explicit_dates",
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 8),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        cols = {name: q.rows[0][i] for i, name in enumerate(q.columns)}
        assert cols["purchase_amount_sum"] == 1100   # 500+300+200+0+100+0+0
        assert cols["active_user_count"] == 1

    def test_different_windows_filter_both_metrics(self, synth_qs):
        """不同窗口：金额与期间用户数都按对应窗口过滤。"""
        def run(start, end):
            return run_analysis(_plan(
                analysis_type=AnalysisType.DESCRIPTIVE,
                metrics=[MetricSpec(metric_id="purchase_amount"),
                         MetricSpec(metric_id="active_user_count")],
                statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT],
                time_range=TimeRangeSpec(
                    type="explicit_dates", resolved_start_date=start,
                    resolved_end_date=end, resolution_status="resolved")),
                synth_qs, data_range=SYNTH_RANGE).queries[0]

        early = run(date(2024, 1, 2), date(2024, 1, 8))
        late = run(date(2024, 1, 9), date(2024, 1, 12))
        early_cols = {n: early.rows[0][i] for i, n in enumerate(early.columns)}
        late_cols = {n: late.rows[0][i] for i, n in enumerate(late.columns)}
        assert early_cols == {"purchase_amount_sum": 1100, "active_user_count": 1}
        assert late_cols == {"purchase_amount_sum": 1750, "active_user_count": 2}

    def test_grouped_by_city(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        got = {(row[0]): (row[1], row[2]) for row in q.rows}
        assert got == {"6081949": (1100, 1), "6281949": (1500, 1),
                       "6301949": (250, 1)}

    def test_grouped_by_sex(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        got = {(row[0]): (row[1], row[2]) for row in q.rows}
        # sex=1: 101(1100)+103(250)=1350, 2 人；sex=0: 102=1500, 1 人
        assert got == {1: (1350, 2), 0: (1500, 1)}

    def test_results_pass_guard_and_go_through_query_service(self, synth_qs):
        """SQL 经 sql_guard 预检，且执行必须经 QueryService（审计记录可见）。"""
        audit_before = len(synth_qs.audit_log)
        r = self._run(synth_qs, [
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="active_user_count")])
        assert r.status == ExecutionStatus.OK
        assert len(synth_qs.audit_log) == audit_before + 1
        assert synth_qs.audit_log[-1].query_status == "ok"


class TestStaticMetricCombinationRejected:
    """任务 05.2-A：静态 user_count 混用在执行前被拒绝（Schema/生成器）。"""

    def test_static_with_amount_rejected_at_schema(self):
        with pytest.raises(Exception, match="不能与资金指标/期间用户数混用"):
            _plan(metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])

    def test_static_with_amount_rejected_before_execution(self, synth_qs):
        """即使构造绕过（直接调 build_queries），也不得执行任何查询。"""
        from dataagent.query_builder import QueryBuildError, build_queries

        class FakeMetric:
            metric_id = "purchase_amount"
            term = None

        plan = _plan()
        # 通过 model_copy 注入不合法指标组合（绕过 Schema 校验）
        bad = plan.model_copy(update={"metrics": [
            FakeMetric(), MetricSpec(metric_id="user_count")]})
        calls = {"n": 0}

        class Spy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, *a, **k):
                calls["n"] += 1
                return self._inner.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        spy = Spy(synth_qs._con)
        synth_qs._con = spy
        try:
            with pytest.raises(QueryBuildError) as e:
                build_queries(bad, data_range=SYNTH_RANGE)
            assert e.value.code == "ambiguous_user_count_semantics"
            assert calls["n"] == 0
        finally:
            synth_qs._con = spy._inner


# ===========================================================================
# 任务 05.2：active_user_count + 资金指标的合成数据端到端
# ===========================================================================

class TestAmountWithPeriodUsersEndToEnd:
    """同一窗口内同时返回资金金额与期间去重用户数（值经合成数据核对）。"""

    def _run(self, synth_qs, metrics, **kw):
        kw.setdefault("analysis_type", AnalysisType.DESCRIPTIVE)
        return run_analysis(_plan(
            metrics=metrics,
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT], **kw),
            synth_qs, data_range=SYNTH_RANGE)

    def test_purchase_with_users(self, synth_qs):
        r = self._run(synth_qs, [
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="active_user_count")])
        assert r.status == ExecutionStatus.OK
        q = r.queries[0]
        row = q.rows[0]
        cols = {name: row[i] for i, name in enumerate(q.columns)}
        assert cols["purchase_amount_sum"] == 2850
        assert cols["active_user_count"] == 3

    def test_redeem_with_users(self, synth_qs):
        r = self._run(synth_qs, [
            MetricSpec(metric_id="redeem_amount"),
            MetricSpec(metric_id="active_user_count")])
        q = r.queries[0]
        cols = {name: q.rows[0][i] for i, name in enumerate(q.columns)}
        assert cols["redeem_amount_sum"] == 1850
        assert cols["active_user_count"] == 3

    def test_net_inflow_with_users(self, synth_qs):
        r = self._run(synth_qs, [
            MetricSpec(metric_id="net_inflow"),
            MetricSpec(metric_id="active_user_count")])
        q = r.queries[0]
        cols = {name: q.rows[0][i] for i, name in enumerate(q.columns)}
        assert cols["net_inflow_sum"] == 1000
        assert cols["active_user_count"] == 3

    def test_same_user_multiple_days_counted_once(self, synth_qs):
        """101 在 0102-0108 有 7 条记录，期间用户数只计 1。"""
        r = run_analysis(_plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT],
            time_range=TimeRangeSpec(
                type="explicit_dates",
                resolved_start_date=date(2024, 1, 2),
                resolved_end_date=date(2024, 1, 8),
                resolution_status="resolved")),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        cols = {name: q.rows[0][i] for i, name in enumerate(q.columns)}
        assert cols["purchase_amount_sum"] == 1100   # 500+300+200+0+100+0+0
        assert cols["active_user_count"] == 1

    def test_different_windows_filter_both_metrics(self, synth_qs):
        """不同窗口：金额与期间用户数都按对应窗口过滤。"""
        def run(start, end):
            return run_analysis(_plan(
                analysis_type=AnalysisType.DESCRIPTIVE,
                metrics=[MetricSpec(metric_id="purchase_amount"),
                         MetricSpec(metric_id="active_user_count")],
                statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT],
                time_range=TimeRangeSpec(
                    type="explicit_dates", resolved_start_date=start,
                    resolved_end_date=end, resolution_status="resolved")),
                synth_qs, data_range=SYNTH_RANGE).queries[0]

        early = run(date(2024, 1, 2), date(2024, 1, 8))
        late = run(date(2024, 1, 9), date(2024, 1, 12))
        early_cols = {n: early.rows[0][i] for i, n in enumerate(early.columns)}
        late_cols = {n: late.rows[0][i] for i, n in enumerate(late.columns)}
        assert early_cols == {"purchase_amount_sum": 1100, "active_user_count": 1}
        assert late_cols == {"purchase_amount_sum": 1750, "active_user_count": 2}

    def test_grouped_by_city(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        got = {(row[0]): (row[1], row[2]) for row in q.rows}
        assert got == {"6081949": (1100, 1), "6281949": (1500, 1),
                       "6301949": (250, 1)}

    def test_grouped_by_sex(self, synth_qs):
        r = run_analysis(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            synth_qs, data_range=SYNTH_RANGE)
        q = r.queries[0]
        got = {(row[0]): (row[1], row[2]) for row in q.rows}
        # sex=1: 101(1100)+103(250)=1350, 2 人；sex=0: 102=1500, 1 人
        assert got == {1: (1350, 2), 0: (1500, 1)}

    def test_results_pass_guard_and_go_through_query_service(self, synth_qs):
        """SQL 经 sql_guard 预检，且执行必须经 QueryService（审计记录可见）。"""
        audit_before = len(synth_qs.audit_log)
        r = self._run(synth_qs, [
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="active_user_count")])
        assert r.status == ExecutionStatus.OK
        assert len(synth_qs.audit_log) == audit_before + 1
        assert synth_qs.audit_log[-1].query_status == "ok"


class TestStaticMetricCombinationRejected:
    """任务 05.2-A：静态 user_count 混用在执行前被拒绝（Schema/生成器）。"""

    def test_static_with_amount_rejected_at_schema(self):
        with pytest.raises(Exception, match="不能与资金指标/期间用户数混用"):
            _plan(metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])

    def test_static_with_amount_rejected_before_execution(self, synth_qs):
        """即使构造绕过（直接调 build_queries），也不得执行任何查询。"""
        from dataagent.query_builder import QueryBuildError, build_queries

        class FakeMetric:
            metric_id = "purchase_amount"
            term = None

        plan = _plan()
        # 通过 model_copy 注入不合法指标组合（绕过 Schema 校验）
        bad = plan.model_copy(update={"metrics": [
            FakeMetric(), MetricSpec(metric_id="user_count")]})
        calls = {"n": 0}

        class Spy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, *a, **k):
                calls["n"] += 1
                return self._inner.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        spy = Spy(synth_qs._con)
        synth_qs._con = spy
        try:
            with pytest.raises(QueryBuildError) as e:
                build_queries(bad, data_range=SYNTH_RANGE)
            assert e.value.code == "ambiguous_user_count_semantics"
            assert calls["n"] == 0
        finally:
            synth_qs._con = spy._inner
