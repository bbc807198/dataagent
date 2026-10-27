"""结构化分析计划 Schema 测试（第四阶段，无网络/无模型依赖）。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    FilterCondition,
    MetricSpec,
    ResolutionStatus,
    StatOperation,
    TimeRangeSpec,
    TimeRangeType,
)


def _plan(**overrides):
    base = dict(
        question="最近三个月各城市申购金额排名",
        intent="城市级申购排名",
        analysis_type=AnalysisType.GROUP_RANKING,
        metrics=[MetricSpec(metric_id="purchase_amount")],
        time_range=TimeRangeSpec(type=TimeRangeType.LAST_N_MONTHS, n=3),
        group_by=["city"],
        statistics=[StatOperation.SUM, StatOperation.TOP_N],
    )
    base.update(overrides)
    return AnalysisPlan(**base)


class TestValidPlans:
    def test_minimal_valid_plan(self):
        p = _plan()
        assert p.analysis_type == AnalysisType.GROUP_RANKING
        assert p.metric_ids() == ["purchase_amount"]
        assert p.time_range.n == 3

    def test_time_trend_plan(self):
        p = _plan(
            analysis_type=AnalysisType.TIME_TREND,
            group_by=[],
            statistics=[StatOperation.SUM],
            outputs={"trend_chart": True, "chinese_report": True},
        )
        assert p.analysis_type == AnalysisType.TIME_TREND

    def test_window_comparison_plan(self):
        p = _plan(
            analysis_type=AnalysisType.WINDOW_COMPARISON,
            time_range=TimeRangeSpec(type=TimeRangeType.WINDOW_COMPARISON),
            statistics=[StatOperation.WINDOW_DIFF, StatOperation.CHANGE_RATE],
        )
        assert p.analysis_type == AnalysisType.WINDOW_COMPARISON

    def test_net_inflow_derived_metric(self):
        p = _plan(metrics=[MetricSpec(metric_id="net_inflow")])
        assert p.metric_ids() == ["net_inflow"]

    def test_shibor_metric_requires_term(self):
        with pytest.raises(ValidationError):
            _plan(metrics=[MetricSpec(metric_id="shibor_rate")])

    def test_shibor_with_term_ok(self):
        p = _plan(metrics=[MetricSpec(metric_id="shibor_rate", term="1M")],
                  analysis_type=AnalysisType.TIME_TREND,
                  group_by=[], statistics=[StatOperation.AVG])
        assert p.metrics[0].term == "1M"

    def test_count_distinct_user_count_metric_allowed(self):
        """COUNT_DISTINCT 仅用于用户计数类指标（金额列无去重计数语义）。"""
        p = _plan(
            analysis_type=AnalysisType.DESCRIPTIVE,
            metrics=[MetricSpec(metric_id="active_user_count")],
            group_by=[],
            statistics=[StatOperation.COUNT_DISTINCT],
        )
        assert StatOperation.COUNT_DISTINCT in p.statistics

    def test_count_distinct_on_amount_metric_rejected(self):
        """金额指标的 COUNT_DISTINCT 语义不清，已从允许操作中移除。"""
        with pytest.raises(ValidationError):
            _plan(statistics=[StatOperation.COUNT_DISTINCT])


class TestMetricWhitelist:
    def test_unknown_metric_rejected(self):
        with pytest.raises(ValidationError) as e:
            _plan(metrics=[MetricSpec(metric_id="sales_amount")])
        assert "未知指标" in str(e.value)

    def test_disabled_balance_metric_rejected(self):
        """tBalance/yBalance 语义未确认，指标未启用。"""
        with pytest.raises(ValidationError) as e:
            _plan(metrics=[MetricSpec(metric_id="balance")])
        assert "未启用" in str(e.value)

    def test_operation_not_applicable_to_metric_rejected(self):
        # SUM 不适用于收益率类指标（收益率是原值序列）→ 不被任何选定指标支持
        with pytest.raises(ValidationError) as e:
            _plan(metrics=[MetricSpec(metric_id="daily_yield")],
                  statistics=[StatOperation.SUM])
        assert "不被任何选定指标支持" in str(e.value)

    def test_dimension_not_allowed_for_metric_rejected(self):
        # 收益率指标只支持日期维度
        with pytest.raises(ValidationError) as e:
            _plan(metrics=[MetricSpec(metric_id="daily_yield")],
                  group_by=["city"], statistics=[StatOperation.AVG])
        assert "不支持维度" in str(e.value)


class TestDimensionAndFilterRules:
    def test_user_id_dimension_rejected(self):
        with pytest.raises(ValidationError) as e:
            _plan(group_by=["user_id"])
        assert "user_id" in str(e.value)

    def test_unknown_dimension_rejected(self):
        with pytest.raises(ValidationError) as e:
            _plan(group_by=["province"])
        assert "未知分组维度" in str(e.value)

    def test_user_id_filter_rejected(self):
        from dataagent.analysis_plan import FilterCondition
        with pytest.raises(ValidationError):
            FilterCondition(field="user_id", operator="eq", value=101)

    def test_unknown_filter_field_rejected(self):
        from dataagent.analysis_plan import FilterCondition
        with pytest.raises(ValidationError):
            FilterCondition(field="sales_amount", operator="gt", value=100)

    def test_unknown_operator_rejected(self):
        from dataagent.analysis_plan import FilterCondition
        with pytest.raises(ValidationError):
            FilterCondition(field="report_date", operator="like", value="x")

    def test_legal_filter_ok(self):
        from dataagent.analysis_plan import FilterCondition
        f = FilterCondition(field="report_date", operator="between",
                            value=["2014-06-01", "2014-08-31"])
        assert f.operator == "between"

    def test_group_ranking_requires_dimension(self):
        with pytest.raises(ValidationError):
            _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=[])

    def test_window_comparison_requires_matching_time_type(self):
        with pytest.raises(ValidationError):
            _plan(analysis_type=AnalysisType.WINDOW_COMPARISON)


class TestNoSqlField:
    def test_sql_field_forbidden(self):
        """计划中不得包含 SQL 字符串（extra=forbid + 解析器显式拒绝）。"""
        with pytest.raises(ValidationError):
            AnalysisPlan(
                question="x", intent="x",
                analysis_type=AnalysisType.DESCRIPTIVE,
                metrics=[MetricSpec(metric_id="purchase_amount")],
                time_range=TimeRangeSpec(type=TimeRangeType.FULL_RANGE),
                sql="SELECT * FROM user_balance_table",
            )

    def test_unknown_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            _plan(invented_field="oops")


class TestTimeRangeSpec:
    def test_anchor_must_be_data_max(self):
        with pytest.raises(ValidationError) as e:
            TimeRangeSpec(type=TimeRangeType.LAST_N_DAYS, n=7, anchor="today")
        assert "锚点" in str(e.value)

    def test_n_must_be_positive(self):
        with pytest.raises(ValidationError):
            TimeRangeSpec(type=TimeRangeType.LAST_N_DAYS, n=0)

    def test_pending_cannot_have_dates(self):
        with pytest.raises(ValidationError):
            TimeRangeSpec(type=TimeRangeType.LAST_N_DAYS, n=7,
                          resolved_start_date=date(2014, 8, 1),
                          resolved_end_date=date(2014, 8, 31))

    def test_resolved_requires_dates(self):
        with pytest.raises(ValidationError):
            TimeRangeSpec(type=TimeRangeType.LAST_N_DAYS, n=7,
                          resolution_status=ResolutionStatus.RESOLVED)

    def test_resolved_ok(self):
        s = TimeRangeSpec(type=TimeRangeType.EXPLICIT_DATES,
                          resolved_start_date=date(2014, 6, 1),
                          resolved_end_date=date(2014, 8, 31),
                          resolution_status=ResolutionStatus.RESOLVED)
        assert s.is_resolved


# ===========================================================================
# 任务 04.1：unresolved 可执行性 / SHIBOR 期限 / 筛选参数形状
# ===========================================================================

class TestUnresolvedBlocksExecution:
    """任务 04.1-A：阻塞性未解决事项必须阻止可执行状态。"""

    def _resolved_plan(self, **overrides):
        base = dict(
            question="分析销售额的变化",
            intent="销售额分析",
            analysis_type=AnalysisType.TIME_TREND,
            metrics=[MetricSpec(metric_id="purchase_amount")],
            time_range=TimeRangeSpec(
                type=TimeRangeType.EXPLICIT_DATES,
                resolved_start_date=date(2014, 6, 1),
                resolved_end_date=date(2014, 8, 31),
                resolution_status=ResolutionStatus.RESOLVED),
            group_by=[],
            statistics=[StatOperation.SUM],
        )
        base.update(overrides)
        return AnalysisPlan(**base)

    def test_blocking_unresolved_makes_plan_not_ready(self):
        """有指标、有日期、有统计操作，但 unresolved 指出指标不匹配 → 不可执行。"""
        p = self._resolved_plan(unresolved=["销售额指标与现有数据不匹配，未确认"])
        assert p.metrics and p.time_range.is_resolved and p.statistics
        assert p.blocking_unresolved()
        assert not p.is_executable_ready()

    def test_fully_resolved_plan_is_ready(self):
        p = self._resolved_plan()
        assert p.unresolved == []
        assert p.is_executable_ready()

    def test_time_unresolved_blocks(self):
        p = self._resolved_plan(unresolved=["时间范围缺少起止日期"])
        assert not p.is_executable_ready()

    def test_non_blocking_note_not_blocking(self):
        """一般说明性内容应放 notes（永不被检查），不阻止执行。"""
        p = self._resolved_plan(notes=["口径说明：净流入为派生指标"])
        assert p.notes
        assert p.blocking_unresolved() == []
        assert p.is_executable_ready()

    def test_clarification_state_not_ready(self):
        p = self._resolved_plan(clarification_needed=True,
                                clarification_questions=["请确认指标"])
        assert not p.is_executable_ready()

    def test_unresolved_survives_serialization(self):
        """未解决事项不能被序列化后静默丢失。"""
        p = self._resolved_plan(unresolved=["销售额指标不匹配"])
        dumped = p.model_dump(mode="json")
        assert "销售额指标不匹配" in dumped["unresolved"]
        restored = AnalysisPlan(**dumped)
        assert restored.blocking_unresolved() == ["销售额指标不匹配"]
        assert not restored.is_executable_ready()

    def test_any_unresolved_blocks(self):
        """unresolved 语义为未解决事项：非空即阻塞（无法判断影响时默认拒绝）。"""
        p = self._resolved_plan(unresolved=["分析目标不明确：用户未说明关注哪个指标"])
        assert p.blocking_unresolved()
        assert not p.is_executable_ready()


class TestShiborTermValidation:
    """任务 04.1-B：SHIBOR 期限必须在 Schema 阶段校验。"""

    @pytest.mark.parametrize("term", ["O_N", "1W", "2W", "1M", "3M", "6M", "9M", "1Y"])
    def test_supported_terms(self, term):
        m = MetricSpec(metric_id="shibor_rate", term=term)
        assert m.term == term

    def test_missing_term_rejected(self):
        with pytest.raises(ValidationError) as e:
            MetricSpec(metric_id="shibor_rate")
        assert "term" in str(e.value)

    @pytest.mark.parametrize("term", ["3Y", "5Y", "1D", "overnight", ""])
    def test_unknown_terms_rejected(self, term):
        with pytest.raises(ValidationError) as e:
            MetricSpec(metric_id="shibor_rate", term=term)
        assert "期限" in str(e.value)

    def test_non_shibor_term_rejected(self):
        with pytest.raises(ValidationError) as e:
            MetricSpec(metric_id="purchase_amount", term="1M")
        assert "term" in str(e.value)


class TestFilterValueShapes:
    """任务 04.1-C：筛选操作符与参数值形状校验。"""

    def test_legal_date_between(self):
        f = FilterCondition(field="report_date", operator="between",
                            value=["2014-06-01", "2014-08-31"])
        assert f.value == ["2014-06-01", "2014-08-31"]

    def test_between_single_bound_rejected(self):
        with pytest.raises(ValidationError, match="恰好两个边界值"):
            FilterCondition(field="report_date", operator="between",
                            value=["2014-06-01"])

    def test_between_three_bounds_rejected(self):
        with pytest.raises(ValidationError, match="恰好两个边界值"):
            FilterCondition(field="report_date", operator="between",
                            value=["2014-06-01", "2014-07-01", "2014-08-31"])

    def test_empty_in_list_rejected(self):
        with pytest.raises(ValidationError, match="非空列表"):
            FilterCondition(field="city", operator="in", value=[])

    def test_legal_in_list(self):
        f = FilterCondition(field="city", operator="in",
                            value=["6081949", "6281949"])
        assert len(f.value) == 2

    @pytest.mark.parametrize("op", ["eq", "ne", "gt", "gte", "lt", "lte"])
    def test_comparison_operators_reject_lists(self, op):
        with pytest.raises(ValidationError, match="不接收列表参数"):
            FilterCondition(field="report_date", operator=op, value=["2014-06-01"])

    def test_invalid_date_rejected(self):
        with pytest.raises(ValidationError, match="YYYY-MM-DD"):
            FilterCondition(field="report_date", operator="eq", value="2014/06/01")

    def test_compact_date_rejected(self):
        """紧凑格式 20140601 会被 date.fromisoformat 接受，必须显式拒绝。"""
        with pytest.raises(ValidationError, match="YYYY-MM-DD"):
            FilterCondition(field="report_date", operator="eq", value="20140601")

    def test_impossible_calendar_date_rejected(self):
        with pytest.raises(ValidationError, match="无法解析"):
            FilterCondition(field="report_date", operator="eq", value="2014-02-30")

    def test_non_iso_date_rejected(self):
        with pytest.raises(ValidationError, match="ISO 日期字符串"):
            FilterCondition(field="report_date", operator="eq", value=20140601)

    def test_bool_value_rejected(self):
        """bool 是 int 子类，不得静默转为 1/0。"""
        with pytest.raises(ValidationError, match="布尔"):
            FilterCondition(field="city", operator="eq", value=True)

    def test_bool_in_list_rejected(self):
        with pytest.raises(ValidationError, match="布尔"):
            FilterCondition(field="city", operator="in", value=[True])

    def test_category_integer_allowed(self):
        f = FilterCondition(field="city", operator="eq", value=6081949)
        assert f.value == 6081949

    def test_comparison_none_value_rejected(self):
        with pytest.raises(ValidationError):
            FilterCondition(field="report_date", operator="gt", value=None)

    def test_unknown_operator_still_rejected(self):
        with pytest.raises(ValidationError):
            FilterCondition(field="report_date", operator="like", value="x")

    def test_parametrized_design_preserved(self):
        """筛选保持参数化设计：值是数据，不拼接 SQL。"""
        f = FilterCondition(field="report_date", operator="between",
                            value=["2014-06-01", "2014-08-31"])
        assert isinstance(f.value, list)
        assert all(isinstance(v, str) for v in f.value)
