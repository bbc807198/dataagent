"""Benchmark 执行器（阶段 11）。

**离线模式（默认）**：对每个案例注入该案例对应的 Fake 模型输出（`fixture_plan`），
真实经过 `AnalysisPlan → QueryBuilder → SQLGuard → QueryService → Analytics →
ReportGenerator`。它衡量的是**确定性执行链路**，**不是**真实模型的自然语言理解准确率。

**真实模型模式（显式启用）**：`model_mode="real"`，需要 `--real` 与 API Key；
调用前打印案例数与费用信息（无价格配置时标记为未知）；不自动重试。
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .. import analytics, db, ingestion, shibor
from ..agent import AgentConfig, AgentStatus, run_agent
from ..analysis_plan import AnalysisPlan
from ..query_service import QueryService
from .metrics import compare_check, summarize
from .models import (CaseResult, CheckResult, CostInfo, EvaluationSummary,
                     RunLogRecord, TokenUsage)

ROOT = Path(__file__).resolve().parents[3]
BENCH_DIR = ROOT / "benchmark"
SYNTH_DIR = ROOT / "tests" / "fixtures" / "synthetic"
SYNTH_RANGE = (date(2024, 1, 2), date(2024, 1, 12))

# 异常分类（不得全部归为“未知失败”）
EXCEPTION_CLASSES = {
    "api_error": "API/模型调用错误",
    "plan_parse_error": "计划解析错误（Schema/JSON 不合法）",
    "sql_rejected": "SQL 安全拒绝",
    "db_error": "数据库执行错误",
    "insufficient_data": "数据不足",
    "stats_error": "统计计算失败",
    "report_error": "报告生成失败",
    "timeout": "超时",
    "unknown": "未知失败",
}

SUCCESS_STATUSES = {AgentStatus.REPORT_GENERATED,
                    AgentStatus.REPORT_GENERATED_WITH_WARNINGS}


def load_benchmark(root: Path | None = None) -> tuple[list[dict], dict]:
    """加载问题集与标准答案，并做基础格式校验。"""
    base = Path(root) if root else BENCH_DIR
    q_file = base / "questions.jsonl"
    a_file = base / "golden_answers.json"
    cases = [json.loads(line) for line in q_file.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    answers = json.loads(a_file.read_text(encoding="utf-8"))["answers"]
    missing = [c["case_id"] for c in cases if c["case_id"] not in answers]
    if missing:
        raise ValueError(f"以下案例缺少标准答案：{missing}")
    return cases, answers


def classify_exception(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "validation" in name or "json" in name or "plan" in text:
        return "plan_parse_error"
    if "timeout" in name or "timeout" in text:
        return "timeout"
    if "duckdb" in name or "database" in text or "binder" in text:
        return "db_error"
    if "connection" in name or "api" in text or "openai" in text:
        return "api_error"
    return "unknown"


def classify_status(status: str, error_code: str | None) -> str | None:
    """把系统状态映射到异常分类；成功返回 None。"""
    if status in {s.value for s in SUCCESS_STATUSES}:
        return None
    code = (error_code or "").lower()
    if "timeout" in code or "timeout" in status:
        return "timeout"
    if "sql_rejected" in code or "sql" in code and "reject" in code:
        return "sql_rejected"
    if "execution_failed" in code or status == "execution_failed":
        return "db_error"
    if "insufficient" in code or "insufficient" in status:
        return "insufficient_data"
    if "plan" in code or "invalid" in code or status in ("plan_rejected",):
        return "plan_parse_error"
    if "llm" in code or "api" in code:
        return "api_error"
    if "report" in code:
        return "report_error"
    return "unknown"


class FakePlanClient:
    """注入指定计划（模拟“模型对该问题给出了这个计划”）。"""

    def __init__(self, plan: dict, fail: bool = False):
        self.plan = plan
        self.fail = fail
        self.model = "benchmark-fake-client"
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise ConnectionError("benchmark: simulated api failure")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content=json.dumps(self.plan, ensure_ascii=False)),
            finish_reason="stop")])


class UsageTrackingClient:
    """真实模式：包装 openai 客户端，**累计**用量并支持按案例取增量。

    - `requests`：发起的调用次数（含失败）
    - `responses`：成功返回的次数
    - `usage_responses`：返回了 usage 的次数（缺失 usage 时不伪造成 0）
    """

    def __init__(self, inner):
        self._inner = inner
        self.model = getattr(inner, "model", None)
        self.requests = 0
        self.responses = 0
        self.usage_responses = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests += 1
        resp = self._inner.chat.completions.create(**kwargs)
        self.responses += 1
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.usage_responses += 1
            self.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        return resp

    def counters(self) -> dict:
        return {"requests": self.requests, "responses": self.responses,
                "usage_responses": self.usage_responses,
                "input": self.input_tokens, "output": self.output_tokens}

    @staticmethod
    def delta_usage(base: dict, after: dict, model: str | None) -> TokenUsage:
        """由两次快照的差值得到**该案例自身**的用量（不复制批次累计值）。"""
        calls = after["responses"] - base["responses"]
        usage_calls = after["usage_responses"] - base["usage_responses"]
        has_usage = usage_calls > 0
        in_tok = after["input"] - base["input"]
        out_tok = after["output"] - base["output"]
        return TokenUsage(
            model=model, calls=calls,
            input_tokens=in_tok if has_usage else None,
            output_tokens=out_tok if has_usage else None,
            total_tokens=(in_tok + out_tok) if has_usage else None,
            applicable=True)


def load_pricing() -> dict | None:
    """可选价格表：benchmark/pricing.json。缺失/为空 → None（成本记 N/A）。"""
    f = BENCH_DIR / "pricing.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if data.get("models") else None


def estimate_cost(usage: TokenUsage, pricing: dict | None) -> CostInfo:
    if usage.total_tokens is None:
        return CostInfo(estimated_cost=None, note="API 未返回 token 用量，成本记 N/A")
    if not pricing:
        return CostInfo(estimated_cost=None,
                        note="未配置可信价格表（benchmark/pricing.json），成本记 N/A")
    entry = (pricing.get("models") or {}).get(usage.model or "")
    if not entry:
        return CostInfo(estimated_cost=None,
                        note=f"价格表中无模型 {usage.model} 的条目，成本记 N/A")
    price_in = entry.get("input_per_1k")
    price_out = entry.get("output_per_1k")
    if price_in is None or price_out is None:
        return CostInfo(estimated_cost=None, note="价格表条目缺少单价，成本记 N/A")
    cost = ((usage.input_tokens or 0) / 1000.0 * price_in
            + (usage.output_tokens or 0) / 1000.0 * price_out)
    return CostInfo(estimated_cost=round(cost, 6),
                    currency=pricing.get("currency"), price_source=pricing.get("source"),
                    price_date=pricing.get("date"), unit=pricing.get("unit"),
                    note="按配置价格表估算")


class EvaluationRunner:
    def __init__(self, cases: list[dict], answers: dict, *,
                 model_mode: str = "mock", db_path: Path | None = None,
                 data_range: tuple[date, date] | None = None,
                 output_root: Path | None = None,
                 real_client=None, quiet: bool = False,
                 max_requests: int | None = None):
        self.cases = cases
        self.answers = answers
        self.model_mode = model_mode
        self.data_mode = "synthetic"
        self.output_root = Path(output_root) if output_root else (
            ROOT / "artifacts" / "evaluation")
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.real_client = real_client
        self.quiet = quiet
        self._tmpdir: Path | None = None
        self._pricing = load_pricing()
        # A2-6：真实评测的总调用预算（达到即停止，不自动追加请求）
        self.max_requests = max_requests
        self.db_path = Path(db_path) if db_path else None
        self.data_range = data_range or SYNTH_RANGE

    # ---------------- 数据库 ----------------
    def _prepare_db(self) -> Path:
        if self.db_path is not None:
            return self.db_path
        self._tmpdir = Path(tempfile.mkdtemp(prefix="dataagent_bench_"))
        path = self._tmpdir / "benchmark.duckdb"
        con = db.connect(path)
        db.init_schema(con)
        reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
        assert all(r.ok for r in reports), [r.error for r in reports if not r.ok]
        shibor.create_fill_view(con)
        con.close()
        return path

    def cleanup(self) -> None:
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    # ---------------- 单案例 ----------------
    def _run_case(self, case: dict, answer: dict, db_path: Path,
                  out_dir: Path) -> CaseResult:
        r = CaseResult(
            case_id=case["case_id"], question=case["question"],
            category=case["category"],
            expected_behavior=case["expected_behavior"],
            expected_status=case["expected_status"],
            expected_analysis_type=case["expected_analysis_type"],
            expected_metrics=case["expected_metrics"],
        )
        # question 由解析器填充（不是模型输出内容），这里按真实流程注入
        plan_dict = dict(case.get("fixture_plan") or {})
        plan_dict.setdefault("question", case["question"])
        # A1-1：离线模式可用 fixture_plan 校验 Schema；
        # 真实模式的拒绝判定**必须**来自本次真实模型的响应与解析结果
        schema_rejected = False
        if self.model_mode == "mock":
            try:
                AnalysisPlan.model_validate(plan_dict)
            except Exception:   # noqa: BLE001 - Schema 拒绝是预期行为之一
                schema_rejected = True

        r.verifiable_expected = bool(answer.get("verifiable", False))
        r.verification_kind = answer.get("verification_kind",
                                         "numeric" if r.verifiable_expected else "behavior")
        if case["expected_behavior"] != "success":
            # 负面案例的校验依据必须显式标注：
            # 离线模式 = 注入计划的 Schema 测试；真实模式 = 真实模型输出
            r.negative_basis = ("offline_injected_plan"
                                if self.model_mode == "mock" else "real_model_output")

        if self.model_mode == "mock":
            client = FakePlanClient(plan_dict)
        else:
            client = self.real_client
            if client is None:
                raise RuntimeError("真实模式需要 real_client")
        base_counters = (client.counters()
                         if self.model_mode == "real" and hasattr(client, "counters")
                         else None)

        # A1-2 / A2-5：评测显式关闭 Schema 修复重试——
        # 否则一个案例可能追加一次模型调用，绕过请求预算与费用控制。
        # 普通命令行/Web 的既有修复能力保持不变（AgentConfig 默认 True）。
        cfg = AgentConfig(model_mode=self.model_mode, data_mode=self.data_mode,
                          db_path=db_path, data_range=self.data_range,
                          output_dir=out_dir, llm_client=client,
                          use_llm_narrative=False,
                          allow_schema_repair=False)

        t0 = time.perf_counter()
        result = None
        exc: BaseException | None = None
        try:
            with QueryService(db_path) as qs:
                result = run_agent(case["question"], cfg, query_service=qs)
        except Exception as e:      # noqa: BLE001 - 单案例异常不得中断整批
            exc = e
        finally:
            # A2-1：无论成功/异常，都在 finally 记录本案例的请求/响应/Token 增量
            r.latency_ms = (time.perf_counter() - t0) * 1000
            self._record_usage(r, client, base_counters, db_path)
        if exc is not None:
            r.exception_class = classify_exception(exc)
            r.status = "exception"
            r.error_code = r.exception_class
            r.failure_reason = f"{type(exc).__name__}: {exc}"[:200]
            return r

        r.status = result.status.value
        r.error_code = result.error_code
        r.analysis_type = (result.plan or {}).get("analysis_type")
        r.actual_metrics = [m.get("metric_id") for m in
                            (result.plan or {}).get("metrics", [])]
        r.warnings = list(result.warnings or [])

        ex = result.execution_summary or {}
        r.query_count = len(ex.get("queries") or [])
        r.query_executed = (r.query_count > 0
                            and all(q.get("status") == "ok" for q in ex.get("queries") or []))
        r.analysis_succeeded = (result.analysis_summary or {}).get("status") == "ok"
        r.report_generated = bool(result.report_path)

        analysis_obj = result.analysis_result
        clarification = list(result.clarification_questions or [])

        # 行为判定
        if case["expected_behavior"] == "success":
            r.behavior_correct = result.status in SUCCESS_STATUSES
            if not r.behavior_correct:
                r.failure_reason = (f"期望成功，实际 {r.status}"
                                    f"（{r.error_code or '无错误码'}）")
        else:
            # A1-4：**先**依据最终 Agent 状态判定；"意外成功"必须为错，
            # 后续任何 check 不得把它改判为正确
            r.behavior_correct = result.status not in SUCCESS_STATUSES
            r.status_based_correct = dict(r.as_dict()).get("behavior_correct")

        # 数值 checks
        if answer.get("checks"):
            for spec in answer["checks"]:
                cr = compare_check(spec, r, analysis_obj, r.status, r.error_code,
                                   clarification, schema_rejected)
                r.checks.append(cr)

        # status_only / error_code_in / clarification_present / schema_rejected
        # 属"执行或行为"检查，**不计入数值正确率**
        # A3-1：真实模式下"注入计划的 Schema 拒绝"不可观察 → 先移出 checks，
        # 这样它既不会进入 behavior_checks，也不会影响最终评分
        if self.model_mode != "mock":
            for c in list(r.checks):
                if c.kind == "schema_rejected":
                    r.checks.remove(c)
                    r.unverifiable_checks.append(
                        "schema_rejected（真实模式未使用注入计划，无法观察 Schema 拒绝）")

        numeric = [c for c in r.checks
                   if c.kind not in ("error_code_in", "clarification_present",
                                     "schema_rejected", "status_only")]
        # A3-2：行为检查只允许"可观察"的类别；unverifiable 项已不在 r.checks 中
        behavior_checks = [c for c in r.checks
                           if c.kind in ("error_code_in", "clarification_present")]
        r.answers_verified = bool(numeric) and r.behavior_correct is True
        if numeric:
            r.answers_correct = all(c.passed for c in numeric) and r.behavior_correct
            if not r.answers_correct and not r.failure_reason:
                bad = [c.kind for c in numeric if not c.passed]
                r.failure_reason = f"数值/口径不符：{bad}"
        if behavior_checks and case["expected_behavior"] != "success":
            # A1-4：行为检查只能把"正确"降级为"错误"，不能把"意外成功"升为"正确"
            if r.behavior_correct:
                if not all(c.passed for c in behavior_checks):
                    r.behavior_correct = False
                    r.failure_reason = ("拒绝/澄清类别不符合预期"
                                        + ("（意外成功）" if r.status in
                                           {st.value for st in SUCCESS_STATUSES}
                                           else ""))

        return r

    def _record_usage(self, r: CaseResult, client, base_counters: dict | None,
                      db_path: Path) -> None:
        """记录单案例的请求/响应/Token 增量（异常路径同样调用）。"""
        if self.model_mode == "mock":
            r.token_usage = TokenUsage(applicable=False,
                                       model="不适用（离线 Fake 客户端）")
            r.cost = CostInfo(estimated_cost=None,
                              note="不适用：离线模式未调用真实 API")
            r.request_count = 0
            r.response_count = 0
            return
        if base_counters is None or not hasattr(client, "counters"):
            return
        after = client.counters()
        r.request_count = after["requests"] - base_counters["requests"]
        r.response_count = after["responses"] - base_counters["responses"]
        r.token_usage = UsageTrackingClient.delta_usage(
            base_counters, after, getattr(client, "model", None))
        # A2-2：混合"有/无 usage"时标记统计不完整（不把部分总和当完整成本）
        usage_calls = after["usage_responses"] - base_counters["usage_responses"]
        complete = (r.response_count > 0 and usage_calls == r.response_count)
        r.usage_complete = bool(complete)
        r.cost = (estimate_cost(r.token_usage, self._pricing)
                  if complete else
                  CostInfo(estimated_cost=None,
                           note="Token 统计不完整（部分响应无 usage），成本记 N/A"))

    # ---------------- 批次 ----------------
    def run(self, limit: int | None = None) -> tuple[list[CaseResult], EvaluationSummary]:
        cases = self.cases[:limit] if limit else self.cases
        db_path = self._prepare_db()
        out_dir = self.output_root / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        latest = self.output_root / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        log_path = latest / "run_log.jsonl"

        results: list[CaseResult] = []
        run_started = time.perf_counter()
        with log_path.open("w", encoding="utf-8") as logf:
            for i, case in enumerate(cases, 1):
                # A2-6：预算耗尽 → 剩余案例标记为未执行（不追加请求）
                if (self.model_mode == "real" and self.max_requests is not None
                        and isinstance(self.real_client, UsageTrackingClient)
                        and self.real_client.counters()["requests"] >= self.max_requests):
                    r = CaseResult(
                        case_id=case["case_id"], question=case["question"],
                        category=case["category"],
                        expected_behavior=case["expected_behavior"],
                        expected_status=case["expected_status"],
                        expected_analysis_type=case["expected_analysis_type"],
                        expected_metrics=case["expected_metrics"],
                        status="skipped_budget",
                        error_code="skipped_budget",
                        failure_reason="达到评测调用预算上限，未执行（未追加请求）")
                    results.append(r)
                    continue
                answer = self.answers[case["case_id"]]
                try:
                    r = self._run_case(case, answer, db_path, out_dir)
                except Exception as e:      # noqa: BLE001 - 兜底：不让整批中断
                    r = CaseResult(
                        case_id=case["case_id"], question=case["question"],
                        category=case["category"],
                        expected_behavior=case["expected_behavior"],
                        expected_status=case["expected_status"],
                        expected_analysis_type=case["expected_analysis_type"],
                        expected_metrics=case["expected_metrics"],
                        status="exception",
                        error_code=classify_exception(e),
                        exception_class=classify_exception(e),
                        failure_reason=f"{type(e).__name__}: {e}"[:200],
                    )
                results.append(r)
                rec = RunLogRecord(
                    run_id=self.run_id, case_id=r.case_id,
                    model_mode=self.model_mode, data_mode=self.data_mode,
                    analysis_type=r.analysis_type or r.expected_analysis_type,
                    status=r.status, error_code=r.error_code,
                    latency_ms=r.latency_ms, query_count=r.query_count,
                    token_usage=(r.token_usage.as_dict()
                                 if r.token_usage.applicable else None),
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    exception_class=r.exception_class,
                )
                logf.write(json.dumps(rec.as_dict(), ensure_ascii=False) + "\n")
                if not self.quiet and i % 10 == 0:
                    print(f"  … 已完成 {i}/{len(cases)}")
        elapsed = time.perf_counter() - run_started

        # 批次合计 = 各案例增量之和；并校验与客户端总计数一致（不一致即报错）
        if self.model_mode == "real" and self.real_client is not None \
                and hasattr(self.real_client, "counters"):
            total = self.real_client.counters()
            sum_in = sum((r.token_usage.input_tokens or 0) for r in results)
            sum_out = sum((r.token_usage.output_tokens or 0) for r in results)
            if total["usage_responses"] > 0 and (sum_in != total["input"]
                                                 or sum_out != total["output"]):
                raise RuntimeError(
                    "Token 归属校验失败：各案例增量之和与批次总计不一致")

        env = {
            "version": _project_version(),
            "python": os.sys.version.split()[0],
            "model_mode": self.model_mode,
            "data_mode": self.data_mode,
            "dataset": "tests/fixtures/synthetic（人工构造合成样本）",
            "benchmark_dir": str(BENCH_DIR.relative_to(ROOT)),
            "batch_elapsed_s": round(elapsed, 2),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "note": ("离线模式衡量确定性执行链路；不代表真实模型自然语言理解准确率"
                     if self.model_mode == "mock" else "真实模型模式（显式启用）"),
        }
        verifiable_ids = {cid for cid, a in self.answers.items()
                          if a.get("verifiable")}
        summary = summarize(results, self.run_id, self.model_mode,
                            self.data_mode, env,
                            extra_verifiable_ids=verifiable_ids)
        self._write_outputs(results, summary, latest)
        return results, summary

    # ---------------- 输出 ----------------
    def _write_outputs(self, results: list[CaseResult],
                       summary: EvaluationSummary, latest: Path) -> None:
        (latest / "evaluation_results.json").write_text(
            json.dumps({"run_id": self.run_id, "summary": summary.as_dict(),
                        "cases": [r.as_dict() for r in results]},
                       ensure_ascii=False, indent=2), encoding="utf-8")

        cols = ["case_id", "category", "expected_behavior", "expected_status",
                "status", "error_code", "analysis_type", "query_executed",
                "analysis_succeeded", "report_generated", "behavior_correct",
                "answers_verified", "answers_correct", "latency_ms",
                "query_count", "failure_reason"]
        with (latest / "evaluation_results.csv").open("w", encoding="utf-8",
                                                      newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in results:
                w.writerow({c: v for c, v in r.as_dict().items() if c in cols})

        (latest / "summary.md").write_text(self._render_summary(results, summary),
                                           encoding="utf-8")

    def _render_summary(self, results: list[CaseResult],
                        s: EvaluationSummary) -> str:
        def pct(v):
            return "N/A" if v is None else f"{v * 100:.1f}%"

        mode_note = ("**离线（Mock/Fake）确定性执行链路评测** —— 该结果**不代表**真实模型"
                     "的自然语言理解准确率。" if self.model_mode == "mock"
                     else "**真实模型模式**（显式启用，实际调用 API）。")

        neg = [r for r in results if r.expected_behavior != "success"]
        neg_success_status = {st.value for st in SUCCESS_STATUSES}
        neg_unexpected_success = [r for r in neg if r.status in neg_success_status]
        neg_wrong_category = [r for r in neg if r.behavior_correct is False
                              and r.status not in neg_success_status]
        neg_correct = [r for r in neg if r.behavior_correct is True]
        basis_note = ("离线模式：负面案例的拒绝结论来自**注入计划的 Schema 校验**"
                      "（`negative_basis=offline_injected_plan`）——仅证明系统会拒绝该计划，"
                      "**不证明真实模型抗注入能力**"
                      if self.model_mode == "mock"
                      else "真实模式：拒绝结论来自**真实模型输出**的实际解析/校验结果")

        lines = [
            "# DataAgent Benchmark 汇总", "",
            f"- run_id：`{s.run_id}`",
            f"- 模型模式：`{s.model_mode}`　|　数据模式：`{s.data_mode}`",
            f"- 数据集：{s.environment.get('dataset', '')}",
            f"- 评估时间：{s.environment.get('evaluated_at', '')}",
            f"- 版本：{s.environment.get('version', '')}　|　Python {s.environment.get('python', '')}",
            "",
            f"> {mode_note}", "",
            "## 三项指标（口径互不替代）", "",
            "| 指标 | 值 | 分母 | 含义 |",
            "|---|---|---|---|",
            f"| **执行成功率** | {pct(s.query_success_rate)} | {s.valid_cases}（有效案例） | "
            "SQL 全部执行成功且统计分析成功 |",
            f"| **答案正确率** | {pct(s.answer_accuracy)} | {s.verifiable_cases}"
            "（标准答案声明具备独立数值标准，**事前固定**） | 数值/日期/指标/口径全部符合容差 |",
            f"| **行为正确率** | {pct(s.behavior_accuracy)} | {s.total_cases}（全部案例） | "
            "成功/拒绝/澄清的行为类别符合预期 |",
            "",
            "口径说明：`report_generated` **不等于**答案正确；执行成功、报告生成、"
            "答案正确三者分开统计。仅验证执行成功（无独立数值标准）的案例"
            f"**不计入**答案正确率分子分母（本批次此类案例：{s.execution_only_cases} 个）。",
            f"可验证案例中实际产出可评估结果：{s.verifiable_evaluated}/{s.verifiable_cases}"
            "（执行失败不会缩小分母）。",
            "",
            "## 有效案例", "",
            f"- 查询成功率（SQL 与统计都成功）：**{pct(s.query_success_rate)}**"
            f"（{s.query_success}/{s.valid_cases}）",
            f"- 报告生成成功率（单独口径）：{pct(s.report_success_rate)}"
            f"（{s.report_generated}/{s.valid_cases}）",
            f"- 答案正确率：**{pct(s.answer_accuracy)}**"
            f"（{s.answers_correct}/{s.verifiable_cases}）",
            f"- 误拒绝（有效问题未产出报告）：{s.false_rejections} 个"
            f"（{pct(s.false_rejection_rate)}）",
            "",
            "## 负面与边界案例", "",
            f"- 正确拒绝/澄清：**{len(neg_correct)}/{len(neg)}**"
            f"（{pct(s.negative_accuracy)}）",
            f"- 拒绝类别错误：{len(neg_wrong_category)}",
            f"- 意外成功（本应拒绝却产出报告）：{len(neg_unexpected_success)}",
            f"- 校验依据：{basis_note}",
            "",
            "## 响应时间（端到端，`perf_counter`）", "",
            f"- 平均：{s.latency_avg_ms} ms　|　P50：{s.latency_p50_ms} ms　|　"
            f"P95：{s.latency_p95_ms} ms",
            f"- 最小：{s.latency_min_ms} ms　|　最大：{s.latency_max_ms} ms",
            f"- 成功案例平均：{_fmt_ms(s.latency_avg_success_ms)}　|　"
            f"失败案例平均：{_fmt_ms(s.latency_avg_failure_ms)}",
            "",
            "## Token 与成本", "",
            f"- Token 用量：{('N/A（未调用真实 API）' if s.token_total is None else s.token_total)}"
            f"　|　调用次数：{s.token_calls}",
            f"- 估计成本：{('N/A' if s.cost_total is None else s.cost_total)}",
            f"- 说明：{s.cost_note}",
            "",
            "## 按类别表现", "",
            "| 类别 | 总数 | 行为符合 | 可验证 | 答案正确 |",
            "|---|---|---|---|---|",
        ]
        for k, v in sorted(s.by_category.items()):
            lines.append(f"| {k} | {v['total']} | {v['behavior_correct']} | "
                         f"{v['answers_verified']} | {v['answers_correct']} |")

        lines += ["", "## 按分析类型表现", "",
                  "| 分析类型 | 总数 | 行为符合 | 可验证 | 答案正确 |",
                  "|---|---|---|---|---|"]
        for k, v in sorted(s.by_analysis_type.items()):
            lines.append(f"| {k} | {v['total']} | {v['behavior_correct']} | "
                         f"{v['answers_verified']} | {v['answers_correct']} |")

        lines += ["", "## 失败案例", ""]
        if not s.failures:
            lines.append("无。")
        else:
            lines += ["| case_id | 类别 | 期望行为 | 实际状态 | 失败原因 |",
                      "|---|---|---|---|---|"]
            for f in s.failures:
                lines.append(f"| {f['case_id']} | {f['category']} | "
                             f"{f['expected_behavior']} | {f['status']} | "
                             f"{(f['failure_reason'] or '')[:90]} |")
        lines += ["", "## 复现", "",
                  "```powershell",
                  ".\\.venv\\Scripts\\python.exe scripts\\evaluate_agent.py",
                  "```", ""]
        return "\n".join(lines)


def _fmt_ms(v: float | None) -> str:
    return "N/A（无此类案例）" if v is None else f"{v} ms"


def _project_version() -> str:
    try:
        from importlib.metadata import version
        return version("dataagent")
    except Exception:      # noqa: BLE001
        return "unknown"
