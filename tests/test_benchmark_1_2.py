"""阶段 12 任务 A：真实模式判定、计量、预算的 Fake SDK 回归测试（不调用真实 API）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataagent.evaluation import EvaluationRunner, load_benchmark
from dataagent.evaluation.runner import UsageTrackingClient

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmark"


class _Usage:
    def __init__(self, p=10, c=5):
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, content: str, usage: _Usage | None):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": "stop"})()]
        self.usage = usage


class _Inner:
    """可控 Fake SDK：按队列返回内容/异常/usage。"""

    def __init__(self, items):
        self.items = list(items)
        self.model = "fake-real-model"
        self.chat = type("C", (), {})()
        self.chat.completions = self
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        item = self.items[min(self.calls - 1, len(self.items) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _client(items) -> UsageTrackingClient:
    return UsageTrackingClient(_Inner(items))


def _cases(*ids):
    cases, answers = load_benchmark()
    picked = [c for c in cases if c["case_id"] in set(ids)]
    return picked, answers


def _plan_json(case_id: str) -> str:
    cases, _ = load_benchmark()
    plan = next(c["fixture_plan"] for c in cases if c["case_id"] == case_id)
    return json.dumps(plan, ensure_ascii=False)


class TestRealModeVerdict:
    def test_legal_plan_succeeds(self, tmp_path):
        """① 真实模式返回合法计划 → 正常成功，且请求计入。"""
        cases, answers = _cases("T01")
        client = _client([_Resp(_plan_json("T01"), _Usage())])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, summary = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.report_generated and r.behavior_correct is True
        assert r.request_count >= 1 and r.response_count >= 1
        assert r.negative_basis is None      # 有效案例无负面依据
        assert r.usage_complete is True
        assert r.cost.estimated_cost is None  # 无价格表 → N/A

    def test_correct_rejection_from_real_output(self, tmp_path):
        """② 真实模式：模型输出不支持指标 → 正确拒绝，依据为真实输出。"""
        cases, answers = _cases("N01")
        client = _client([_Resp(_plan_json("N01"), _Usage()),
                          _Resp(_plan_json("N01"), _Usage())])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.status not in ("report_generated", "report_generated_with_warnings")
        assert r.behavior_correct is True
        assert r.negative_basis == "real_model_output"
        # 真实模式下不得出现"注入计划 Schema 拒绝"这一证据
        assert "schema_rejected" not in {c.kind for c in r.checks}

    def test_unexpected_success_not_overwritten(self, tmp_path):
        """③ 负面案例却成功生成报告 → 必须判错，不得被 check 覆盖。"""
        # 用"数据不足"案例（单日）注入全期合法计划 → Agent 会成功，
        # 但预期行为是拒绝，因此必须判为"意外成功"
        cases, answers = _cases("N06")
        client = _client([_Resp(_plan_json("T01"), _Usage())])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, summary = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.report_generated is True          # 确实意外成功
        assert r.behavior_correct is False         # 但判定必须为错
        assert summary.negative_correct == 0

    def test_illegal_plan_rejected_without_fixture(self, tmp_path):
        """④ 真实模型输出非法计划（含 sql 字段）→ 拒绝；不依赖 fixture_plan。"""
        cases, answers = _cases("N14")
        bad = {"intent": "x", "analysis_type": "time_trend",
               "metrics": [{"metric_id": "purchase_amount"}],
               "time_range": {"type": "full_range"}, "group_by": [], "filters": [],
               "statistics": ["SUM"], "outputs": {"summary_table": True},
               "unresolved": [], "notes": [],
               "sql": "DROP TABLE user_balance_table"}
        client = _client([_Resp(json.dumps(bad), _Usage())])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.status not in ("report_generated", "report_generated_with_warnings")
        assert r.behavior_correct is True
        assert r.negative_basis == "real_model_output"
        # 真实模式下不得出现"注入计划 Schema 拒绝"这类离线证据
        assert "schema_rejected" not in {c.kind for c in r.checks}

    def test_schema_rejected_check_marked_unverifiable_in_real(self, tmp_path):
        """⑤ Schema 类负面案例：真实模式下该 check 标记为不可验证（不凭标签判对）。"""
        cases, answers = _cases("N15")
        client = _client([_Resp(_plan_json("N15"), _Usage())])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.unverifiable_checks, "应记录不可验证项"
        assert all("schema_rejected" in x for x in r.unverifiable_checks)

    def test_schema_repair_disabled_limits_calls(self, tmp_path):
        """⑤ 关闭 Schema 修复重试 → 非法输出只调用一次模型。"""
        cases, answers = _cases("N01")
        bad = _plan_json("N01")
        bad = bad.replace('"unresolved": []', '"unresolved": [] , "bad_field": 1')
        client = _client([_Resp(bad, _Usage())])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        # 评测默认关闭修复重试（allow_schema_repair=False）
        assert client.counters()["requests"] == 1


class TestMetering:
    def test_mixed_usage_marks_incomplete(self, tmp_path):
        """⑥ 有/无 usage 混合 → usage_complete=False，成本不编造。"""
        cases, answers = _cases("T01")
        # 第一次带 usage，第二次缺失（若发生修复重试）
        client = _client([_Resp(_plan_json("T01"), _Usage(100, 20)),
                          _Resp(_plan_json("T01"), None)])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, summary = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.request_count >= 1
        if r.request_count > 1:
            assert r.usage_complete is False
            assert r.cost.estimated_cost is None

    def test_exception_still_records_requests(self, tmp_path):
        """⑦ run_agent 抛异常时仍记录请求（finally）。"""
        cases, answers = _cases("T01")
        client = _client([ConnectionError("api down"), ConnectionError("api down")])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.request_count >= 1                 # finally 中记录
        assert r.response_count == 0                # 没有成功响应
        assert r.status in ("exception", "plan_rejected", "needs_clarification",
                            "execution_failed")

    def test_budget_stops_further_requests(self, tmp_path):
        """⑧ 达到调用预算 → 剩余案例标记 skipped_budget，不再请求。"""
        cases, answers = _cases("T01", "T02", "T03")
        client = _client([_Resp(_plan_json("T01"), _Usage())])
        runner = EvaluationRunner(cases, answers, model_mode="real",
                                  real_client=client, output_root=tmp_path,
                                  quiet=True, max_requests=1)
        try:
            results, summary = runner.run()
        finally:
            runner.cleanup()
        assert client.counters()["requests"] <= 1
        skipped = [r for r in results if r.status == "skipped_budget"]
        assert len(skipped) >= 1
        assert summary.total_cases == 3


class TestOfflineStillBasisInjected:
    def test_offline_uses_injected_plan_basis(self, tmp_path):
        """离线模式仍以注入计划为依据（并明确标注）。"""
        cases, answers = _cases("N01")
        runner = EvaluationRunner(cases, answers, model_mode="mock",
                                  output_root=tmp_path, quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.negative_basis == "offline_injected_plan"
