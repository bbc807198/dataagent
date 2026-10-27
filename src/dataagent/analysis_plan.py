"""结构化分析计划 Schema（Pydantic v2，第四阶段）。

自然语言解析的唯一输出形态。设计原则：

1. **封闭白名单**：指标、维度、操作全部来自 `metric_catalog`，模型不得发明；
2. **无 SQL 字段**：计划中不存在可执行 SQL 字符串（`extra="forbid"` + 显式拒绝），
   后续 SQL 生成必须单独开发并经过 `sql_guard` 校验；
3. **时间范围可追溯**：相对时间保留原始表述与锚点定义，解析状态显式标记
   （`resolved` / `pending`），不得编造日期；
4. **用户级保护**：`user_id` 不可作为分组维度；分组维度必须来自白名单。
"""

from __future__ import annotations

import re
from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import metric_catalog as mc


# 静态画像指标（无日期字段）与资金/期间用户指标的组合约束（任务 05.2）
STATIC_PROFILE_METRICS = {"user_count"}
FUND_OR_PERIOD_METRICS = {"purchase_amount", "redeem_amount", "net_inflow",
                          "active_user_count"}
TIME_SERIES_ANALYSIS_TYPES = {"time_trend", "anomaly", "correlation",
                              "window_comparison"}
# 仅支持单指标的分析类型（分布/窗口对比）；排名与描述性支持多指标
SINGLE_METRIC_ANALYSIS_TYPES = {"distribution", "window_comparison"}
# 仅支持单指标的分析类型（分布/窗口对比）；排名与描述性支持多指标
SINGLE_METRIC_ANALYSIS_TYPES = {"distribution", "window_comparison"}


class AnalysisType(str, Enum):
    TIME_TREND = "time_trend"            # 时间趋势
    WINDOW_COMPARISON = "window_comparison"  # 两个时间窗口对比
    GROUP_RANKING = "group_ranking"      # 分组排名
    DISTRIBUTION = "distribution"        # 分布统计
    ANOMALY = "anomaly"                  # 异常识别
    CORRELATION = "correlation"          # 多指标关联分析
    DESCRIPTIVE = "descriptive"          # 描述性汇总


class StatOperation(str, Enum):
    SUM = "SUM"
    AVG = "AVG"
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    MIN = "MIN"
    MAX = "MAX"
    PERIOD_OVER_PERIOD = "PERIOD_OVER_PERIOD"  # 环比
    CHANGE_RATE = "CHANGE_RATE"                # 变化率
    WINDOW_DIFF = "WINDOW_DIFF"                # 两窗口差值
    TOP_N = "TOP_N"                            # Top N 排名


class TimeRangeType(str, Enum):
    EXPLICIT_DATES = "explicit_dates"        # 明确起止日期
    LAST_N_DAYS = "last_n_days"              # 最近 N 天
    LAST_N_MONTHS = "last_n_months"          # 最近 N 个自然月
    PREVIOUS_MONTH = "previous_month"        # 上一个自然月
    WINDOW_COMPARISON = "window_comparison"  # 与上一时间窗口比较
    FULL_RANGE = "full_range"                # 全量数据范围


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"   # 已解析为具体日期
    PENDING = "pending"     # 待解析（缺少锚点或参数），不得编造日期


class TimeRangeSpec(BaseModel):
    """时间范围规格。

    锚点固定为数据集实际最大日期（`max_available_data_date`）——
    当前数据截止 2014 年，**不以系统当前日期为锚点**。
    """

    model_config = ConfigDict(extra="forbid")

    type: TimeRangeType
    relative_period: str | None = Field(
        default=None, description="用户原始相对时间表述，如“最近三个月”")
    anchor: str = Field(
        default="max_available_data_date",
        description="相对时间的锚点定义；当前唯一合法值为数据集实际最大日期")
    n: int | None = Field(default=None, ge=1, le=120, description="最近 N 天/月 的 N")
    resolved_start_date: date | None = None
    resolved_end_date: date | None = None
    comparison_start_date: date | None = None   # 上一窗口（已解析）
    comparison_end_date: date | None = None
    resolution_status: ResolutionStatus = ResolutionStatus.PENDING
    unresolved: list[str] = Field(
        default_factory=list, description="未能解析的原因（pending 时填写）")

    @field_validator("anchor")
    @classmethod
    def _anchor_must_be_data_max(cls, v: str) -> str:
        if v != "max_available_data_date":
            raise ValueError(
                f"不支持的锚点定义: {v}；相对时间必须以数据集实际最大日期为锚点")
        return v

    @model_validator(mode="after")
    def _check_resolved_fields(self):
        if self.resolution_status == ResolutionStatus.RESOLVED:
            if self.resolved_start_date is None or self.resolved_end_date is None:
                raise ValueError("resolution_status=resolved 时必须提供起止日期")
            if self.resolved_start_date > self.resolved_end_date:
                raise ValueError("resolved_start_date 不得晚于 resolved_end_date")
        else:
            if self.resolved_start_date is not None or self.resolved_end_date is not None:
                raise ValueError(
                    "resolution_status=pending 时不得填充具体日期（不得编造日期）")
        return self

    @property
    def is_resolved(self) -> bool:
        return self.resolution_status == ResolutionStatus.RESOLVED


class MetricSpec(BaseModel):
    """计划中的指标引用（引用 metric_catalog，不得发明）。"""

    model_config = ConfigDict(extra="forbid")

    metric_id: str = Field(description="指标目录中的指标 ID")
    term: str | None = Field(
        default=None, description="SHIBOR 期限参数（O_N/1W/2W/1M/3M/6M/9M/1Y）")

    @field_validator("metric_id")
    @classmethod
    def _must_be_known_enabled_metric(cls, v: str) -> str:
        try:
            mc.get_metric(v)
        except KeyError:
            raise ValueError(f"未知指标: {v}（可用指标: {sorted(mc.METRIC_CATALOG)}）")
        except ValueError as e:
            raise ValueError(str(e))
        return v

    @field_validator("term")
    @classmethod
    def _term_must_be_supported(cls, v: str | None) -> str | None:
        """SHIBOR 期限必须属于目录支持集合（Schema 阶段拒绝未知期限）。"""
        if v is not None and v not in mc.SHIBOR_TERM_COLUMNS:
            raise ValueError(
                f"未知 SHIBOR 期限: {v}（可选: {sorted(mc.SHIBOR_TERM_COLUMNS)}）")
        return v

    @model_validator(mode="after")
    def _shibor_requires_term(self):
        if self.metric_id == "shibor_rate" and self.term is None:
            raise ValueError("shibor_rate 必须通过 term 指定期限")
        if self.term is not None and self.metric_id != "shibor_rate":
            raise ValueError(f"指标 {self.metric_id} 不接受 term 参数")
        return self


class FilterCondition(BaseModel):
    """结构化筛选条件（不保存未经验证的 SQL 字符串）。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="筛选字段（必须来自允许维度/白名单列）")
    operator: str = Field(description="eq/ne/gt/gte/lt/lte/between/in")
    value: str | int | float | list[str | int | float] | None = None

    @field_validator("field")
    @classmethod
    def _field_must_be_known(cls, v: str) -> str:
        if v == "user_id":
            raise ValueError("不允许以 user_id 作为筛选目标（用户级保护）")
        if not mc.is_known_dimension(v):
            raise ValueError(
                f"筛选字段必须是已审核维度之一: {sorted(mc.ALLOWED_DIMENSIONS)}")
        return v

    @field_validator("value", mode="before")
    @classmethod
    def _reject_bool_values(cls, v):
        """bool 是 int 子类，会被静默转换为 1/0——显式拒绝（任务 04.1-C）。"""
        if isinstance(v, bool):
            raise ValueError("参数值不支持布尔类型")
        if isinstance(v, (list, tuple)):
            for item in v:
                if isinstance(item, bool):
                    raise ValueError("参数值列表不支持布尔元素")
        return v

    @field_validator("operator")
    @classmethod
    def _operator_must_be_known(cls, v: str) -> str:
        if v not in {"eq", "ne", "gt", "gte", "lt", "lte", "between", "in"}:
            raise ValueError(f"不支持的筛选操作符: {v}")
        return v

    @model_validator(mode="after")
    def _validate_value_shape(self):
        """参数值形状与类型校验（任务 04.1-C / 05.1-C）。

        规则：
        - between：恰好两个边界值；
        - in：非空列表；
        - 比较操作符（eq/ne/gt/gte/lt/lte）不得接收列表；
        - 日期字段（report_date/mfd_date）的值必须可解析为 ISO 日期；
        - **类别字段（city/sex/constellation）只允许 eq/ne/in**——它们是离散类别编码，
          取值范围比较（gt/gte/lt/lte/between）没有业务语义（城市编码不是连续数值）；
        - 类别字段值必须命中已知取值域；无法确定类型或形状不合法 → 默认拒绝。
        参数化查询设计不变：这里只校验值的形状，不拼接 SQL。
        """
        op, val = self.operator, self.value
        date_fields = {"report_date", "mfd_date"}
        category_fields = {"city", "sex", "constellation"}

        if self.field in category_fields and op not in ("eq", "ne", "in"):
            raise ValueError(
                f"类别字段 {self.field} 只允许 eq/ne/in（当前 {op}）；"
                "类别编码不是连续数值，不支持范围比较")

        if op == "between":
            if not isinstance(val, (list, tuple)) or len(val) != 2:
                raise ValueError("between 需要恰好两个边界值 [start, end]")
            if not all(isinstance(b, (str, int, float)) and not isinstance(b, bool)
                       for b in val):
                raise ValueError("between 边界必须是字符串或数值")
        elif op == "in":
            if not isinstance(val, (list, tuple)) or len(val) == 0:
                raise ValueError("in 需要非空列表")
        elif op in ("eq", "ne", "gt", "gte", "lt", "lte"):
            if isinstance(val, (list, tuple)):
                raise ValueError(f"操作符 {op} 不接收列表参数（多值请用 in）")
            if val is None:
                raise ValueError(f"操作符 {op} 需要一个标量值")
            if isinstance(val, bool):
                raise ValueError(f"操作符 {op} 不接收布尔值")

        values = list(val) if isinstance(val, (list, tuple)) else [val]
        if self.field in date_fields:
            for v in values:
                if v is None:
                    continue
                if not isinstance(v, str):
                    raise ValueError(f"日期字段 {self.field} 的值必须是 ISO 日期字符串")
                # 严格 YYYY-MM-DD：date.fromisoformat 在 Python 3.11+ 也接受
                # 紧凑格式 20140601，会造成与数据字典格式不一致的静默变化。
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    raise ValueError(
                        f"日期值格式必须为 YYYY-MM-DD: {v!r}（不得使用紧凑格式或斜杠分隔）")
                try:
                    date.fromisoformat(v)
                except ValueError:
                    raise ValueError(f"日期值无法解析: {v}（应为合法日历日期）")
        if self.field in category_fields:
            domain = mc.CATEGORY_DOMAINS[self.field]
            for v in values:
                if not isinstance(v, (str, int)) or isinstance(v, bool):
                    raise ValueError(
                        f"类别字段 {self.field} 的值必须是字符串或整数编码")
                if domain.normalize(v) is None:
                    raise ValueError(
                        f"类别字段 {self.field} 的值 {v!r} 不在已知取值域内"
                        f"（允许: {sorted(domain.values)}）")
        return self


class OutputRequirement(BaseModel):
    """用户输出需求（结构化）。"""

    model_config = ConfigDict(extra="forbid")

    summary_table: bool = True
    trend_chart: bool = False
    ranking_chart: bool = False
    comparison_chart: bool = False
    chinese_report: bool = True
    cause_analysis: bool = False   # 只能建立在实际查询证据上


# ---------------------------------------------------------------------------
# 未解决事项分级（任务 04.1-A）
# ---------------------------------------------------------------------------

def blocking_unresolved(unresolved: list[str]) -> list[str]:
    """未解决事项默认全部视为阻塞项（无法静态判断影响范围时默认拒绝）。

    设计依据：
    - `unresolved` 的语义是"尚未解决的事项"，只要非空就可能影响指标、
      时间范围、统计口径或分析目标——一律阻止计划进入可执行状态；
    - 一般说明性内容应放入 `notes`（永不被检查），不放入 `unresolved`；
    - 这避免了关键词枚举的漏判（如"分析目标不明确"不含任何列出的关键词）。
    """
    return list(unresolved)


class ComparisonAlignment(str, Enum):
    """窗口比较的对齐方式（受控枚举；禁止模型自由指定表达式）。

    - EXACT_GROUP：默认。按真实分类维度（如城市）匹配两窗口记录，
      保留"缺失分组不补零"的既有语义。
    - DAY_OF_MONTH：跨年同月逐日比较。按**月内日期**（1—31）对齐
      （例如 2013 年 8 月 1 日 ↔ 2014 年 8 月 1 日），
      同时保留两窗口的原始完整日期以便展示与审计。
    """

    EXACT_GROUP = "exact_group"
    DAY_OF_MONTH = "day_of_month"


class AnalysisPlan(BaseModel):
    """结构化分析计划（自然语言解析的唯一输出形态）。"""

    model_config = ConfigDict(extra="forbid")   # 模型输出任何额外字段（含 sql）一律拒绝

    question: str = Field(description="原始用户问题")
    intent: str = Field(description="识别出的主要意图（中文简述）")
    analysis_type: AnalysisType
    metrics: list[MetricSpec] = Field(min_length=1)
    time_range: TimeRangeSpec
    group_by: list[str] = Field(default_factory=list)
    filters: list[FilterCondition] = Field(default_factory=list)
    statistics: list[StatOperation] = Field(default_factory=list)
    outputs: OutputRequirement = Field(default_factory=OutputRequirement)
    top_n: int = Field(default=10, ge=1, le=100,
                       description="TOP_N 排名的 N（仅在统计操作含 TOP_N 时生效）")
    # 窗口比较的对齐方式（仅窗口比较可用；day_of_month 需按月内日期逐日对齐）
    comparison_alignment: ComparisonAlignment = Field(
        default=ComparisonAlignment.EXACT_GROUP,
        description="窗口比较对齐方式：exact_group（按分类维度，默认）/ "
                    "day_of_month（跨年同月逐日，按月内日期 1—31 对齐）")

    # 澄清与不确定性（解析层填写）
    clarification_needed: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list, description="暂无法确认的部分")
    notes: list[str] = Field(default_factory=list, description="口径与限制说明")

    # ---------------- 校验器 ----------------

    @field_validator("group_by")
    @classmethod
    def _dimensions_must_be_whitelisted(cls, v: list[str]) -> list[str]:
        for d in v:
            if d == "user_id":
                raise ValueError("不允许把 user_id 作为分组维度（用户级保护）")
            if not mc.is_known_dimension(d):
                raise ValueError(
                    f"未知分组维度: {d}（允许: {sorted(mc.ALLOWED_DIMENSIONS)}）")
        return v

    @model_validator(mode="after")
    def _validate_comparison_alignment(self):
        """day_of_month 只允许在"时间窗口比较 + 单一日期维度"场景使用。"""
        if self.comparison_alignment == ComparisonAlignment.EXACT_GROUP:
            return self
        if self.analysis_type != AnalysisType.WINDOW_COMPARISON:
            raise ValueError(
                "comparison_alignment=day_of_month 仅适用于 window_comparison 分析")
        if self.time_range.type != TimeRangeType.WINDOW_COMPARISON:
            raise ValueError(
                "comparison_alignment=day_of_month 要求时间范围类型为 window_comparison")
        date_dims = [d for d in self.group_by if d in ("report_date", "mfd_date")]
        if len(date_dims) != 1 or len(self.group_by) != 1:
            raise ValueError(
                "comparison_alignment=day_of_month 要求 group_by 恰好为一个日期维度"
                "（report_date 或 mfd_date），不得混入其他维度")
        return self

    @field_validator("statistics")
    @classmethod
    def _operations_must_be_known(cls, v: list[StatOperation]) -> list[StatOperation]:
        return v  # 枚举本身即白名单；与指标的兼容性见 model_validator

    @model_validator(mode="after")
    def _check_metric_operation_compatibility(self):
        """多指标场景下操作按指标分别适用（任务 05：资金+收益率混合分析）。

        - 每个统计操作必须被**至少一个**选定指标支持（否则无意义）；
        - 每个指标必须被**至少一个**统计操作覆盖（否则无法生成聚合）；
        - 生成器为每个指标选取"该指标允许且计划声明"的操作集合。
        """
        for op in self.statistics:
            if not any(op.value in mc.get_metric(m.metric_id).allowed_operations
                       for m in self.metrics):
                allowed = sorted({a for m in self.metrics
                                  for a in mc.get_metric(m.metric_id).allowed_operations})
                raise ValueError(
                    f"操作 {op.value} 不被任何选定指标支持（可用操作: {allowed}）")
        for spec in self.metrics:
            m = mc.get_metric(spec.metric_id)
            if not any(op.value in m.allowed_operations for op in self.statistics):
                raise ValueError(
                    f"指标 {spec.metric_id} 没有可用的统计操作"
                    f"（允许: {list(m.allowed_operations)}）")
            for d in self.group_by:
                if d not in m.allowed_dimensions:
                    raise ValueError(
                        f"指标 {spec.metric_id} 不支持维度 {d}"
                        f"（允许: {list(m.allowed_dimensions)}）")
        return self

    @model_validator(mode="after")
    def _check_metric_analysis_compatibility(self):
        """静态画像指标的口径约束（任务 05.2-A/C）。

        1. 静态画像总人数（画像表无日期字段）不得与资金指标或期间用户数
           出现在同一聚合查询中——INNER JOIN 会把它污染成"期间去重用户数"；
        2. 静态画像指标不支持时间序列类分析（趋势/异常/关联/窗口对比），
           不得伪造时间维度。
        确需同时返回两种口径时，应拆分为独立查询（本版本未实现该扩展）。
        """
        ids = {m.metric_id for m in self.metrics}
        if ids & STATIC_PROFILE_METRICS and ids & FUND_OR_PERIOD_METRICS:
            raise ValueError(
                "静态画像指标 user_count 不能与资金指标/期间用户数混用"
                f"（当前指标: {sorted(ids)}）；两种口径需拆分为独立查询，"
                "期间用户数请使用 active_user_count")
        if (ids & STATIC_PROFILE_METRICS
                and self.analysis_type.value in TIME_SERIES_ANALYSIS_TYPES):
            raise ValueError(
                f"静态画像指标 user_count 不支持 {self.analysis_type.value} 分析"
                "（用户画像表无日期字段，无法形成时间序列）；"
                "请改用分组统计（group_ranking/descriptive/distribution）")
        # 指标去重（07.5.1）：模型可能重复输出同一指标（冗余），
        # 会让 SQL 生成两个同名聚合列（DuckDB 自动改名 _1）并导致分析层拒绝。
        # 同一 (metric_id, term) 只保留首个；不同 term 视为不同口径，予以保留。
        seen_metric_keys: set[tuple[str, str | None]] = set()
        unique_metrics = []
        dropped_metrics: list[str] = []
        for m in self.metrics:
            key = (m.metric_id, m.term)
            if key in seen_metric_keys:
                dropped_metrics.append(m.metric_id)
                continue
            seen_metric_keys.add(key)
            unique_metrics.append(m)
        if dropped_metrics:
            self.metrics = unique_metrics
            if self.notes is not None:
                self.notes.append(
                    "已合并重复指标：" + "、".join(sorted(set(dropped_metrics)))
                    + "（同一指标只保留一个聚合口径）")
        # 单指标分析类型：多指标必须拒绝（06.2-A，不得只处理第一列）
        if (self.analysis_type.value in SINGLE_METRIC_ANALYSIS_TYPES
                and len(self.metrics) > 1):
            raise ValueError(
                f"{self.analysis_type.value} 仅支持单指标分析"
                f"（当前 {len(self.metrics)} 个：{[m.metric_id for m in self.metrics]}）；"
                "请分别提交单指标计划")
        # 单指标分析类型：多指标必须拒绝（06.2-A，不得只处理第一列）
        if (self.analysis_type.value in SINGLE_METRIC_ANALYSIS_TYPES
                and len(self.metrics) > 1):
            raise ValueError(
                f"{self.analysis_type.value} 仅支持单指标分析"
                f"（当前 {len(self.metrics)} 个：{[m.metric_id for m in self.metrics]}）；"
                "请分别提交单指标计划")
        # 相关性分析：恰好两个指标，且不支持用户属性分组（06.1-B）
        if self.analysis_type.value == "correlation":
            if len(self.metrics) != 2:
                raise ValueError(
                    f"相关性分析要求恰好 2 个指标（当前 {len(self.metrics)} 个）；"
                    "第三个及之后的指标不会被分析")
            if any(d in ("city", "sex", "constellation") for d in self.group_by):
                raise ValueError(
                    "相关性分析暂不支持用户属性分组（city/sex/constellation）——"
                    "同日期多行无法可靠按日期对齐；请仅按日期对齐")
        return self

    @model_validator(mode="after")
    def _check_analysis_type_requirements(self):
        if self.analysis_type == AnalysisType.GROUP_RANKING and not self.group_by:
            raise ValueError("分组排名分析必须指定分组维度")
        if (self.analysis_type == AnalysisType.WINDOW_COMPARISON
                and self.time_range.type != TimeRangeType.WINDOW_COMPARISON):
            raise ValueError("窗口对比分析要求 time_range.type=window_comparison")
        return self

    # ---------------- 便捷方法 ----------------

    def metric_ids(self) -> list[str]:
        return [s.metric_id for s in self.metrics]

    def blocking_unresolved(self) -> list[str]:
        """未解决事项（非空即不可执行；一般说明请放入 notes）。"""
        return blocking_unresolved(self.unresolved)

    def is_executable_ready(self) -> bool:
        """计划是否已具体到可进入 SQL 生成阶段（本阶段不执行 SQL）。

        条件：无需澄清、无未解决事项（unresolved 为空）、时间范围已解析、
        有指标与统计操作。
        """
        return (not self.clarification_needed
                and not self.unresolved
                and self.time_range.is_resolved
                and bool(self.metrics)
                and bool(self.statistics))
