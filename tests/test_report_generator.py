"""报告生成器测试（阶段七）：结构、证据、模式标识与 LLM 叙述校验。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dataagent import analytics, report_generator as rg
from dataagent.agent import AgentRunResult, AgentStatus
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
)
from dataagent.analysis_runner import AnalysisExecutionResult, QueryExecution


def _d(s):
    return date.fromisoformat(s)


def _plan(**kw):
    base = dict(
        question="申购与赎回趋势", intent="趋势",
        analysis_type=AnalysisType.TIME_TREND,
        metrics=[MetricSpec(metric_id="purchase_amount"),
                 MetricSpec(metric_id="redeem_amount")],
        time_range=TimeRangeSpec(
            type="explicit_dates",
            resolved_start_date=date(2024, 1, 2),
            resolved_end_date=date(2024, 1, 12),
            resolution_status="resolved"),
        group_by=[], statistics=[StatOperation.SUM])
    base.update(kw)
    return AnalysisPlan(**base)


def make_agent_result(tmp_path, *, rows=None, window=None, charts=None,
                      data_mode="synthetic", model_mode="mock",
                      plan_kw=None):
    plan = _plan(**(plan_kw or {}))
    rows = rows if rows is not None else [
        (_d("2024-01-02"), 500.0, 0.0), (_d("2024-01-03"), 300.0, 200.0),
        (_d("2024-01-04"), 200.0, 350.0)]
    q = QueryExecution(name="time_trend_report_date", status="ok",
                       columns=["report_date", "purchase_amount_sum",
                                "redeem_amount_sum"],
                       rows=rows, row_count=len(rows),
                       window_start=(window[0] if window else None),
                       window_end=(window[1] if window else None))
    execution = AnalysisExecutionResult(status="ok", plan_summary={}, queries=[q])
    analysis = analytics.analyze_execution_result(execution, plan)
    analysis.chart_paths = charts or []
    return AgentRunResult(
        status=AgentStatus.REPORT_GENERATED, question=plan.question,
        model_mode=model_mode, data_mode=data_mode,
        plan=plan.model_dump(mode="json"),
        execution_summary={"status": "ok", "queries": [
            {"name": q.name, "status": "ok", "columns": list(q.columns),
             "row_count": q.row_count, "truncated": False, "window_label": None,
             "window_start": q.window_start, "window_end": q.window_end,
             "execution_ms": 1.0, "error": None}]},
        analysis_summary={"status": analysis.status.value,
                          "method": analysis.method,
                          "conclusion_count": len(analysis.conclusions)},
        chart_paths=charts or [], analysis_result=analysis)


class TestReportStructure:
    def test_all_sections_present(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path), tmp_path)
        assert res.status in ("generated", "generated_with_warnings")
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert text.startswith("# 数据分析报告")
        for heading in ["## 2. 用户原始问题", "## 3. 执行状态与数据来源",
                        "## 4. 分析目标与指标定义", "## 5. 实际查询时间范围",
                        "## 6. 数据覆盖范围与截取说明", "## 7. 核心结论",
                        "## 8. 统计结果与计算口径", "## 9. 图表与说明",
                        "## 10. 关键数据证据",
                        "## 11. 异常、缺失值与数据质量警告",
                        "## 12. 可能的解释与待验证假设",
                        "## 13. 结论局限", "## 14. 本次分析的复现信息"]:
            assert heading in text, heading

    def test_mode_labels_explicit(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path), tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "非真实模型调用" in text
        assert "非真实业务数据" in text
        assert "mock" in text and "synthetic" in text

    def test_metric_definitions_table(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path), tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "purchase_amount" in text and "申购金额" in text
        assert "redeem_amount" in text and "赎回金额" in text
        assert "不是商品销售额" in text

    def test_no_charts_says_not_available(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path, charts=[]), tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        section9 = text.split("## 9. 图表与说明")[1].split("## 10.")[0]
        assert "数据不足 / 不适用" in section9

    def test_numbers_and_totals_consistent(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path), tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "1000" in text.replace(",", "")     # 500+300+200
        assert "550" in text.replace(",", "")      # 0+200+350
        assert "2024-01-02 至 2024-01-12" in text


class TestEvidenceFile:
    def test_evidence_json_structure(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path), tmp_path)
        import json
        ev = json.loads(Path(res.evidence_path).read_text(encoding="utf-8"))
        assert ev["metadata"]["model_mode"] == "mock"
        assert ev["metadata"]["data_mode"] == "synthetic"
        assert ev["evidence"], "证据清单不能为空"
        for e in ev["evidence"]:
            for key in ("id", "text", "metric_id", "unit", "query_name",
                        "data_points", "formula", "evidence_strength"):
                assert key in e

    def test_evidence_ids_unique_and_cited(self, tmp_path):
        import re
        res = rg.generate_report(make_agent_result(tmp_path), tmp_path)
        import json
        ev = json.loads(Path(res.evidence_path).read_text(encoding="utf-8"))
        ids = [e["id"] for e in ev["evidence"]]
        assert len(ids) == len(set(ids))
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        cited = set(re.findall(r"\[(E\d{3})\]", text))
        assert cited == set(ids)

    def test_no_user_level_records(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path), tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "user_id=" not in text
        for uid in ("101", "102", "103"):
            assert not re_contains_uid(text, uid)


def re_contains_uid(text: str, uid: str) -> bool:
    import re
    return re.search(rf"user[_-]?id\D{{0,4}}{uid}\b", text) is not None


class TestWindowAndDateSemantics:
    def test_dual_windows_not_merged(self, tmp_path):
        plan = _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                     metrics=[MetricSpec(metric_id="net_inflow")],
                     group_by=["city"],
                     time_range=TimeRangeSpec(
                         type="window_comparison",
                         resolved_start_date=date(2024, 3, 1),
                         resolved_end_date=date(2024, 3, 31),
                         comparison_start_date=date(2024, 1, 1),
                         comparison_end_date=date(2024, 1, 31),
                         resolution_status="resolved"),
                     statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])
        q_main = QueryExecution(name="m", status="ok",
                                columns=["city", "net_inflow"],
                                rows=[("6081949", 80.0)], row_count=1,
                                window_label="main",
                                window_start="2024-03-01", window_end="2024-03-31")
        q_comp = QueryExecution(name="c", status="ok",
                                columns=["city", "net_inflow"],
                                rows=[("6081949", 100.0)], row_count=1,
                                window_label="comparison",
                                window_start="2024-01-01", window_end="2024-01-31")
        execution = AnalysisExecutionResult(status="ok", plan_summary={},
                                            queries=[q_main, q_comp])
        analysis = analytics.analyze_execution_result(execution, plan)
        result = AgentRunResult(
            status=AgentStatus.REPORT_GENERATED, question="窗口对比",
            model_mode="mock", data_mode="synthetic",
            plan=plan.model_dump(mode="json"), analysis_result=analysis,
            chart_paths=[])
        res = rg.generate_report(result, tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        section5 = text.split("## 5. 实际查询时间范围")[1].split("## 6.")[0]
        assert "2024-03-01 至 2024-03-31" in section5
        assert "2024-01-01 至 2024-01-31" in section5
        # 不得出现跨两窗口的连续跨度
        assert "2024-01-01 至 2024-03-31" not in section5

    def test_missing_metadata_marked_unknown(self, tmp_path):
        res = rg.generate_report(make_agent_result(tmp_path, window=None), tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        section5 = text.split("## 5. 实际查询时间范围")[1].split("## 6.")[0]
        assert "无法确认" in section5
        import json
        ev = json.loads(Path(res.evidence_path).read_text(encoding="utf-8"))
        assert ev["time_range"]["effective"] is None
        assert all(e["date_range"] is None for e in ev["evidence"])


class TestNarrativeValidation:
    def _facts(self, tmp_path):
        base = make_agent_result(tmp_path)
        return rg.build_report_facts(base)

    def test_valid_narrative_accepted(self, tmp_path):
        facts = self._facts(tmp_path)
        text = "申购金额合计 1000 元，赎回金额合计 550 元 [E001]。"
        ok, problems = rg.validate_narrative(text, facts)
        assert ok, problems

    def test_missing_citation_rejected(self, tmp_path):
        facts = self._facts(tmp_path)
        ok, problems = rg.validate_narrative("申购金额有所变化。", facts)
        assert not ok
        assert any("未引用" in p for p in problems)

    def test_unknown_evidence_id_rejected(self, tmp_path):
        facts = self._facts(tmp_path)
        ok, problems = rg.validate_narrative("结论如常 [E999]。", facts)
        assert not ok
        assert any("不存在的证据 ID" in p for p in problems)

    def test_new_number_rejected(self, tmp_path):
        facts = self._facts(tmp_path)
        ok, problems = rg.validate_narrative("申购金额合计 123456789 元 [E001]。", facts)
        assert not ok
        assert any("不存在的数字" in p for p in problems)

    def test_sales_wording_rejected(self, tmp_path):
        facts = self._facts(tmp_path)
        ok, problems = rg.validate_narrative("销售额增长明显 [E001]。", facts)
        assert not ok
        assert any("销售额" in p for p in problems)

    def test_causal_wording_rejected(self, tmp_path):
        facts = self._facts(tmp_path)
        ok, problems = rg.validate_narrative(
            "因为营销活动导致申购增加 [E001]。", facts)
        assert not ok
        assert any("因果" in p for p in problems)

    def test_city_name_rejected(self, tmp_path):
        facts = self._facts(tmp_path)
        ok, problems = rg.validate_narrative("北京地区表现突出 [E001]。", facts)
        assert not ok
        assert any("城市名称" in p for p in problems)


class TestNarrativeIntegration:
    def test_llm_success_marks_source(self, tmp_path):
        class Good:
            model = "good"

            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content="申购与赎回在观察期内均有记录 [E001]。"))])

        from types import SimpleNamespace as SN
        base = make_agent_result(tmp_path)
        res = rg.generate_report(base, tmp_path, llm_narrator=Good())
        assert res.narrative_source == "llm"
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "叙述摘要（受证据约束）" in text

    def test_llm_failure_keeps_deterministic(self, tmp_path):
        class Broken:
            model = "broken"

            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                raise RuntimeError("boom")

        base = make_agent_result(tmp_path)
        res = rg.generate_report(base, tmp_path, llm_narrator=Broken())
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("回退确定性报告" in w for w in res.warnings)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "核心结论" in text

    def test_report_write_failure_structured(self, tmp_path, monkeypatch):
        base = make_agent_result(tmp_path)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", boom)
        res = rg.generate_report(base, tmp_path)
        assert res.status == "failed"
        assert "写入失败" in (res.error or "")
        assert res.markdown_path is None


from types import SimpleNamespace  # noqa: E402  (供上面的 Good/Broken 使用)


# ===========================================================================
# 任务 07.1-A/E：llm_model 参数与路径脱敏
# ===========================================================================

class TestLlmModelAndPathSanitizing:
    def test_generate_report_accepts_llm_model(self, tmp_path):
        class WithModel:
            model = "cfg-model"

            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):
                self.used = kw.get("model")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="结论 [E001]。"))])

        client = WithModel()
        base = make_agent_result(tmp_path)
        res = rg.generate_report(base, tmp_path, llm_narrator=client,
                                 llm_model="passed-model")
        assert res.narrative_source == "llm"
        assert client.used == "passed-model"

    def test_no_model_name_does_not_use_placeholder(self, tmp_path):
        class NoModel:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):     # 不应被调用
                raise AssertionError("不应在没有模型名时发起调用")

        base = make_agent_result(tmp_path)
        res = rg.generate_report(base, tmp_path, llm_narrator=NoModel())
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("模型名称" in w for w in res.warnings)

    def test_chart_paths_are_relative(self, tmp_path):
        from dataagent import config
        chart = Path(config.PROJECT_ROOT) / "artifacts" / "generated" / "demo_fake.png"
        base = make_agent_result(tmp_path, charts=[str(chart)])
        res = rg.generate_report(base, tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "artifacts/generated/demo_fake.png" in text
        assert str(config.PROJECT_ROOT) not in text

    def test_no_absolute_paths_in_evidence(self, tmp_path):
        import json as _json
        from dataagent import config
        chart = Path(config.PROJECT_ROOT) / "artifacts" / "generated" / "x.png"
        base = make_agent_result(tmp_path, charts=[str(chart)])
        res = rg.generate_report(base, tmp_path)
        ev = Path(res.evidence_path).read_text(encoding="utf-8")
        assert str(config.PROJECT_ROOT).replace("\\", "\\\\") not in ev
        assert "artifacts/generated/x.png" in ev


# ===========================================================================
# 任务 07.1-A/E：llm_model 参数与路径脱敏
# ===========================================================================

class TestLlmModelAndPathSanitizing:
    def test_generate_report_accepts_llm_model(self, tmp_path):
        class WithModel:
            model = "cfg-model"

            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):
                self.used = kw.get("model")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content="结论 [E001]。"))])

        client = WithModel()
        base = make_agent_result(tmp_path)
        res = rg.generate_report(base, tmp_path, llm_narrator=client,
                                 llm_model="passed-model")
        assert res.narrative_source == "llm"
        assert client.used == "passed-model"

    def test_no_model_name_does_not_use_placeholder(self, tmp_path):
        class NoModel:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):     # 不应被调用
                raise AssertionError("不应在没有模型名时发起调用")

        base = make_agent_result(tmp_path)
        res = rg.generate_report(base, tmp_path, llm_narrator=NoModel())
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("模型名称" in w for w in res.warnings)

    def test_chart_paths_are_relative(self, tmp_path):
        from dataagent import config
        chart = Path(config.PROJECT_ROOT) / "artifacts" / "generated" / "demo_fake.png"
        base = make_agent_result(tmp_path, charts=[str(chart)])
        res = rg.generate_report(base, tmp_path)
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "artifacts/generated/demo_fake.png" in text
        assert str(config.PROJECT_ROOT) not in text

    def test_no_absolute_paths_in_evidence(self, tmp_path):
        import json as _json
        from dataagent import config
        chart = Path(config.PROJECT_ROOT) / "artifacts" / "generated" / "x.png"
        base = make_agent_result(tmp_path, charts=[str(chart)])
        res = rg.generate_report(base, tmp_path)
        ev = Path(res.evidence_path).read_text(encoding="utf-8")
        assert str(config.PROJECT_ROOT).replace("\\", "\\\\") not in ev
        assert "artifacts/generated/x.png" in ev
