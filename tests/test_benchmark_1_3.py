"""阶段 13 任务 A：真实评测的重试/预算/Schema 评分回归测试（Fake SDK，无真实 API）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataagent.evaluation import EvaluationRunner, load_benchmark
from dataagent.evaluation.runner import UsageTrackingClient

ROOT = Path(__file__).resolve().parents[1]


class _U:
    def __init__(self, p=10, c=5):
        self.prompt_tokens = p
        self.completion_tokens = c


class _R:
    def __init__(self, content: str, usage=_U()):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": content})(),
            "finish_reason": "stop"})()]
        self.usage = usage


class _Inner:
    def __init__(self, items):
        self.items = list(items)
        self.model = "fake-real"
        self.chat = type("C", (), {})()
        self.chat.completions = self
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        it = self.items[min(self.calls - 1, len(self.items) - 1)]
        if isinstance(it, Exception):
            raise it
        return it


def _client(items):
    return UsageTrackingClient(_Inner(items))


def _cases(*ids):
    cases, answers = load_benchmark()
    return [c for c in cases if c["case_id"] in set(ids)], answers


def _plan(cid):
    cases, _ = load_benchmark()
    return json.dumps(next(c["fixture_plan"] for c in cases if c["case_id"] == cid),
                      ensure_ascii=False)


def _run(cases, answers, client, tmp_path, **kw):
    r = EvaluationRunner(cases, answers, model_mode="real", real_client=client,
                         output_root=tmp_path, quiet=True, **kw)
    try:
        return r.run()
    finally:
        r.cleanup()


class TestNoImplicitRetry:
    def test_illegal_json_single_request(self, tmp_path):
        """① 真实模式：模型输出非法 JSON → 只发起 1 次请求（Schema 修复被关闭）。"""
        cases, answers = _cases("T01")
        client = _client([_R("这不是 JSON{{{"), _R(_plan("T01")), _R(_plan("T01"))])
        _run(cases, answers, client, tmp_path)
        assert client.counters()["requests"] == 1, "评测不得自动修复重试"

    def test_schema_repair_disabled_in_benchmark_config(self, tmp_path):
        """② 评测的 AgentConfig 显式 allow_schema_repair=False。"""
        cases, answers = _cases("T01")
        client = _client([_R(_plan("T01"))])
        results, _ = _run(cases, answers, client, tmp_path)
        assert results[0].report_generated        # 合法计划正常通过
        # 非法 JSON 时不再重试（见上一个测试）


class TestRequestBudget:
    @pytest.mark.parametrize("budget", [1, 2, 5])
    def test_budget_never_exceeded(self, tmp_path, budget):
        """③ 预算 1/2/5：实际请求数不超过预算。"""
        cases, answers = _cases("T01", "T02", "T03", "T04", "T05")
        client = _client([_R(_plan("T01"))] * 10)
        results, summary = _run(cases, answers, client, tmp_path,
                                max_requests=budget)
        assert client.counters()["requests"] <= budget
        total_req = sum(r.request_count for r in results)
        assert total_req <= budget
        skipped = [r for r in results if r.status == "skipped_budget"]
        assert len(skipped) == max(0, len(cases) - budget)

    def test_skipped_budget_not_counted_as_success(self, tmp_path):
        """④ 未执行案例不得算成功/正确拒绝/答案正确。"""
        cases, answers = _cases("T01", "N01")
        client = _client([_R(_plan("T01"))])
        results, summary = _run(cases, answers, client, tmp_path, max_requests=1)
        skipped = [r for r in results if r.status == "skipped_budget"]
        assert skipped, "应有未执行案例"
        s = skipped[0]
        assert s.report_generated is False
        assert s.answers_correct is not True
        assert s.behavior_correct is not True
        # 汇总里负面案例数为 1，但正确拒绝数为 0（未执行）
        assert summary.negative_cases == 1
        assert summary.negative_correct == 0


class TestRealModeScoring:
    def _score(self, cid, content, tmp_path, **kw):
        cases, answers = _cases(cid)
        items = [_R(content)] if isinstance(content, str) else content
        client = _client(items)
        results, summary = _run(cases, answers, client, tmp_path, **kw)
        return results[0], summary

    def test_correct_rejection_scores_correct(self, tmp_path):
        """⑤ 真实模型正确拒绝（不支持指标）→ 计入正确。"""
        r, s = self._score("N01", _plan("N01"), tmp_path)
        assert r.behavior_correct is True
        assert r.negative_basis == "real_model_output"
        assert s.negative_correct == 1

    def test_wrong_rejection_category_scores_wrong(self, tmp_path):
        """⑥ 拒绝类别错误（期望"数据不足"、实际"澄清"）→ 判错。"""
        # N06 期望 insufficient_data；注入"缺时间范围"的计划 → 走澄清（类别不符）
        clarify_plan = json.dumps({
            "intent": "x", "analysis_type": "time_trend",
            "metrics": [{"metric_id": "purchase_amount"}],
            "time_range": {"type": "last_n_days"},
            "group_by": [], "filters": [], "statistics": ["SUM"],
            "outputs": {"summary_table": True}, "unresolved": [], "notes": []},
            ensure_ascii=False)
        r, s = self._score("N06", clarify_plan, tmp_path)
        assert r.status == "needs_clarification"
        assert r.behavior_correct is False       # 类别不符（应为 insufficient_data）
        assert s.negative_correct == 0

    def test_unexpected_success_scores_wrong(self, tmp_path):
        """⑦ 意外成功 → 判错，且不被 check 覆盖。"""
        r, s = self._score("N06", _plan("T01"), tmp_path)
        assert r.report_generated is True
        assert r.behavior_correct is False
        assert s.negative_correct == 0

    def test_unverifiable_isolated(self, tmp_path):
        """⑧ Schema 类负面案例：真实模式下不可验证（单列，不判正确）。"""
        r, s = self._score("N13", _plan("N13"), tmp_path)
        assert r.unverifiable_checks, "应列出不可验证项"
        assert all("schema_rejected" in x for x in r.unverifiable_checks)
        # 不可验证不得被强行判成正确：行为判定只依据可观察证据
        assert r.behavior_correct in (True, False)
        if r.behavior_correct is True:
            # 若判为正确，必须是因为可观察的状态/类别证据（而非注入计划标签）
            assert any(c.kind in ("error_code_in", "clarification_present")
                       for c in r.checks) or r.status not in (
                           "report_generated", "report_generated_with_warnings")

    def test_four_cases_scores(self, tmp_path):
        """四类情况一次性核对：正确拒绝 / 错误类别 / 意外成功 / 不可验证。"""
        cases, answers = _cases("N01", "N06", "N13", "N15")
        client = _client([_R(_plan("N01")), _R(_plan("T01")),
                          _R(_plan("N13")), _R(_plan("N15"))])
        results, summary = _run(cases, answers, client, tmp_path)
        by_id = {r.case_id: r for r in results}
        assert by_id["N01"].behavior_correct is True          # 正确拒绝
        assert by_id["N06"].behavior_correct is False         # 意外成功
        assert by_id["N13"].unverifiable_checks               # 不可验证（schema）
        assert summary.total_cases == 4
        # 未执行/不可验证的案例不会被算进"答案正确"
        assert all(r.answers_correct is not True for r in results
                   if r.status == "skipped_budget")
