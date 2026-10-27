"""确定性 SQL 生成器（阶段五 / 05.1 修订）。

把已验证的 `AnalysisPlan` 编译为**参数化 SQL**。这是封闭目录上的确定性编译器，
不是自由 SQL Agent：

- 只能使用 `metric_catalog` 中的指标/字段、已审核维度、固定模板；
- 用户输入（筛选值、日期、N）只作为**绑定参数**，绝不成为 SQL 片段；
- 只允许两类已审核关联：用户关联（`b.user_id = p.user_id`）与
  日期关联（`b.report_date = y.mfd_date`），不支持由用户指定 JOIN；
- **分组维度完整落地**：`plan.group_by` 中的所有维度都进入 SELECT 与 GROUP BY
  （时间趋势在日期维度基础上叠加用户属性维度），不会被静默忽略；
- **用户数口径明确**：`user_count` 为静态画像总人数（仅全期），
  `active_user_count` 为期间去重用户数（必须有限时间窗口）；
- 不连接数据库、不执行 SQL（只做 `sql_guard.validate_sql` 静态预检查）；
  真正执行必须走 `QueryService.execute()`。

不支持的分析组合返回结构化错误（`QueryBuildError`），不猜测、不降级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import metric_catalog as mc
from . import sql_guard
from .analysis_plan import AnalysisPlan, AnalysisType, StatOperation, TimeRangeType

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class BuildErrorCode:
    PLAN_NOT_EXECUTABLE = "plan_not_executable"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNRESOLVED_PRESENT = "unresolved_present"
    TIME_RANGE_NOT_RESOLVED = "time_range_not_resolved"
    UNKNOWN_METRIC = "unknown_metric"
    DISABLED_METRIC = "disabled_metric"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNSUPPORTED_DIMENSION = "unsupported_dimension"
    UNSUPPORTED_PLAN = "unsupported_plan"
    AMBIGUOUS_USER_COUNT = "ambiguous_user_count_semantics"
    DATA_RANGE_MISMATCH = "data_range_mismatch"
    FILTER_UNSUPPORTED = "filter_unsupported"
    SQL_GUARD_REJECTED = "sql_guard_rejected"


class QueryBuildError(Exception):
    """SQL 生成失败（结构化错误，不产生可执行查询）。"""

    def __init__(self, code: str, message: str, details: list[str] | None = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details or []

    def as_dict(self) -> dict:
        return {"error_code": self.code, "message": self.message,
                "details": self.details}


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------


@dataclass
class GeneratedQuery:
    name: str
    sql: str
    params: list
    columns: list[str]
    purpose: str
    metric_ids: list[str] = field(default_factory=list)
    window_label: str | None = None
    group_by: list[str] = field(default_factory=list)
    # 实际执行的日期窗口（截取后的有效范围；None=该查询无日期条件）
    window_start: str | None = None
    window_end: str | None = None
    # 用户**原始请求**的窗口（若发生截取，与上面的有效窗口不同）
    requested_window_start: str | None = None
    requested_window_end: str | None = None
    # 参数含义标签（与 params 一一对应；供展示层按白名单决定是否公开）
    # 取值示例："start_date" / "end_date" / "limit" / "filter:city"
    param_labels: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "sql": self.sql,
            "param_count": len(self.params),
            "columns": self.columns,
            "purpose": self.purpose,
            "metric_ids": self.metric_ids,
            "window_label": self.window_label,
            "group_by": self.group_by,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "requested_window_start": self.requested_window_start,
            "requested_window_end": self.requested_window_end,
            "param_labels": list(self.param_labels),
        }


@dataclass
class QueryBundle:
    question: str
    analysis_type: str
    queries: list[GeneratedQuery]
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "analysis_type": self.analysis_type,
            "queries": [q.as_dict() for q in self.queries],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# 指标 → SQL 表达式
# ---------------------------------------------------------------------------

MONEY_METRICS = {"purchase_amount", "redeem_amount", "net_inflow"}
MARKET_METRICS = {"daily_yield", "seven_day_yield", "shibor_rate"}
STATIC_PROFILE_METRICS = {"user_count"}
PERIOD_USER_METRICS = {"active_user_count"}
# 由资金表驱动（决定时间字段与日期过滤）
FUND_DRIVEN_METRICS = MONEY_METRICS | PERIOD_USER_METRICS
USER_DIMENSIONS = ("city", "sex", "constellation")
CATEGORY_FILTER_OPERATORS = ("eq", "ne", "in")

_AGG_FUNC = {
    StatOperation.SUM: "SUM",
    StatOperation.AVG: "AVG",
    StatOperation.MIN: "MIN",
    StatOperation.MAX: "MAX",
    StatOperation.COUNT: "COUNT",
    StatOperation.COUNT_DISTINCT: "COUNT_DISTINCT",
}

_OPERATOR_SQL = {
    "eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
}

_SOURCE_BALANCE = "user_balance_table b"
_SOURCE_PROFILE = "user_profile_table p"
_SOURCE_MARKET = "mfd_day_share_interest y"
_SOURCE_SHIBOR = "v_mfd_bank_shibor_daily_filled s"


def _metric_field_sql(metric_id: str, term: str | None) -> str:
    """指标的基础字段表达式（不含聚合函数；用户计数类特殊处理）。"""
    if metric_id == "purchase_amount":
        return "{a}.total_purchase_amt"
    if metric_id == "redeem_amount":
        return "{a}.total_redeem_amt"
    if metric_id == "net_inflow":
        return "NET_INFLOW"
    if metric_id == "daily_yield":
        return "{a}.mfd_daily_yield"
    if metric_id == "seven_day_yield":
        return "{a}.mfd_7daily_yield"
    if metric_id == "shibor_rate":
        if term is None:
            raise QueryBuildError(BuildErrorCode.UNSUPPORTED_OPERATION,
                                  "shibor_rate 缺少期限参数 term")
        try:
            return "{a}." + mc.shibor_column(term)
        except KeyError as e:
            raise QueryBuildError(BuildErrorCode.UNSUPPORTED_OPERATION, str(e))
    if metric_id in STATIC_PROFILE_METRICS or metric_id in PERIOD_USER_METRICS:
        return "USER_COUNT"
    raise QueryBuildError(BuildErrorCode.UNKNOWN_METRIC, f"未知指标 {metric_id}")


def _aggregate_expression(metric_id: str, term: str | None, alias: str,
                          op: StatOperation) -> str:
    if metric_id == "net_inflow":
        if op != StatOperation.SUM:
            raise QueryBuildError(
                BuildErrorCode.UNSUPPORTED_OPERATION,
                f"net_inflow 仅支持 SUM（当前 {op.value}）")
        return f"SUM({alias}.total_purchase_amt) - SUM({alias}.total_redeem_amt)"
    if metric_id in STATIC_PROFILE_METRICS or metric_id in PERIOD_USER_METRICS:
        if op not in (StatOperation.COUNT_DISTINCT, StatOperation.COUNT):
            raise QueryBuildError(
                BuildErrorCode.UNSUPPORTED_OPERATION,
                f"{metric_id} 仅支持 COUNT_DISTINCT/COUNT（当前 {op.value}）")
        return f"COUNT(DISTINCT {alias}.user_id)"
    field_sql = _metric_field_sql(metric_id, term).format(a=alias)
    func = _AGG_FUNC.get(op)
    if func is None or func == "COUNT_DISTINCT":
        raise QueryBuildError(
            BuildErrorCode.UNSUPPORTED_OPERATION,
            f"指标 {metric_id} 不支持聚合操作 {op.value}")
    return f"{func}({field_sql})"


def _metric_source(metric_id: str) -> str:
    if metric_id in FUND_DRIVEN_METRICS:
        return "b"
    if metric_id in ("daily_yield", "seven_day_yield"):
        return "y"
    if metric_id == "shibor_rate":
        return "s"
    if metric_id in STATIC_PROFILE_METRICS:
        return "p"
    raise QueryBuildError(BuildErrorCode.UNKNOWN_METRIC, f"未知指标 {metric_id}")


# ---------------------------------------------------------------------------
# 数据源与关联解析
# ---------------------------------------------------------------------------


@dataclass
class _QueryContext:
    from_clause: str
    time_alias: str
    time_field: str
    needs_date_filter: bool
    notes: list[str] = field(default_factory=list)


def _resolve_context(plan: AnalysisPlan) -> _QueryContext:
    """解析数据源、JOIN 与时间字段；未审核的组合直接拒绝。"""
    metrics = [m.metric_id for m in plan.metrics]
    dims = list(plan.group_by)
    filter_fields = {f.field for f in plan.filters}
    notes: list[str] = []

    has_money = any(m in MONEY_METRICS for m in metrics)
    has_period_users = any(m in PERIOD_USER_METRICS for m in metrics)
    has_static_users = any(m in STATIC_PROFILE_METRICS for m in metrics)
    has_fund = has_money or has_period_users
    has_yield = any(m in ("daily_yield", "seven_day_yield") for m in metrics)
    has_shibor = "shibor_rate" in metrics

    if has_static_users and (has_fund):
        raise QueryBuildError(
            BuildErrorCode.AMBIGUOUS_USER_COUNT,
            "静态画像指标 user_count 不能与资金指标/期间用户数在同一查询中混用"
            "——INNER JOIN 会把静态画像总人数污染成资金表中的期间去重用户数",
            details=["确需同时返回两种口径时，应拆分为独立查询（本版本未实现）",
                     "期间用户数请使用 active_user_count；静态总数请单独查询"])

    needs_profile = (has_static_users or has_period_users
                     or any(d in USER_DIMENSIONS for d in dims)
                     or bool(filter_fields & set(USER_DIMENSIONS)))

    # ---- 组合审核 ----
    market_count = sum([has_yield, has_shibor])
    if market_count > 1 and (has_fund or needs_profile):
        raise QueryBuildError(
            BuildErrorCode.UNSUPPORTED_PLAN,
            "收益率与 SHIBOR 同时与资金/用户指标混合的组合尚未审核（只允许单一市场指标）")
    if has_yield and has_shibor:
        raise QueryBuildError(
            BuildErrorCode.UNSUPPORTED_PLAN, "收益率与 SHIBOR 的组合尚未审核")
    if (has_yield or has_shibor) and needs_profile:
        raise QueryBuildError(
            BuildErrorCode.UNSUPPORTED_PLAN,
            "市场指标（收益率/SHIBOR）与用户属性维度/用户计数的组合尚未审核")

    # ---- FROM / JOIN ----
    joins: list[str] = []
    if has_fund:
        from_clause = _SOURCE_BALANCE
        time_alias, time_field = "b", "report_date"
        if needs_profile:
            joins.append(f"JOIN {_SOURCE_PROFILE} ON b.user_id = p.user_id")
        if has_yield:
            joins.append(f"JOIN {_SOURCE_MARKET} ON b.report_date = y.mfd_date")
            notes.append("资金与收益率按日期关联（report_date = mfd_date，已审核）")
        elif has_shibor:
            joins.append(f"JOIN {_SOURCE_SHIBOR} ON b.report_date = s.mfd_date")
            notes.append("资金与 SHIBOR 按日期关联（report_date = mfd_date，已审核）")
        if has_period_users:
            notes.append("active_user_count 为指定时间窗口内出现过资金记录的去重用户数"
                         "（同一用户多天只计一次）")
    elif has_yield:
        from_clause = _SOURCE_MARKET
        time_alias, time_field = "y", "mfd_date"
    elif has_shibor:
        from_clause = _SOURCE_SHIBOR
        time_alias, time_field = "s", "mfd_date"
    elif has_static_users:
        from_clause = _SOURCE_PROFILE
        time_alias, time_field = "p", ""    # 画像表无日期字段
        notes.append("user_count 为静态用户画像总人数（画像表无日期字段），"
                     "不代表任何时间窗口内的活跃用户数")
        if plan.time_range.type != TimeRangeType.FULL_RANGE:
            raise QueryBuildError(
                BuildErrorCode.AMBIGUOUS_USER_COUNT,
                "user_count 为静态画像总人数，不受时间范围约束；"
                "若需指定时间窗口内的去重用户数，请改用 active_user_count 指标"
                "（或把时间范围改为 full_range）",
                details=["user_count：静态快照总人数（仅 full_range）",
                         "active_user_count：期间去重用户数（需有限起止日期）"])
    else:
        raise QueryBuildError(BuildErrorCode.UNKNOWN_METRIC,
                              f"无法解析指标集合 {metrics}")

    # ---- 维度合法性（已审核映射）----
    for d in dims:
        if d in USER_DIMENSIONS:
            if not needs_profile:
                raise QueryBuildError(
                    BuildErrorCode.UNSUPPORTED_DIMENSION,
                    f"维度 {d} 需要用户属性表关联，但当前组合未启用该关联")
        elif d == "report_date":
            if not has_fund:
                raise QueryBuildError(
                    BuildErrorCode.UNSUPPORTED_DIMENSION,
                    "维度 report_date 仅适用于资金/期间用户指标")
        elif d == "mfd_date":
            if has_fund:
                raise QueryBuildError(
                    BuildErrorCode.UNSUPPORTED_DIMENSION,
                    "维度 mfd_date 与资金指标混合尚未审核（请用 report_date）")
            if not (has_yield or has_shibor):
                raise QueryBuildError(
                    BuildErrorCode.UNSUPPORTED_DIMENSION,
                    "维度 mfd_date 仅适用于市场指标")
        else:
            raise QueryBuildError(BuildErrorCode.UNSUPPORTED_DIMENSION,
                                  f"未审核的分组维度: {d}")

    return _QueryContext(
        from_clause=from_clause + (" " + " ".join(joins) if joins else ""),
        time_alias=time_alias,
        time_field=time_field,
        needs_date_filter=bool(time_field),
        notes=notes)


# ---------------------------------------------------------------------------
# 维度别名与 SELECT 构造
# ---------------------------------------------------------------------------


def _dim_alias(dim: str, ctx: _QueryContext) -> str:
    if dim in USER_DIMENSIONS:
        return "p"
    if dim == "report_date":
        return "b"
    if dim == "mfd_date":
        return ctx.time_alias if ctx.time_alias in ("y", "s") else "y"
    raise QueryBuildError(BuildErrorCode.UNSUPPORTED_DIMENSION,
                          f"未审核的分组维度: {dim}")


def _dedup(seq: list[str]) -> list[str]:
    out: list[str] = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def _filter_sql(f: dict, ctx: _QueryContext, params: list,
                labels: list | None = None) -> str:
    field = f["field"]
    op = f["operator"]
    value = f["value"]

    if field in USER_DIMENSIONS:
        alias = "p"
        if op not in CATEGORY_FILTER_OPERATORS:
            raise QueryBuildError(
                BuildErrorCode.FILTER_UNSUPPORTED,
                f"类别字段 {field} 只允许 eq/ne/in（当前 {op}）")
    elif field == "report_date":
        alias = "b"
    elif field == "mfd_date":
        alias = ctx.time_alias if ctx.time_alias in ("y", "s") else "y"
    else:
        raise QueryBuildError(BuildErrorCode.FILTER_UNSUPPORTED,
                              f"未审核的筛选字段: {field}")

    col = f"{alias}.{field}"
    domain = mc.CATEGORY_DOMAINS.get(field)

    def _bind(v):
        if domain is not None:
            try:
                return domain.to_binding(v)
            except ValueError as e:
                raise QueryBuildError(BuildErrorCode.FILTER_UNSUPPORTED, str(e))
        return v

    if op == "between":
        lo, hi = value
        params.extend([lo, hi])
        if labels is not None:
            labels.extend([f"filter:{f['field']}"] * 2)
        return f"{col} BETWEEN ? AND ?"
    if op == "in":
        placeholders = ", ".join("?" for _ in value)
        bound = [_bind(v) for v in value]
        params.extend(bound)
        if labels is not None:
            labels.extend([f"filter:{f['field']}"] * len(bound))
        return f"{col} IN ({placeholders})"
    if op in _OPERATOR_SQL:
        params.append(_bind(value))
        if labels is not None:
            labels.append(f"filter:{f['field']}")
        return f"{col} {_OPERATOR_SQL[op]} ?"
    raise QueryBuildError(BuildErrorCode.FILTER_UNSUPPORTED,
                          f"不支持的筛选操作符: {op}")


def _resolve_window(plan: AnalysisPlan, notes: list[str],
                    data_range: tuple[date, date] | None = None
                    ) -> tuple[date, date, date | None, date | None]:
    tr = plan.time_range
    if not tr.is_resolved or tr.resolved_start_date is None or tr.resolved_end_date is None:
        raise QueryBuildError(BuildErrorCode.TIME_RANGE_NOT_RESOLVED,
                              "时间范围尚未解析为具体日期")
    if data_range is not None:
        data_start, data_end = data_range
    else:
        data_start = date.fromisoformat(mc.DATA_DATE_MIN)
        data_end = date.fromisoformat(mc.DATA_DATE_MAX)
    start, end = tr.resolved_start_date, tr.resolved_end_date

    if start > data_end or end < data_start:
        raise QueryBuildError(
            BuildErrorCode.DATA_RANGE_MISMATCH,
            f"请求区间 {start} 至 {end} 完全超出数据覆盖范围 "
            f"（{data_start} 至 {data_end}）")
    clipped_start = max(start, data_start)
    clipped_end = min(end, data_end)
    if (clipped_start, clipped_end) != (start, end):
        notes.append(
            f"请求区间 {start} 至 {end} 部分超出数据覆盖范围，已截取为 "
            f"{clipped_start} 至 {clipped_end}")

    comp_start = comp_end = None
    if tr.comparison_start_date and tr.comparison_end_date:
        cs, ce = tr.comparison_start_date, tr.comparison_end_date
        if cs <= data_end and ce >= data_start:
            comp_start, comp_end = max(cs, data_start), min(ce, data_end)
            if (comp_start, comp_end) != (cs, ce):
                notes.append(
                    f"对比窗口 {cs} 至 {ce} 部分超出数据覆盖范围，已截取为 "
                    f"{comp_start} 至 {comp_end}")
        else:
            notes.append(f"对比窗口 {cs} 至 {ce} 完全超出数据覆盖范围，已跳过")
    return clipped_start, clipped_end, comp_start, comp_end


def _agg_columns(plan: AnalysisPlan) -> tuple[list[str], list[str], list[str]]:
    declared_ops = [op for op in plan.statistics if op in _AGG_FUNC]
    if not declared_ops:
        raise QueryBuildError(BuildErrorCode.UNSUPPORTED_OPERATION,
                              "计划中没有可用的聚合操作（SUM/AVG/COUNT/...）")
    per_metric_ops: dict[str, list[StatOperation]] = {}
    for spec in plan.metrics:
        m = mc.get_metric(spec.metric_id)
        ops = [op for op in declared_ops if op.value in m.allowed_operations]
        if not ops:
            raise QueryBuildError(
                BuildErrorCode.UNSUPPORTED_OPERATION,
                f"指标 {spec.metric_id} 没有可用的聚合操作"
                f"（允许: {list(m.allowed_operations)}）")
        per_metric_ops[spec.metric_id] = ops

    multi_column = (len(plan.metrics) > 1
                    or any(len(v) > 1 for v in per_metric_ops.values()))
    cols, names, ids = [], [], []
    # 去重键含期限：同一 (metric_id, term, op) 只输出一个聚合列；
    # 同一指标的不同 term 视为不同口径，必须各出一列（07.5.2）。
    emitted: set[tuple[str, str | None, str]] = set()
    for spec in plan.metrics:
        alias = _metric_source(spec.metric_id)
        for op in per_metric_ops[spec.metric_id]:
            key = (spec.metric_id, spec.term, op.value)
            if key in emitted:
                continue      # 完全相同的 (指标, 期限, 口径) 只输出一次
            emitted.add(key)
            expr = _aggregate_expression(spec.metric_id, spec.term, alias, op)
            user_count_metric = (spec.metric_id in STATIC_PROFILE_METRICS
                                 or spec.metric_id in PERIOD_USER_METRICS)
            if spec.term:
                # 参数化指标（不同期限）：别名必须含期限，避免同名冲突与歧义
                col_name = f"{spec.metric_id}_{spec.term.lower()}_{op.value.lower()}"
            elif not multi_column:
                col_name = spec.metric_id
            elif user_count_metric:
                # 用户计数的指标名本身即口径，无需再拼操作名
                col_name = spec.metric_id
            else:
                col_name = f"{spec.metric_id}_{op.value.lower()}"
            cols.append(f"{expr} AS {col_name}")
            names.append(col_name)
        ids.append(spec.metric_id)
    return cols, names, ids


def _build_select(plan: AnalysisPlan, ctx: _QueryContext,
                  window: tuple[date, date] | None,
                  window_label: str | None,
                  notes: list[str],
                  dims: list[str],
                  order_and_limit: bool,
                  requested_window: tuple[date, date] | None = None
                  ) -> GeneratedQuery:
    """构造单条查询：dims 中所有维度都进入 SELECT / GROUP BY / ORDER BY。"""
    agg_cols, col_names, metric_ids = _agg_columns(plan)
    dims = _dedup([d for d in dims if d])
    select_parts: list[str] = []
    columns: list[str] = []

    if window_label:
        select_parts.append(f"'{window_label}' AS window_label")
        columns.append("window_label")
    for d in dims:
        select_parts.append(f"{_dim_alias(d, ctx)}.{d} AS {d}")
        columns.append(d)
    select_parts.extend(agg_cols)
    columns.extend(col_names)

    params: list = []
    param_labels: list[str] = []
    conditions: list[str] = []
    if window is not None and ctx.needs_date_filter:
        conditions.append(f"{ctx.time_alias}.{ctx.time_field} BETWEEN ? AND ?")
        params.extend([window[0].isoformat(), window[1].isoformat()])
        param_labels.extend(["start_date", "end_date"])
    for f in plan.filters:
        conditions.append(_filter_sql(
            {"field": f.field, "operator": f.operator, "value": f.value}, ctx,
            params, param_labels))

    sql = "SELECT " + ", ".join(select_parts) + "\nFROM " + ctx.from_clause
    if conditions:
        sql += "\nWHERE " + "\n  AND ".join(conditions)
    if dims:
        sql += "\nGROUP BY " + ", ".join(f"{_dim_alias(d, ctx)}.{d}" for d in dims)

    if order_and_limit and dims:
        time_dims = [d for d in dims if d in ("report_date", "mfd_date")]
        cat_dims = [d for d in dims if d in USER_DIMENSIONS]
        if time_dims:
            order_parts = [f"{_dim_alias(d, ctx)}.{d}" for d in time_dims + cat_dims]
            sql += "\nORDER BY " + ", ".join(order_parts)
        else:
            # 类别维度排名/分布：按第一个聚合列降序（结果列中可识别）
            sql += f"\nORDER BY {col_names[0]} DESC"
            if StatOperation.TOP_N in plan.statistics:
                sql += "\nLIMIT ?"
                params.append(int(plan.top_n))
                param_labels.append("limit")

    name = plan.analysis_type.value + ("_" + "_".join(dims) if dims else "_summary")
    if window_label:
        name += f"_{window_label}"
    return GeneratedQuery(
        name=name, sql=sql, params=params, columns=columns,
        purpose=_purpose(plan, dims, window_label), metric_ids=metric_ids,
        window_label=window_label, group_by=dims,
        window_start=(window[0].isoformat() if window else None),
        window_end=(window[1].isoformat() if window else None),
        requested_window_start=(requested_window[0].isoformat()
                                if requested_window else None),
        requested_window_end=(requested_window[1].isoformat()
                              if requested_window else None),
        param_labels=param_labels)


def _purpose(plan: AnalysisPlan, dims: list[str], window_label: str | None) -> str:
    dim_desc = "、".join(mc.dimension_label(d) for d in dims) if dims else "无分组"
    base = {
        AnalysisType.TIME_TREND: f"按{dim_desc}汇总指标时序",
        AnalysisType.GROUP_RANKING: f"按{dim_desc}汇总并排序",
        AnalysisType.DISTRIBUTION: f"按{dim_desc}统计分布",
        AnalysisType.DESCRIPTIVE: "描述性汇总（无分组）",
        AnalysisType.ANOMALY: "提取分析所需的时序数据（异常检测属后续阶段）",
        AnalysisType.CORRELATION: "提取多指标时序（相关性计算属后续阶段）",
        AnalysisType.WINDOW_COMPARISON: "提取单窗口汇总用于对比（差值与变化率属后续阶段）",
    }.get(plan.analysis_type, "查询")
    if window_label:
        base += f"（{window_label} 窗口）"
    return base


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_queries(plan: AnalysisPlan,
                  data_range: tuple[date, date] | None = None) -> QueryBundle:
    """把分析计划编译为查询 bundle（失败抛 QueryBuildError）。

    `data_range`：数据集实际覆盖范围（默认取真实数据常量；测试可注入合成库范围）。
    """
    # ---- 生成前强制检查 ----
    if plan.clarification_needed:
        raise QueryBuildError(BuildErrorCode.CLARIFICATION_REQUIRED,
                              "计划需要澄清，不得生成查询")
    if plan.unresolved:
        raise QueryBuildError(BuildErrorCode.UNRESOLVED_PRESENT,
                              f"计划存在未解决事项: {plan.unresolved}")
    if not plan.time_range.is_resolved:
        raise QueryBuildError(BuildErrorCode.TIME_RANGE_NOT_RESOLVED,
                              "时间范围尚未解析为具体日期")
    if not plan.is_executable_ready():
        raise QueryBuildError(BuildErrorCode.PLAN_NOT_EXECUTABLE,
                              "计划不满足可执行条件")
    for spec in plan.metrics:
        try:
            mc.get_metric(spec.metric_id)
        except KeyError:
            raise QueryBuildError(BuildErrorCode.UNKNOWN_METRIC,
                                  f"未知指标: {spec.metric_id}")
        except ValueError as e:
            raise QueryBuildError(BuildErrorCode.DISABLED_METRIC, str(e))

    notes: list[str] = []
    ctx = _resolve_context(plan)
    notes.extend(ctx.notes)
    start, end, comp_start, comp_end = _resolve_window(plan, notes, data_range)
    # 用户**原始请求**窗口（未截取）；用于展示"请求 → 有效"的差异
    _tr = plan.time_range
    req_main = ((_tr.resolved_start_date, _tr.resolved_end_date)
                if _tr.resolved_start_date and _tr.resolved_end_date else None)
    req_comp = ((_tr.comparison_start_date, _tr.comparison_end_date)
                if _tr.comparison_start_date and _tr.comparison_end_date else None)
    plan_dims = _dedup(list(plan.group_by))

    queries: list[GeneratedQuery] = []

    if plan.analysis_type == AnalysisType.WINDOW_COMPARISON:
        if not ctx.time_field:
            raise QueryBuildError(
                BuildErrorCode.UNSUPPORTED_PLAN,
                "所选指标没有时间字段，无法生成窗口对比查询")
        if comp_start is None or comp_end is None:
            raise QueryBuildError(
                BuildErrorCode.TIME_RANGE_NOT_RESOLVED,
                "窗口对比缺少可用的对比窗口日期")
        # 主窗口与对比窗口使用相同的分组维度（保证可匹配）
        queries.append(_build_select(plan, ctx, (start, end), "main", notes,
                                     plan_dims, order_and_limit=False,
                                     requested_window=req_main))
        queries.append(_build_select(plan, ctx, (comp_start, comp_end), "comparison",
                                     notes, plan_dims, order_and_limit=False,
                                     requested_window=req_comp))
        notes.append("主窗口与对比窗口分别由独立查询返回，分组维度一致，"
                     "window_label 标识来源；差值与变化率由后续分析层计算")
    elif plan.analysis_type in (AnalysisType.TIME_TREND, AnalysisType.ANOMALY,
                                AnalysisType.CORRELATION):
        if not ctx.time_field:
            raise QueryBuildError(
                BuildErrorCode.UNSUPPORTED_PLAN,
                f"所选指标没有时间字段，无法生成 {plan.analysis_type.value} 时间序列；"
                "静态画像指标请改用分组统计（group_ranking/descriptive/distribution）")
        # 时间维度必须保留；叠加计划指定的用户属性维度
        time_dim = "report_date" if ctx.time_field == "report_date" else "mfd_date"
        dims = _dedup([time_dim] + plan_dims)
        queries.append(_build_select(plan, ctx, (start, end), None, notes,
                                     dims, order_and_limit=True,
                                     requested_window=req_main))
        if plan.analysis_type == AnalysisType.ANOMALY:
            notes.append("本阶段仅提取时序数据；异常检测/归因属后续阶段")
        if plan.analysis_type == AnalysisType.CORRELATION:
            notes.append("本阶段仅提取多指标时序；相关性计算属后续阶段")
    elif plan.analysis_type == AnalysisType.GROUP_RANKING:
        if not plan_dims:
            raise QueryBuildError(BuildErrorCode.UNSUPPORTED_PLAN,
                                  "分组排名分析缺少分组维度")
        queries.append(_build_select(plan, ctx, (start, end), None, notes,
                                     plan_dims, order_and_limit=True,
                                     requested_window=req_main))
    elif plan.analysis_type == AnalysisType.DISTRIBUTION:
        queries.append(_build_select(plan, ctx, (start, end), None, notes,
                                     plan_dims, order_and_limit=bool(plan_dims),
                                     requested_window=req_main))
    elif plan.analysis_type == AnalysisType.DESCRIPTIVE:
        queries.append(_build_select(plan, ctx, (start, end), None, notes,
                                     [], order_and_limit=False,
                                     requested_window=req_main))
    else:
        raise QueryBuildError(BuildErrorCode.UNSUPPORTED_PLAN,
                              f"未支持的分析类型: {plan.analysis_type.value}")

    # ---- sql_guard 静态预检查（真正执行仍在 QueryService）----
    for q in queries:
        v = sql_guard.validate_sql(q.sql)
        if not v.passed:
            raise QueryBuildError(
                BuildErrorCode.SQL_GUARD_REJECTED,
                f"生成的 SQL 未通过安全校验: {q.name}",
                details=v.reasons)

    return QueryBundle(question=plan.question,
                       analysis_type=plan.analysis_type.value,
                       queries=queries, notes=notes)
