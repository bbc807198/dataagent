"""SQL 安全校验器（AST 级）。

基于 SQLGlot 的抽象语法树实现，**不依赖正则表达式作为唯一安全措施**。校验维度：

1. 语句类型：仅允许单条 SELECT（含只读 CTE）；拒绝一切写操作/管理语句/命令。
2. 表与视图白名单：仅允许已审核的四张业务表与 SHIBOR 填充视图；递归检查 CTE、
   子查询中的所有数据源；拒绝跨库/未知 schema 引用。
3. 外部访问阻断：拒绝 read_csv/read_parquet/read_json/glob 等表函数及一切匿名
   （未识别）函数、生成序列函数等。
4. 用户级数据保护（任务 03.1 修复后）：
   - 禁止 SELECT * 导出；
   - ``user_id`` **只允许**出现在 ``COUNT(user_id)`` / ``COUNT(DISTINCT user_id)``
     的投影参数中，以及 WHERE / JOIN ON 的筛选关联中；
     ``MIN/MAX/SUM/AVG(user_id)``、算术/Cast/Case/窗口包装、GROUP BY / ORDER BY /
     HAVING / QUALIFY 中的 user_id 引用一律拒绝（别名、CTE、子查询不构成豁免）；
   - 14 个用户级金额/余额列只允许出现在聚合函数参数或 WHERE/JOIN ON 中；
     投影、GROUP BY、ORDER BY、HAVING、QUALIFY、窗口中的裸引用一律在 AST 阶段拒绝，
     **不依赖数据库执行层报错兜底**。
5. 列-表静态绑定：限定符列按表别名/表名绑定到具体数据源并校验列存在性；
   无限定符列仅在唯一候选数据源中放行，多表歧义或未知列拒绝；
   CTE/子查询输出名与投影别名参与绑定（ORDER BY/HAVING 可引用投影别名）。
6. 函数白名单与已知列校验：未知函数、未知字段一律拒绝并给出清晰原因。

校验失败时不执行 SQL；返回结构化 ValidationResult，不含堆栈/路径/秘密。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

logging.getLogger("sqlglot").setLevel(logging.ERROR)

DIALECT = "duckdb"

# ---------------------------------------------------------------------------
# 白名单定义（与 db.py 的 Schema 保持一致；由测试保证同步）
# ---------------------------------------------------------------------------

ALLOWED_TABLES = {
    "user_balance_table",
    "user_profile_table",
    "mfd_day_share_interest",
    "mfd_bank_shibor",
    "v_mfd_bank_shibor_daily_filled",
}

TABLE_COLUMNS: dict[str, set[str]] = {
    "user_balance_table": {
        "user_id", "report_date", "tBalance", "yBalance", "total_purchase_amt",
        "direct_purchase_amt", "purchase_bal_amt", "purchase_bank_amt", "total_redeem_amt",
        "consume_amt", "transfer_amt", "tftobal_amt", "tftocard_amt", "share_amt",
        "category1", "category2", "category3", "category4",
    },
    "user_profile_table": {"user_id", "sex", "city", "constellation"},
    "mfd_day_share_interest": {"mfd_date", "mfd_daily_yield", "mfd_7daily_yield"},
    "mfd_bank_shibor": {
        "mfd_date", "Interest_O_N", "Interest_1_W", "Interest_2_W", "Interest_1_M",
        "Interest_3_M", "Interest_6_M", "Interest_9_M", "Interest_1_Y",
    },
    "v_mfd_bank_shibor_daily_filled": {
        "mfd_date", "source_obs_date", "is_filled", "Interest_O_N", "Interest_1_W",
        "Interest_2_W", "Interest_1_M", "Interest_3_M", "Interest_6_M",
        "Interest_9_M", "Interest_1_Y",
    },
}

# 用户级表（默认必须聚合口径；不允许逐条导出）
USER_LEVEL_TABLES: set[str] = {"user_balance_table", "user_profile_table"}

# 用户级表上的维度列（裸投影必须处于聚合/分组口径）
GRAIN_DIMENSION_COLUMNS: set[str] = {
    "report_date", "mfd_date", "city", "sex", "constellation",
}

KNOWN_COLUMNS: set[str] = set().union(*TABLE_COLUMNS.values())
# 大小写不敏感比较（DuckDB 标识符大小写不敏感）
KNOWN_COLUMNS_LOWER: set[str] = {c.lower() for c in KNOWN_COLUMNS}

# 用户级/金额级列：仅允许聚合参数或 WHERE/JOIN ON
USER_LEVEL_COLUMNS: set[str] = {
    "user_id", "tBalance", "yBalance", "total_purchase_amt", "direct_purchase_amt",
    "purchase_bal_amt", "purchase_bank_amt", "total_redeem_amt", "consume_amt",
    "transfer_amt", "tftobal_amt", "tftocard_amt", "share_amt",
    "category1", "category2", "category3", "category4",
}
USER_LEVEL_COLUMNS_LOWER: set[str] = {c.lower() for c in USER_LEVEL_COLUMNS}

# 可作为裸投影/裸分组的已审核维度（不含用户标识）
DIMENSION_COLUMNS: set[str] = {"report_date", "mfd_date", "city", "sex", "constellation"}
DIMENSION_COLUMNS_LOWER: set[str] = {c.lower() for c in DIMENSION_COLUMNS}

# 宏观/市场数据列（非用户数据），可裸投影
MACRO_COLUMNS: set[str] = {
    "Interest_O_N", "Interest_1_W", "Interest_2_W", "Interest_1_M", "Interest_3_M",
    "Interest_6_M", "Interest_9_M", "Interest_1_Y",
    "mfd_daily_yield", "mfd_7daily_yield", "source_obs_date", "is_filled",
}
MACRO_COLUMNS_LOWER: set[str] = {c.lower() for c in MACRO_COLUMNS}

# 聚合函数（用户级列只允许作为其参数出现）
# 注意：仅使用 sqlglot 27.x 实际存在的表达式类；不存在专类的函数
# （如 var_pop/string_agg/ntile/read_json）按 Anonymous 处理并受名称白名单约束。
AGGREGATE_FUNCS = (
    exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max, exp.Median, exp.Stddev,
    exp.StddevPop, exp.StddevSamp, exp.Variance,
    exp.PercentileCont, exp.PercentileDisc, exp.Quantile, exp.ApproxQuantile,
    exp.ApproxDistinct, exp.First, exp.Last, exp.AnyValue,
    exp.ArgMax, exp.ArgMin, exp.Corr, exp.CovarPop, exp.CovarSamp,
)

# 允许的函数（按 sqlglot 函数名；Anonymous 函数单独按名称白名单放行）
FUNC_ALLOWLIST: set[str] = {
    # 聚合
    "SUM", "AVG", "MIN", "MAX", "COUNT", "MEDIAN", "STDDEV", "STDDEV_SAMP",
    "STDDEV_POP", "VARIANCE", "VAR_SAMP", "VAR_POP", "PERCENTILE_CONT",
    "PERCENTILE_DISC", "QUANTILE_CONT", "QUANTILE_DISC", "FIRST", "LAST",
    "ANY_VALUE", "ARG_MAX", "ARG_MIN", "APPROX_COUNT_DISTINCT", "APPROX_QUANTILE",
    "CORR", "COVAR_POP", "COVAR_SAMP",
    # 数值
    "ABS", "CEIL", "CEILING", "FLOOR", "ROUND", "POWER", "POW", "SQRT", "EXP",
    "LN", "LOG", "LOG2", "LOG10", "MOD", "SIGN", "GREATEST", "LEAST", "NANVL",
    # 条件与转换
    "COALESCE", "NULLIF", "IFNULL", "TRY_CAST", "CAST", "CASE", "IF",
    # 逻辑连接符（sqlglot 中 And/Or/Not 继承 Func，属安全布尔组合）
    "AND", "OR", "NOT", "XOR",
    # 日期
    "DATE_TRUNC", "DATE_PART", "EXTRACT", "DATEDIFF", "DATE_DIFF", "DATE_ADD",
    "DATE_SUB", "DATE_BIN", "MAKE_DATE", "LAST_DAY", "EPOCH", "STRFTIME",
    "STR_TO_UNIX", "UNIXEPOCH",
    # 字符串
    "LOWER", "UPPER", "LENGTH", "CHAR_LENGTH", "SUBSTRING", "SUBSTR", "CONCAT",
    "CONCAT_WS", "TRIM", "LTRIM", "RTRIM", "REPLACE", "STRPOS", "POSITION",
    "LEFT", "RIGHT", "REPEAT", "REVERSE", "LPAD", "RPAD", "SPLIT_PART",
    "STARTS_WITH", "ENDS_WITH", "CONTAINS", "STRING_SPLIT", "LISTAGG",
    "STRING_AGG", "FORMAT",
    # 窗口
    "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE", "ROW_NUMBER", "RANK",
    "DENSE_RANK", "NTILE", "PERCENT_RANK", "CUME_DIST", "NTH_VALUE",
    # 日期提取（sqlglot 专类）
    "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "WEEK", "QUARTER",
    "DAYOFWEEK", "DAYOFWEEK_ISO", "DAYOFYEAR", "ISODOW", "WEEKDAY", "LAST_DAY",
}

# 允许的匿名函数名（DuckDB 中 sqlglot 未建类的常用函数，小写比较）
# 注意：集合聚合类函数（list/listagg/string_agg/array_agg/histogram 等）一律
# **不在此白名单**——它们可将多条记录收集进单个结果单元格，属默认拒绝项。
ANONYMOUS_FUNC_ALLOWLIST: set[str] = {
    "year", "month", "day", "hour", "minute", "second", "week", "weekday",
    "dayofweek", "dayofweek_iso", "dayofyear", "quarter", "isodow",
    "date_part", "date_trunc", "datediff", "date_add", "date_sub", "last_day",
    "make_date", "strftime", "epoch", "epoch_ms", "current_date", "now",
    "abs", "round", "ceil", "ceiling", "floor", "power", "sqrt", "exp",
    "ln", "log", "log2", "log10", "mod", "sign", "coalesce", "nullif",
    "ifnull", "greatest", "least",
    "lower", "upper", "length", "char_length", "substring", "substr",
    "concat", "concat_ws", "trim", "ltrim", "rtrim", "replace", "strpos",
    "position", "left", "right", "repeat", "reverse", "lpad", "rpad",
    "split_part", "starts_with", "ends_with", "contains",
    "array_length",
    # sqlglot 无专类、以 Anonymous 解析的聚合/窗口函数
    "ntile", "var_pop", "var_samp", "stddev_pop", "stddev_samp",
    "quantile_cont", "quantile_disc", "percentile_cont", "percentile_disc",
    "approx_count_distinct", "approx_quantile", "covar_pop", "covar_samp",
    "corr", "regr_slope", "regr_intercept", "regr_r2", "regr_avgx",
    "regr_avgy", "regr_count", "regr_sxx", "regr_sxy", "regr_syy",
    "arg_max", "arg_min", "any_value", "first", "last", "mode",
}

# 拒绝的语句/命令/外部读取节点（整树扫描）
# 注意：仅使用 sqlglot 27.x 实际存在的表达式类；read_json/read_text/uninstall 等
# 无专类的会落入 exp.Command 或 exp.Anonymous，同样被拒绝。
# 集合聚合函数（List/GroupConcat/ArrayAgg/Array）一律拒绝：它们能把多条用户级
# 记录收集进单个结果单元格，是用户标识与属性保护的绕过路径。
COLLECTION_FUNC_CLASSES = (exp.List, exp.GroupConcat, exp.ArrayAgg, exp.Array)

FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Drop, exp.Delete, exp.Update, exp.Insert, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Attach, exp.Detach, exp.Copy, exp.Pragma, exp.Set,
    exp.Install, exp.Use, exp.Transaction, exp.Commit,
    exp.Rollback, exp.Command, exp.ReadCSV, exp.ReadParquet,
    exp.Anonymous, exp.GenerateSeries, exp.Merge, exp.Describe,
    exp.Export, exp.Comment, exp.Grant, exp.SemanticView,
) + COLLECTION_FUNC_CLASSES

FORBIDDEN_NODE_NAMES = {
    "Export": "EXPORT/IMPORT 数据导入导出语句",
    "List": "禁止集合聚合函数 LIST()（可将多条用户级记录收集进单个结果单元格）",
    "GroupConcat": "禁止集合聚合函数 STRING_AGG/LISTAGG()（可将多条用户级记录收集进单个结果单元格）",
    "ArrayAgg": "禁止集合聚合函数 ARRAY_AGG()（可将多条用户级记录收集进单个结果单元格）",
    "Array": "禁止数组构造函数 LIST_VALUE()（可将多条用户级记录收集进单个结果单元格）",
}

# 子句上下文描述（用于错误信息）
_CLAUSE_DESC = {
    "where": "WHERE 筛选", "join": "JOIN 关联条件", "having": "HAVING",
    "qualify": "QUALIFY", "order": "ORDER BY", "group": "GROUP BY",
    "window": "窗口函数", "projection": "查询投影",
}


# ---------------------------------------------------------------------------
# 校验结果
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    statement_type: str | None = None
    tables: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    normalized_sql: str | None = None
    rules_applied: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "statement_type": self.statement_type,
            "tables": self.tables,
            "functions": self.functions,
            "columns": self.columns,
            "normalized_sql": self.normalized_sql,
            "rules_applied": self.rules_applied,
        }


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------

def _ancestor_has_aggregate(node: exp.Expression) -> bool:
    """节点到 Select 之间的祖先链中是否存在聚合函数。"""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, AGGREGATE_FUNCS):
            return True
        if isinstance(parent, exp.Select):
            return False
        parent = parent.parent
    return False


def _clause_context(col: exp.Column) -> str:
    """列引用所处的 SQL 子句上下文。"""
    parent = col.parent
    while parent is not None:
        if isinstance(parent, exp.Where):
            return "where"
        if isinstance(parent, exp.Join):
            return "join"
        if isinstance(parent, exp.Having):
            return "having"
        if isinstance(parent, exp.Qualify):
            return "qualify"
        if isinstance(parent, (exp.Order, exp.Ordered)):
            return "order"
        if isinstance(parent, exp.Group):
            return "group"
        if isinstance(parent, exp.Window):
            return "window"
        if isinstance(parent, exp.Select):
            return "projection"
        parent = parent.parent
    return "unknown"


def _collect_output_names(query: exp.Expression, out: set[str]) -> None:
    """递归收集查询（Select/Union 等）投影输出的别名与列名。"""
    if isinstance(query, exp.Select):
        for e in query.expressions:
            if isinstance(e, exp.Alias):
                out.add(e.alias.lower())
            elif isinstance(e, exp.Column):
                out.add(e.name.lower())
    elif isinstance(query, exp.SetOperation):
        _collect_output_names(query.this, out)
        _collect_output_names(query.expression, out)


def _inside_cte(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.CTE):
            return True
        parent = parent.parent
    return False


def _child_expressions(node: exp.Expression):
    """产出节点的直接子表达式（遍历 args，兼容 list 值）。"""
    for v in node.args.values():
        if isinstance(v, exp.Expression):
            yield v
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, exp.Expression):
                    yield item


def _select_own_nodes(select: exp.Select, cls):
    """产出归属本 SELECT 的某类节点（不进入子 SELECT，子查询前停止）。"""

    def visit(node):
        for child in _child_expressions(node):
            if isinstance(child, exp.Select):
                continue
            if isinstance(child, cls):
                yield child
            if isinstance(child, exp.Subquery):
                continue  # 子查询由该子 SELECT 自行检查
            yield from visit(child)

    yield from visit(select)


# ---------------------------------------------------------------------------
# 数据源收集（列-表绑定）
# ---------------------------------------------------------------------------

def _collect_sources(select: exp.Select, cte_defs: dict[str, dict]) -> list[dict]:
    """收集本 SELECT 可见的数据源：[{keys, cols, label, user_level}]。

    - Table 节点：keys={别名, 表名}，cols=白名单表列或 CTE 输出列；
      user_level=该表是否为用户级表（或引用用户级 CTE）；
    - Subquery 节点：keys={别名}，cols=子查询投影输出名；user_level=False
      （子查询内部的粒度由该子 SELECT 自行检查）；
    - CTE 引用（Table.name 为 CTE 名）：cols/user_level 来自 cte_defs。
    """
    sources: list[dict] = []

    def visit(node):
        for child in _child_expressions(node):
            if isinstance(child, exp.Select):
                continue
            if isinstance(child, exp.Subquery):
                alias = child.alias.lower()
                cols: set[str] = set()
                _collect_output_names(child.this, cols)
                sources.append({"keys": {alias} if alias else set(),
                                "cols": cols, "label": f"子查询[{alias or '?'}]",
                                "user_level": False})
                continue
            if isinstance(child, exp.Table):
                tname = child.name.lower()
                alias = child.alias.lower()
                keys = {k for k in (alias, tname) if k}
                if tname in cte_defs:
                    cols = {c.lower() for c in cte_defs[tname]["cols"]}
                    label = f"CTE[{tname}]"
                    user_level = cte_defs[tname]["user_level"]
                else:
                    cols = {c.lower() for c in TABLE_COLUMNS.get(tname, set())}
                    label = tname or "?"
                    user_level = tname in USER_LEVEL_TABLES
                sources.append({"keys": keys, "cols": cols, "label": label,
                                "user_level": user_level})
                continue
            visit(child)

    visit(select)
    return sources


def _cte_is_user_level(cte: exp.CTE) -> bool:
    """CTE 是否处于用户级粒度：内部直接引用用户级表且没有任何 GROUP BY。

    CTE 内有 GROUP BY（或纯聚合）时，其输出已是聚合粒度，外层透传不视为用户级。
    """
    tables = {t.name.lower() for t in cte.this.find_all(exp.Table)}
    if not (tables & USER_LEVEL_TABLES):
        return False
    has_group = any(isinstance(n, exp.Group) for n in cte.this.find_all(exp.Group))
    return not has_group


def _projection_is_aggregated(expr: exp.Expression) -> bool:
    """投影表达式是否整体处于聚合口径（其中所有 Column 都被聚合函数包裹）。"""
    cols = list(expr.find_all(exp.Column))
    if not cols:
        return True  # 常量/字面量
    return all(_ancestor_has_aggregate(c) for c in cols)


def _inside_window(node: exp.Expression) -> bool:
    """节点是否位于窗口函数参数（ORDER BY/PARTITION BY）内。"""
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Window):
            return True
        if isinstance(parent, exp.Select):
            return False
        parent = parent.parent
    return False


def _output_grain_columns(expr: exp.Expression) -> list[exp.Column]:
    """投影表达式中决定输出粒度的列：不在聚合参数内、不在窗口参数内。

    窗口函数不改变行粒度，但其 ORDER BY/PARTITION 参数不是输出投影的一部分，
    因此不参与粒度判定（输出粒度由最外层投影与 GROUP BY 决定）。
    """
    out = []
    for c in expr.find_all(exp.Column):
        if _ancestor_has_aggregate(c):
            continue
        if _inside_window(c):
            continue
        out.append(c)
    return out


def _check_grain(select: exp.Select, sources: list[dict], reasons: list[str]) -> None:
    """用户级粒度保护（任务 03.2-A）。

    规则：若 SELECT 的数据源包含用户级表/CTE，则投影中决定输出粒度的维度列
    必须被 GROUP BY 覆盖（列名或同一表达式），或整体处于聚合口径；
    DISTINCT 单列投影允许（输出去重维度值，不构成用户记录导出）。
    窗口函数不改变行粒度：窗口内的裸维度列同样要求 GROUP BY 覆盖。
    CTE/子查询逐层递归检查，JOIN 不构成豁免。
    """
    if not any(s["user_level"] for s in sources):
        return

    # 投影 core（剥掉输出别名）
    proj_cores = [e.this if isinstance(e, exp.Alias) else e for e in select.expressions]
    proj_alias_to_core = {
        e.alias.lower(): (e.this if isinstance(e, exp.Alias) else e)
        for e in select.expressions if isinstance(e, exp.Alias)
    }

    group = select.args.get("group")
    group_exprs = list(group.expressions) if group else []
    expanded: list[exp.Expression] = []
    for g in group_exprs:
        if isinstance(g, exp.Literal) and not g.is_string and g.is_number:
            idx = int(g.name) - 1
            if 0 <= idx < len(proj_cores):
                expanded.append(proj_cores[idx])
                continue
        if isinstance(g, exp.Column) and g.name.lower() in proj_alias_to_core:
            expanded.append(proj_alias_to_core[g.name.lower()])
            continue
        expanded.append(g)
    group_cols = {g.name.lower() for g in expanded if isinstance(g, exp.Column)}
    group_sqls = {g.sql(dialect=DIALECT) for g in expanded}
    distinct = bool(select.args.get("distinct"))
    single_projection = len(select.expressions) == 1

    for idx, expr in enumerate(select.expressions):
        core = proj_cores[idx]
        # DISTINCT 单列豁免（任务 03-Final 收紧）：仅当整个查询是 DISTINCT 且
        # 投影是**单个裸维度 Column** 时适用（输出去重维度值，不构成用户记录）。
        # CONCAT/算术/CASE/CAST/嵌套函数等包装表达式、以及伪装成维度名的别名
        # 都不得享受本豁免——它们可能把多个维度组合成更细的粒度。
        if (distinct and single_projection
                and isinstance(core, exp.Column)
                and core.name.lower() in GRAIN_DIMENSION_COLUMNS):
            continue
        grain_cols = _output_grain_columns(core)
        dim_cols = [c for c in grain_cols if c.name.lower() in GRAIN_DIMENSION_COLUMNS]
        if not dim_cols:
            continue  # 纯聚合/常量/窗口计算，或无维度列（其余规则处理）
        if isinstance(core, exp.Column) and core.name.lower() in group_cols:
            continue  # 维度列出现在 GROUP BY
        if core.sql(dialect=DIALECT) in group_sqls:
            continue  # 同一分组键表达式（如 GROUP BY year(report_date)）
        names = ", ".join(sorted({c.name for c in dim_cols}))
        reasons.append(
            f"禁止在无聚合口径时输出用户级表维度列 {names}（第 {idx + 1} 个投影表达式）；"
            "请使用 GROUP BY 分组、聚合函数，或 DISTINCT 单个裸维度列去重")


def _using_columns(select: exp.Select) -> set[str]:
    """JOIN ... USING (col) 的列名集合（这些列在合并后不判歧义）。"""
    out: set[str] = set()
    for join in _select_own_nodes(select, exp.Join):
        using = join.args.get("using")
        if using:
            for c in using:
                name = getattr(c, "name", None) or str(c)
                out.add(name.lower())
    return out


# ---------------------------------------------------------------------------
# user_id 引用分类（任务 03.1-A）
# ---------------------------------------------------------------------------

def _classify_user_id(col: exp.Column) -> tuple[bool, str]:
    """判定单个 user_id 列引用是否合法。

    返回 (allowed, tag)。合法仅两类：
    - filter_join：位于 WHERE / JOIN ON（筛选关联，不输出）；
    - count_user：作为 COUNT(user_id) / COUNT(DISTINCT user_id) 的投影参数，
      且计数表达式之外无其他包装（除输出别名）。
    """
    parent = col.parent
    count_func = None
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, (exp.Where, exp.Join)):
            return True, "filter_join"
        if isinstance(parent, (exp.Group, exp.Order, exp.Ordered, exp.Having,
                               exp.Qualify, exp.Window)):
            return False, "forbidden_clause"
        if isinstance(parent, exp.Func) and count_func is None:
            count_func = parent
        parent = parent.parent
    if count_func is None:
        return False, "bare_or_expression"     # 裸列/算术/Cast/Case 包装
    if not isinstance(count_func, exp.Count):
        return False, "non_count_aggregate"    # MIN/MAX/SUM/AVG/... 一律拒绝
    # 计数表达式之外不允许其他包装（COUNT(user_id)+1 等派生值拒绝）
    p = count_func.parent
    while p is not None and not isinstance(p, exp.Select):
        if not isinstance(p, exp.Alias):
            return False, "wrapped_count"
        p = p.parent
    # 参数只能是 user_id 或 DISTINCT user_id（单列）
    arg = count_func.this
    if isinstance(arg, exp.Distinct):
        exprs = arg.expressions
        if len(exprs) != 1 or not (isinstance(exprs[0], exp.Column)
                                   and exprs[0].name.lower() == "user_id"):
            return False, "count_multi_args"
    elif not (isinstance(arg, exp.Column) and arg.name.lower() == "user_id"):
        return False, "count_other_args"
    return True, "count_user"


_USER_ID_REASON = {
    "forbidden_clause": "禁止在 GROUP BY / ORDER BY / HAVING / QUALIFY / 窗口函数中引用 user_id",
    "bare_or_expression": "禁止裸输出 user_id 或经算术/Cast/CASE 等表达式派生输出",
    "non_count_aggregate": "禁止对 user_id 使用 MIN/MAX/SUM/AVG 等聚合（会暴露用户标识）；仅允许 COUNT(DISTINCT user_id) 计数",
    "wrapped_count": "禁止对 user_id 计数结果做进一步派生（如 COUNT(user_id)+1）",
    "count_multi_args": "COUNT(DISTINCT ...) 只允许单列 user_id 参数",
    "count_other_args": "COUNT 只允许作用于 user_id",
}


# ---------------------------------------------------------------------------
# 逐层检查
# ---------------------------------------------------------------------------

def _check_projection(select: exp.Select, reasons: list[str]) -> None:
    """SELECT * 导出检查（列级规则统一在 _check_columns_binding）。"""
    for idx, expr in enumerate(select.expressions):
        if isinstance(expr, exp.Star):
            reasons.append(
                f"禁止 SELECT * 直接导出原始表记录（第 {idx + 1} 个投影表达式）；"
                "请显式列出列或使用聚合")


def _check_columns_binding(select: exp.Select, reasons: list[str],
                           cte_defs: dict[str, dict]) -> None:
    """列-表绑定 + 用户级列上下文规则 + 用户级粒度保护（对所有层 SELECT 生效）。"""
    in_cte = _inside_cte(select)
    sources = _collect_sources(select, cte_defs)
    using_cols = _using_columns(select)
    # 本层投影别名（ORDER BY/HAVING 可引用）
    aliases = {e.alias.lower() for e in select.expressions if isinstance(e, exp.Alias)}

    # 用户级粒度保护（维度列必须处于聚合口径）
    _check_grain(select, sources, reasons)

    for col in _select_own_nodes(select, exp.Column):
        name = col.name.lower()

        # ---- 无限定符列：先做歧义/未知判定（含 user_id；WHERE/JOIN 中的
        #      无限定引用同样可能歧义，由 DuckDB 语义保证，这里统一前移）----
        qualifier = (col.table or "").lower()
        if not qualifier and not in_cte and name not in aliases:
            candidates = [s for s in sources if name in s["cols"]]
            if not candidates:
                reasons.append(f"未知字段 {name}（不在任何可见数据源中）")
                continue
            if (len(candidates) > 1 and len(sources) > 1
                    and name not in using_cols):
                labels = ", ".join(s["label"] for s in candidates)
                reasons.append(
                    f"字段 {name} 在多个数据源中歧义（{labels}）；请使用表别名限定")
                continue

        # ---- user_id：仅 COUNT 计数或 WHERE/JOIN ----
        if name == "user_id":
            allowed, tag = _classify_user_id(col)
            if not allowed:
                reasons.append(_USER_ID_REASON.get(tag, f"禁止引用 user_id（{tag}）"))
            continue

        # ---- 其他用户级金额/余额列：仅聚合参数或 WHERE/JOIN ON ----
        if name in USER_LEVEL_COLUMNS_LOWER:
            if _ancestor_has_aggregate(col):
                continue
            ctx = _clause_context(col)
            if ctx in ("where", "join"):
                continue
            reasons.append(
                f"禁止在{_CLAUSE_DESC.get(ctx, ctx)}中裸引用用户级金额/余额列 {name}；"
                "资金流水与用户属性查询必须采用聚合口径（如 SUM/AVG/COUNT）"
                if ctx in ("order", "having", "qualify", "group", "window", "projection")
                else f"禁止在无法确定的位置裸引用用户级列 {name}")
            continue

        # ---- 投影别名引用（ORDER BY/HAVING）放行 ----
        if name in aliases:
            continue

        # ---- CTE 内部允许定义新列/递归自引用，跳过绑定未知性 ----
        if in_cte:
            continue

        # ---- 限定符列：绑定到具体数据源并校验列存在性
        #      （无限定符列已在上方统一处理）----
        if qualifier:
            src = next((s for s in sources if qualifier in s["keys"]), None)
            if src is None:
                reasons.append(f"未知表别名或数据源: {qualifier}.{name}")
            elif name not in src["cols"]:
                reasons.append(f"数据源 {src['label']} 中不存在字段 {name}")
            continue


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def validate_sql(sql: str) -> ValidationResult:
    """校验单条 SQL。返回结构化结果；校验失败不执行。"""
    result = ValidationResult(passed=False)
    rules = [
        "single_select_statement",
        "no_write_or_admin_statements",
        "table_view_whitelist",
        "no_external_access_functions",
        "no_select_star_export",
        "user_id_count_only",
        "user_level_columns_aggregate_or_filter_only",
        "column_table_binding",
        "function_whitelist",
    ]

    if not isinstance(sql, str) or not sql.strip():
        result.reasons.append("SQL 为空或不是字符串")
        result.rules_applied = rules
        return result

    # 1) 解析（DuckDB 方言）
    try:
        exprs = sqlglot.parse(sql, dialect=DIALECT)
    except sqlglot.errors.ParseError:
        result.reasons.append("SQL 无法解析（语法错误）")
        result.rules_applied = rules
        return result
    except Exception as e:  # 解析器未知异常一律拒绝
        result.reasons.append(f"SQL 解析失败: {type(e).__name__}")
        result.rules_applied = rules
        return result

    if not exprs or all(e is None for e in exprs):
        result.reasons.append("SQL 解析结果为空")
        result.rules_applied = rules
        return result
    if len(exprs) > 1:
        result.reasons.append(f"只允许单条 SQL 语句；检测到 {len(exprs)} 条语句")
        result.rules_applied = rules
        return result

    root = exprs[0]
    result.statement_type = type(root).__name__

    # 2) 根节点必须是 SELECT
    if not isinstance(root, exp.Select):
        result.reasons.append(f"只允许 SELECT 查询；检测到 {result.statement_type} 语句")
        result.rules_applied = rules
        return result

    # 3) CTE 定义：名称、输出列与粒度标志（供绑定与粒度检查）
    cte_defs: dict[str, dict] = {}
    cte_names: set[str] = set()
    for cte in root.find_all(exp.CTE):
        if cte.alias:
            name = cte.alias.lower()
            cte_names.add(name)
            cols: set[str] = set()
            _collect_output_names(cte.this, cols)
            cte_defs[name] = {"cols": cols, "user_level": _cte_is_user_level(cte)}

    # 4) 整树扫描：禁止节点 / 表白名单 / 函数白名单
    tables: list[str] = []
    functions: list[str] = []

    for node in root.walk():
        # 4a) 禁止的语句/命令/外部读取
        if isinstance(node, FORBIDDEN_NODES):
            name = type(node).__name__
            if isinstance(node, exp.Anonymous):
                fname = node.name.lower()
                if fname in ANONYMOUS_FUNC_ALLOWLIST:
                    functions.append(fname)
                    continue
                result.reasons.append(
                    f"禁止未授权/外部访问函数 {node.name}()（疑似文件读取、系统函数或扩展函数）")
            elif name in FORBIDDEN_NODE_NAMES:
                result.reasons.append(FORBIDDEN_NODE_NAMES[name])
            elif isinstance(node, (exp.ReadCSV, exp.ReadParquet)):
                result.reasons.append(f"禁止外部文件读取函数 {name}")
            else:
                result.reasons.append(f"禁止非 SELECT 语句或命令: {name}")
            continue

        # 4b) 表/视图引用（含 CTE、子查询、JOIN）
        if isinstance(node, exp.Table):
            tname = node.name.lower()
            db = (node.db or "").lower()
            catalog = (node.catalog or "").lower()
            if not tname:
                # 空表名通常是表函数（read_csv/read_parquet 等）的占位 Table 节点
                inner = node.find(exp.ReadCSV, exp.ReadParquet, exp.Anonymous)
                if inner is not None:
                    result.reasons.append(
                        f"禁止外部文件读取或未授权表函数: "
                        f"{getattr(inner, 'name', '') or type(inner).__name__}")
                else:
                    result.reasons.append("禁止空表名引用")
                continue
            if catalog:
                result.reasons.append(f"禁止跨库引用 {catalog}.{db}.{tname}")
                continue
            if db and db != "main":
                result.reasons.append(f"禁止访问非 main schema: {db}.{tname}")
                continue
            if tname not in ALLOWED_TABLES and tname not in cte_names:
                result.reasons.append(
                    f"未授权的表或视图: {tname}；仅允许 {sorted(ALLOWED_TABLES)} 及查询内 CTE")
                continue
            if tname not in tables:
                tables.append(tname)
            continue

        # 4c) 函数白名单（非 Anonymous 的已识别函数）
        if isinstance(node, exp.Func):
            names = [n.upper() for n in node.sql_names()]
            fname = names[0] if names else type(node).__name__.upper()
            if not any(n in FUNC_ALLOWLIST for n in names):
                result.reasons.append(f"未授权函数 {fname}()")
                continue
            if fname not in functions:
                functions.append(fname)
            continue

    # 5) 逐层 SELECT：SELECT * / 用户级列规则 / 列-表绑定
    for select in root.find_all(exp.Select):
        _check_projection(select, result.reasons)
        _check_columns_binding(select, result.reasons, cte_defs)

    result.tables = tables
    result.functions = functions
    result.columns = sorted({c.name.lower() for c in root.find_all(exp.Column)})
    result.rules_applied = rules
    if not result.reasons:
        try:
            result.normalized_sql = root.sql(dialect=DIALECT)
        except Exception:
            result.normalized_sql = sql
        result.passed = True
    return result
