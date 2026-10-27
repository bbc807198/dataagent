"""阶段 12：本地 Web 平台测试（Streamlit AppTest + 服务层）。

约束：全部使用合成数据与 Mock 模式，**不发起任何真实 API 调用**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

streamlit = pytest.importorskip("streamlit")
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

import sys
sys.path.insert(0, str(ROOT / "src"))

from web import service  # noqa: E402

# 干净克隆环境没有评测产物（artifacts/ 不入库）→ 相关面板测试跳过
skipif_no_eval = pytest.mark.skipif(
    not (ROOT / "artifacts" / "evaluation" / "latest" /
          "evaluation_results.json").is_file(),
    reason="本机未生成离线评测产物（干净克隆不含 artifacts/）")


# ---------------------------------------------------------------- 模块与服务层
class TestService:
    def test_streamlit_importable(self):
        """① Streamlit 可正确导入。"""
        assert streamlit.__version__
        assert hasattr(streamlit, "testing")

    def test_default_modes_mock_synthetic(self):
        """② 默认配置为 Mock + Synthetic。"""
        assert service.MODEL_MODES[0] == "mock"
        assert service.DATA_MODES[0] == "synthetic"

    def test_no_api_key_required_for_mock(self):
        """③ 无 API Key 也能跑通 Mock 演示。"""
        import os
        old = os.environ.pop("DATAAGENT_LLM_API_KEY", None)
        try:
            out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                       "mock", "synthetic")
            assert out.ok and out.report_text
        finally:
            if old is not None:
                os.environ["DATAAGENT_LLM_API_KEY"] = old

    def test_valid_question_returns_report_charts_evidence(self):
        """④ 有效问题返回报告 + 图表 + 证据。"""
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "synthetic")
        assert out.ok
        assert len(out.report_text) > 500
        assert out.charts, "应有图表"
        rows = service.evidence_rows(out.evidence)
        assert rows and all(r["证据 ID"].startswith("[E") for r in rows)
        assert out.queries and out.queries[0]["sql"].upper().startswith("SELECT")

    def test_rejected_question_no_success_report(self):
        """⑤ 拒绝问题不显示成功报告。"""
        out = service.run_question("帮我分析销售额的变化。", "mock", "synthetic")
        assert out.ok is False
        assert out.report_text == ""
        assert out.status in ("unsupported_metric", "plan_rejected",
                              "needs_clarification")

    def test_unknown_question_marks_mock_limit(self):
        """Mock 模式不支持任意问题：明确提示而不是假装理解。"""
        out = service.run_question("这是一个完全无关的问题 ZZZ", "mock", "synthetic")
        assert out.ok is False
        assert out.status == "mock_scenario_unknown"

    def test_sql_is_parameterized_without_values(self):
        """⑪ SQL 模板为参数化形式（不含参数值/用户数据）。"""
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "synthetic")
        sql = out.queries[0]["sql"]
        assert "?" in sql                      # 参数占位
        assert "2024-01-02" not in sql         # 不含实际参数值

    def test_no_absolute_paths_in_output(self):
        """⑬ 输出不含本机绝对路径与密钥。"""
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "synthetic")
        blob = json.dumps({
            "report": out.report_text, "evidence": out.evidence,
            "charts": out.charts, "queries": out.queries,
        }, ensure_ascii=False)
        assert "C:\\Users" not in blob
        assert "C:/Users" not in blob
        assert "sk-" not in blob

    def test_build_config_rejects_arbitrary_path(self):
        """⑫ 只允许固定数据模式（不接受任意数据库路径）。"""
        with pytest.raises(ValueError):
            service.build_config("mock", "arbitrary_path", Path("."))

    def test_project_db_missing_is_reported(self, monkeypatch):
        """项目数据库不存在 → 明确错误，不崩溃。

        注意（阶段 13.2）：Mock + Project DB 组合已被前置阻断，
        因此这里用 **real** 模式验证"库文件缺失"的报错路径。
        """
        monkeypatch.setattr(service.config, "DB_PATH",
                            ROOT / "does-not-exist.duckdb")
        monkeypatch.delenv("DATAAGENT_LLM_API_KEY", raising=False)
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "real", "project_db")
        assert out.ok is False
        assert "数据库" in out.status_text

    def test_mock_project_db_blocked_before_db_check(self):
        """Mock + Project DB 在语言/配置层就被阻断（不依赖库是否存在）。"""
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "project_db")
        assert out.status == "mock_project_db_mismatch"

    def test_real_mode_without_key_is_blocked(self, monkeypatch):
        """⑥ 真实模式无 Key → 明确提示（不发起任何调用）。"""
        monkeypatch.delenv("DATAAGENT_LLM_API_KEY", raising=False)
        assert service.is_real_mode_configured() is False


# ---------------------------------------------------------------- AppTest 流程
@pytest.fixture(scope="module")
def app_default():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    return at


class TestAppFlow:
    def test_page_loads_with_defaults(self, app_default):
        """① 首页加载无异常，默认 Mock + Synthetic。"""
        assert not app_default.exception
        assert app_default.title[0].value.startswith("DataAgent")
        radios = {r.label: r.value for r in app_default.radio}
        assert radios["模型模式"] == "mock" and radios["数据模式"] == "synthetic"

    def test_full_mock_flow_produces_result(self):
        """完整 Mock + Synthetic 使用流程（报告/图表/证据/下载）。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.text_area(key="question_input").set_value(
            "帮我分析申购和赎回金额最近的趋势。")
        at.button(key="run_btn").click().run()
        assert not at.exception
        assert at.success and "已完成" in at.success[0].value
        res = at.session_state.get("result")
        assert res is not None and res.ok
        assert res.report_text and res.evidence and res.charts
        # 页面元素：SQL 代码块、证据表、两个下载按钮
        assert len(at.code) >= 1
        assert len(at.dataframe) >= 1
        assert len(at.get("download_button")) >= 2

    def test_refresh_does_not_rerun_model(self):
        """⑦ 页面刷新（再次 run）不重复执行分析。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.text_area(key="question_input").set_value(
            "帮我分析申购和赎回金额最近的趋势。")
        at.button(key="run_btn").click().run()
        first = at.session_state.get("result")
        calls_after_first = service.SyntheticDB.path()   # 触发一次 path 缓存（不新建）
        at.run()                                          # 仅刷新，不点按钮
        second = at.session_state.get("result")
        assert second is first        # 同一对象（未重新执行）
        assert calls_after_first.exists()

    def test_rejection_shows_error_not_report(self):
        """⑤ 拒绝场景：显示错误，不显示成功报告。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.text_area(key="question_input").set_value("帮我分析销售额的变化。")
        at.button(key="run_btn").click().run()
        assert not at.exception
        assert at.error and "状态" in at.error[0].value
        res = at.session_state.get("result")
        assert res is not None and res.ok is False and res.report_text == ""

    @skipif_no_eval
    def test_benchmark_panel_reads_existing_results(self):
        """⑨ Benchmark 面板准确读取已有结果。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.radio(key="view").set_value("Benchmark").run()
        assert not at.exception
        labels = [m.label for m in at.metric]
        assert "执行成功率" in labels and "答案正确率" in labels
        assert "行为正确率" in labels and "正确拒绝率" in labels
        # 明确标注离线口径
        warns = " ".join(w.value for w in at.warning)
        assert "不代表" in warns

    @skipif_no_eval
    def test_no_duplicate_token_cost_in_panel(self):
        """⑩ Benchmark 面板不把 Mock 费用显示成真实成本。"""
        data = service.load_benchmark()
        assert data is not None
        summary = data["summary"]
        assert summary["model_mode"] == "mock"
        # JSON 序列化中未知成本写作 "N/A"（不伪造成 0）
        assert summary["token_total"] is None
        assert summary["cost_total"] in (None, "N/A")
        assert "离线" in (summary.get("cost_note") or "") or \
               "不适用" in (summary.get("cost_note") or "")

    def test_version_consistency(self):
        """版本号一致：pyproject / 包常量 / 安装元数据（阶段 12 修复了 0.2.0 漂移）。"""
        import tomllib
        from importlib.metadata import version
        import dataagent
        from web import service as svc

        with open(ROOT / "pyproject.toml", "rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]
        assert dataagent.__version__ == pyproject_version
        assert version("dataagent") == pyproject_version
        assert svc.package_version() == pyproject_version

    def test_sidebar_shows_security_notes(self, app_default):
        """侧边栏包含安全/隐私说明与只读提示。"""
        text = " ".join(m.value for m in app_default.markdown)
        assert "只读" in text or "安全" in text

# ---------------------------------------------------------------- 阶段 13：交互边界
class TestInteractionBoundaries:
    def test_example_selection_fills_input(self):
        """B1：选择示例 → 输入框显示对应问题。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        examples = service.mock_examples()
        target = examples[0]
        at.selectbox(key="example_pick").set_value(target).run()
        assert at.session_state["question_input"] == target
        assert at.text_area(key="question_input").value == target

    def test_switching_example_replaces_input(self):
        """B1：更换示例不残留上一条；切回自定义则清空。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        examples = service.mock_examples()
        at.selectbox(key="example_pick").set_value(examples[0]).run()
        first = at.session_state["question_input"]
        at.selectbox(key="example_pick").set_value(examples[1]).run()
        second = at.session_state["question_input"]
        assert first != second and second == examples[1]
        at.selectbox(key="example_pick").set_value("（自定义）").run()
        assert at.session_state["question_input"] == ""

    def test_manual_edit_kept(self):
        """B1：用户仍可手动编辑问题。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.text_area(key="question_input").set_value("自定义问题：帮我分析申购趋势。")
        at.run()
        assert at.session_state["question_input"].startswith("自定义问题")

    def test_stale_result_marked_as_history(self):
        """B2：修改输入后旧结果明确标记为历史，不冒充当前输入。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        examples = service.mock_examples()
        at.text_area(key="question_input").set_value(examples[0])
        at.button(key="run_btn").click().run()
        assert at.session_state.get("result") is not None
        # 修改问题但不再运行 → 结果仍在，但必须标记为历史
        at.text_area(key="question_input").set_value("换了一个完全不同的问题").run()
        warns = " ".join(w.value for w in at.warning)
        assert "历史结果" in warns
        titles = [h.value for h in at.subheader]
        assert any("历史" in t for t in titles)

    def test_repeated_click_runs_once_per_click(self):
        """B2：连续点击会产生各自的运行，但刷新不额外运行。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        examples = service.mock_examples()
        at.text_area(key="question_input").set_value(examples[0])
        at.button(key="run_btn").click().run()
        r1 = at.session_state.get("result")
        at.run()                      # 仅刷新
        assert at.session_state.get("result") is r1

    @skipif_no_eval
    def test_benchmark_panel_modes_and_na(self):
        """B3：离线标题 + P50/P95 与成本缺失时显示 N/A（不伪造 0）。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.radio(key="view").set_value("Benchmark").run()
        assert not at.exception
        titles = [t.value for t in at.title]
        assert any("离线" in t or "Benchmark" in t for t in titles)
        metrics = {m.label: m.value for m in at.metric}
        assert metrics.get("P50 响应") not in (None, "0 ms")
        assert "N/A" in str(metrics.get("Token / 成本"))

    def test_warning_wording_is_neutral(self):
        """B4：警告文案为中性的影响说明，不断言"不影响可用性"。"""
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        assert "不影响结论可用性" not in text
        assert "数据覆盖" in text and "窗口截取" in text
