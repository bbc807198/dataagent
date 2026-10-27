"""Agent 编排入口（阶段七）。

`run_agent(question)`：自然语言问题 → 结构化计划 → 安全 SQL 执行 → Python 统计
→ 图表 → **证据化中文报告**。

编排约束：
- 只调用既有模块（`IntentParser` / `run_analysis` / `analyze_execution_result` /
  `generate_charts` / `generate_report`）；**不在此执行任意 SQL**，
  所有查询经 `run_analysis()` → `QueryService`；
- 澄清、计划被拒、SQL 被拒、超时、截断、空结果、数据不足**一律不生成成功报告**；
- 明确区分模型模式（mock/real）与数据模式（project_db/synthetic），
  任何 Mock 或合成数据结果都不会被标注为真实业务结果；
- 报告失败/图表失败不会覆盖已获得的查询与统计结果（保留诊断摘要）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path

from . import analytics, config as app_config, metric_catalog as mc, visualization
from .analysis_plan import AnalysisPlan
from .analysis_runner import ExecutionStatus, run_analysis
from .clarification import detect_concept_mismatch
from .intent_parser import IntentParser, ParseStatus
from .query_service import QueryService
from .report_generator import ReportResult, generate_report

log = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED_METRIC = "unsupported_metric"
    PLAN_REJECTED = "plan_rejected"
    SQL_REJECTED = "sql_rejected"
    EXECUTION_FAILED = "execution_failed"
    TIMEOUT = "timeout"
    EMPTY_RESULT = "empty_result"
    INSUFFICIENT_DATA = "insufficient_data"
    TRUNCATED = "truncated"
    ANALYSIS_FAILED = "analysis_failed"
    REPORT_GENERATED = "report_generated"
    REPORT_GENERATED_WITH_WARNINGS = "report_generated_with_warnings"


MODEL_MODES = ("mock", "real")
DATA_MODES = ("project_db", "synthetic")


@dataclass
class AgentConfig:
    """运行配置（可注入，便于测试与演示）。"""

    model_mode: str = "mock"                 # "mock" | "real"
    data_mode: str = "project_db"            # "project_db" | "synthetic"
    db_path: Path | None = None              # 默认 config.DB_PATH
    data_range: tuple[date, date] | None = None   # 合成数据覆盖范围（日期校验）
    output_dir: Path | None = None           # 报告/图表输出（默认 artifacts/generated）
    llm_client: object | None = None         # 注入的模型客户端（测试用）
    llm_model: str | None = None             # 叙述层模型名（默认为解析器使用的模型）
    use_llm_narrative: bool = False          # 是否启用 LLM 叙述层（需真实客户端）
    allow_schema_repair: bool = True         # 是否允许一次 Schema 修复重试（评测可关）
    anchor_end: date | None = None           # 相对时间锚点（合成模式对齐合成数据）
    data_start: date | None = None

    def validate(self) -> None:
        if self.model_mode not in MODEL_MODES:
            raise ValueError(f"model_mode 必须是 {MODEL_MODES} 之一")
        if self.data_mode not in DATA_MODES:
            raise ValueError(f"data_mode 必须是 {DATA_MODES} 之一")


@dataclass
class AgentRunResult:
    status: AgentStatus
    question: str
    model_mode: str
    data_mode: str
    plan: dict | None = None
    clarification_questions: list[str] = field(default_factory=list)
    execution_summary: dict | None = None
    analysis_summary: dict | None = None
    report_path: str | None = None
    evidence_path: str | None = None
    chart_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    report_status: str | None = None
    narrative_source: str | None = None
    llm_model: str | None = None      # 实际使用的叙述层模型名（不含密钥）
    analysis_result: analytics.AnalysisResult | None = field(
        default=None, repr=False)   # 供报告生成使用（不进入 as_dict）

    def is_success(self) -> bool:
        return self.status in (AgentStatus.REPORT_GENERATED,
                               AgentStatus.REPORT_GENERATED_WITH_WARNINGS)

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "question": self.question,
            "model_mode": self.model_mode,
            "data_mode": self.data_mode,
            "plan": self.plan,
            "clarification_questions": self.clarification_questions,
            "execution_summary": self.execution_summary,
            "analysis_summary": self.analysis_summary,
            "report_path": self.report_path,
            "evidence_path": self.evidence_path,
            "chart_paths": self.chart_paths,
            "warnings": self.warnings,
            "limitations": self.limitations,
            "error": self.error,
            "error_code": self.error_code,
            "report_status": self.report_status,
            "narrative_source": self.narrative_source,
        }


# ---------------------------------------------------------------------------
# 内部映射
# ---------------------------------------------------------------------------

_ANALYSIS_STATUS_MAP = {
    analytics.AnalysisStatus.EMPTY_RESULT: AgentStatus.EMPTY_RESULT,
    analytics.AnalysisStatus.INSUFFICIENT_DATA: AgentStatus.INSUFFICIENT_DATA,
    analytics.AnalysisStatus.TRUNCATED: AgentStatus.TRUNCATED,
    analytics.AnalysisStatus.INCOMPATIBLE_METRIC: AgentStatus.PLAN_REJECTED,
    analytics.AnalysisStatus.METHOD_NOT_APPLICABLE: AgentStatus.INSUFFICIENT_DATA,
    analytics.AnalysisStatus.EXECUTION_FAILED: AgentStatus.EXECUTION_FAILED,
}


def _execution_summary(result) -> dict:
    """查询执行摘要（不含参数值）。"""
    return {
        "status": result.status,
        "total_ms": result.total_ms,
        "queries": [
            {
                "name": q.name,
                "status": q.status,
                "columns": list(q.columns),
                "row_count": q.row_count,
                "truncated": q.truncated,
                "window_label": q.window_label,
                "sql": q.sql_text,      # 参数化模板（不含参数值），仅用于展示
                # 已绑定的参数（白名单过滤；隐藏项 value=None）
                "bindings": list(getattr(q, "bindings", []) or []),
                # 用户原始请求窗口（截取时与实际执行窗口不同）
                "requested_window_start": getattr(q, "requested_window_start", None),
                "requested_window_end": getattr(q, "requested_window_end", None),
                "window_start": q.window_start,
                "window_end": q.window_end,
                "execution_ms": q.execution_ms,
                "error": q.error,
            }
            for q in result.queries
        ],
        "error": result.error,
    }


def _analysis_summary(result: analytics.AnalysisResult) -> dict:
    stats = result.statistics
    return {
        "status": result.status.value,
        "status_reason": result.status_reason,
        "method": result.method,
        "method_params": result.method_params,
        "statistics_kind": getattr(stats, "kind", None),
        "conclusion_count": len(result.conclusions),
        "time_range": result.time_range,
        "warnings": list(result.warnings),
        "limitations": list(result.limitations),
        "chart_paths": list(result.chart_paths),
    }


def _make_parser(cfg: AgentConfig) -> IntentParser:
    kwargs = {"llm_client": cfg.llm_client,
              "allow_repair": cfg.allow_schema_repair}
    if cfg.model_mode == "real" and cfg.llm_client is None:
        # IntentParser 自行从环境变量读取配置
        kwargs = {"allow_repair": cfg.allow_schema_repair}
    anchor = cfg.anchor_end or (cfg.data_range[1] if cfg.data_range else None)
    dstart = cfg.data_start or (cfg.data_range[0] if cfg.data_range else None)
    if anchor is not None:
        kwargs["anchor_end"] = anchor
    if dstart is not None:
        kwargs["data_start"] = dstart
    return IntentParser(**kwargs)


def _failed(cfg: AgentConfig, question: str, status: AgentStatus,
            *, error: str | None = None, error_code: str | None = None,
            plan: dict | None = None, questions: list[str] | None = None,
            execution: dict | None = None, analysis: dict | None = None,
            warnings: list[str] | None = None,
            analysis_result: analytics.AnalysisResult | None = None
            ) -> AgentRunResult:
    return AgentRunResult(
        status=status, question=question, model_mode=cfg.model_mode,
        data_mode=cfg.data_mode, plan=plan,
        clarification_questions=questions or [],
        execution_summary=execution, analysis_summary=analysis,
        warnings=list(warnings or []),
        error=error, error_code=error_code,
        analysis_result=analysis_result)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_agent(question: str, config: AgentConfig | None = None,
              query_service=None) -> AgentRunResult:
    """执行完整分析流程并生成中文报告。

    `query_service`：可注入的只读查询服务（测试/演示用）；缺省时按 `data_mode`
    打开项目数据库（只读）。**不会**在内部执行任何手写 SQL。
    """
    cfg = config or AgentConfig()
    cfg.validate()

    # ---- 1) 自然语言 → 结构化计划 ----
    try:
        parser = _make_parser(cfg)
    except Exception as e:  # noqa: BLE001 - 初始化失败必须结构化返回
        log.warning("intent parser init failed: %s", type(e).__name__)
        return _failed(cfg, question, AgentStatus.EXECUTION_FAILED,
                       error=f"解析器初始化失败：{type(e).__name__}: {e}",
                       error_code="llm_init_failed")
    try:
        parse = parser.parse_question(question)
    except Exception as e:  # noqa: BLE001 - 调用失败不生成计划，也不继续执行
        log.warning("intent parse failed: %s", type(e).__name__)
        return _failed(cfg, question, AgentStatus.EXECUTION_FAILED,
                       error=f"模型调用失败：{type(e).__name__}: {e}",
                       error_code="llm_call_failed")
    if parse.status == ParseStatus.NOT_CONFIGURED:
        return _failed(cfg, question, AgentStatus.EXECUTION_FAILED,
                       error=parse.errors[0] if parse.errors else "未配置大模型 API Key",
                       error_code="llm_not_configured")
    if parse.status == ParseStatus.API_ERROR:
        return _failed(cfg, question, AgentStatus.EXECUTION_FAILED,
                       error=parse.errors[0] if parse.errors else "模型调用失败",
                       error_code="llm_api_error")
    if parse.status in (ParseStatus.INVALID, ParseStatus.CLARIFICATION):
        questions = []
        if parse.clarification is not None:
            questions = list(parse.clarification.questions)
        # 业务概念不匹配（如"销售额"）→ unsupported_metric（不映射为申购金额）
        concept = detect_concept_mismatch(question)
        plan_dict = (parse.plan.model_dump(mode="json")
                      if parse.plan is not None else None)
        if concept:
            merged = list(dict.fromkeys(questions + [concept]))
            return _failed(
                cfg, question, AgentStatus.UNSUPPORTED_METRIC,
                error=concept, error_code="unsupported_metric",
                plan=plan_dict, questions=merged)
        return _failed(
            cfg, question, AgentStatus.NEEDS_CLARIFICATION,
            error="问题需要澄清后才能生成查询",
            error_code="needs_clarification", plan=plan_dict,
            questions=questions or ["请补充指标、时间范围与分组维度"],
            warnings=[e for e in parse.errors][:5])

    plan = parse.plan
    if plan is None or not plan.is_executable_ready():
        return _failed(cfg, question, AgentStatus.PLAN_REJECTED,
                       error="分析计划不满足可执行条件（缺少指标/统计操作或时间未解析）",
                       error_code="plan_not_executable",
                       plan=plan.model_dump(mode="json") if plan else None)

    # ---- 2) 安全执行（QueryService）----
    owns_service = False
    service = query_service
    if service is None:
        db_path = Path(cfg.db_path) if cfg.db_path else Path(app_config.DB_PATH)
        if not db_path.exists():
            return _failed(cfg, question, AgentStatus.EXECUTION_FAILED,
                           error=f"数据库不存在: {db_path.name}",
                           error_code="db_not_found",
                           plan=plan.model_dump(mode="json"))
        try:
            service = QueryService(db_path)
            owns_service = True
        except Exception as e:  # noqa: BLE001
            log.warning("query service init failed: %s", type(e).__name__)
            return _failed(cfg, question, AgentStatus.EXECUTION_FAILED,
                           error=f"查询服务初始化失败：{type(e).__name__}: {e}",
                           error_code="db_init_failed",
                           plan=plan.model_dump(mode="json"))
    try:
        execution = run_analysis(plan, service, data_range=cfg.data_range)
    except Exception as e:  # noqa: BLE001 - 执行异常不得转为成功
        log.exception("run_analysis raised")
        return _failed(cfg, question, AgentStatus.EXECUTION_FAILED,
                       error=f"查询执行异常：{type(e).__name__}: {e}",
                       error_code="execution_exception",
                       plan=plan.model_dump(mode="json"))
    finally:
        if owns_service:
            try:
                service.close()
            except Exception:  # noqa: BLE001 - 关闭异常不得掩盖原始错误
                log.warning("query service close failed", exc_info=True)

    status_map = {
        ExecutionStatus.NEEDS_CLARIFICATION: AgentStatus.NEEDS_CLARIFICATION,
        ExecutionStatus.PLAN_REJECTED: AgentStatus.PLAN_REJECTED,
        ExecutionStatus.SQL_REJECTED: AgentStatus.SQL_REJECTED,
        ExecutionStatus.EXECUTION_FAILED: AgentStatus.EXECUTION_FAILED,
        ExecutionStatus.TIMEOUT: AgentStatus.TIMEOUT,
        ExecutionStatus.PARTIAL: AgentStatus.EXECUTION_FAILED,
    }
    exec_summary = _execution_summary(execution)
    if execution.status != ExecutionStatus.OK:
        return _failed(cfg, question, status_map.get(
            execution.status, AgentStatus.EXECUTION_FAILED),
            error=execution.error or f"查询未成功（{execution.status}）",
            error_code=execution.status,
            plan=plan.model_dump(mode="json"), execution=exec_summary,
            warnings=list(execution.warnings))
    if any(q.truncated for q in execution.queries):
        return _failed(cfg, question, AgentStatus.TRUNCATED,
                       error="查询结果被行数上限截断，不足以生成完整结论",
                       error_code="truncated", plan=plan.model_dump(mode="json"),
                       execution=exec_summary)

    # ---- 3) Python 统计 ----
    analysis = analytics.analyze_execution_result(execution, plan)
    analysis_summary = _analysis_summary(analysis)
    if analysis.status != analytics.AnalysisStatus.OK:
        return _failed(cfg, question,
                       _ANALYSIS_STATUS_MAP.get(analysis.status,
                                                AgentStatus.ANALYSIS_FAILED),
                       error=analysis.status_reason or "统计分析未成功",
                       error_code=analysis.status.value,
                       plan=plan.model_dump(mode="json"), execution=exec_summary,
                       analysis=analysis_summary,
                       warnings=list(analysis.warnings),
                       analysis_result=analysis)

    # ---- 4) 图表（失败不影响报告生成）----
    out_dir = Path(cfg.output_dir) if cfg.output_dir else visualization.DEFAULT_OUTPUT_DIR
    chart_paths: list[str] = []
    chart_warnings: list[str] = []
    try:
        chart_results = visualization.generate_charts(analysis, out_dir)
    except Exception as e:  # noqa: BLE001 - 绘图异常不阻断报告
        log.warning("generate_charts raised: %s", type(e).__name__)
        chart_results = []
        chart_warnings.append(f"图表生成异常（{type(e).__name__}）：已生成不含图表的报告")
    chart_paths = [c.path for c in chart_results if c.status == "ok" and c.path]
    analysis.chart_paths = chart_paths
    chart_warnings.extend(f"图表生成失败（{c.kind}）：{c.error}"
                          for c in chart_results if c.status != "ok")
    chart_warnings.extend(w for c in chart_results for w in c.warnings)

    # ---- 5) 证据化报告 ----
    result = AgentRunResult(
        status=AgentStatus.REPORT_GENERATED, question=question,
        model_mode=cfg.model_mode, data_mode=cfg.data_mode,
        plan=plan.model_dump(mode="json"), execution_summary=exec_summary,
        analysis_summary=analysis_summary, chart_paths=chart_paths,
        warnings=chart_warnings + list(analysis.warnings),
        limitations=list(analysis.limitations),
        analysis_result=analysis)

    # 叙述层：真实模式下复用解析器创建的客户端与模型名（不得静默跳过）
    narrator = None
    narrator_model = None
    if cfg.use_llm_narrative:
        narrator = cfg.llm_client or parser.client        # 公开只读属性（IntentParser）
        narrator_model = cfg.llm_model or parser.model
        if narrator is None:
            result.warnings.append(
                "已请求 LLM 叙述，但未配置可用的模型客户端"
                f"（{getattr(parser, 'client_error', None) or '无可用客户端'}）；"
                "已仅生成确定性报告")
    result.llm_model = narrator_model
    try:
        report: ReportResult = generate_report(
            result, out_dir, llm_narrator=narrator, llm_model=narrator_model)
    except Exception as e:  # noqa: BLE001 - 报告异常不覆盖统计结果
        log.exception("generate_report raised")
        result.status = AgentStatus.ANALYSIS_FAILED
        result.error = f"报告生成异常：{type(e).__name__}: {e}"
        result.error_code = "report_exception"
        return result
    result.report_status = report.status
    result.narrative_source = report.narrative_source
    result.report_path = report.markdown_path
    result.evidence_path = report.evidence_path
    result.warnings.extend(report.warnings)
    if report.status == "failed":
        result.status = AgentStatus.ANALYSIS_FAILED
        result.error = report.error
        result.error_code = "report_failed"
        return result

    if result.warnings or report.status == "generated_with_warnings":
        result.status = AgentStatus.REPORT_GENERATED_WITH_WARNINGS
    return result
