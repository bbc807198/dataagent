"""Agent 编排端到端测试（阶段七）。

全部使用合成数据 + Mock 解析器，不依赖真实用户记录与网络。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataagent import agent as agent_mod
from dataagent import config, db, ingestion, shibor
from dataagent.agent import AgentConfig, AgentStatus, run_agent
from dataagent.analysis_runner import ExecutionStatus
from dataagent.query_service import QueryService

SYNTH_DIR = Path(__file__).parent / "fixtures" / "synthetic"
DATA_RANGE = (date(2024, 1, 2), date(2024, 1, 12))

PLANS = {
    "trend": {
        "intent": "按日展示申购与赎回趋势", "analysis_type": "time_trend",
        "metrics": [{"metric_id": "purchase_amount"},
                    {"metric_id": "redeem_amount"}],
        "time_range": {"type": "explicit_dates",
                       "resolved_start_date": "2024-01-02",
                       "resolved_end_date": "2024-01-12",
                       "resolution_status": "resolved"},
        "group_by": [], "filters": [], "statistics": ["SUM"],
        "outputs": {"summary_table": True, "trend_chart": True},
        "unresolved": [], "notes": [],
    },
    "ranking": {
        "intent": "城市净资金流入排名", "analysis_type": "group_ranking",
        "metrics": [{"metric_id": "net_inflow"}],
        "time_range": {"type": "explicit_dates",
                       "resolved_start_date": "2024-01-02",
                       "resolved_end_date": "2024-01-12",
                       "resolution_status": "resolved"},
        "group_by": ["city"], "filters": [],
        "statistics": ["SUM", "TOP_N"], "top_n": 3,
        "outputs": {"summary_table": True, "ranking_chart": True},
        "unresolved": [], "notes": [],
    },
    "window": {
        "intent": "城市净流入窗口对比", "analysis_type": "window_comparison",
        "metrics": [{"metric_id": "net_inflow"}],
        "time_range": {"type": "window_comparison",
                       "resolved_start_date": "2024-01-08",
                       "resolved_end_date": "2024-01-12",
                       "comparison_start_date": "2024-01-02",
                       "comparison_end_date": "2024-01-07",
                       "resolution_status": "resolved"},
        "group_by": ["city"], "filters": [],
        "statistics": ["SUM", "WINDOW_DIFF"],
        "outputs": {"summary_table": True, "comparison_chart": True},
        "unresolved": [], "notes": [],
    },
    "absolute": {
        "intent": "绝对日期范围趋势", "analysis_type": "time_trend",
        "metrics": [{"metric_id": "purchase_amount"},
                    {"metric_id": "redeem_amount"}],
        "time_range": {"type": "explicit_dates",
                       "resolved_start_date": "2024-01-02",
                       "resolved_end_date": "2024-01-12",
                       "resolution_status": "resolved",
                       "anchor": "max_available_data_date"},
        "group_by": [], "filters": [], "statistics": ["SUM"],
        "outputs": {"summary_table": True, "trend_chart": True},
        "unresolved": [], "notes": [],
    },
    "absolute": {
        "intent": "绝对日期范围趋势", "analysis_type": "time_trend",
        "metrics": [{"metric_id": "purchase_amount"},
                    {"metric_id": "redeem_amount"}],
        "time_range": {"type": "explicit_dates",
                       "resolved_start_date": "2024-01-02",
                       "resolved_end_date": "2024-01-12",
                       "resolution_status": "resolved",
                       "anchor": "max_available_data_date"},
        "group_by": [], "filters": [], "statistics": ["SUM"],
        "outputs": {"summary_table": True, "trend_chart": True},
        "unresolved": [], "notes": [],
    },
    "sales": {
        "intent": "销售额分析", "analysis_type": "time_trend",
        "metrics": [{"metric_id": "purchase_amount"}],
        "time_range": {"type": "explicit_dates",
                       "resolved_start_date": "2024-01-02",
                       "resolved_end_date": "2024-01-12",
                       "resolution_status": "resolved"},
        "group_by": [], "filters": [], "statistics": ["SUM"],
        "outputs": {"summary_table": True},
        "unresolved": ["没有经确认的商品销售额指标"], "notes": [],
    },
    "vague": {
        "intent": "分析一下", "analysis_type": "time_trend",
        "metrics": [{"metric_id": "purchase_amount"}],
        "time_range": {"type": "last_n_months"},
        "group_by": [], "filters": [], "statistics": ["SUM"],
        "outputs": {"summary_table": True}, "unresolved": [], "notes": [],
    },
}


class MockCompletions:
    def __init__(self, key: str):
        self.key = key

    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=json.dumps(PLANS[self.key], ensure_ascii=False)))])


def mock_client(key: str):
    return SimpleNamespace(chat=SimpleNamespace(completions=MockCompletions(key)))


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory) -> Path:
    db_path = tmp_path_factory.mktemp("stage7") / "s7_agent.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
    assert all(r.ok for r in reports), [r.error for r in reports if r.error]
    shibor.create_fill_view(con)
    con.close()
    return db_path


@pytest.fixture()
def qs(synth_db):
    service = QueryService(synth_db)
    yield service
    service.close()


def cfg_for(key: str, out_dir: Path, **kw) -> AgentConfig:
    kw.setdefault("model_mode", "mock")
    kw.setdefault("data_mode", "synthetic")
    kw.setdefault("llm_client", mock_client(key))
    return AgentConfig(db_path=kw.pop("db_path", synth_db_path_holder[0]),
                       data_range=DATA_RANGE, output_dir=out_dir, **kw)


synth_db_path_holder: list[Path] = []


@pytest.fixture(autouse=True)
def _bind_db(synth_db):
    synth_db_path_holder.clear()
    synth_db_path_holder.append(synth_db)


# ---------------------------------------------------------------------------
# 1-3：完整链路 / 澄清 / 不支持指标
# ---------------------------------------------------------------------------

class TestHappyPathAndClarification:
    def test_full_pipeline_generates_report(self, qs, tmp_path):
        r = run_agent("帮我分析最近申购和赎回金额的趋势。",
                      cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED
        assert r.report_path and Path(r.report_path).exists()
        assert r.evidence_path and Path(r.evidence_path).exists()
        assert r.chart_paths and all(Path(p).exists() for p in r.chart_paths)
        assert r.model_mode == "mock" and r.data_mode == "synthetic"
        assert r.analysis_summary["status"] == "ok"

    def test_clarification_does_not_execute_sql(self, qs, tmp_path, monkeypatch):
        calls = {"n": 0}
        real_execute = QueryService.execute

        def spy(self, *a, **k):
            calls["n"] += 1
            return real_execute(self, *a, **k)

        monkeypatch.setattr(QueryService, "execute", spy)
        r = run_agent("分析一下。", cfg_for("vague", tmp_path), query_service=qs)
        assert r.status == AgentStatus.NEEDS_CLARIFICATION
        assert r.clarification_questions
        assert r.report_path is None
        assert calls["n"] == 0

    def test_sales_not_mapped_to_purchase(self, qs, tmp_path):
        r = run_agent("帮我分析销售额的变化。", cfg_for("sales", tmp_path),
                      query_service=qs)
        assert r.status == AgentStatus.UNSUPPORTED_METRIC
        assert r.error_code == "unsupported_metric"
        assert "销售额" in (r.error or "")
        assert r.report_path is None
        assert r.execution_summary is None


# ---------------------------------------------------------------------------
# 4-7：失败/超时/截断/空数据状态传播
# ---------------------------------------------------------------------------

class TestFailurePropagation:
    def test_sql_rejected_no_report(self, qs, tmp_path, monkeypatch):
        from dataagent.query_builder import GeneratedQuery, QueryBundle

        def tampered(plan, data_range=None):
            return QueryBundle(question=plan.question, analysis_type="time_trend",
                               queries=[GeneratedQuery(
                                   name="bad", sql="DROP TABLE user_balance_table",
                                   params=[], columns=[], purpose="x")])

        monkeypatch.setattr(agent_mod, "run_analysis",
                            lambda plan, svc, data_range=None: __import__(
                                "dataagent.analysis_runner", fromlist=["x"]
                            ).run_analysis(plan, svc, data_range=data_range))
        # 直接构造被拒绝的执行结果
        from dataagent.analysis_runner import AnalysisExecutionResult, QueryExecution
        rejected = AnalysisExecutionResult(
            status=ExecutionStatus.SQL_REJECTED, plan_summary={},
            queries=[QueryExecution(name="bad", status="rejected",
                                    error="安全校验拒绝")],
            error="安全校验拒绝")
        monkeypatch.setattr(agent_mod, "run_analysis",
                            lambda *a, **k: rejected)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.SQL_REJECTED
        assert r.report_path is None

    def test_timeout_propagated(self, qs, tmp_path, monkeypatch):
        from dataagent.analysis_runner import AnalysisExecutionResult
        timeout = AnalysisExecutionResult(
            status=ExecutionStatus.TIMEOUT, plan_summary={}, queries=[],
            error="查询超时")
        monkeypatch.setattr(agent_mod, "run_analysis", lambda *a, **k: timeout)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.TIMEOUT
        assert r.report_path is None

    def test_execution_failed_propagated(self, qs, tmp_path, monkeypatch):
        from dataagent.analysis_runner import AnalysisExecutionResult
        failed = AnalysisExecutionResult(
            status=ExecutionStatus.EXECUTION_FAILED, plan_summary={}, queries=[],
            error="DuckDB 错误")
        monkeypatch.setattr(agent_mod, "run_analysis", lambda *a, **k: failed)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.report_path is None

    def test_truncated_no_complete_report(self, qs, tmp_path, monkeypatch):
        from dataagent.analysis_runner import (AnalysisExecutionResult,
                                               QueryExecution)
        truncated = AnalysisExecutionResult(
            status=ExecutionStatus.OK, plan_summary={},
            queries=[QueryExecution(name="q", status="ok",
                                    columns=["report_date", "purchase_amount_sum",
                                             "redeem_amount_sum"],
                                    rows=[(date(2024, 1, 2), 100.0, 50.0)],
                                    row_count=1, truncated=True)])
        monkeypatch.setattr(agent_mod, "run_analysis", lambda *a, **k: truncated)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.TRUNCATED
        assert r.report_path is None
        assert "截断" in (r.error or "")

    def test_empty_result_status(self, qs, tmp_path, monkeypatch):
        from dataagent.analysis_runner import (AnalysisExecutionResult,
                                               QueryExecution)
        empty = AnalysisExecutionResult(
            status=ExecutionStatus.OK, plan_summary={},
            queries=[QueryExecution(name="q", status="ok",
                                    columns=["report_date", "purchase_amount_sum",
                                             "redeem_amount_sum"],
                                    rows=[], row_count=0)])
        monkeypatch.setattr(agent_mod, "run_analysis", lambda *a, **k: empty)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.EMPTY_RESULT
        assert r.report_path is None

    def test_insufficient_data_status(self, qs, tmp_path, monkeypatch):
        from dataagent.analysis_runner import (AnalysisExecutionResult,
                                               QueryExecution)
        one_point = AnalysisExecutionResult(
            status=ExecutionStatus.OK, plan_summary={},
            queries=[QueryExecution(name="q", status="ok",
                                    columns=["report_date", "purchase_amount_sum",
                                             "redeem_amount_sum"],
                                    rows=[(date(2024, 1, 2), 100.0, 50.0)],
                                    row_count=1)])
        monkeypatch.setattr(agent_mod, "run_analysis", lambda *a, **k: one_point)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.INSUFFICIENT_DATA
        assert r.report_path is None


# ---------------------------------------------------------------------------
# 8-9：多指标与双窗口
# ---------------------------------------------------------------------------

class TestReportContent:
    def test_multi_metric_all_preserved(self, qs, tmp_path):
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "purchase_amount" in text and "redeem_amount" in text
        ev = json.loads(Path(r.evidence_path).read_text(encoding="utf-8"))
        metric_ids = {e["metric_id"] for e in ev["evidence"]}
        assert metric_ids == {"purchase_amount", "redeem_amount"}

    def test_window_report_has_two_windows(self, qs, tmp_path):
        r = run_agent("城市净流入窗口对比", cfg_for("window", tmp_path),
                      query_service=qs)
        assert r.status in (AgentStatus.REPORT_GENERATED,
                            AgentStatus.REPORT_GENERATED_WITH_WARNINGS)
        text = Path(r.report_path).read_text(encoding="utf-8")
        # 两窗口分别标注（不合并连续跨度）
        assert "2024-01-08 至 2024-01-12" in text
        assert "2024-01-02 至 2024-01-07" in text
        assert "2024-01-02 至 2024-01-12" not in text.split("## 5")[1].split("## 6")[0]

    def test_evidence_ids_traceable(self, qs, tmp_path):
        import re
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        text = Path(r.report_path).read_text(encoding="utf-8")
        ev = json.loads(Path(r.evidence_path).read_text(encoding="utf-8"))
        cited = set(re.findall(r"\[(E\d{3})\]", text))
        ids = {e["id"] for e in ev["evidence"]}
        assert cited and cited <= ids

    def test_numbers_match_structured_result(self, qs, tmp_path):
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        ev = json.loads(Path(r.evidence_path).read_text(encoding="utf-8"))
        stats = ev["statistics"]
        purchase = next(s for s in stats["series"]
                        if s["metric_id"] == "purchase_amount")
        assert purchase["total"] == 2850.0
        # 报告中应出现该合计值（含千分位格式）
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "2850" in text.replace(",", "")

    def test_chart_paths_exist(self, qs, tmp_path):
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.chart_paths
        for p in r.chart_paths:
            assert Path(p).exists() and Path(p).stat().st_size > 1000

    def test_no_user_level_records_in_report(self, qs, tmp_path):
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        text = Path(r.report_path).read_text(encoding="utf-8")
        for uid in ("101", "102", "103", "999"):
            assert f"user_id={uid}" not in text
        assert "user_id" not in text.replace("user_id 不是维度", "")


# ---------------------------------------------------------------------------
# 10-11：窗口契约与元数据缺失
# ---------------------------------------------------------------------------

class TestWindowContract:
    def test_inconsistent_columns_rejected(self, qs, tmp_path, monkeypatch):
        from dataagent.analysis_runner import (AnalysisExecutionResult,
                                               QueryExecution)
        bad = AnalysisExecutionResult(
            status=ExecutionStatus.OK, plan_summary={},
            queries=[
                QueryExecution(name="m", status="ok",
                               columns=["city", "net_inflow"],
                               rows=[("6081949", 80.0)], row_count=1,
                               window_label="main",
                               window_start="2024-01-08", window_end="2024-01-12"),
                QueryExecution(name="c", status="ok",
                               columns=["city", "net_inflow", "extra_col"],
                               rows=[("6081949", 100.0, 1.0)], row_count=1,
                               window_label="comparison",
                               window_start="2024-01-02", window_end="2024-01-07"),
            ])
        monkeypatch.setattr(agent_mod, "run_analysis", lambda *a, **k: bad)
        r = run_agent("窗口", cfg_for("window", tmp_path), query_service=qs)
        assert r.status == AgentStatus.PLAN_REJECTED
        assert "列结构不一致" in (r.analysis_summary or {}).get("status_reason", "")
        assert r.report_path is None

    def test_missing_window_metadata_not_fabricated(self, qs, tmp_path, monkeypatch):
        from dataagent.analysis_runner import (AnalysisExecutionResult,
                                               QueryExecution)
        no_meta = AnalysisExecutionResult(
            status=ExecutionStatus.OK, plan_summary={},
            queries=[QueryExecution(name="q", status="ok",
                                    columns=["report_date", "purchase_amount_sum",
                                             "redeem_amount_sum"],
                                    rows=[(date(2024, 1, 2), 100.0, 50.0),
                                          (date(2024, 1, 3), 120.0, 60.0)],
                                    row_count=2)])
        monkeypatch.setattr(agent_mod, "run_analysis", lambda *a, **k: no_meta)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status in (AgentStatus.REPORT_GENERATED,
                            AgentStatus.REPORT_GENERATED_WITH_WARNINGS)
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "无法确认" in text
        ev = json.loads(Path(r.evidence_path).read_text(encoding="utf-8"))
        assert ev["time_range"]["effective"] is None
        for e in ev["evidence"]:
            assert e["date_range"] is None


# ---------------------------------------------------------------------------
# 12-17：图表失败 / LLM 叙述
# ---------------------------------------------------------------------------

class TestChartAndNarrative:
    def test_chart_failure_reports_warning(self, qs, tmp_path, monkeypatch):
        def boom(result, out_dir=None):
            from dataagent.visualization import ChartResult
            return [ChartResult(kind="trend", path=None, status="failed",
                                error="模拟绘图失败")]

        monkeypatch.setattr(agent_mod.visualization, "generate_charts", boom)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED_WITH_WARNINGS
        assert any("图表生成失败" in w for w in r.warnings)
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "数据不足 / 不适用" in text        # 第 9 节如实说明无图表

    def test_llm_narrative_success(self, qs, tmp_path):
        class Narrative:
            model = "mock-narrator"

            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                text = "申购金额在观察期内合计为正，赎回金额同期亦有记录 [E001]。"
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=text))])

        cfg = cfg_for("trend", tmp_path)
        cfg.llm_client = mock_client("trend")
        r = run_agent("趋势", cfg, query_service=qs,
                      )  # 默认不启用叙述
        assert r.narrative_source == "deterministic"

        from dataagent import report_generator
        facts = report_generator.build_report_facts(r)
        text = "申购金额在观察期内合计为正，赎回金额同期亦有记录 [E001]。"
        ok, problems = report_generator.validate_narrative(text, facts)
        assert ok, problems

    def test_llm_narrative_failure_falls_back(self, qs, tmp_path, monkeypatch):
        class Broken:
            model = "broken"

            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                raise TimeoutError("model timeout")

        cfg = cfg_for("trend", tmp_path)
        cfg.use_llm_narrative = True
        cfg.llm_client = mock_client("trend")
        # 叙述客户端与解析客户端分离：直接调 generate_report 验证回退
        base = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        from dataagent import report_generator
        res = report_generator.generate_report(base, tmp_path, llm_narrator=Broken())
        assert res.status in ("generated", "generated_with_warnings")
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("回退确定性报告" in w for w in res.warnings)

    def test_llm_inconsistent_output_rejected(self, qs, tmp_path):
        from dataagent import report_generator

        class Hallucinating:
            model = "hallucinating"

            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kwargs):
                text = ("申购金额合计为 999999 元 [E001]，销售额大幅增长，"
                        "因为春节活动导致。")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=text))])

        base = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        res = report_generator.generate_report(base, tmp_path,
                                               llm_narrator=Hallucinating())
        assert res.narrative_source == "llm_rejected_fallback"
        joined = " ".join(res.warnings)
        assert "不存在的数字" in joined or "销售额" in joined or "因果" in joined
        text = Path(res.markdown_path).read_text(encoding="utf-8")
        assert "999999" not in text       # 幻觉数字不得进入报告


# ---------------------------------------------------------------------------
# 18-20：模式标识与失败不转成功
# ---------------------------------------------------------------------------

class TestModeLabelsAndStatuses:
    def test_absolute_date_question_end_to_end(self, qs, tmp_path):
        """负责人场景：绝对日期范围问题 → 计划校验 → SQL 执行 → 报告（07.4）。"""
        r = run_agent("分析2024年1月2日至2024年1月12日的申购和赎回趋势",
                      cfg_for("absolute", tmp_path), query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED
        # 计划通过校验且 anchor 干净
        assert r.plan["time_range"]["anchor"] == "max_available_data_date"
        assert r.plan["time_range"]["resolved_start_date"] == "2024-01-02"
        assert r.plan["time_range"]["resolved_end_date"] == "2024-01-12"
        # SQL 查询成功（双窗口/单窗口均可，但执行状态必须 ok）
        assert r.execution_summary["status"] == "ok"
        assert all(q["status"] == "ok" for q in r.execution_summary["queries"])
        # 有效日期范围正确
        tr = r.analysis_result.time_range
        assert tr["requested"]["start"] == "2024-01-02"
        assert tr["requested"]["end"] == "2024-01-12"
        assert tr["effective"]["start"] == "2024-01-02"
        assert tr["effective"]["end"] == "2024-01-12"
        # 报告生成成功且范围一致
        assert r.report_path and Path(r.report_path).exists()
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "2024-01-02 至 2024-01-12" in text

    def test_mode_labels(self, qs, tmp_path):
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.model_mode == "mock"
        assert r.data_mode == "synthetic"
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "非真实模型调用" in text
        assert "非真实业务数据" in text

    def test_real_mode_without_key_reports_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATAAGENT_LLM_API_KEY", raising=False)
        cfg = AgentConfig(model_mode="real", data_mode="synthetic",
                          db_path=synth_db_path_holder[0],
                          data_range=DATA_RANGE, output_dir=tmp_path)
        r = run_agent("趋势", cfg)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_not_configured"
        assert r.report_path is None

    def test_all_failure_statuses_not_success(self, qs, tmp_path):
        results = [
            run_agent("分析一下。", cfg_for("vague", tmp_path), query_service=qs),
            run_agent("销售额", cfg_for("sales", tmp_path), query_service=qs),
        ]
        for r in results:
            assert not r.is_success()
            assert r.report_path is None


# ===========================================================================
# 任务 07.1-A：客户端与模型名传递
# ===========================================================================

class RecordingClient:
    """解析 + 叙述双用途假客户端，记录调用次数与模型名。"""

    def __init__(self, plan_key="trend", narrate_text="申购金额有记录 [E001]。"):
        from dataagent import agent as _a  # noqa: F401
        self.model = "client-model-attr"
        self.plan_key = plan_key
        self.narrate_text = narrate_text
        self.calls = {"parse": 0, "narrate": 0, "models": []}
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls["models"].append(kw.get("model"))
        messages = kw.get("messages") or []
        system = messages[0]["content"] if messages else ""
        if "文字组织者" in system:
            self.calls["narrate"] += 1
            text = self.narrate_text
        else:
            self.calls["parse"] += 1
            text = json.dumps(PLANS[self.plan_key], ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])


class TestClientPropagation:
    def test_no_narrative_means_no_extra_call(self, qs, tmp_path):
        client = RecordingClient()
        cfg = cfg_for("trend", tmp_path, llm_client=client)
        r = run_agent("趋势", cfg, query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED
        assert client.calls["parse"] == 1
        assert client.calls["narrate"] == 0
        assert r.narrative_source == "deterministic"

    def test_narrative_uses_same_client(self, qs, tmp_path):
        client = RecordingClient()
        cfg = cfg_for("trend", tmp_path, llm_client=client,
                      use_llm_narrative=True)
        r = run_agent("趋势", cfg, query_service=qs)
        assert client.calls["narrate"] == 1          # 确实调用了叙述
        assert r.narrative_source == "llm"
        # 解析与叙述使用同一配置模型名（来自 IntentParser.model，而非占位名）
        assert len(set(client.calls["models"])) == 1
        assert client.calls["models"][0] != "mock-narrator"

    def test_explicit_llm_model_wins(self, qs, tmp_path):
        client = RecordingClient()
        cfg = cfg_for("trend", tmp_path, llm_client=client,
                      use_llm_narrative=True, llm_model="explicit-narrative-model")
        r = run_agent("趋势", cfg, query_service=qs)
        assert r.narrative_source == "llm"
        assert client.calls["models"][-1] == "explicit-narrative-model"

    def test_real_mode_without_key_structured_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATAAGENT_LLM_API_KEY", raising=False)
        cfg = AgentConfig(model_mode="real", data_mode="synthetic",
                          db_path=synth_db_path_holder[0],
                          data_range=DATA_RANGE, output_dir=tmp_path,
                          use_llm_narrative=True)
        r = run_agent("趋势", cfg)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_not_configured"
        assert r.report_path is None
        assert r.narrative_source is None

    def test_narrative_without_model_name_warns(self, qs, tmp_path):
        """叙述客户端无 model 属性且未提供 llm_model → 结构化警告 + 确定性报告。"""
        from types import SimpleNamespace as SN
        from dataagent import report_generator

        class BareClient:
            def __init__(self):
                self.chat = SN(completions=self)

            def create(self, **kw):
                return SN(choices=[SN(message=SN(content="叙述 [E001]。"))])

        base = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        res = report_generator.generate_report(base, tmp_path,
                                               llm_narrator=BareClient(),
                                               llm_model=None)
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("模型名称" in w for w in res.warnings)


# ===========================================================================
# 任务 07.1-C：异常处理与资源安全
# ===========================================================================

class TestExceptionHandling:
    def test_parser_init_exception_structured(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("client init exploded")

        monkeypatch.setattr(agent_mod, "_make_parser", boom)
        cfg = AgentConfig(model_mode="mock", data_mode="synthetic",
                          db_path=synth_db_path_holder[0],
                          data_range=DATA_RANGE, output_dir=tmp_path,
                          llm_client=mock_client("trend"))
        r = run_agent("趋势", cfg)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_init_failed"
        assert r.report_path is None

    def test_parse_call_exception_structured(self, qs, tmp_path, monkeypatch):
        class Boom:
            def parse_question(self, q):
                raise ConnectionError("api down")

        monkeypatch.setattr(agent_mod, "_make_parser", lambda cfg: Boom())
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_call_failed"
        assert r.report_path is None

    def test_run_analysis_exception_structured(self, qs, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("duckdb panic")

        monkeypatch.setattr(agent_mod, "run_analysis", boom)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "execution_exception"
        assert r.report_path is None

    def test_charts_exception_still_reports(self, qs, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("matplotlib crashed")

        monkeypatch.setattr(agent_mod.visualization, "generate_charts", boom)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED_WITH_WARNINGS
        assert r.chart_paths == []
        assert any("图表生成异常" in w for w in r.warnings)
        assert Path(r.report_path).exists()

    def test_report_exception_preserves_analysis(self, qs, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("render failed")

        monkeypatch.setattr(agent_mod, "generate_report", boom)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.ANALYSIS_FAILED
        assert r.error_code == "report_exception"
        # 统计结果仍然保留（不被覆盖）
        assert r.analysis_summary["status"] == "ok"
        assert r.execution_summary["status"] == "ok"

    def test_service_closed_on_failure(self, tmp_path, monkeypatch, synth_db):
        """自有 QueryService 在失败路径也必须关闭。"""
        closed = {"n": 0}
        real_close = QueryService.close

        def spy_close(self):
            closed["n"] += 1
            return real_close(self)

        def boom(*a, **k):
            raise RuntimeError("analysis failed")

        monkeypatch.setattr(QueryService, "close", spy_close)
        monkeypatch.setattr(agent_mod, "run_analysis", boom)
        cfg = AgentConfig(model_mode="mock", data_mode="synthetic",
                          db_path=synth_db, data_range=DATA_RANGE,
                          output_dir=tmp_path, llm_client=mock_client("trend"))
        r = run_agent("趋势", cfg)          # 不注入 query_service → 内部创建
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert closed["n"] >= 1

    def test_close_exception_does_not_mask_error(self, tmp_path, monkeypatch, synth_db):
        def boom_close(self):
            raise OSError("close failed")

        def boom(*a, **k):
            raise RuntimeError("real error")

        monkeypatch.setattr(QueryService, "close", boom_close)
        monkeypatch.setattr(agent_mod, "run_analysis", boom)
        cfg = AgentConfig(model_mode="mock", data_mode="synthetic",
                          db_path=synth_db, data_range=DATA_RANGE,
                          output_dir=tmp_path, llm_client=mock_client("trend"))
        r = run_agent("趋势", cfg)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert "real error" in (r.error or "")   # 原始错误不被掩盖


# ===========================================================================
# 任务 07.1-A：客户端与模型名传递
# ===========================================================================

class RecordingClient:
    """解析 + 叙述双用途假客户端，记录调用次数与模型名。"""

    def __init__(self, plan_key="trend", narrate_text="申购金额有记录 [E001]。"):
        from dataagent import agent as _a  # noqa: F401
        self.model = "client-model-attr"
        self.plan_key = plan_key
        self.narrate_text = narrate_text
        self.calls = {"parse": 0, "narrate": 0, "models": []}
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls["models"].append(kw.get("model"))
        messages = kw.get("messages") or []
        system = messages[0]["content"] if messages else ""
        if "文字组织者" in system:
            self.calls["narrate"] += 1
            text = self.narrate_text
        else:
            self.calls["parse"] += 1
            text = json.dumps(PLANS[self.plan_key], ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])


class TestClientPropagation:
    def test_no_narrative_means_no_extra_call(self, qs, tmp_path):
        client = RecordingClient()
        cfg = cfg_for("trend", tmp_path, llm_client=client)
        r = run_agent("趋势", cfg, query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED
        assert client.calls["parse"] == 1
        assert client.calls["narrate"] == 0
        assert r.narrative_source == "deterministic"

    def test_narrative_uses_same_client(self, qs, tmp_path):
        client = RecordingClient()
        cfg = cfg_for("trend", tmp_path, llm_client=client,
                      use_llm_narrative=True)
        r = run_agent("趋势", cfg, query_service=qs)
        assert client.calls["narrate"] == 1          # 确实调用了叙述
        assert r.narrative_source == "llm"
        # 解析与叙述使用同一配置模型名（来自 IntentParser.model，而非占位名）
        assert len(set(client.calls["models"])) == 1
        assert client.calls["models"][0] != "mock-narrator"

    def test_explicit_llm_model_wins(self, qs, tmp_path):
        client = RecordingClient()
        cfg = cfg_for("trend", tmp_path, llm_client=client,
                      use_llm_narrative=True, llm_model="explicit-narrative-model")
        r = run_agent("趋势", cfg, query_service=qs)
        assert r.narrative_source == "llm"
        assert client.calls["models"][-1] == "explicit-narrative-model"

    def test_real_mode_without_key_structured_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATAAGENT_LLM_API_KEY", raising=False)
        cfg = AgentConfig(model_mode="real", data_mode="synthetic",
                          db_path=synth_db_path_holder[0],
                          data_range=DATA_RANGE, output_dir=tmp_path,
                          use_llm_narrative=True)
        r = run_agent("趋势", cfg)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_not_configured"
        assert r.report_path is None
        assert r.narrative_source is None

    def test_narrative_without_model_name_warns(self, qs, tmp_path):
        """叙述客户端无 model 属性且未提供 llm_model → 结构化警告 + 确定性报告。"""
        from types import SimpleNamespace as SN
        from dataagent import report_generator

        class BareClient:
            def __init__(self):
                self.chat = SN(completions=self)

            def create(self, **kw):
                return SN(choices=[SN(message=SN(content="叙述 [E001]。"))])

        base = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        res = report_generator.generate_report(base, tmp_path,
                                               llm_narrator=BareClient(),
                                               llm_model=None)
        assert res.narrative_source == "llm_rejected_fallback"
        assert any("模型名称" in w for w in res.warnings)


# ===========================================================================
# 任务 07.1-C：异常处理与资源安全
# ===========================================================================

class TestExceptionHandling:
    def test_parser_init_exception_structured(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("client init exploded")

        monkeypatch.setattr(agent_mod, "_make_parser", boom)
        cfg = AgentConfig(model_mode="mock", data_mode="synthetic",
                          db_path=synth_db_path_holder[0],
                          data_range=DATA_RANGE, output_dir=tmp_path,
                          llm_client=mock_client("trend"))
        r = run_agent("趋势", cfg)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_init_failed"
        assert r.report_path is None

    def test_parse_call_exception_structured(self, qs, tmp_path, monkeypatch):
        class Boom:
            def parse_question(self, q):
                raise ConnectionError("api down")

        monkeypatch.setattr(agent_mod, "_make_parser", lambda cfg: Boom())
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_call_failed"
        assert r.report_path is None

    def test_run_analysis_exception_structured(self, qs, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("duckdb panic")

        monkeypatch.setattr(agent_mod, "run_analysis", boom)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "execution_exception"
        assert r.report_path is None

    def test_charts_exception_still_reports(self, qs, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("matplotlib crashed")

        monkeypatch.setattr(agent_mod.visualization, "generate_charts", boom)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED_WITH_WARNINGS
        assert r.chart_paths == []
        assert any("图表生成异常" in w for w in r.warnings)
        assert Path(r.report_path).exists()

    def test_report_exception_preserves_analysis(self, qs, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("render failed")

        monkeypatch.setattr(agent_mod, "generate_report", boom)
        r = run_agent("趋势", cfg_for("trend", tmp_path), query_service=qs)
        assert r.status == AgentStatus.ANALYSIS_FAILED
        assert r.error_code == "report_exception"
        # 统计结果仍然保留（不被覆盖）
        assert r.analysis_summary["status"] == "ok"
        assert r.execution_summary["status"] == "ok"

    def test_service_closed_on_failure(self, tmp_path, monkeypatch, synth_db):
        """自有 QueryService 在失败路径也必须关闭。"""
        closed = {"n": 0}
        real_close = QueryService.close

        def spy_close(self):
            closed["n"] += 1
            return real_close(self)

        def boom(*a, **k):
            raise RuntimeError("analysis failed")

        monkeypatch.setattr(QueryService, "close", spy_close)
        monkeypatch.setattr(agent_mod, "run_analysis", boom)
        cfg = AgentConfig(model_mode="mock", data_mode="synthetic",
                          db_path=synth_db, data_range=DATA_RANGE,
                          output_dir=tmp_path, llm_client=mock_client("trend"))
        r = run_agent("趋势", cfg)          # 不注入 query_service → 内部创建
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert closed["n"] >= 1

    def test_close_exception_does_not_mask_error(self, tmp_path, monkeypatch, synth_db):
        def boom_close(self):
            raise OSError("close failed")

        def boom(*a, **k):
            raise RuntimeError("real error")

        monkeypatch.setattr(QueryService, "close", boom_close)
        monkeypatch.setattr(agent_mod, "run_analysis", boom)
        cfg = AgentConfig(model_mode="mock", data_mode="synthetic",
                          db_path=synth_db, data_range=DATA_RANGE,
                          output_dir=tmp_path, llm_client=mock_client("trend"))
        r = run_agent("趋势", cfg)
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert "real error" in (r.error or "")   # 原始错误不被掩盖


# ===========================================================================
# 任务 07.2-B：真实模式内部初始化客户端（Fake OpenAI，无网络）
# ===========================================================================

class FakeOpenAI:
    """替代 openai.OpenAI 的假客户端工厂产物（记录实例与调用）。"""

    instances: list["FakeOpenAI"] = []

    def __init__(self, api_key=None, base_url=None, **kw):
        self.api_key = api_key
        self.base_url = base_url
        self.calls: list[dict] = []
        FakeOpenAI.instances.append(self)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls.append({"model": kw.get("model"),
                           "system": (kw.get("messages") or [{}])[0]
                           .get("content", "")})
        system = self.calls[-1]["system"]
        if "文字组织者" in system:
            text = "申购金额有记录 [E001]。"
        else:
            text = json.dumps(PLANS["trend"], ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])


class TestRealModeInternalClient:
    """覆盖"客户端由 IntentParser 依据环境变量内部初始化"的路径。"""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        FakeOpenAI.instances = []
        monkeypatch.setenv("DATAAGENT_LLM_API_KEY", "test-key-000")
        monkeypatch.setenv("DATAAGENT_LLM_MODEL", "fake-model-from-env")
        monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    def _cfg(self, tmp_path, **kw):
        kw.setdefault("model_mode", "real")
        kw.setdefault("data_mode", "synthetic")
        return AgentConfig(db_path=synth_db_path_holder[0],
                           data_range=DATA_RANGE, output_dir=tmp_path, **kw)

    def test_parser_initializes_client_from_env(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED
        assert len(FakeOpenAI.instances) == 1
        inst = FakeOpenAI.instances[0]
        assert inst.api_key == "test-key-000"

    def test_parser_and_narrative_share_same_client(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        assert r.status in (AgentStatus.REPORT_GENERATED,
                            AgentStatus.REPORT_GENERATED_WITH_WARNINGS)
        assert r.narrative_source == "llm"          # 叙述确实被调用（非静默回退）
        assert len(FakeOpenAI.instances) == 1       # 同一客户端实例
        inst = FakeOpenAI.instances[0]
        models = [c["model"] for c in inst.calls]
        # 解析与叙述都使用环境变量配置的模型名
        assert models == ["fake-model-from-env", "fake-model-from-env"]
        assert "mock-narrator" not in models

    def test_no_narrative_means_no_extra_call(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        assert r.narrative_source == "deterministic"
        inst = FakeOpenAI.instances[0]
        assert len(inst.calls) == 1                 # 仅解析一次

    def test_model_name_from_env_not_placeholder(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "mock-narrator" not in text

    def test_openai_init_failure_structured(self, qs, tmp_path, monkeypatch):
        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("sdk init failed")

        monkeypatch.setattr("openai.OpenAI", Boom)
        from dataagent.intent_parser import IntentParser
        parser = IntentParser()      # 构造失败被捕获为 client_error
        assert parser.client is None
        assert "LLM 客户端初始化失败" in (parser.client_error or "")
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        # IntentParser 已内部捕获 init 失败 → Agent 收到结构化 API 错误
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code in ("llm_init_failed", "llm_api_error",
                                "llm_not_configured")
        assert r.report_path is None

    def test_api_call_failure_keeps_structured_state(self, qs, tmp_path,
                                                     monkeypatch):
        class FailCall(FakeOpenAI):
            def create(self, **kw):
                raise ConnectionError("api unreachable")

        monkeypatch.setattr("openai.OpenAI", FailCall)
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        # IntentParser 内部捕获 API 异常 → 结构化 llm_api_error（不生成报告）
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_api_error"
        assert r.report_path is None

    def test_narrative_api_failure_falls_back(self, qs, tmp_path, monkeypatch):
        """解析成功、叙述调用失败 → 保留确定性报告。"""
        class ParseOkNarrateFail(FakeOpenAI):
            def create(self, **kw):
                system = (kw.get("messages") or [{}])[0].get("content", "")
                if "文字组织者" in system:
                    raise ConnectionError("narrative api down")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(PLANS["trend"], ensure_ascii=False)))])

        monkeypatch.setattr("openai.OpenAI", ParseOkNarrateFail)
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        assert r.status in (AgentStatus.REPORT_GENERATED,
                            AgentStatus.REPORT_GENERATED_WITH_WARNINGS)
        assert r.narrative_source == "llm_rejected_fallback"
        assert any("回退" in w for w in r.warnings)

    def test_hallucinated_narrative_rejected(self, qs, tmp_path, monkeypatch):
        class Hallucinating(FakeOpenAI):
            def create(self, **kw):
                system = (kw.get("messages") or [{}])[0].get("content", "")
                if "文字组织者" in system:
                    text = ("销售额为 999999 元 [E001]，因为促销导致增长。")
                    return SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content=text))])
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(PLANS["trend"], ensure_ascii=False)))])

        monkeypatch.setattr("openai.OpenAI", Hallucinating)
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        assert r.narrative_source == "llm_rejected_fallback"
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "999999" not in text

    def test_public_interface_exists(self, qs, tmp_path):
        """公开属性 client/client_error 必须存在（不得只依赖私有字段）。"""
        from dataagent.intent_parser import IntentParser
        parser = IntentParser()
        assert hasattr(parser, "client")
        assert hasattr(parser, "client_error")
        assert parser.client is None or parser.client is not None  # 可访问
        assert parser.client_error is None

    def test_agent_uses_public_attribute(self, qs, tmp_path):
        """agent 不得通过私有 _client 获取叙述客户端（源码级断言）。"""
        import inspect
        src = inspect.getsource(agent_mod)
        assert 'getattr(parser, "client"' not in src
        assert "parser.client" in src
        assert "parser._client" not in src


# ===========================================================================
# 任务 07.2-B：真实模式内部初始化客户端（Fake OpenAI，无网络）
# ===========================================================================

class FakeOpenAI:
    """替代 openai.OpenAI 的假客户端工厂产物（记录实例与调用）。"""

    instances: list["FakeOpenAI"] = []

    def __init__(self, api_key=None, base_url=None, **kw):
        self.api_key = api_key
        self.base_url = base_url
        self.calls: list[dict] = []
        FakeOpenAI.instances.append(self)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls.append({"model": kw.get("model"),
                           "system": (kw.get("messages") or [{}])[0]
                           .get("content", "")})
        system = self.calls[-1]["system"]
        if "文字组织者" in system:
            text = "申购金额有记录 [E001]。"
        else:
            text = json.dumps(PLANS["trend"], ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])


class TestRealModeInternalClient:
    """覆盖"客户端由 IntentParser 依据环境变量内部初始化"的路径。"""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        FakeOpenAI.instances = []
        monkeypatch.setenv("DATAAGENT_LLM_API_KEY", "test-key-000")
        monkeypatch.setenv("DATAAGENT_LLM_MODEL", "fake-model-from-env")
        monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    def _cfg(self, tmp_path, **kw):
        kw.setdefault("model_mode", "real")
        kw.setdefault("data_mode", "synthetic")
        return AgentConfig(db_path=synth_db_path_holder[0],
                           data_range=DATA_RANGE, output_dir=tmp_path, **kw)

    def test_parser_initializes_client_from_env(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        assert r.status == AgentStatus.REPORT_GENERATED
        assert len(FakeOpenAI.instances) == 1
        inst = FakeOpenAI.instances[0]
        assert inst.api_key == "test-key-000"

    def test_parser_and_narrative_share_same_client(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        assert r.status in (AgentStatus.REPORT_GENERATED,
                            AgentStatus.REPORT_GENERATED_WITH_WARNINGS)
        assert r.narrative_source == "llm"          # 叙述确实被调用（非静默回退）
        assert len(FakeOpenAI.instances) == 1       # 同一客户端实例
        inst = FakeOpenAI.instances[0]
        models = [c["model"] for c in inst.calls]
        # 解析与叙述都使用环境变量配置的模型名
        assert models == ["fake-model-from-env", "fake-model-from-env"]
        assert "mock-narrator" not in models

    def test_no_narrative_means_no_extra_call(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        assert r.narrative_source == "deterministic"
        inst = FakeOpenAI.instances[0]
        assert len(inst.calls) == 1                 # 仅解析一次

    def test_model_name_from_env_not_placeholder(self, qs, tmp_path):
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "mock-narrator" not in text

    def test_openai_init_failure_structured(self, qs, tmp_path, monkeypatch):
        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("sdk init failed")

        monkeypatch.setattr("openai.OpenAI", Boom)
        from dataagent.intent_parser import IntentParser
        parser = IntentParser()      # 构造失败被捕获为 client_error
        assert parser.client is None
        assert "LLM 客户端初始化失败" in (parser.client_error or "")
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        # IntentParser 已内部捕获 init 失败 → Agent 收到结构化 API 错误
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code in ("llm_init_failed", "llm_api_error",
                                "llm_not_configured")
        assert r.report_path is None

    def test_api_call_failure_keeps_structured_state(self, qs, tmp_path,
                                                     monkeypatch):
        class FailCall(FakeOpenAI):
            def create(self, **kw):
                raise ConnectionError("api unreachable")

        monkeypatch.setattr("openai.OpenAI", FailCall)
        r = run_agent("趋势", self._cfg(tmp_path), query_service=qs)
        # IntentParser 内部捕获 API 异常 → 结构化 llm_api_error（不生成报告）
        assert r.status == AgentStatus.EXECUTION_FAILED
        assert r.error_code == "llm_api_error"
        assert r.report_path is None

    def test_narrative_api_failure_falls_back(self, qs, tmp_path, monkeypatch):
        """解析成功、叙述调用失败 → 保留确定性报告。"""
        class ParseOkNarrateFail(FakeOpenAI):
            def create(self, **kw):
                system = (kw.get("messages") or [{}])[0].get("content", "")
                if "文字组织者" in system:
                    raise ConnectionError("narrative api down")
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(PLANS["trend"], ensure_ascii=False)))])

        monkeypatch.setattr("openai.OpenAI", ParseOkNarrateFail)
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        assert r.status in (AgentStatus.REPORT_GENERATED,
                            AgentStatus.REPORT_GENERATED_WITH_WARNINGS)
        assert r.narrative_source == "llm_rejected_fallback"
        assert any("回退" in w for w in r.warnings)

    def test_hallucinated_narrative_rejected(self, qs, tmp_path, monkeypatch):
        class Hallucinating(FakeOpenAI):
            def create(self, **kw):
                system = (kw.get("messages") or [{}])[0].get("content", "")
                if "文字组织者" in system:
                    text = ("销售额为 999999 元 [E001]，因为促销导致增长。")
                    return SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content=text))])
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(PLANS["trend"], ensure_ascii=False)))])

        monkeypatch.setattr("openai.OpenAI", Hallucinating)
        r = run_agent("趋势", self._cfg(tmp_path, use_llm_narrative=True),
                      query_service=qs)
        assert r.narrative_source == "llm_rejected_fallback"
        text = Path(r.report_path).read_text(encoding="utf-8")
        assert "999999" not in text

    def test_public_interface_exists(self, qs, tmp_path):
        """公开属性 client/client_error 必须存在（不得只依赖私有字段）。"""
        from dataagent.intent_parser import IntentParser
        parser = IntentParser()
        assert hasattr(parser, "client")
        assert hasattr(parser, "client_error")
        assert parser.client is None or parser.client is not None  # 可访问
        assert parser.client_error is None

    def test_agent_uses_public_attribute(self, qs, tmp_path):
        """agent 不得通过私有 _client 获取叙述客户端（源码级断言）。"""
        import inspect
        src = inspect.getsource(agent_mod)
        assert 'getattr(parser, "client"' not in src
        assert "parser.client" in src
        assert "parser._client" not in src
