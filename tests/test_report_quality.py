"""阶段 07.6 回归测试：LLM 摘要完整性、趋势极值口径、复现元数据。

全部使用 Fake LLM 与固定统计结果，不调用真实 API。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataagent import analytics, report_generator as rg
from dataagent.agent import AgentRunResult, AgentStatus
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
    TimeRangeType,
)
from dataagent.analysis_runner import AnalysisExecutionResult, QueryExecution


def _d(s):
    return date.fromisoformat(s)


def _plan():
    return AnalysisPlan(
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


def _exec(columns, rows):
    q = QueryExecution(name="time_trend_report_date", status="ok",
                       columns=columns, rows=rows, row_count=len(rows),
                       window_start="2024-01-02", window_end="2024-01-12")
    return AnalysisExecutionResult(status="ok", plan_summary={}, queries=[q])


def _agent_result(rows, *, model_mode="mock", data_mode="synthetic",
                  narrative_source="deterministic", llm_model=None):
    plan = _plan()
    ex = _exec(["report_date", "purchase_amount_sum", "redeem_amount_sum"], rows)
    an = analytics.analyze_execution_result(ex, plan)
    return AgentRunResult(
        status=AgentStatus.REPORT_GENERATED, question=plan.question,
        model_mode=model_mode, data_mode=data_mode,
        plan=plan.model_dump(mode="json"),
        execution_summary={"status": "ok", "queries": [
            {"name": ex.queries[0].name, "status": "ok",
             "columns": list(ex.queries[0].columns),
             "row_count": ex.queries[0].row_count, "truncated": False,
             "window_label": None, "window_start": "2024-01-02",
             "window_end": "2024-01-12", "execution_ms": 1.0, "error": None}]},
        analysis_summary={"status": an.status.value, "method": an.method,
                          "conclusion_count": len(an.conclusions)},
        chart_paths=[], analysis_result=an,
        narrative_source=narrative_source, llm_model=llm_model)


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = SimpleNamespace(content=content)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]


class FakeLLM:
    def __init__(self, content, finish_reason="stop", model="fake-narrator"):
        self.model = model
        self.content = content
        self.finish_reason = finish_reason
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls += 1
        return FakeResponse(self.content, self.finish_reason)


# ===========================================================================
# 任务 07.6-A：LLM 摘要完整性与确定性回退
# ===========================================================================

class TestNarrativeCompleteness:
    def test_complete_narrative_accepted(self, tmp_path):
        """① 完整的 LLM 摘要被接受。"""
        llm = FakeLLM("申购金额在观察期内合计为正，赎回金额同期亦有记录 [E001]。")
        res = rg.generate_report(_agent_result([(_d("2024-01-02"), 100.0, 50.0),
                                                 (_d("2024-01-03"), 120.0, 60.0)]),
                                 tmp_path, llm_narrator=llm, llm_model="fake-narrator")
        assert res.narrative_source == "llm"
        assert llm.calls == 1
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "叙述摘要" in text

    def test_truncated_narrative_rejected(self, tmp_path):
        """② 以逗号/悬空连接词结束的摘要被拒绝。"""
        for bad in ("此外，申购金额有4个日期、赎回",
                    "申购金额上升，赎回金额下降，"):
            llm = FakeLLM(bad)
            res = rg.generate_report(
                _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                               (_d("2024-01-03"), 120.0, 60.0)]),
                tmp_path, llm_narrator=llm, llm_model="fake-narrator")
            assert res.narrative_source == "llm_rejected_fallback"
            assert any("截断" in w or "未完成" in w for w in res.warnings)

    def test_length_finish_reason_falls_back(self, tmp_path):
        """③ API 返回长度截断标记 → 触发确定性回退。"""
        llm = FakeLLM("申购金额在观察期内合计为正，赎回金额同期亦有记录 [E001]。",
                      finish_reason="length")
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-narrator")
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("长度上限" in w for w in res.warnings)
        assert llm.calls == 1                      # 不无限重试

    def test_fallback_summary_complete_with_evidence(self, tmp_path):
        """④ 回退摘要包含完整句子和必要证据。"""
        llm = FakeLLM("此外，申购金额有4个日期、赎回")
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-narrator")
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        # 确定性结论仍在，且带证据 ID
        assert "[E001]" in text
        assert "核心结论" in text

    def test_fallback_marks_narrative_source(self, tmp_path):
        """⑤ 回退后报告正确标记叙述来源。"""
        llm = FakeLLM("此外，申购金额有4个日期、赎回")
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-narrator")
        assert res.narrative_source == "llm_rejected_fallback"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "llm_rejected_fallback" in text

    def test_invalid_narrative_does_not_change_evidence(self, tmp_path):
        """⑥ 无效摘要不会修改统计证据。"""
        base = _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                              (_d("2024-01-03"), 120.0, 60.0)])
        before = json.dumps(rg.build_report_facts(base)["evidence"],
                            ensure_ascii=False, sort_keys=True)
        llm = FakeLLM("此外，申购金额有4个日期、赎回")
        res = rg.generate_report(base, tmp_path, llm_narrator=llm,
                                 llm_model="fake-narrator")
        after = json.dumps(rg.build_report_facts(base)["evidence"],
                           ensure_ascii=False, sort_keys=True)
        assert before == after
        ev = json.loads(Path(res.evidence_path).read_text(encoding="utf-8"))
        assert ev["evidence"]


# ===========================================================================
# 任务 07.6-B：趋势极值口径
# ===========================================================================

class TestTrendExtremes:
    def _texts(self, rows):
        an = analytics.analyze_execution_result(
            _exec(["report_date", "purchase_amount_sum", "redeem_amount_sum"], rows),
            _plan())
        return " ".join(c.text for c in an.conclusions), an

    def test_largest_drop_is_most_negative(self):
        """⑦ -33.33% 与 -100% 同时存在 → 最大降幅为 -100%。"""
        rows = [(_d("2024-01-02"), 300.0, 0.0), (_d("2024-01-03"), 200.0, 0.0),
                (_d("2024-01-04"), 0.0, 0.0), (_d("2024-01-05"), 100.0, 0.0)]
        texts, _ = self._texts(rows)
        # 300→200 = -33.33%；200→0 = -100%（最负）；0→100 前值为 0 不可计算
        assert "降幅最大" in texts
        drop = [seg for seg in texts.split("。") if "降幅最大" in seg]
        assert drop and "-100.00%" in drop[0]      # 最大降幅取最负变化率
        assert "2024-01-04" in drop[0]
        assert "-33.33%" not in drop[0]             # 不得把较轻的降幅当作最大降幅
        assert "涨幅最大" not in texts              # 无正变化率 → 不虚构涨幅

    def test_extremes_are_separate(self):
        """⑧ 最大涨幅与最大绝对波动分别正确识别。"""
        rows = [(_d("2024-01-02"), 100.0, 0.0), (_d("2024-01-03"), 101.0, 0.0),
                (_d("2024-01-04"), 10.0, 0.0)]
        texts, _ = self._texts(rows)
        assert "涨幅最大" in texts and "+1.00%" in texts
        assert "降幅最大" in texts and "-90.10%" in texts
        assert "绝对波动最大" in texts and "90.10%" in texts

    def test_zero_base_excluded_from_extremes(self):
        """⑨ 前值为零时变化率不参与极值计算。"""
        rows = [(_d("2024-01-02"), 0.0, 0.0), (_d("2024-01-03"), 50.0, 0.0),
                (_d("2024-01-04"), 100.0, 0.0)]
        texts, an = self._texts(rows)
        assert "变化率不可计算" in texts
        # 不可计算项不得被当作最大降幅/涨幅
        assert "降幅最大" not in texts
        assert "涨幅最大（变化率 +100.00%" in texts

    def test_no_negative_no_fabricated_drop(self):
        """⑩ 只有正变化率时不生成虚构的最大降幅。"""
        rows = [(_d("2024-01-02"), 100.0, 0.0), (_d("2024-01-03"), 110.0, 0.0),
                (_d("2024-01-04"), 120.0, 0.0)]
        texts, _ = self._texts(rows)
        assert "涨幅最大" in texts
        assert "降幅最大" not in texts

    def test_no_positive_no_fabricated_rise(self):
        """⑪ 只有负变化率时不生成虚构的最大涨幅。"""
        rows = [(_d("2024-01-02"), 120.0, 0.0), (_d("2024-01-03"), 110.0, 0.0),
                (_d("2024-01-04"), 100.0, 0.0)]
        texts, _ = self._texts(rows)
        assert "降幅最大" in texts
        assert "涨幅最大" not in texts

    def test_extremum_fields_come_from_same_record(self):
        """⑫ 证据 ID、日期、前值和本期值与实际统计记录一致。"""
        rows = [(_d("2024-01-02"), 100.0, 0.0), (_d("2024-01-03"), 90.0, 0.0)]
        an = analytics.analyze_execution_result(
            _exec(["report_date", "purchase_amount_sum", "redeem_amount_sum"], rows),
            _plan())
        series = getattr(an.statistics, "series", None)
        stats = series[0] if series else an.statistics
        change = stats.daily_changes[0]
        drop_text = [c.text for c in an.conclusions if "降幅最大" in c.text][0]
        assert change.date in drop_text
        assert f"{change.previous_value:.2f}" in drop_text
        assert f"{change.current_value:.2f}" in drop_text
        assert f"{change.change_rate:+.2%}" in drop_text


# ===========================================================================
# 任务 07.6-C：复现元数据
# ===========================================================================

class TestReproductionMetadata:
    def test_real_mode_recorded(self, tmp_path):
        """⑬ CLI 真实模型模式与 LLM 叙述模式正确记录。"""
        llm = FakeLLM("申购金额与赎回金额在观察期内均有记录 [E001]。",
                      model="fake-test-narrator-model")
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)],
                          model_mode="real", data_mode="synthetic",
                          narrative_source="llm", llm_model="fake-test-narrator-model"),
            tmp_path, llm_narrator=llm, llm_model="fake-test-narrator-model")
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "有效运行配置" in text
        assert "真实大模型调用" in text
        assert "fake-test-narrator-model" in text

    def test_mock_mode_not_labelled_real(self, tmp_path):
        """⑭ Mock 模式不会被误标为真实模型。"""
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "非真实模型调用" in text
        assert "真实大模型调用" not in text

    def test_synthetic_not_labelled_business_data(self, tmp_path):
        """⑮ 合成数据不会被描述成真实业务数据。"""
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "非真实业务数据" in text
        assert "合成测试数据的实际覆盖范围" in text
        assert "真实业务数据集的配置覆盖范围" in text

    def test_no_secrets_in_report(self, tmp_path):
        """⑯ 报告不包含 API Key 或其他敏感信息。"""
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)],
                          model_mode="real", narrative_source="llm",
                          llm_model="fake-test-narrator-model"),
            tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        ev = Path(res.evidence_path).read_text(encoding="utf-8")
        for blob in (text, ev):
            assert "sk-" not in blob
            assert "Authorization" not in blob
            assert "DATAAGENT_LLM_API_KEY=" not in blob


# ===========================================================================
# 任务 07.6.1-A：摘要校验边界（引号配对 / 闭合括号 / 短句）
# ===========================================================================

class TestNarrativeBoundary:
    @pytest.mark.parametrize("bad,expect", [
        ("申购金额上升，「隔夜利率上行。", "直角引号"),
        ("申购金额上升，『隔夜利率上行。", "双直角引号"),
        ("申购金额上升，“隔夜利率上行。", "弯引号"),
        ("申购金额上升（详见 E001）", "句末标点"),
        ("申购金额上升，", "句子未完成"),
        ("此外，申购金额有4个日期、赎回", "句末标点"),
    ])
    def test_incomplete_narrative_rejected(self, tmp_path, bad, expect):
        """引号未闭合 / 仅以闭合括号结束 / 逗号结尾 / 悬空连接词 → 拒绝并回退。"""
        llm = FakeLLM(bad)
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_source == "llm_rejected_fallback"
        joined = " ".join(res.warnings)
        assert expect in joined
        # 不得把不完整摘要写入最终报告
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert bad not in text
        assert "叙述摘要（受证据约束）" not in text

    def test_complete_paired_quotes_pass(self, tmp_path):
        """完整且正确闭合的引号与句末标点 → 正常通过。"""
        good = "申购金额上升（“隔夜利率上行”），赎回金额回落 [E001]。"
        ok, problems = rg.validate_narrative_completeness(good)
        assert ok, problems
        llm = FakeLLM(good)
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_source == "llm"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert good in text

    def test_short_but_complete_sentence_passes(self, tmp_path):
        """较短但完整的中文句子应通过。"""
        good = "金额上升 [E001]。"
        ok, problems = rg.validate_narrative_completeness(good)
        assert ok, problems
        llm = FakeLLM(good)
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_source == "llm"

    def test_length_finish_reason_still_falls_back(self, tmp_path):
        """保留 finish_reason=length 的回退行为。"""
        llm = FakeLLM("金额上升 [E001]。", finish_reason="length")
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("长度上限" in w for w in res.warnings)

    def test_evidence_checks_still_active(self, tmp_path):
        """证据 ID / 数字白名单 / 因果表述校验保持有效。"""
        llm = FakeLLM("申购金额合计为 999999 元 [E001]，因为活动导致增长。")
        res = rg.generate_report(
            _agent_result([(_d("2024-01-02"), 100.0, 50.0),
                           (_d("2024-01-03"), 120.0, 60.0)]),
            tmp_path, llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_source == "llm_rejected_fallback"
        joined = " ".join(res.warnings)
        assert "不存在的数字" in joined or "因果" in joined


# ===========================================================================
# 任务 07.6.2-B：回退原因分类 / 模型名缺失 / 数据覆盖说明
# ===========================================================================

class NoModelClient:
    """没有 model 属性的假客户端（用于验证“未记录”分支）。"""

    def __init__(self, content="金额上升 [E001]。"):
        self.content = content
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        return FakeResponse(self.content)


class ExplodingLLM:
    """调用即抛异常的假客户端（API 调用失败路径）。"""

    def __init__(self):
        self.model = "fake-test-narrator-model"
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        raise ConnectionError("llm api down")


class TestFallbackReasonAndMetadata:
    def _rows(self):
        return [(_d("2024-01-02"), 100.0, 50.0), (_d("2024-01-03"), 120.0, 60.0)]

    def test_truncation_reason(self, tmp_path):
        """① 模型输出被截断 → reason=truncated，报告给出可读原因。"""
        llm = FakeLLM("金额上升 [E001]。", finish_reason="length")
        res = rg.generate_report(_agent_result(self._rows()), tmp_path,
                                 llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_source == "llm_rejected_fallback"
        assert res.narrative_fallback_reason == "truncated"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "截断" in text
        assert "已回退确定性模板" in text

    def test_incomplete_reason(self, tmp_path):
        """② 摘要完整性校验失败 → reason=incomplete。"""
        llm = FakeLLM("此外，申购金额有4个日期、赎回")
        res = rg.generate_report(_agent_result(self._rows()), tmp_path,
                                 llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_fallback_reason == "incomplete"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        # 报告使用分类描述（不依赖 Markdown 加粗星号位置）
        assert "已回退确定性模板" in text
        assert "完整性" in text and "保留确定性报告" in text

    def test_evidence_reason(self, tmp_path):
        """③ 证据一致性校验失败 → reason=evidence。"""
        llm = FakeLLM("申购金额合计为 999999 元 [E001]，因为活动导致增长。")
        res = rg.generate_report(_agent_result(self._rows()), tmp_path,
                                 llm_narrator=llm, llm_model="fake-test-narrator-model")
        assert res.narrative_fallback_reason == "evidence"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "已回退确定性模板" in text
        assert "一致性" in text and "保留确定性报告" in text
        # 只展示问题类别，不回显被拒数字
        assert "999999" not in text
        assert "问题类别" in text

    def test_api_error_reason(self, tmp_path):
        """④ API 调用失败 → reason=api_error。"""
        res = rg.generate_report(_agent_result(self._rows()), tmp_path,
                                 llm_narrator=ExplodingLLM(),
                                 llm_model="fake-test-narrator-model")
        assert res.narrative_source == "llm_rejected_fallback"
        assert res.narrative_fallback_reason == "api_error"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "API 调用失败" in text

    def test_no_model_reason(self, tmp_path):
        """⑤ 无法确定模型名称 → 记录 no_model，报告显示“未记录”。"""
        res = rg.generate_report(_agent_result(self._rows()), tmp_path,
                                 llm_narrator=NoModelClient(), llm_model=None)
        assert res.narrative_fallback_reason == "no_model"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "未记录" in text

    def test_fallback_reason_codes_are_documented(self):
        """回退原因分类常量与说明一致。"""
        codes = set(rg.NARRATIVE_FALLBACK_REASONS)
        assert codes == {"no_model", "api_error", "truncated", "incomplete",
                         "evidence"}
        for label in rg.NARRATIVE_FALLBACK_REASONS.values():
            assert label and isinstance(label, str)

    def test_data_coverage_three_parts(self, tmp_path):
        """⑥ 第 6 节区分：合成实际覆盖 / 真实配置覆盖 / 超范围处理规则。"""
        res = rg.generate_report(_agent_result(self._rows()), tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "本次合成测试数据的实际覆盖范围" in text
        assert "真实业务数据集的配置覆盖范围" in text
        assert "请求日期超出覆盖范围时的处理规则" in text

    def test_section6_windowed_coverage_distinguishes_windows(self):
        """⑥-fix：含对比窗口（跨年对比）时，第 6 节须分别列主窗口/对比窗口，
        不得把主窗口单独当作“本次合成数据的全部覆盖范围”。"""
        text = "\n".join(rg._synthetic_coverage_lines({
            "effective": {"start": "2014-08-01", "end": "2014-08-31"},
            "effective_comparison": {"start": "2013-08-01", "end": "2013-08-31"},
        }))
        assert "实际覆盖范围（主窗口）" in text
        assert "2014-08-01 至 2014-08-31" in text
        assert "实际覆盖范围（对比窗口）" in text
        assert "2013-08-01 至 2013-08-31" in text
        assert "两个窗口" in text
        assert "非真实业务数据" in text
        # 单窗口（无对比）保持原措辞，且不得出现“对比窗口”
        single = "\n".join(rg._synthetic_coverage_lines(
            {"effective": {"start": "2024-01-02", "end": "2024-01-12"}}))
        assert "本次合成测试数据的实际覆盖范围" in single
        assert "对比窗口" not in single
        assert "非真实业务数据" in single

    def test_no_secret_in_report(self, tmp_path):
        """⑦ 报告不含 API Key / Authorization。"""
        res = rg.generate_report(_agent_result(self._rows()), tmp_path,
                                 llm_narrator=FakeLLM("金额上升 [E001]。"),
                                 llm_model="fake-test-narrator-model")
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "sk-" not in text and "Authorization" not in text
