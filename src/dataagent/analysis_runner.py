"""分析执行器（阶段五）。

`run_analysis(plan, query_service)`：把分析计划编译为受控 SQL 并通过
`QueryService` 执行，返回结构化结果。

约束：
- **不得绕过 QueryService**（不直接 duckdb.connect / 不直接 execute 连接）；
- 安全校验由 `QueryService` 内部强制执行（`sql_guard` 已在生成期预检一次）；
- 任一查询被安全拒绝/超时/执行失败，都不会被当作成功结果；
- 不记录 SQL 参数值与用户级记录到日志；
- 本阶段只返回查询数据，**不做趋势解释、异常识别或因果分析**（第六阶段）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from .analysis_plan import AnalysisPlan
from .query_bindings import public_bindings
from .query_builder import BuildErrorCode, QueryBuildError, build_queries

log = logging.getLogger(__name__)


class ExecutionStatus:
    OK = "ok"                                   # 全部查询成功
    PARTIAL = "partial"                         # 部分查询成功
    NEEDS_CLARIFICATION = "needs_clarification"  # 计划需澄清，未执行
    PLAN_REJECTED = "plan_rejected"              # 计划不可执行/无法生成查询
    SQL_REJECTED = "sql_rejected"                # 生成的 SQL 被安全校验拒绝
    EXECUTION_FAILED = "execution_failed"        # 数据库执行失败
    TIMEOUT = "timeout"                          # 执行超时


@dataclass
class QueryExecution:
    name: str
    status: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    execution_ms: float = 0.0
    window_label: str | None = None
    metric_ids: list[str] = field(default_factory=list)
    purpose: str = ""
    param_count: int = 0            # 只记录参数个数，不记录参数值
    window_start: str | None = None  # 实际执行的日期窗口（截取后）
    window_end: str | None = None
    group_by: list[str] = field(default_factory=list)
    # 参数化 SQL 模板（值以 ? 占位，**不含**参数值/用户数据）；仅供展示与审计
    sql_text: str = ""
    # 已绑定的参数（白名单过滤后的可公开展示项；隐藏项 value=None）
    bindings: list[dict] = field(default_factory=list)
    # 用户原始请求窗口（若发生截取，与 window_start/window_end 不同）
    requested_window_start: str | None = None
    requested_window_end: str | None = None
    error: str | None = None
    error_category: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "execution_ms": self.execution_ms,
            "window_label": self.window_label,
            "metric_ids": self.metric_ids,
            "purpose": self.purpose,
            "param_count": self.param_count,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "group_by": self.group_by,
            "error": self.error,
            "error_category": self.error_category,
        }


@dataclass
class AnalysisExecutionResult:
    status: str
    plan_summary: dict
    queries: list[QueryExecution] = field(default_factory=list)
    total_ms: float = 0.0
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "plan_summary": self.plan_summary,
            "queries": [q.as_dict() for q in self.queries],
            "total_ms": self.total_ms,
            "notes": self.notes,
            "warnings": self.warnings,
            "error": self.error,
            "error_code": self.error_code,
        }

    def all_ok(self) -> bool:
        return self.status == ExecutionStatus.OK


def _plan_summary(plan: AnalysisPlan) -> dict:
    tr = plan.time_range
    return {
        "question": plan.question,
        "analysis_type": plan.analysis_type.value,
        "metrics": [{"metric_id": m.metric_id, "term": m.term} for m in plan.metrics],
        "statistics": [s.value for s in plan.statistics],
        "group_by": list(plan.group_by),
        "time_range": {
            "type": tr.type.value,
            "relative_period": tr.relative_period,
            "anchor": tr.anchor,
            "resolved_start_date": (tr.resolved_start_date.isoformat()
                                    if tr.resolved_start_date else None),
            "resolved_end_date": (tr.resolved_end_date.isoformat()
                                  if tr.resolved_end_date else None),
            "comparison_start_date": (tr.comparison_start_date.isoformat()
                                      if tr.comparison_start_date else None),
            "comparison_end_date": (tr.comparison_end_date.isoformat()
                                    if tr.comparison_end_date else None),
            "resolution_status": tr.resolution_status.value,
        },
        "top_n": plan.top_n,
    }


def run_analysis(plan: AnalysisPlan, query_service,
                 data_range: tuple[date, date] | None = None
                 ) -> AnalysisExecutionResult:
    """执行分析计划（唯一执行入口；必须经 QueryService）。

    `data_range` 为数据集实际覆盖范围（默认真实数据常量；测试可注入合成库范围）。
    """
    t0 = time.perf_counter()
    summary = _plan_summary(plan)

    # ---- 1) 计划可执行性（不满足则不生成、不执行）----
    if plan.clarification_needed:
        return AnalysisExecutionResult(
            status=ExecutionStatus.NEEDS_CLARIFICATION, plan_summary=summary,
            error="计划存在待澄清问题，未生成 SQL",
            error_code=BuildErrorCode.CLARIFICATION_REQUIRED,
            warnings=list(plan.clarification_questions))
    if plan.unresolved:
        return AnalysisExecutionResult(
            status=ExecutionStatus.PLAN_REJECTED, plan_summary=summary,
            error=f"计划存在未解决事项: {plan.unresolved}",
            error_code=BuildErrorCode.UNRESOLVED_PRESENT)
    if not plan.is_executable_ready():
        return AnalysisExecutionResult(
            status=ExecutionStatus.PLAN_REJECTED, plan_summary=summary,
            error="计划不满足可执行条件（时间范围未解析或缺少指标/统计操作）",
            error_code=BuildErrorCode.PLAN_NOT_EXECUTABLE)

    # ---- 2) 生成查询（确定性编译器）----
    try:
        bundle = build_queries(plan, data_range=data_range)
    except QueryBuildError as e:
        log.info("query build rejected: %s", e.code)
        return AnalysisExecutionResult(
            status=ExecutionStatus.PLAN_REJECTED, plan_summary=summary,
            error=e.message, error_code=e.code,
            warnings=e.details,
            total_ms=round((time.perf_counter() - t0) * 1000, 2))

    # ---- 3) 逐条执行（必须经 QueryService）----
    executions: list[QueryExecution] = []
    for gq in bundle.queries:
        result = query_service.execute(gq.sql, gq.params)
        qe = QueryExecution(
            name=gq.name, status=result.query_status,
            columns=list(result.columns), rows=list(result.rows),
            row_count=result.row_count, truncated=result.truncated,
            execution_ms=result.execution_ms, window_label=gq.window_label,
            metric_ids=list(gq.metric_ids), purpose=gq.purpose,
            param_count=len(gq.params),
            window_start=gq.window_start, window_end=gq.window_end,
            group_by=list(gq.group_by), sql_text=gq.sql,
            bindings=public_bindings(getattr(gq, "param_labels", None), gq.params),
            requested_window_start=getattr(gq, "requested_window_start", None),
            requested_window_end=getattr(gq, "requested_window_end", None),
            error=result.error, error_category=result.error_category)
        executions.append(qe)
        # 审计日志只记录查询名/状态/行数，不含参数值与用户数据
        log.info("analysis query %s: %s rows=%d ms=%.1f",
                 gq.name, result.query_status, result.row_count, result.execution_ms)

    # ---- 4) 汇总状态（安全拒绝最优先，其次超时，再执行失败，最后部分成功）----
    statuses = [q.status for q in executions]
    if all(s == "ok" for s in statuses):
        status = ExecutionStatus.OK
    elif any(s == "rejected" for s in statuses):
        status = ExecutionStatus.SQL_REJECTED
    elif any(s == "timeout" for s in statuses):
        status = ExecutionStatus.TIMEOUT
    elif all(s != "ok" for s in statuses):
        status = ExecutionStatus.EXECUTION_FAILED
    else:
        status = ExecutionStatus.PARTIAL

    notes = list(bundle.notes)
    notes.append("本阶段仅返回查询数据；趋势解释、异常检测、相关性与归因分析属后续阶段")

    return AnalysisExecutionResult(
        status=status, plan_summary=summary, queries=executions,
        total_ms=round((time.perf_counter() - t0) * 1000, 2),
        notes=notes,
        warnings=[w for q in executions if q.status != "ok" and q.error
                  for w in [f"{q.name}: {q.error}"]],
        error=None if status == ExecutionStatus.OK else
        next((q.error for q in executions if q.status != "ok"), "部分查询失败"),
    )
