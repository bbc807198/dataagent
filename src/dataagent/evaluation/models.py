"""评测数据模型（阶段 11）。

设计原则：
- **不伪造数值**：Token 缺失时为 `None`，成本未知时为 `None`（序列化为 `N/A`）；
- **口径分离**：查询执行成功、统计分析成功、报告生成成功分别记录，不合并成“成功”；
- **可审计**：每个案例保留 check 明细与失败原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CheckResult:
    kind: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""
    tolerance: float | None = None


@dataclass
class TokenUsage:
    """Token 用量；离线/Mock 模式统一为 None（不适用）。"""

    model: str | None = None
    calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    applicable: bool = False       # False = 离线/Mock，不适用

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "applicable": self.applicable,
        }


@dataclass
class CostInfo:
    """成本；价格未知或无 Token 时为 None（**不填 0**）。"""

    estimated_cost: float | None = None
    currency: str | None = None
    price_source: str | None = None
    price_date: str | None = None
    unit: str | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "estimated_cost": self.estimated_cost
            if self.estimated_cost is not None else "N/A",
            "currency": self.currency,
            "price_source": self.price_source,
            "price_date": self.price_date,
            "unit": self.unit,
            "note": self.note,
        }


@dataclass
class CaseResult:
    case_id: str
    question: str
    category: str
    expected_behavior: str
    expected_status: str
    expected_analysis_type: str
    expected_metrics: list[str]

    # 实际执行
    status: str = ""
    error_code: str | None = None
    analysis_type: str | None = None
    actual_metrics: list[str] = field(default_factory=list)

    # 三态可观测性（口径分离）
    query_executed: bool = False        # SQL 全部执行成功
    analysis_succeeded: bool = False    # 统计分析成功
    report_generated: bool = False      # 报告落盘成功

    latency_ms: float = 0.0
    stage_ms: dict[str, float] = field(default_factory=dict)
    query_count: int = 0

    checks: list[CheckResult] = field(default_factory=list)
    behavior_correct: bool | None = None   # 行为是否符合预期（成功/拒绝/澄清）
    answers_verified: bool = False         # 是否具备可验证的数值 checks
    answers_correct: bool | None = None    # 数值/日期/口径是否全部通过
    # 标准答案事先声明的"是否具备独立数值标准"（execution_only 的案例仅验证执行成功）
    verifiable_expected: bool = False
    verification_kind: str = "numeric"      # numeric / execution_only / behavior
    # 负面案例的校验依据：offline_injected_plan（离线注入计划的 Schema 测试）
    # 或 real_model_output（真实模型输出）；不得混用
    negative_basis: str | None = None
    status_based_correct: bool | None = None  # 仅依据最终状态的判定（防被 check 覆盖）
    usage_complete: bool = True            # Token 统计是否完整（混合有无 usage → False）
    unverifiable_checks: list[str] = field(default_factory=list)
    request_count: int = 0                  # 发起的模型调用次数
    response_count: int = 0                 # 成功返回的调用次数

    warnings: list[str] = field(default_factory=list)
    failure_reason: str = ""
    exception_class: str | None = None

    token_usage: TokenUsage = field(default_factory=TokenUsage)
    cost: CostInfo = field(default_factory=CostInfo)

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "category": self.category,
            "expected_behavior": self.expected_behavior,
            "expected_status": self.expected_status,
            "expected_analysis_type": self.expected_analysis_type,
            "expected_metrics": self.expected_metrics,
            "status": self.status,
            "error_code": self.error_code,
            "analysis_type": self.analysis_type,
            "actual_metrics": self.actual_metrics,
            "query_executed": self.query_executed,
            "analysis_succeeded": self.analysis_succeeded,
            "report_generated": self.report_generated,
            "latency_ms": round(self.latency_ms, 3),
            "stage_ms": {k: round(v, 3) for k, v in self.stage_ms.items()},
            "query_count": self.query_count,
            "behavior_correct": self.behavior_correct,
            "answers_verified": self.answers_verified,
            "answers_correct": self.answers_correct,
            "verifiable_expected": self.verifiable_expected,
            "verification_kind": self.verification_kind,
            "negative_basis": self.negative_basis,
            "status_based_correct": self.status_based_correct,
            "usage_complete": self.usage_complete,
            "unverifiable_checks": self.unverifiable_checks,
            "request_count": self.request_count,
            "response_count": self.response_count,
            "checks": [
                {"kind": c.kind, "passed": c.passed, "expected": c.expected,
                 "actual": c.actual, "detail": c.detail, "tolerance": c.tolerance}
                for c in self.checks
            ],
            "warnings": self.warnings,
            "failure_reason": self.failure_reason,
            "exception_class": self.exception_class,
            "token_usage": self.token_usage.as_dict(),
            "cost": self.cost.as_dict(),
        }


@dataclass
class RunLogRecord:
    """结构化运行日志（不含密钥、参数值、用户级记录、被拒幻觉内容）。"""

    run_id: str
    case_id: str
    model_mode: str
    data_mode: str
    analysis_type: str | None
    status: str
    error_code: str | None
    latency_ms: float
    query_count: int
    token_usage: dict | None
    timestamp: str
    exception_class: str | None = None

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "model_mode": self.model_mode,
            "data_mode": self.data_mode,
            "analysis_type": self.analysis_type,
            "status": self.status,
            "error_code": self.error_code,
            "latency_ms": round(self.latency_ms, 3),
            "query_count": self.query_count,
            "token_usage": self.token_usage,
            "timestamp": self.timestamp,
            "exception_class": self.exception_class,
        }


@dataclass
class EvaluationSummary:
    run_id: str
    model_mode: str
    data_mode: str
    total_cases: int = 0
    valid_cases: int = 0
    negative_cases: int = 0

    # 查询成功率（有效案例中 SQL 与统计都成功）
    query_success: int = 0
    query_success_rate: float | None = None
    report_generated: int = 0
    report_success_rate: float | None = None

    # 答案正确率（可验证案例）—— 分母由标准答案**事前**声明，不因执行失败缩小
    verifiable_cases: int = 0          # 标准答案声明为可验证的有效案例数（固定）
    verifiable_evaluated: int = 0      # 其中实际产出可评估结果的案例数
    execution_only_cases: int = 0      # 仅验证执行成功（无独立数值标准）
    answers_correct: int = 0
    answer_accuracy: float | None = None
    behavior_accuracy: float | None = None   # 全部案例的行为符合率（与成功率、正确率并列）

    # 正确拒绝率 / 误拒绝率
    negative_correct: int = 0
    negative_accuracy: float | None = None
    false_rejections: int = 0
    false_rejection_rate: float | None = None

    # 响应时间
    latency_avg_ms: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_min_ms: float | None = None
    latency_max_ms: float | None = None
    latency_avg_success_ms: float | None = None
    latency_avg_failure_ms: float | None = None

    # 成本
    token_total: int | None = None
    token_calls: int = 0
    token_incomplete_cases: int = 0   # 含无 usage 响应的案例数（统计不完整）
    cost_total: float | None = None
    cost_note: str = ""

    by_category: dict[str, dict] = field(default_factory=dict)
    by_analysis_type: dict[str, dict] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    environment: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "model_mode": self.model_mode,
            "data_mode": self.data_mode,
            "total_cases": self.total_cases,
            "valid_cases": self.valid_cases,
            "negative_cases": self.negative_cases,
            "query_success": self.query_success,
            "query_success_rate": self.query_success_rate,
            "report_generated": self.report_generated,
            "report_success_rate": self.report_success_rate,
            "verifiable_cases": self.verifiable_cases,
            "verifiable_evaluated": self.verifiable_evaluated,
            "execution_only_cases": self.execution_only_cases,
            "answers_correct": self.answers_correct,
            "answer_accuracy": self.answer_accuracy,
            "behavior_accuracy": self.behavior_accuracy,
            "negative_correct": self.negative_correct,
            "negative_accuracy": self.negative_accuracy,
            "false_rejections": self.false_rejections,
            "false_rejection_rate": self.false_rejection_rate,
            "latency_avg_ms": self.latency_avg_ms,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "latency_min_ms": self.latency_min_ms,
            "latency_max_ms": self.latency_max_ms,
            "latency_avg_success_ms": self.latency_avg_success_ms,
            "latency_avg_failure_ms": self.latency_avg_failure_ms,
            "token_total": self.token_total,
            "token_calls": self.token_calls,
            "token_incomplete_cases": self.token_incomplete_cases,
            "cost_total": self.cost_total if self.cost_total is not None else "N/A",
            "cost_note": self.cost_note,
            "by_category": self.by_category,
            "by_analysis_type": self.by_analysis_type,
            "failures": self.failures,
            "environment": self.environment,
        }
