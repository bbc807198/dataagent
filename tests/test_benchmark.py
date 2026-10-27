"""阶段 11：Benchmark / 评测体系测试。

全部使用合成数据与 Fake 客户端，**不发起任何真实 API 请求**。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dataagent.evaluation import (CaseResult, CostInfo, EvaluationRunner,
                                  TokenUsage, load_benchmark)
from dataagent.evaluation.metrics import compare_check, percentile, summarize
from dataagent.evaluation.models import CheckResult
from dataagent.evaluation.runner import (EXCEPTION_CLASSES, FakePlanClient,
                                         classify_exception, estimate_cost)

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmark"


@pytest.fixture(scope="module")
def bench():
    cases, answers = load_benchmark()
    return cases, answers


# ---------------------------------------------------------------- 问题集
class TestQuestionSet:
    def test_count_and_uniqueness(self, bench):
        """① 50 道题、case_id 唯一、字段齐备。"""
        cases, _ = bench
        assert len(cases) == 50
        ids = [c["case_id"] for c in cases]
        assert len(ids) == len(set(ids))
        required = {"case_id", "question", "category", "expected_behavior",
                    "expected_analysis_type", "expected_metrics",
                    "expected_status", "golden_reference", "tags",
                    "fixture_plan"}
        for c in cases:
            assert required <= set(c), f"{c['case_id']} 缺字段 {required - set(c)}"
            assert c["golden_reference"].endswith(c["case_id"])

    def test_valid_negative_split_and_coverage(self, bench):
        """有效 ≥35、负面 ≥15；七类分析全覆盖。"""
        cases, _ = bench
        valid = [c for c in cases if c["expected_behavior"] == "success"]
        neg = [c for c in cases if c["expected_behavior"] != "success"]
        assert len(valid) >= 35
        assert len(neg) >= 15
        types = {c["expected_analysis_type"] for c in valid}
        assert {"time_trend", "window_comparison", "group_ranking",
                "distribution", "anomaly", "correlation",
                "descriptive"} <= types

    def test_negative_categories_present(self, bench):
        cases, _ = bench
        cats = {c["category"] for c in cases if c["expected_behavior"] != "success"}
        for need in ("unsupported_metric", "clarification", "insufficient_data",
                     "date_out_of_range", "metric_conflict", "schema_reject",
                     "prompt_injection"):
            assert need in cats, f"缺少负面类别 {need}"


# ---------------------------------------------------------------- 标准答案
class TestGoldenAnswers:
    def test_independent_recalculation(self, bench):
        """② 标准答案与独立 SQL 重算一致（抽样关键值）。"""
        _, answers = bench
        # 全期申购合计（独立 SQL 计算所得）
        sums = [ch for ch in answers["T01"]["checks"] if ch["kind"] == "series_sum"]
        assert sums and abs(sums[0]["value"] - 2850) < 1e-9
        # 排名首名城市与值
        top = [ch for ch in answers["R01"]["checks"]
               if ch["kind"] == "ranking_top1_value"]
        assert top and abs(top[0]["value"] - 1500) < 1e-9
        # 窗口差（主 1750 / 对比 1100）；注意 checks 现在含无 value 的口径检查
        w = {ch["kind"]: ch.get("value") for ch in answers["W02"]["checks"]}
        assert abs(w["window_main_total"] - 1750) < 1e-9
        assert abs(w["window_comparison_total"] - 1100) < 1e-9

    def test_tolerance_declared(self, bench):
        cases, answers = bench
        meta = json.loads((BENCH / "golden_answers.json").read_text(encoding="utf-8"))
        assert meta["method"]["tolerance"]["amount_abs"] == 0.01
        # 数值 checks 必须显式声明容差
        for cid, a in answers.items():
            for ch in a["checks"]:
                if ch["kind"] in ("series_sum", "metric_sum", "first_value",
                                  "last_value", "period_mean", "ranking_top1_value",
                                  "window_main_total", "window_comparison_total",
                                  "descriptive_value", "term_period_mean"):
                    assert "tol" in ch, f"{cid}/{ch['kind']} 未声明容差"


# ---------------------------------------------------------------- 数值比较
class TestComparison:
    def _res(self, **kw):
        r = CaseResult(case_id="X", question="q", category="c",
                       expected_behavior="success", expected_status="ok",
                       expected_analysis_type="time_trend", expected_metrics=[])
        r.analysis_succeeded = kw.get("analysis_succeeded", True)
        r.status = kw.get("status", "report_generated")
        return r

    def test_numeric_tolerance(self):
        """⑥ 数值容差：0.01 内通过，超出失败。"""
        from types import SimpleNamespace
        series = SimpleNamespace(kind="trend", points=[], total=2850.005,
                                 period_mean=None, daily_changes=[])
        analysis = SimpleNamespace(statistics=series)
        spec = {"kind": "series_sum", "value": 2850.0, "tol": 0.01}
        assert compare_check(spec, self._res(), analysis, "report_generated",
                             None, [], False).passed
        spec2 = {"kind": "series_sum", "value": 2850.0, "tol": 0.001}
        assert not compare_check(spec2, self._res(), analysis, "report_generated",
                                 None, [], False).passed

    def test_date_strict(self):
        """⑥ 日期严格比较（不允许容差）。"""
        from types import SimpleNamespace
        change = SimpleNamespace(date="2024-01-05", previous_value=200.0,
                                 current_value=0.0, change_rate=-1.0)
        series = SimpleNamespace(kind="trend", points=[], daily_changes=[change],
                                 period_mean=None, total=None)
        analysis = SimpleNamespace(statistics=series)
        ok = {"kind": "max_drop", "date": "2024-01-05", "rate": -1.0, "tol": 1e-6}
        bad = {"kind": "max_drop", "date": "2024-01-04", "rate": -1.0, "tol": 1e-6}
        assert compare_check(ok, self._res(), analysis, "x", None, [], False).passed
        assert not compare_check(bad, self._res(), analysis, "x", None, [], False).passed


# ---------------------------------------------------------------- 指标计算
class TestMetricsMath:
    def test_percentile(self):
        """⑩ P50/P95 计算正确（线性插值）。"""
        xs = [1, 2, 3, 4, 5]
        assert percentile(xs, 50) == 3.0
        assert percentile(xs, 0) == 1.0
        assert percentile(xs, 100) == 5.0
        assert abs(percentile(xs, 95) - 4.8) < 1e-9
        assert percentile([], 50) is None

    def _mk(self, cid, behavior, expected_status="ok", report=True,
            query=True, analysis=True, verified=None, correct=None, lat=10.0):
        r = CaseResult(case_id=cid, question="q", category="c",
                       expected_behavior=behavior, expected_status=expected_status,
                       expected_analysis_type="time_trend", expected_metrics=[])
        r.report_generated = report
        r.query_executed = query
        r.analysis_succeeded = analysis
        r.latency_ms = lat
        r.behavior_correct = correct if correct is not None else (behavior == "success")
        if verified is not None:
            r.answers_verified = verified
        r.answers_correct = correct if verified else None
        return r

    def test_denominators(self):
        """⑦⑧⑨ 成功率/正确率/拒绝率分母正确，误拒绝单独统计。"""
        results = [
            self._mk("A1", "success", verified=True, correct=True, lat=10),
            self._mk("A2", "success", verified=True, correct=False, lat=20),
            self._mk("A3", "success", report=False, query=False, analysis=False,
                     correct=False, lat=30),
            self._mk("N1", "reject", expected_status="plan_rejected", correct=True),
            self._mk("N2", "reject", expected_status="plan_rejected", correct=False),
        ]
        # 11.1：可验证分母由标准答案**事前**声明（此处声明 A1/A2），不因失败缩小
        s = summarize(results, "run", "mock", "synthetic", {},
                      extra_verifiable_ids={"A1", "A2"})
        assert s.valid_cases == 3 and s.negative_cases == 2
        # 查询成功率：3 个有效案例中 2 个 SQL+统计都成功
        assert s.query_success == 2
        assert abs(s.query_success_rate - 2 / 3) < 1e-9
        # 报告生成成功率单独口径
        assert s.report_generated == 2
        # 答案正确率：分母固定为 2（A2 执行失败也不缩小），1 个正确
        assert s.verifiable_cases == 2 and s.answers_correct == 1
        assert abs(s.answer_accuracy - 0.5) < 1e-9
        # 行为正确率（全部案例）
        assert s.behavior_accuracy is not None
        # 正确拒绝率：2 个负面案例中 1 个正确
        assert s.negative_correct == 1
        assert abs(s.negative_accuracy - 0.5) < 1e-9
        # 误拒绝：3 个有效案例中 1 个被判失败
        assert s.false_rejections == 1
        assert abs(s.false_rejection_rate - 1 / 3) < 1e-9

    def test_latency_split(self):
        results = [self._mk("A1", "success", correct=True, lat=10),
                   self._mk("A2", "success", correct=False, lat=30)]
        s = summarize(results, "r", "mock", "synthetic", {})
        assert s.latency_min_ms == 10 and s.latency_max_ms == 30
        assert s.latency_avg_success_ms == 10
        assert s.latency_avg_failure_ms == 30


# ---------------------------------------------------------------- Token / 成本
class TestTokenAndCost:
    def test_token_not_fabricated(self):
        """⑪ Token 缺失时为 None（不填 0）。"""
        u = TokenUsage()
        assert u.total_tokens is None and u.applicable is False
        d = u.as_dict()
        assert d["total_tokens"] is None

    def test_cost_unknown_is_na(self):
        """⑫ 价格未知时成本为 None（不猜测）。"""
        usage = TokenUsage(model="m", calls=1, input_tokens=1000,
                           output_tokens=500, total_tokens=1500, applicable=True)
        c1 = estimate_cost(usage, None)
        assert c1.estimated_cost is None and c1.as_dict()["estimated_cost"] == "N/A"
        c2 = estimate_cost(TokenUsage(model="m", applicable=True), {"models": {}})
        assert c2.estimated_cost is None
        # 有可信价格表时才计算
        pricing = {"models": {"m": {"input_per_1k": 0.001, "output_per_1k": 0.002}},
                   "currency": "USD", "unit": "per 1K tokens", "source": "test",
                   "date": "2026-10-26"}
        c3 = estimate_cost(usage, pricing)
        assert abs(c3.estimated_cost - (1.0 * 0.001 + 0.5 * 0.002)) < 1e-9
        assert c3.currency == "USD"


# ---------------------------------------------------------------- 端到端（离线）
class TestOfflineRun:
    @pytest.fixture(scope="class")
    def run_result(self, tmp_path_factory):
        cases, answers = load_benchmark()
        subset = [c for c in cases if c["case_id"] in ("T01", "R01", "N01", "N06")]
        out = tmp_path_factory.mktemp("bench_out")
        runner = EvaluationRunner(subset, answers, model_mode="mock",
                                  output_root=out, quiet=True)
        try:
            results, summary = runner.run()
        finally:
            runner.cleanup()
        return results, summary, out / "latest"

    def test_valid_cases_succeed(self, run_result):
        """③ 有效问题成功（含数值校验）。"""
        results, _, _ = run_result
        t01 = next(r for r in results if r.case_id == "T01")
        assert t01.report_generated and t01.query_executed
        assert t01.answers_correct is True
        r01 = next(r for r in results if r.case_id == "R01")
        assert r01.behavior_correct is True

    def test_unsupported_rejected(self, run_result):
        """④ 不支持指标被正确拒绝/澄清。"""
        results, _, _ = run_result
        n01 = next(r for r in results if r.case_id == "N01")
        assert n01.status not in ("report_generated", "report_generated_with_warnings")
        assert n01.behavior_correct is True

    def test_insufficient_not_success(self, run_result):
        """⑤ 数据不足不被判成成功。"""
        results, _, _ = run_result
        n06 = next(r for r in results if r.case_id == "N06")
        assert n06.status != "report_generated"
        assert n06.behavior_correct is True

    def test_outputs_written(self, run_result):
        """G：四类产物齐备。"""
        _, _, latest = run_result
        for name in ("evaluation_results.json", "evaluation_results.csv",
                     "summary.md", "run_log.jsonl"):
            assert (latest / name).is_file(), f"缺少 {name}"
        summary_text = (latest / "summary.md").read_text(encoding="utf-8")
        assert "不代表" in summary_text or "不适用" in summary_text

    def test_log_fields_and_no_secrets(self, run_result):
        """⑭ 日志字段齐备且不含密钥/敏感值。"""
        _, _, latest = run_result
        lines = [json.loads(l) for l in
                 (latest / "run_log.jsonl").read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        assert lines
        need = {"run_id", "case_id", "model_mode", "data_mode", "analysis_type",
                "status", "error_code", "latency_ms", "query_count",
                "token_usage", "timestamp"}
        for rec in lines:
            assert need <= set(rec)
        blob = (latest / "run_log.jsonl").read_text(encoding="utf-8")
        assert not re.search(r"sk-[A-Za-z0-9]{20,}", blob)
        assert "Authorization" not in blob
        # 不记录用户级记录（user_id 数值不应出现在日志）
        assert "user_balance_table" not in blob

    def test_no_real_api_in_default_mode(self, run_result):
        """⑬ 默认模式不使用真实客户端（无网络调用）。"""
        _, summary, _ = run_result
        assert summary.model_mode == "mock"
        assert summary.token_total is None
        assert summary.cost_total is None
        client = FakePlanClient({"intent": "x"})
        assert hasattr(client, "plan")     # 纯内存假客户端
        assert client.calls == 0

    def test_exception_does_not_abort_batch(self, tmp_path):
        """⑮ 单案例异常不中断整批，且被正确分类。"""
        cases, answers = load_benchmark()
        subset = [c for c in cases if c["case_id"] in ("T01", "T02")]
        # 破坏第二个案例的 plan，使其在构造阶段抛异常
        subset[1] = {**subset[1], "fixture_plan": {"__broken__": True}}
        runner = EvaluationRunner(subset, answers, model_mode="mock",
                                  output_root=tmp_path, quiet=True)
        try:
            results, summary = runner.run()
        finally:
            runner.cleanup()
        assert len(results) == 2
        ok = next(r for r in results if r.case_id == "T01")
        broken = next(r for r in results if r.case_id == "T02")
        assert ok.report_generated
        assert broken.status in ("exception", "plan_rejected", "needs_clarification")
        assert summary.total_cases == 2

    def test_exception_classification(self):
        """异常分类不得全部归为 unknown。"""
        assert classify_exception(TimeoutError("timed out")) == "timeout"
        assert classify_exception(ConnectionError("api down")) == "api_error"
        assert classify_exception(ValueError("plan invalid")) in (
            "plan_parse_error", "unknown")
        assert len(EXCEPTION_CLASSES) >= 7
        assert "unknown" in EXCEPTION_CLASSES

    def test_real_mode_requires_explicit_flag(self):
        """真实模式需显式启用（默认 mock）。"""
        cases, answers = load_benchmark()
        runner = EvaluationRunner(cases[:1], answers, model_mode="mock")
        assert runner.model_mode == "mock"
        assert runner.real_client is None
