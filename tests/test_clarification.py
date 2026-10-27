"""澄清机制测试（第四阶段）。"""

from __future__ import annotations

import pytest

from dataagent import clarification as clar
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
    TimeRangeType,
)


def _plan(**overrides):
    base = dict(
        question="分析最近三个月",
        intent="趋势",
        analysis_type=AnalysisType.TIME_TREND,
        metrics=[MetricSpec(metric_id="purchase_amount")],
        time_range=TimeRangeSpec(type=TimeRangeType.LAST_N_MONTHS, n=3),
        group_by=[],
        statistics=[StatOperation.SUM],
    )
    base.update(overrides)
    return AnalysisPlan(**base)


class TestConceptMismatch:
    @pytest.mark.parametrize("word", ["销售额", "销量", "收入", "利润"])
    def test_concept_hint(self, word):
        hint = clar.detect_concept_mismatch(f"帮我分析{word}")
        assert hint is not None
        assert "没有" in hint or "仅为" in hint or "仅包含" in hint

    def test_no_mismatch_for_fund_flow(self):
        assert clar.detect_concept_mismatch("分析最近三个月申购金额") is None


class TestClarificationBuilding:
    def test_sales_question_triggers_clarification(self):
        r = clar.build_clarification("帮我分析销售额的变化")
        assert r.needed
        assert any("销售额" in q for q in r.questions)
        assert "经确认的分析指标" in r.missing_info

    def test_explicit_question_no_clarification(self):
        p = _plan(time_range=TimeRangeSpec(
            type=TimeRangeType.EXPLICIT_DATES,
            resolved_start_date="2014-06-01", resolved_end_date="2014-08-31",
            resolution_status="resolved"))
        r = clar.build_clarification(p.question, plan=p)
        assert not r.needed
        assert r.confirmed  # 已识别部分有输出

    def test_missing_metric(self):
        """Schema 层保证 metrics 非空；缺失指标经 validation_errors 路径澄清。"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            _plan(metrics=[])
        r = clar.build_clarification(
            "分析一下", validation_errors=["metrics: 至少需要一个指标"])
        assert r.needed
        assert "可校验的分析计划要素" in r.missing_info

    def test_missing_time_range(self):
        p = _plan(time_range=TimeRangeSpec(type=TimeRangeType.LAST_N_MONTHS))
        r = clar.build_clarification("申购趋势", plan=p)
        assert r.needed
        assert "时间范围" in r.missing_info

    def test_ranking_without_dimension(self):
        """GROUP_RANKING 无维度被 Schema 拒绝（构造即失败）。"""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=[])
        # DESCRIPTIVE + 时间完整：不追问维度也不追问时间
        p2 = _plan(
            analysis_type=AnalysisType.DESCRIPTIVE, group_by=[],
            time_range=TimeRangeSpec(
                type=TimeRangeType.EXPLICIT_DATES,
                resolved_start_date="2014-06-01",
                resolved_end_date="2014-08-31",
                resolution_status="resolved"))
        r = clar.build_clarification("做个汇总", plan=p2)
        assert not r.needed

    def test_validation_errors_converted(self):
        r = clar.build_clarification("问题", validation_errors=["metrics: 未知指标: x"])
        assert r.needed
        assert any("未知指标" in q for q in r.questions)

    def test_as_dict(self):
        r = clar.build_clarification("分析销售额")
        d = r.as_dict()
        assert d["needed"] is True
        assert d["questions"]
