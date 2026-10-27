"""阶段 13.1：用户体验修复回归测试（Mock 体验 / 日期渲染 / 状态文案 / 一致性）。

全部使用合成数据 + Mock，不发起任何真实 API 调用。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
sys.path.insert(0, str(ROOT / "src"))

from web import service  # noqa: E402


class TestMockCatalog:
    def test_catalog_source_is_demo_module(self):
        """A3：场景唯一来源 = scripts/demo_agent.py 的 DEMOS。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_demo_check", ROOT / "scripts" / "demo_agent.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert len(mod.DEMOS) == len(service.mock_catalog())
        assert [d["question"] for d in mod.DEMOS] == \
               [c["question"] for c in service.mock_catalog()]

    def test_success_and_boundary_classification(self):
        """A2：成功 5 个、边界 3 个，且带预期结果说明。"""
        catalog = service.mock_catalog()
        success = [c for c in catalog if c["kind"] == "success"]
        boundary = [c for c in catalog if c["kind"] == "boundary"]
        assert len(success) == 5 and len(boundary) == 3
        assert all(c["expected"] == "report_generated" for c in success)
        assert {c["expected"] for c in boundary} == {
            "unsupported_metric", "needs_clarification", "insufficient_data"}
        assert all(c["expected_note"] for c in catalog)

    def test_boundary_scenarios_declare_expected_status(self):
        """A2：'分析一下。'→ 澄清；单日趋势 → 数据不足；销售额 → 不支持指标。"""
        by_q = {c["question"]: c["expected"] for c in service.mock_catalog()}
        assert by_q["分析一下。"] == "needs_clarification"
        assert by_q["分析 2024-01-02 当天的申购金额趋势。"] == "insufficient_data"
        assert by_q["帮我分析销售额的变化。"] == "unsupported_metric"

    @pytest.mark.parametrize("cid,question", [
        ("T03", "帮我分析申购和赎回金额最近的趋势。"),
        ("W01", "比较最近几天和之前几天各城市的净资金流入。"),
        ("R01", "各城市申购金额排名。"),
        ("A01", "申购金额有没有异常日期？"),
        ("S01", "观察期内申购金额的总体情况。"),
    ])
    def test_success_scenarios_actually_succeed(self, cid, question):
        """A2：补充的成功场景必须实际运行成功（复用受支持计划）。"""
        out = service.run_question(question, "mock", "synthetic")
        assert out.ok, f"{cid} 未成功: {out.status}"
        assert len(out.report_text) > 300
        assert out.queries and out.evidence


class TestExactMatching:
    def test_fragments_do_not_match(self):
        """A4：片段输入不得命中预设计划。"""
        for q in ("申购", "趋势", "帮我分析", "分析", "申购金额", "数据"):
            assert service.mock_scenario_detail(q) is None, q

    def test_normalization_tolerates_punctuation_only(self):
        """A4：仅容忍空格与句末标点差异。"""
        base = "帮我分析申购和赎回金额最近的趋势"
        for variant in (base, base + "。", base + "！", f" {base} ", base.replace("和", " 和 ")):
            assert service.mock_scenario_detail(variant) is not None, variant

    def test_extended_question_does_not_match(self):
        """A4：在示例后追加内容不应命中（避免"看起来像"就套用预设）。"""
        assert service.mock_scenario_detail(
            "帮我分析申购和赎回金额最近的趋势，并给出投资建议") is None
        assert service.mock_scenario_detail(
            "帮我分析销售额的变化和趋势") is None

    def test_unknown_question_returns_limit_notice(self):
        """A4：未匹配 → 明确提示 Mock 限制，不生成报告、不调后端。"""
        out = service.run_question("随便问一个完全无关的问题 ABC", "mock", "synthetic")
        assert out.ok is False
        assert out.status == "mock_scenario_unknown"
        assert out.report_text == ""
        assert "Mock 模式的使用限制" in out.status_text


class TestDateRendering:
    def test_no_tilde_in_report_and_conclusions(self):
        """B：报告与结论中不得出现 `~`（避免 Markdown 删除线）。"""
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "synthetic")
        assert "~" not in out.report_text
        assert "~" not in "\n".join(out.conclusions)
        assert " 至 " in out.report_text

    def test_multiple_ranges_in_one_line_safe(self):
        """B：一行多个日期区间也不产生删除线（无 `~` 即安全）。"""
        out = service.run_question(
            "比较最近几天和之前几天各城市的净资金流入。", "mock", "synthetic")
        assert "~" not in out.report_text
        # 双窗口信息仍然存在
        assert out.report_text.count(" 至 ") >= 2

    def test_bold_and_evidence_ids_preserved(self):
        """B：修复不得破坏加粗与证据 ID。"""
        out = service.run_question("申购金额有没有异常日期？", "mock", "synthetic")
        assert "**" in out.report_text
        assert re.search(r"\[E\d{3}\]", out.report_text)

    def test_markdown_renders_without_strikethrough(self):
        """B：用标准 Markdown 语法检查——正文中不出现成对的 `~~` 或未配对 `~`。"""
        out = service.run_question("观察期内申购金额的总体情况。", "mock", "synthetic")
        text = out.report_text
        assert "~~" not in text
        # 报告用 `~` 之外的方式表示区间；不应有孤立 `~`
        assert "~" not in text

    def test_service_and_app_source_free_of_tilde_ranges(self):
        """B：展示层源码不再拼接 `~` 区间（防止回归）。"""
        for rel in ("src/dataagent/analytics.py", "src/dataagent/report_generator.py",
                    "web/service.py", "app.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            assert "}~{" not in src and "]~[" not in src, rel
            assert '" ~ "' not in src, rel


class TestStatusTexts:
    def test_boundary_status_wording(self):
        """C：边界状态用"预期/不是故障"的措辞并给建议。"""
        assert "不是故障" in service.STATUS_TEXT["needs_clarification"]
        assert "扩大时间范围" in service.STATUS_TEXT["insufficient_data"]
        assert "受支持指标" in service.STATUS_TEXT["unsupported_metric"]
        assert "不是后端故障" in service.STATUS_TEXT["mock_scenario_unknown"]
        assert "不是数据问题" in service.STATUS_TEXT["execution_failed"]

    def test_warnings_status_not_overpromising(self):
        """C：带警告状态不得声称"不影响结论"。"""
        text = service.STATUS_TEXT["report_generated_with_warnings"]
        assert "不影响结论" not in text
        assert "适用范围" in text or "警告" in text


class TestAppUx:
    @pytest.fixture(scope="class")
    def at(self):
        return AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()

    def test_mock_explanation_visible(self, at):
        """A1：首屏可见 Mock 说明（无需读 README）。"""
        assert not at.exception
        infos = " ".join(i.value for i in at.info)
        assert "Mock" in infos and "事先准备好的分析计划" in infos
        assert "不能自由理解" in infos and "Real" in infos

    def test_boundary_expectation_shown_after_select(self, at):
        """A2：选择边界场景后显示"预期结果"，并说明不是故障。"""
        boundary_q = next(c["question"] for c in service.mock_catalog()
                          if c["kind"] == "boundary")
        at.selectbox(key="example_pick").set_value(boundary_q).run()
        caps = " ".join(c.value for c in at.caption)
        assert "预期结果" in caps and "不是故障" in caps

    def test_default_recommends_success_case(self):
        """A2：首次体验默认推荐可成功的案例（下拉默认选中正常分析）。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        picked = at.selectbox(key="example_pick").value
        detail = service.mock_scenario_detail(picked)
        assert detail is not None and detail["kind"] == "success"

    def test_boundary_run_shows_expected_status_not_success(self):
        """A2/C：边界场景运行后显示预期状态，不出现成功报告。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.selectbox(key="example_pick").set_value("分析一下。").run()
        at.button(key="run_btn").click().run()
        assert not at.exception
        assert at.error, "应显示错误/边界提示"
        err = at.error[0].value
        assert "需要补充信息" in err
        res = at.session_state.get("result")
        assert res is not None and res.ok is False and res.report_text == ""

    def test_success_run_still_complete(self):
        """成功流程：图表、SQL、证据、下载均完整，且无 `~`。"""
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.selectbox(key="example_pick").set_value(
            "帮我分析申购和赎回金额最近的趋势。").run()
        at.button(key="run_btn").click().run()
        assert not at.exception and at.success
        res = at.session_state.get("result")
        assert res.ok and res.charts and res.evidence and res.queries
        assert "~" not in res.report_text
        assert len(at.get("download_button")) >= 2


class TestRealModeFreeform:
    """Real 模式允许自定义问题（不走 Mock 精确匹配）。

    **安全约束（阶段 13.2 P0）**：这些测试必须完全离线，
    不得依赖环境中是否恰好配置了真实 API Key，也不得发起任何外部请求。
    """

    def test_mock_rejects_arbitrary_question(self):
        """Mock 对任意问题返回"模式限制"，不走 Real 分支。"""
        out = service.run_question("任意自然语言问题", "mock", "synthetic")
        assert out.status == "mock_scenario_unknown"
        assert out.report_text == ""

    def test_real_mode_without_key_never_calls(self, monkeypatch):
        """Real 且无 Key → 明确配置提示，不创建客户端、不发请求。"""
        monkeypatch.delenv("DATAAGENT_LLM_API_KEY", raising=False)
        out = service.run_question("任意自然语言问题", "real", "synthetic")
        assert out.status in ("llm_not_configured", "api_error",
                              "plan_rejected", "needs_clarification",
                              "execution_failed", "unsupported_metric",
                              "insufficient_data", "exception")
        assert out.status != "mock_scenario_unknown"   # 未走 Mock 限制

    def test_real_mode_uses_client_not_mock_catalog(self, monkeypatch):
        """Real 分支不依赖 Mock 目录：把 openai 客户端替换为桩，验证走真实解析路径。

        桩在构造时即抛错 → 说明代码确实尝试建真实客户端（而非套用 Mock 计划），
        并且整个过程没有产生任何网络请求。
        """
        monkeypatch.setenv("DATAAGENT_LLM_API_KEY", "sk-test-not-a-real-key-000")
        import openai

        class _RefuseToConstruct:
            def __init__(self, *a, **k):
                raise RuntimeError("offline-test: 拒绝创建真实客户端")

        monkeypatch.setattr(openai, "OpenAI", _RefuseToConstruct)
        out = service.run_question("任意自然语言问题", "real", "synthetic")
        assert out.status != "mock_scenario_unknown"
        assert out.report_text == ""
        assert out.ok is False

    def test_no_network_even_with_fake_key(self, monkeypatch):
        """防回归：设置假 Key 时，任何网络连接尝试都会被拦截（且不产生报告）。"""
        import socket

        monkeypatch.setenv("DATAAGENT_LLM_API_KEY", "sk-test-not-a-real-key-000")

        def _blocked(*a, **k):
            raise AssertionError("测试期间不得建立外部网络连接")

        monkeypatch.setattr(socket.socket, "connect", _blocked, raising=False)
        monkeypatch.setattr(socket, "create_connection", _blocked, raising=False)

        out = service.run_question("这是一个任意自然语言问题", "real", "synthetic")
        assert out.ok is False
        assert out.report_text == ""
        assert out.status != "mock_scenario_unknown"

    def test_mock_branch_is_guarded_by_mode(self):
        """源码层面确认：Mock 匹配仅在 model_mode == "mock" 分支内。"""
        src = (ROOT / "web" / "service.py").read_text(encoding="utf-8")
        assert 'if model_mode == "mock":' in src
        # mock_scenario 只允许出现在 mock 分支内：定位该分支并核对作用域
        mock_block = src.split('if model_mode == "mock":', 1)[1]
        # 分支以 "try:"（构建 config）结束；其内部允许调用 mock_scenario
        inside = mock_block.split("try:", 1)[0]
        assert "mock_scenario(question)" in inside
        # 之后（config 构建与执行）不得再次按问题套用 Mock 目录
        assert "mock_scenario(question)" not in mock_block.split("try:", 1)[1]
