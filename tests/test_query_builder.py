"""SQL 生成器测试（阶段五）：确定性编译、参数化、安全校验、负向拒绝。

全部基于合成数据夹具与纯静态生成，不依赖真实用户数据、不需要网络。
"""

from __future__ import annotations

from datetime import date

import pytest

from dataagent import query_builder as qb
from dataagent import sql_guard
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    FilterCondition,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
)

SYNTH_RANGE = (date(2024, 1, 2), date(2024, 1, 12))


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
# 正向：各分析类型生成规格
# ---------------------------------------------------------------------------

class TestTimeTrend:
    def test_single_metric(self):
        b = qb.build_queries(_plan(), data_range=SYNTH_RANGE)
        assert len(b.queries) == 1
        q = b.queries[0]
        assert q.columns == ["report_date", "purchase_amount"]
        assert "SUM(b.total_purchase_amt)" in q.sql
        assert "GROUP BY b.report_date" in q.sql
        assert "ORDER BY b.report_date" in q.sql
        assert q.params == ["2024-01-02", "2024-01-12"]

    def test_two_metrics(self):
        b = qb.build_queries(_plan(metrics=[
            MetricSpec(metric_id="purchase_amount"),
            MetricSpec(metric_id="redeem_amount")]), data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.columns == ["report_date", "purchase_amount_sum", "redeem_amount_sum"]
        assert "SUM(b.total_redeem_amt)" in q.sql

    def test_net_inflow_expression(self):
        b = qb.build_queries(_plan(metrics=[MetricSpec(metric_id="net_inflow")]),
                             data_range=SYNTH_RANGE)
        assert ("SUM(b.total_purchase_amt) - SUM(b.total_redeem_amt)"
                in b.queries[0].sql)

    def test_market_yield_series(self):
        b = qb.build_queries(_plan(metrics=[MetricSpec(metric_id="daily_yield")],
                                   statistics=[StatOperation.AVG]),
                             data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "AVG(y.mfd_daily_yield)" in q.sql
        assert "mfd_day_share_interest" in q.sql
        # 不得对市场收益率求和
        assert "SUM(y.mfd_daily_yield)" not in q.sql
        assert "GROUP BY y.mfd_date" in q.sql

    def test_shibor_uses_term_column_and_view(self):
        b = qb.build_queries(
            _plan(metrics=[MetricSpec(metric_id="shibor_rate", term="1M")],
                  statistics=[StatOperation.AVG]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "v_mfd_bank_shibor_daily_filled" in q.sql
        assert "s.Interest_1_M" in q.sql

    def test_fund_and_yield_date_join(self):
        b = qb.build_queries(_plan(
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="daily_yield")],
            group_by=["report_date"],
            statistics=[StatOperation.SUM, StatOperation.AVG]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "JOIN mfd_day_share_interest y ON b.report_date = y.mfd_date" in q.sql
        assert any("已审核" in n for n in b.notes)


class TestGroupRanking:
    def test_city_ranking_with_top_n(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            statistics=[StatOperation.SUM, StatOperation.TOP_N], top_n=3),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "JOIN user_profile_table p ON b.user_id = p.user_id" in q.sql
        assert "GROUP BY p.city" in q.sql
        assert "ORDER BY purchase_amount DESC" in q.sql
        assert "LIMIT ?" in q.sql
        assert q.params[-1] == 3

    def test_ranking_without_top_n_has_no_limit(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"]),
            data_range=SYNTH_RANGE)
        assert "LIMIT" not in b.queries[0].sql

    def test_sex_period_user_count(self):
        """指定时间窗口的用户数 → active_user_count（期间去重口径）。"""
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "COUNT(DISTINCT b.user_id)" in q.sql
        assert "JOIN user_profile_table p ON b.user_id = p.user_id" in q.sql
        assert "b.report_date BETWEEN ? AND ?" in q.sql

    def test_static_user_count_requires_full_range(self):
        """静态画像总数只允许 full_range；有限窗口必须改用期间口径。"""
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(
                analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
                metrics=[MetricSpec(metric_id="user_count")],
                statistics=[StatOperation.COUNT_DISTINCT]),
                data_range=SYNTH_RANGE)
        assert e.value.code == qb.BuildErrorCode.AMBIGUOUS_USER_COUNT


class TestWindowComparison:
    def test_two_queries_with_labels(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.WINDOW_COMPARISON,
            time_range=TimeRangeSpec(
                type="window_comparison",
                resolved_start_date=date(2024, 1, 8),
                resolved_end_date=date(2024, 1, 12),
                comparison_start_date=date(2024, 1, 2),
                comparison_end_date=date(2024, 1, 7),
                resolution_status="resolved"),
            statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF]),
            data_range=SYNTH_RANGE)
        assert len(b.queries) == 2
        main, comp = b.queries
        assert main.window_label == "main"
        assert comp.window_label == "comparison"
        assert main.params == ["2024-01-08", "2024-01-12"]
        assert comp.params == ["2024-01-02", "2024-01-07"]
        assert "'main' AS window_label" in main.sql
        assert "'comparison' AS window_label" in comp.sql


class TestDescriptiveAndDistribution:
    def test_descriptive_summary(self):
        b = qb.build_queries(_plan(analysis_type=AnalysisType.DESCRIPTIVE),
                             data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "GROUP BY" not in q.sql
        assert q.columns == ["purchase_amount"]

    def test_distribution_by_city(self):
        b = qb.build_queries(_plan(analysis_type=AnalysisType.DISTRIBUTION,
                                   group_by=["city"],
                                   statistics=[StatOperation.COUNT]),
                             data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "COUNT(b.total_purchase_amt)" in q.sql
        assert "GROUP BY p.city" in q.sql


class TestParameterization:
    def test_all_values_are_bound_parameters(self):
        """日期与筛选值只出现在 params，不出现于 SQL 文本。"""
        b = qb.build_queries(_plan(filters=[
            FilterCondition(field="city", operator="in",
                            value=["6081949", "6281949"]),
            FilterCondition(field="report_date", operator="gte",
                            value="2024-01-05")]), data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "6081949" not in q.sql
        assert "2024-01-02" not in q.sql
        assert q.params == ["2024-01-02", "2024-01-12", "6081949", "6281949",
                            "2024-01-05"]

    def test_sql_injection_value_bound_not_spliced(self):
        """注入式值只作为参数，不进入 SQL 语法。"""
        with pytest.raises(Exception):
            # 含引号的值无法通过类别域校验（先拒绝）
            FilterCondition(field="city", operator="eq", value="1' OR '1'='1")
        # 用日期字段验证：注入串被参数化（若能通过 Schema）
        b = qb.build_queries(_plan(filters=[
            FilterCondition(field="report_date", operator="gte",
                            value="2024-01-05")]), data_range=SYNTH_RANGE)
        assert "OR '1'='1" not in b.queries[0].sql

    def test_between_generates_two_placeholders(self):
        b = qb.build_queries(_plan(filters=[
            FilterCondition(field="report_date", operator="between",
                            value=["2024-01-03", "2024-01-06"])]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "BETWEEN ? AND ?" in q.sql
        assert len(q.params) == 4   # 窗口 2 + between 2

    def test_in_generates_matching_placeholders(self):
        b = qb.build_queries(_plan(filters=[
            FilterCondition(field="sex", operator="in", value=[0, 1])]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "p.sex IN (?, ?)" in q.sql
        assert q.params[-2:] == [0, 1]

    def test_eq_binds_category_typed_value(self):
        b = qb.build_queries(_plan(filters=[
            FilterCondition(field="sex", operator="eq", value="1")]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.params[-1] == 1     # sex 按 int 绑定
        assert isinstance(q.params[-1], int)


class TestSqlGuardPasses:
    @pytest.mark.parametrize("factory", [
        lambda: _plan(),
        lambda: _plan(metrics=[MetricSpec(metric_id="net_inflow")]),
        lambda: _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"]),
        lambda: _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
                      metrics=[MetricSpec(metric_id="active_user_count")],
                      statistics=[StatOperation.COUNT_DISTINCT]),
        lambda: _plan(metrics=[MetricSpec(metric_id="daily_yield")],
                      statistics=[StatOperation.AVG]),
        lambda: _plan(metrics=[MetricSpec(metric_id="shibor_rate", term="1W")],
                      statistics=[StatOperation.AVG]),
        lambda: _plan(analysis_type=AnalysisType.DESCRIPTIVE),
        lambda: _plan(analysis_type=AnalysisType.DISTRIBUTION, group_by=["city"],
                      statistics=[StatOperation.COUNT]),
    ])
    def test_generated_sql_passes_guard(self, factory):
        b = qb.build_queries(factory(), data_range=SYNTH_RANGE)
        for q in b.queries:
            v = sql_guard.validate_sql(q.sql)
            assert v.passed, (q.sql, v.reasons)


# ---------------------------------------------------------------------------
# 负向：不生成可执行查询
# ---------------------------------------------------------------------------

class TestNegativePlans:
    def test_clarification_plan_rejected(self):
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(clarification_needed=True,
                                   clarification_questions=["请确认指标"]))
        assert e.value.code == qb.BuildErrorCode.CLARIFICATION_REQUIRED

    def test_unresolved_plan_rejected(self):
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(unresolved=["指标口径未确认"]),
                             data_range=SYNTH_RANGE)
        assert e.value.code == qb.BuildErrorCode.UNRESOLVED_PRESENT

    def test_unresolved_time_rejected(self):
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(time_range=TimeRangeSpec(
                type="last_n_months", n=3)), data_range=SYNTH_RANGE)
        assert e.value.code in (qb.BuildErrorCode.TIME_RANGE_NOT_RESOLVED,
                                qb.BuildErrorCode.PLAN_NOT_EXECUTABLE)

    def test_unknown_metric_rejected_at_schema(self):
        with pytest.raises(Exception):
            MetricSpec(metric_id="sales_amount")

    def test_disabled_balance_metric_rejected_at_schema(self):
        with pytest.raises(Exception):
            MetricSpec(metric_id="balance")

    def test_unknown_shibor_term_rejected_at_schema(self):
        with pytest.raises(Exception):
            MetricSpec(metric_id="shibor_rate", term="3Y")

    def test_market_with_user_count_unsupported(self):
        """市场指标 + 用户计数的混合组合尚未审核 → 生成器拒绝。"""
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(
                metrics=[MetricSpec(metric_id="daily_yield"),
                         MetricSpec(metric_id="active_user_count")],
                group_by=["report_date"],
                statistics=[StatOperation.AVG, StatOperation.COUNT_DISTINCT]),
                data_range=SYNTH_RANGE)
        assert e.value.code in (qb.BuildErrorCode.UNSUPPORTED_PLAN,
                                qb.BuildErrorCode.UNSUPPORTED_DIMENSION)

    def test_date_fully_out_of_range(self):
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(time_range=TimeRangeSpec(
                type="explicit_dates",
                resolved_start_date=date(2030, 1, 1),
                resolved_end_date=date(2030, 1, 31),
                resolution_status="resolved")), data_range=SYNTH_RANGE)
        assert e.value.code == qb.BuildErrorCode.DATA_RANGE_MISMATCH

    def test_date_partially_out_of_range_clipped_with_note(self):
        b = qb.build_queries(_plan(time_range=TimeRangeSpec(
            type="explicit_dates",
            resolved_start_date=date(2023, 12, 1),
            resolved_end_date=date(2024, 1, 10),
            resolution_status="resolved")), data_range=SYNTH_RANGE)
        assert b.queries[0].params[0] == "2024-01-02"
        assert any("已截取" in n for n in b.notes)

    def test_group_ranking_without_dimension(self):
        with pytest.raises(Exception):
            _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=[])

    def test_invalid_filter_value_rejected(self):
        with pytest.raises(Exception):
            FilterCondition(field="city", operator="eq", value="9999999")


# ===========================================================================
# 任务 05.1：分组语义 / 类别筛选 / 用户数口径
# ===========================================================================

class TestTrendWithDimensions:
    """任务 05.1-A：时间趋势必须保留日期维度并叠加用户属性维度。"""

    @pytest.mark.parametrize("dim", ["city", "sex", "constellation"])
    def test_trend_with_user_dimension(self, dim):
        b = qb.build_queries(_plan(group_by=[dim]), data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.columns == ["report_date", dim, "purchase_amount"]
        assert f"p.{dim} AS {dim}" in q.sql
        assert f"GROUP BY b.report_date, p.{dim}" in q.sql
        assert "JOIN user_profile_table p ON b.user_id = p.user_id" in q.sql
        assert f"ORDER BY b.report_date, p.{dim}" in q.sql
        assert q.group_by == ["report_date", dim]

    def test_trend_multi_user_dimensions(self):
        b = qb.build_queries(_plan(group_by=["city", "sex"]),
                             data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.columns == ["report_date", "city", "sex", "purchase_amount"]
        assert "GROUP BY b.report_date, p.city, p.sex" in q.sql

    def test_market_trend_with_mfd_date(self):
        b = qb.build_queries(_plan(metrics=[MetricSpec(metric_id="daily_yield")],
                                   statistics=[StatOperation.AVG]),
                             data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "GROUP BY y.mfd_date" in q.sql
        assert q.columns == ["mfd_date", "daily_yield"]

    def test_unsupported_market_with_user_dimension(self):
        """市场指标 + 用户属性维度：Schema 层即拒绝（维度不兼容）。"""
        with pytest.raises(Exception):
            _plan(metrics=[MetricSpec(metric_id="daily_yield")],
                  statistics=[StatOperation.AVG], group_by=["city"])


class TestMultiDimensionGrouping:
    """任务 05.1-B：多维分组不得只使用首维度。"""

    def test_city_ranking_single(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            statistics=[StatOperation.SUM, StatOperation.TOP_N], top_n=3),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.columns == ["city", "purchase_amount"]
        assert "GROUP BY p.city" in q.sql

    def test_city_and_sex_ranking(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city", "sex"]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.columns == ["city", "sex", "purchase_amount"]
        assert "GROUP BY p.city, p.sex" in q.sql
        assert "ORDER BY purchase_amount DESC" in q.sql

    def test_city_and_constellation_ranking(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING,
            group_by=["city", "constellation"]), data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.columns == ["city", "constellation", "purchase_amount"]
        assert "GROUP BY p.city, p.constellation" in q.sql

    def test_multi_dimension_distribution(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.DISTRIBUTION, group_by=["city", "sex"],
            statistics=[StatOperation.COUNT]), data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert q.columns == ["city", "sex", "purchase_amount"]
        assert "GROUP BY p.city, p.sex" in q.sql

    def test_multi_dimension_window_comparison(self):
        b = qb.build_queries(_plan(
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
            data_range=SYNTH_RANGE)
        assert len(b.queries) == 2
        for q in b.queries:
            assert q.group_by == ["city", "sex"]
            assert "GROUP BY p.city, p.sex" in q.sql


class TestCategoryFilterOperators:
    """任务 05.1-C：类别字段仅允许 eq/ne/in。"""

    @pytest.mark.parametrize("field,value", [
        ("city", "6081949"), ("sex", 1), ("constellation", "白羊座")])
    @pytest.mark.parametrize("op", ["eq", "ne"])
    def test_eq_ne_allowed(self, field, value, op):
        f = FilterCondition(field=field, operator=op, value=value)
        assert f.operator == op

    def test_in_allowed(self):
        f = FilterCondition(field="city", operator="in",
                            value=["6081949", "6281949"])
        assert len(f.value) == 2

    @pytest.mark.parametrize("field", ["city", "sex", "constellation"])
    @pytest.mark.parametrize("op,val", [
        ("gt", "6081949"), ("gte", "6081949"),
        ("lt", "6581949"), ("lte", "6581949"),
        ("between", ["6081949", "6281949"])])
    def test_range_operators_rejected(self, field, op, val):
        with pytest.raises(Exception, match="只允许 eq/ne/in"):
            FilterCondition(field=field, operator=op, value=val)

    def test_generator_rejects_category_range_filter(self):
        """生成器层双保险：绕过 Schema 的类别范围筛选仍被拒绝。"""
        ctx = qb._QueryContext(from_clause="user_balance_table b",
                               time_alias="b", time_field="report_date",
                               needs_date_filter=True)
        with pytest.raises(qb.QueryBuildError) as e:
            qb._filter_sql({"field": "city", "operator": "gt",
                            "value": "6081949"}, ctx, [])
        assert e.value.code == qb.BuildErrorCode.FILTER_UNSUPPORTED

    def test_category_value_domain_still_enforced(self):
        with pytest.raises(Exception):
            FilterCondition(field="city", operator="eq", value="9999999")


class TestUserCountSemantics:
    """任务 05.1-D：静态画像总数 vs 期间去重用户数。"""

    def test_static_total_requires_full_range(self):
        from dataagent.analysis_plan import TimeRangeSpec as TRS
        plan = _plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="user_count")],
            statistics=[StatOperation.COUNT_DISTINCT],
            time_range=TRS(type="full_range",
                           resolved_start_date=date(2024, 1, 2),
                           resolved_end_date=date(2024, 1, 12),
                           resolution_status="resolved"))
        b = qb.build_queries(plan, data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "FROM user_profile_table p" in q.sql
        assert "BETWEEN ? AND ?" not in q.sql
        assert q.params == []
        assert any("不代表任何时间窗口" in n for n in b.notes)

    def test_period_user_count_has_date_filter(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "COUNT(DISTINCT b.user_id)" in q.sql
        assert q.params == ["2024-01-02", "2024-01-12"]

    def test_limited_window_with_static_metric_rejected(self):
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(
                analysis_type=AnalysisType.DESCRIPTIVE,
                metrics=[MetricSpec(metric_id="user_count")],
                statistics=[StatOperation.COUNT_DISTINCT]),
                data_range=SYNTH_RANGE)
        assert e.value.code == qb.BuildErrorCode.AMBIGUOUS_USER_COUNT
        assert any("active_user_count" in d for d in e.value.details)

    def test_static_metric_in_trend_rejected_at_schema(self):
        """静态画像指标不能用于时间序列分析（Schema 层拒绝）。"""
        with pytest.raises(Exception, match="不支持 time_trend"):
            _plan(analysis_type=AnalysisType.TIME_TREND,
                  metrics=[MetricSpec(metric_id="user_count")],
                  statistics=[StatOperation.COUNT_DISTINCT])

    def test_mixing_two_user_count_semantics_rejected(self):
        """静态与期间两种口径混用 → Schema 层即拒绝（任务 05.2-A）。"""
        with pytest.raises(Exception, match="不能与资金指标/期间用户数混用"):
            _plan(metrics=[MetricSpec(metric_id="user_count"),
                           MetricSpec(metric_id="active_user_count")],
                  statistics=[StatOperation.COUNT_DISTINCT])

    def test_no_count_star_substitution(self):
        b = qb.build_queries(_plan(
            metrics=[MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.COUNT_DISTINCT]), data_range=SYNTH_RANGE)
        assert "COUNT(DISTINCT" in b.queries[0].sql
        assert "COUNT(*)" not in b.queries[0].sql


# ===========================================================================
# 任务 05.2：多指标组合语义（active_user_count + 资金指标）
# ===========================================================================

class TestAmountWithPeriodUsers:
    """资金指标 + 期间去重用户数可组合（同表聚合、同一窗口、同一 JOIN）。"""

    @pytest.mark.parametrize("metric_id", ["purchase_amount", "redeem_amount",
                                           "net_inflow"])
    def test_amount_with_active_users_sql(self, metric_id):
        b = qb.build_queries(_plan(
            metrics=[MetricSpec(metric_id=metric_id),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "active_user_count" in q.columns
        assert metric_id in " ".join(q.columns)
        assert "COUNT(DISTINCT b.user_id)" in q.sql
        # 两个指标共用同一窗口过滤（同一组日期参数）
        assert q.params[:2] == ["2024-01-02", "2024-01-12"]
        assert "COUNT(*)" not in q.sql
        # 通过 sql_guard 预检（生成期已保证，这里是显式断言）
        assert sql_guard.validate_sql(q.sql).passed

    def test_grouped_by_city_shares_join(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "JOIN user_profile_table p ON b.user_id = p.user_id" in q.sql
        assert "GROUP BY p.city" in q.sql
        assert "SUM(b.total_purchase_amt)" in q.sql
        assert "COUNT(DISTINCT b.user_id)" in q.sql

    def test_grouped_by_sex_shares_join(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="net_inflow"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT],
            group_by_hint=None) if False else _plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="net_inflow"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "GROUP BY p.sex" in q.sql
        assert "COUNT(DISTINCT b.user_id)" in q.sql

    def test_market_with_active_users_still_rejected(self):
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(
                metrics=[MetricSpec(metric_id="daily_yield"),
                         MetricSpec(metric_id="active_user_count")],
                group_by=["report_date"],
                statistics=[StatOperation.AVG, StatOperation.COUNT_DISTINCT]),
                data_range=SYNTH_RANGE)
        assert e.value.code in (qb.BuildErrorCode.UNSUPPORTED_PLAN,
                                qb.BuildErrorCode.UNSUPPORTED_DIMENSION)

    def test_static_user_count_cannot_mix_with_amount(self):
        """静态画像总数不得与资金指标同查询（Schema 层拒绝，含 full_range）。"""
        with pytest.raises(Exception, match="不能与资金指标/期间用户数混用"):
            _plan(metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])


# ===========================================================================
# 任务 05.2：多指标组合语义（active_user_count + 资金指标）
# ===========================================================================

class TestAmountWithPeriodUsers:
    """资金指标 + 期间去重用户数可组合（同表聚合、同一窗口、同一 JOIN）。"""

    @pytest.mark.parametrize("metric_id", ["purchase_amount", "redeem_amount",
                                           "net_inflow"])
    def test_amount_with_active_users_sql(self, metric_id):
        b = qb.build_queries(_plan(
            metrics=[MetricSpec(metric_id=metric_id),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "active_user_count" in q.columns
        assert metric_id in " ".join(q.columns)
        assert "COUNT(DISTINCT b.user_id)" in q.sql
        # 两个指标共用同一窗口过滤（同一组日期参数）
        assert q.params[:2] == ["2024-01-02", "2024-01-12"]
        assert "COUNT(*)" not in q.sql
        # 通过 sql_guard 预检（生成期已保证，这里是显式断言）
        assert sql_guard.validate_sql(q.sql).passed

    def test_grouped_by_city_shares_join(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
            metrics=[MetricSpec(metric_id="purchase_amount"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "JOIN user_profile_table p ON b.user_id = p.user_id" in q.sql
        assert "GROUP BY p.city" in q.sql
        assert "SUM(b.total_purchase_amt)" in q.sql
        assert "COUNT(DISTINCT b.user_id)" in q.sql

    def test_grouped_by_sex_shares_join(self):
        b = qb.build_queries(_plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="net_inflow"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT],
            group_by_hint=None) if False else _plan(
            analysis_type=AnalysisType.GROUP_RANKING, group_by=["sex"],
            metrics=[MetricSpec(metric_id="net_inflow"),
                     MetricSpec(metric_id="active_user_count")],
            statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT]),
            data_range=SYNTH_RANGE)
        q = b.queries[0]
        assert "GROUP BY p.sex" in q.sql
        assert "COUNT(DISTINCT b.user_id)" in q.sql

    def test_market_with_active_users_still_rejected(self):
        with pytest.raises(qb.QueryBuildError) as e:
            qb.build_queries(_plan(
                metrics=[MetricSpec(metric_id="daily_yield"),
                         MetricSpec(metric_id="active_user_count")],
                group_by=["report_date"],
                statistics=[StatOperation.AVG, StatOperation.COUNT_DISTINCT]),
                data_range=SYNTH_RANGE)
        assert e.value.code in (qb.BuildErrorCode.UNSUPPORTED_PLAN,
                                qb.BuildErrorCode.UNSUPPORTED_DIMENSION)

    def test_static_user_count_cannot_mix_with_amount(self):
        """静态画像总数不得与资金指标同查询（Schema 层拒绝，含 full_range）。"""
        with pytest.raises(Exception, match="不能与资金指标/期间用户数混用"):
            _plan(metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="user_count")],
                  statistics=[StatOperation.SUM, StatOperation.COUNT_DISTINCT])
