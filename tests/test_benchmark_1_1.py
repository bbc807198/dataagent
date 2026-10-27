"""阶段 11.1 回归测试：评测口径、Token 归属、负面依据、Golden 篡改检测。"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from dataagent.evaluation import CaseResult, EvaluationRunner, TokenUsage, load_benchmark
from dataagent.evaluation.metrics import compare_check, summarize
from dataagent.evaluation.models import CheckResult
from dataagent.evaluation.runner import (FakePlanClient, UsageTrackingClient,
                                         estimate_cost)

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmark"


def _res(cid="X", behavior="success", **kw):
    r = CaseResult(case_id=cid, question="q", category="c",
                   expected_behavior=behavior, expected_status="ok",
                   expected_analysis_type="time_trend", expected_metrics=[])
    r.analysis_succeeded = kw.get("analysis", True)
    r.query_executed = kw.get("query", True)
    r.report_generated = kw.get("report", True)
    r.behavior_correct = kw.get("behavior_correct", behavior == "success")
    r.answers_verified = kw.get("verified", False)
    r.answers_correct = kw.get("correct")
    r.verifiable_expected = kw.get("verifiable", False)
    r.latency_ms = kw.get("lat", 10.0)
    return r


class TestAnswerVsExecution:
    def test_status_only_is_not_numeric(self):
        """① 只有 status_only 时不算数值答案正确。"""
        r = _res(verified=False, correct=None, verifiable=True)
        r.checks = [CheckResult("status_only", True, "analysis_succeeded", "ok")]
        # runner 的 numeric 过滤逻辑：status_only 被排除
        numeric = [c for c in r.checks
                   if c.kind not in ("error_code_in", "clarification_present",
                                     "schema_rejected", "status_only")]
        assert numeric == []
        s = summarize([r], "run", "mock", "synthetic", {},
                      extra_verifiable_ids={"X"})
        # 分母固定为 1（标准答案声明可验证），但分子为 0（未验证数值）
        assert s.verifiable_cases == 1
        assert s.answers_correct == 0
        assert s.answer_accuracy == 0.0

    def test_denominator_fixed_on_failure(self):
        """② 可验证案例执行失败时分母不变，按答案未正确处理。"""
        ok1 = _res("A1", verified=True, correct=True, verifiable=True)
        failed = _res("A2", verified=False, correct=None, verifiable=True,
                      report=False, query=False, analysis=False,
                      behavior_correct=False)
        s = summarize([ok1, failed], "run", "mock", "synthetic", {},
                      extra_verifiable_ids={"A1", "A2"})
        assert s.verifiable_cases == 2          # 分母不因失败缩小
        assert s.answers_correct == 1
        assert s.answer_accuracy == 0.5
        assert s.execution_only_cases == 0

    def test_wrong_analysis_type_or_metric_fails(self):
        """③ 错误分析类型或指标不能被误判为正确。"""
        from types import SimpleNamespace
        analysis = SimpleNamespace(
            analysis_type="group_ranking",
            metrics=[SimpleNamespace(metric_id="redeem_amount")],
            statistics=None)
        r = _res()
        bad_type = compare_check({"kind": "analysis_type_is", "value": "time_trend"},
                                 r, analysis, "report_generated", None, [], False)
        bad_metric = compare_check(
            {"kind": "metrics_exact", "metrics": ["purchase_amount"]},
            r, analysis, "report_generated", None, [], False)
        assert bad_type.passed is False and bad_metric.passed is False
        good_type = compare_check({"kind": "analysis_type_is",
                                   "value": "group_ranking"},
                                  r, analysis, "report_generated", None, [], False)
        assert good_type.passed is True

    def test_execution_only_excluded_from_accuracy(self):
        """仅验证执行成功的案例不计入答案正确率分子分母。"""
        a = _res("A1", verified=True, correct=True, verifiable=True)
        b = _res("A2", verified=True, correct=True, verifiable=False)   # 仅执行
        s = summarize([a, b], "run", "mock", "synthetic", {},
                      extra_verifiable_ids={"A1"})
        assert s.verifiable_cases == 1 and s.execution_only_cases == 1
        assert s.answers_correct == 1 and s.answer_accuracy == 1.0


class _FakeUsageResponse:
    def __init__(self, p, c, has_usage=True):
        self.usage = (type("U", (), {"prompt_tokens": p, "completion_tokens": c})()
                      if has_usage else None)


class _FakeInner:
    """模拟 SDK：按预设序列返回不同 token 用量。"""

    def __init__(self, seq):
        self.seq = list(seq)
        self.model = "fake-model"
        self.chat = type("C", (), {})()
        self.chat.completions = self
        self.i = 0

    def create(self, **kw):
        item = self.seq[self.i]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return item


class TestTokenAccounting:
    def test_per_case_token_not_batch_copied(self):
        """④ 三次不同用量的调用 → 单案例 Token 归属正确、总量=各次之和。"""
        inner = _FakeInner([_FakeUsageResponse(100, 20),
                            _FakeUsageResponse(200, 40),
                            _FakeUsageResponse(300, 60)])
        client = UsageTrackingClient(inner)
        base = client.counters()
        client.create()
        d1 = UsageTrackingClient.delta_usage(base, client.counters(), "fake-model")
        base2 = client.counters()
        client.create()
        d2 = UsageTrackingClient.delta_usage(base2, client.counters(), "fake-model")
        base3 = client.counters()
        client.create()
        d3 = UsageTrackingClient.delta_usage(base3, client.counters(), "fake-model")

        assert (d1.input_tokens, d1.output_tokens) == (100, 20)
        assert (d2.input_tokens, d2.output_tokens) == (200, 40)
        assert (d3.input_tokens, d3.output_tokens) == (300, 60)
        # 总量 = 各案例增量之和（不是把批次值复制给每个案例）
        assert sum(x.input_tokens for x in (d1, d2, d3)) == 600
        assert sum(x.output_tokens for x in (d1, d2, d3)) == 120
        assert client.counters()["requests"] == 3

    def test_missing_usage_not_fabricated(self):
        """⑤ API 未返回 usage → Token 为 None，成本 N/A（不伪造 0）。"""
        inner = _FakeInner([_FakeUsageResponse(0, 0, has_usage=False)])
        client = UsageTrackingClient(inner)
        base = client.counters()
        client.create()
        d = UsageTrackingClient.delta_usage(base, client.counters(), "fake-model")
        assert d.total_tokens is None and d.input_tokens is None
        cost = estimate_cost(d, None)
        assert cost.estimated_cost is None
        assert cost.as_dict()["estimated_cost"] == "N/A"

    def test_request_vs_response_counting(self):
        """失败调用计入 requests，不计入 responses。"""
        inner = _FakeInner([ConnectionError("boom"),
                            _FakeUsageResponse(50, 10)])
        client = UsageTrackingClient(inner)
        with pytest.raises(ConnectionError):
            client.create()
        c1 = client.counters()
        assert c1["requests"] == 1 and c1["responses"] == 0
        client.create()
        c2 = client.counters()
        assert c2["requests"] == 2 and c2["responses"] == 1
        d = UsageTrackingClient.delta_usage(c1, c2, "fake-model")
        assert d.calls == 1 and d.total_tokens == 60

    def test_batch_equals_sum_of_cases(self):
        """⑥ 批次统计与每案例日志一致。"""
        inner = _FakeInner([_FakeUsageResponse(10, 5), _FakeUsageResponse(20, 10)])
        client = UsageTrackingClient(inner)
        snapshots = [client.counters()]
        for _ in range(2):
            client.create()
            snapshots.append(client.counters())
        deltas = [UsageTrackingClient.delta_usage(snapshots[i], snapshots[i + 1],
                                                 "fake-model")
                  for i in range(2)]
        total = client.counters()
        assert sum(d.total_tokens for d in deltas) == (
            total["input"] + total["output"])


class TestNegativeBasis:
    def test_offline_marks_injected_plan_basis(self, tmp_path):
        """⑦ 离线模式的负面案例标记为注入计划 Schema 测试，不冒充真实模型判断。"""
        cases, answers = load_benchmark()
        subset = [c for c in cases if c["case_id"] == "N01"]
        runner = EvaluationRunner(subset, answers, model_mode="mock",
                                  output_root=tmp_path, quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        r = results[0]
        assert r.negative_basis == "offline_injected_plan"
        # 报告中也明确该限定
        text = (tmp_path / "latest" / "summary.md").read_text(encoding="utf-8")
        assert "offline_injected_plan" in text
        assert "不证明真实模型抗注入能力" in text


class TestGoldenTamperDetection:
    @staticmethod
    def _load_verifier():
        spec = importlib.util.spec_from_file_location(
            "verify_golden_mod", ROOT / "scripts" / "verify_golden.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_correct_golden_passes(self):
        """⑧ 未篡改时独立校验通过。"""
        mod = self._load_verifier()
        res = mod.verify()
        assert res["checks_failed"] == 0
        assert res["checks_total"] > 100

    def test_tampered_value_detected(self, tmp_path):
        """⑧ 标准答案数值被改错 → 独立校验必须失败。"""
        tmp_bench = tmp_path / "benchmark"
        tmp_bench.mkdir()
        shutil.copy(BENCH / "questions.jsonl", tmp_bench / "questions.jsonl")
        golden = json.loads((BENCH / "golden_answers.json").read_text(encoding="utf-8"))
        # 篡改 T01 的 series_sum（2850 → 9999）
        for ch in golden["answers"]["T01"]["checks"]:
            if ch["kind"] == "series_sum":
                ch["value"] = 9999.0
        (tmp_bench / "golden_answers.json").write_text(
            json.dumps(golden, ensure_ascii=False), encoding="utf-8")

        mod = self._load_verifier()
        mod.BENCH = tmp_bench
        res = mod.verify()
        assert res["checks_failed"] >= 1
        assert any(f["case_id"] == "T01" and f["kind"] == "series_sum"
                   for f in res["failures"])

    def test_ranking_tamper_detected(self, tmp_path):
        """改错排名首名 → 独立校验失败。"""
        tmp_bench = tmp_path / "benchmark"
        tmp_bench.mkdir()
        shutil.copy(BENCH / "questions.jsonl", tmp_bench / "questions.jsonl")
        golden = json.loads((BENCH / "golden_answers.json").read_text(encoding="utf-8"))
        for ch in golden["answers"]["R01"]["checks"]:
            if ch["kind"] == "ranking_top1_value":
                ch["value"] = 1.0
        (tmp_bench / "golden_answers.json").write_text(
            json.dumps(golden, ensure_ascii=False), encoding="utf-8")
        mod = self._load_verifier()
        mod.BENCH = tmp_bench
        res = mod.verify()
        assert res["checks_failed"] >= 1


class TestOfflineIntegrity:
    def test_all_verifiable_cases_have_numeric_checks(self):
        """35 个有效案例全部具备独立数值标准（无 status_only-only 案例）。"""
        _, answers = load_benchmark()
        numeric_kinds = {"series_sum", "metric_sum", "first_value", "last_value",
                         "period_mean", "term_period_mean", "max_drop", "max_rise",
                         "max_abs", "point_count", "series_count", "ranking_count",
                         "ranking_top1_group", "ranking_top1_value",
                         "window_main_total", "window_comparison_total",
                         "window_main_value", "window_comparison_value",
                         "window_main_value_nongrouped",
                         "window_comparison_value_nongrouped",
                         "window_main_value_nongrouped",
                         "window_comparison_value_nongrouped",
                         "window_missing_in_comparison", "distribution_total",
                         "distribution_sample_size", "distribution_value_stat",
                         "anomaly_sample_size", "anomaly_center", "anomaly_mad",
                         "anomaly_count", "anomaly_dates", "correlation_pair",
                         "correlation_pairs_count", "correlation_coefficient",
                         "descriptive_value"}
        for cid, a in answers.items():
            if a["expected_behavior"] != "success":
                continue
            kinds = {c["kind"] for c in a["checks"]}
            assert kinds & numeric_kinds, f"{cid} 缺少独立数值标准"
            assert a.get("verifiable") is True, f"{cid} 未标记可验证"

    def test_every_valid_case_checks_identity(self):
        """⑥ 所有有效案例都校验 analysis_type 与指标 ID 一致性。"""
        _, answers = load_benchmark()
        for cid, a in answers.items():
            if a["expected_behavior"] != "success":
                continue
            kinds = {c["kind"] for c in a["checks"]}
            assert "analysis_type_is" in kinds, cid
            assert "metrics_exact" in kinds, cid
