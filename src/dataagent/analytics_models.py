"""分析结果与证据模型（阶段六）。

所有统计结果必须可追溯到阶段五的**实际查询结果**——这里定义结构化容器，
让每条结论都带有：指标口径、查询名称、日期范围、数据点数、公式、警告与局限。

设计原则：
- 状态显式：成功 / 数据不足 / 空结果 / 截断 / 指标不兼容 / 方法不适用 / 执行失败；
- 结论必须携带 `Evidence`（不是自由文本）；
- 证据强度显式标注：直接计算 / 数据观察 / 待验证假设（不得伪装成统计结论）。
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class AnalysisStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    EMPTY_RESULT = "empty_result"
    TRUNCATED = "truncated"                    # 查询结果被截断，结论不完整
    INCOMPATIBLE_METRIC = "incompatible_metric"
    METHOD_NOT_APPLICABLE = "method_not_applicable"
    EXECUTION_FAILED = "execution_failed"


class EvidenceStrength(str, Enum):
    DIRECT_COMPUTATION = "direct_computation"   # 直接由查询结果计算得出
    OBSERVATION = "observation"                 # 数据观察（描述性）
    HYPOTHESIS = "hypothesis_to_verify"         # 待验证假设（不得标注为结论）


class MetricInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: str
    name_zh: str
    unit: str
    formula: str


class Evidence(BaseModel):
    """结论证据：指向具体查询与实际计算。"""

    model_config = ConfigDict(extra="forbid")

    query_name: str
    metric_id: str
    metric_name_zh: str
    unit: str
    date_range: tuple[str, str] | None = None      # **实际执行**的有效日期范围（单窗口）
    requested_range: tuple[str, str] | None = None  # 原始请求范围（截取前）
    # 窗口对比专用：两窗口分别保存，**不得**合并成连续范围
    main_window: tuple[str, str] | None = None
    comparison_window: tuple[str, str] | None = None
    requested_main_window: tuple[str, str] | None = None
    requested_comparison_window: tuple[str, str] | None = None
    data_points: int = Field(ge=0)
    formula: str
    input_summary: str          # 计算输入摘要（首值/末值/样本数等）
    method: str = ""
    warnings: list[str] = Field(default_factory=list)


class Conclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    metric_id: str
    metric_name_zh: str
    unit: str
    query_name: str
    date_range: tuple[str, str] | None = None
    # 窗口对比专用（不连续窗口不得合并为连续 date_range）
    main_window: tuple[str, str] | None = None
    comparison_window: tuple[str, str] | None = None
    data_points: int = Field(ge=0)
    formula: str
    evidence_strength: EvidenceStrength = EvidenceStrength.DIRECT_COMPUTATION
    evidence: Evidence
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 各类统计结果
# ---------------------------------------------------------------------------


class SeriesPoint(BaseModel):
    date: str
    value: float


class DailyChange(BaseModel):
    date: str
    previous_value: float
    current_value: float
    abs_diff: float
    change_rate: float | None = None
    rate_note: str | None = None      # 变化率不可计算的原因


class TrendStats(BaseModel):
    kind: Literal["trend"] = "trend"
    # 指标身份（每条序列必须能识别自己的指标，不得用其他指标的名称/单位标注）
    metric_id: str = ""
    metric_name_zh: str = ""
    term: str | None = None      # 参数化指标的期限（如 SHIBOR 的 O_N / 1W）
    unit: str = ""
    points: list[SeriesPoint]
    daily_changes: list[DailyChange]
    # 可加性：additive=True 时 total 为区间合计；False 时 total=None 且给出说明
    additive: bool = True
    total: float | None = None
    total_note: str | None = None
    period_mean: float | None = None      # 不可加指标（比率/计数）的期间均值
    cumulative_last: float | None = None  # 末值（不是累计总值）
    missing_dates: list[str] = Field(default_factory=list)
    duplicate_dates: list[str] = Field(default_factory=list)
    group: dict[str, str] = Field(default_factory=dict)   # 分组键（如 {"city": "6081949"}）


class TrendSetStats(BaseModel):
    """多序列趋势（多指标 / 多分组 拆分后的独立序列集合）。

    `series` 中每条 `TrendStats` 都带自己的 `metric_id` 与 `group`，
    因此多指标 × 多分组不会互相混淆。
    """

    kind: Literal["trend_set"] = "trend_set"
    series: list[TrendStats]
    group_by: list[str] = Field(default_factory=list)
    metric_ids: list[str] = Field(default_factory=list)


class DescriptiveStats(BaseModel):
    """描述性汇总（单行汇总）；`column_ops` 记录每列的实际聚合操作。"""

    kind: Literal["descriptive"] = "descriptive"
    values: dict[str, float]
    columns: list[str]
    column_ops: dict[str, str] = Field(default_factory=dict)   # 列 -> SUM/AVG/...
    data_points: int


class WindowPair(BaseModel):
    group: dict[str, str]
    main_value: float
    comparison_value: float
    abs_diff: float
    change_rate: float | None = None
    rate_note: str | None = None
    direction: Literal["up", "down", "flat"] = "flat"
    main_rank_by_abs_decline: int | None = None
    main_rank_by_rate: int | None = None
    # 对齐键（如 "08-15"；day_of_month 对齐时为月内日期）
    align_key: str | None = None
    # 两窗口的**原始完整日期**（day_of_month 对齐时保留，供展示与审计）
    main_date: str | None = None
    comparison_date: str | None = None


class WindowComparisonStats(BaseModel):
    kind: Literal["window_comparison"] = "window_comparison"
    pairs: list[WindowPair]
    # 对齐方式：exact_group（默认）/ day_of_month（跨年同月逐日）
    alignment: str = "exact_group"
    main_window: tuple[str, str]
    comparison_window: tuple[str, str]
    missing_in_main: list[dict[str, str]] = Field(default_factory=list)
    missing_in_comparison: list[dict[str, str]] = Field(default_factory=list)
    ranking_note: str | None = None
    # 窗口来源：True=实际执行元数据；False=元数据缺失，窗口标注值来自请求范围
    main_window_from_effective: bool = True
    comparison_window_from_effective: bool = True
    # ---- 月度/全窗口合计（与"仅配对日期合计"严格区分）----
    main_total: float | None = None            # 主窗口全部观测合计
    comparison_total: float | None = None      # 对比窗口全部观测合计
    total_abs_diff: float | None = None        # 月总额差额
    total_change_rate: float | None = None     # 月总额同比（对比窗口为 0 → None）
    total_rate_note: str | None = None
    paired_main_total: float | None = None     # 仅配对日期上的主窗口合计
    paired_comparison_total: float | None = None
    paired_days: int = 0                      # 成功配对的月内日期数
    main_days: int = 0                        # 主窗口观测到的日期数
    comparison_days: int = 0                  # 对比窗口观测到的日期数
    coverage_rate: float | None = None        # 配对覆盖率（配对数/主窗口日期数）
    # ---- 跨年逐日：两个年份各自独立的每日原始金额（双折线图用，单边缺失不丢值）----
    main_daily_amounts: dict[int, float] = Field(default_factory=dict)        # 月内日期 -> 主窗口当日值
    comparison_daily_amounts: dict[int, float] = Field(default_factory=dict)   # 月内日期 -> 对比窗口当日值


class RankingEntry(BaseModel):
    group: dict[str, str]
    value: float
    rank: int
    share: float | None = None


class RankingStats(BaseModel):
    kind: Literal["ranking"] = "ranking"
    entries: list[RankingEntry]
    order: Literal["desc", "asc"] = "desc"
    metric_id: str
    unit: str
    share_note: str | None = None      # 占比的分母说明（如"合计 = 全部城市之和"）


class RankingSetStats(BaseModel):
    """多指标分组排名（每个指标独立排名，互不混用）。"""

    kind: Literal["ranking_set"] = "ranking_set"
    series: list[RankingStats]
    metric_ids: list[str] = Field(default_factory=list)


class DistributionCategory(BaseModel):
    group: dict[str, str]
    count: int
    share: float | None = None


class DistributionStats(BaseModel):
    kind: Literal["distribution"] = "distribution"
    categories: list[DistributionCategory]
    total_count: int
    sample_size: int
    missing_count: int = 0
    share_note: str = "占比 = 该类别计数 / 全部类别计数之和"
    value_stats: dict[str, float] | None = None   # 数值描述性统计（min/max/mean/median）


class AnomalyPoint(BaseModel):
    date: str
    value: float
    center: float
    deviation: float
    robust_z: float
    is_anomaly: bool


class AnomalyStats(BaseModel):
    kind: Literal["anomaly"] = "anomaly"
    method: str = "robust_median_mad"
    threshold_k: float = 3.0
    min_points: int = 5
    sample_size: int
    center: float | None = None
    mad: float | None = None
    scaled_mad: float | None = None
    points: list[AnomalyPoint] = Field(default_factory=list)
    anomalies: list[AnomalyPoint] = Field(default_factory=list)
    note: str = ""


class CorrelationStats(BaseModel):
    kind: Literal["correlation"] = "correlation"
    method: Literal["pearson", "spearman"] = "pearson"
    metric_a: str
    metric_b: str
    n_pairs: int
    coefficient: float | None = None
    status_note: str | None = None
    missing_dates: list[str] = Field(default_factory=list)
    pairs: list[dict[str, str | float]] = Field(default_factory=list)


AnalysisStatistics = Annotated[
    Union[TrendStats, TrendSetStats, WindowComparisonStats, RankingStats,
          RankingSetStats, DistributionStats, AnomalyStats, CorrelationStats,
          DescriptiveStats],
    Field(discriminator="kind"),
]


class AnalysisResult(BaseModel):
    """一次分析的结构化结果（可序列化、可审查）。"""

    model_config = ConfigDict(extra="forbid")

    question: str
    analysis_type: str
    status: AnalysisStatus
    status_reason: str | None = None
    metrics: list[MetricInfo]
    time_range: dict = Field(default_factory=dict)
    group_by: list[str] = Field(default_factory=list)
    query_names: list[str] = Field(default_factory=list)
    method: str = ""
    method_params: dict = Field(default_factory=dict)
    statistics: AnalysisStatistics | None = None
    conclusions: list[Conclusion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    chart_paths: list[str] = Field(default_factory=list)

    def is_success(self) -> bool:
        return self.status == AnalysisStatus.OK

    def as_dict(self) -> dict:
        return self.model_dump(mode="json")
