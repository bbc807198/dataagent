"""Python 统计分析（阶段六 / 06.1 修订）。

**唯一输入**是阶段五 `AnalysisExecutionResult`（每条查询都经过 `sql_guard`
与只读 `QueryService`）。本模块：

- 不连接 DuckDB、不生成/执行 SQL、不重新计算时间范围；
- 只对实际返回的列与行做统计，并产出可追溯的 `Evidence`；
- **多指标全部分析**（不再只取第一列），每条序列自带 `metric_id`/名称/单位；
- **可加性由指标目录决定**：金额类可求和（区间合计）；收益率/利率/去重用户数
  不可跨日相加（给期间均值并注明口径，绝不输出"合计"）；
- **有效日期范围来自查询元数据**（截取后的实际执行窗口），不是原始请求；
- 所有边界（零基数、负基数、缺失日期、重复日期、截断、空结果、样本不足、
  常量序列）都有明确处理规则，不伪造数值。

数学定义（可复现）：

- 日环比绝对变化：`Δ_t = x_t − x_{t−1}`
- 变化率：`rate_t = (x_t − x_{t−1}) / x_{t−1}`；`x_{t−1}=0` → `None` + 原因；
  负基数仍计算但附警告。
- 异常检测（稳健 MAD）：`center=median(x)`，`MAD=median(|x−center|)`，
  `scaled=1.4826·MAD`，`robust_z=(x−center)/scaled`，`|z|>k`（默认 3.0）为异常；
  `MAD=0`（恒定）→ 方法不适用；样本 < 5 → 数据不足。
- Pearson：`r=Σ((x−x̄)(y−ȳ))/√(Σ(x−x̄)²·Σ(y−ȳ)²)`；Spearman：对平均秩计算 Pearson。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from statistics import fmean, median

from . import metric_catalog as mc
from .analytics_models import (
    AnalysisResult,
    AnalysisStatus,
    AnomalyPoint,
    AnomalyStats,
    Conclusion,
    CorrelationStats,
    DailyChange,
    DescriptiveStats,
    DistributionCategory,
    DistributionStats,
    Evidence,
    EvidenceStrength,
    MetricInfo,
    RankingEntry,
    RankingSetStats,
    RankingStats,
    SeriesPoint,
    TrendSetStats,
    TrendStats,
    WindowComparisonStats,
    WindowPair,
)

ANOMALY_MIN_POINTS = 5
ANOMALY_THRESHOLD_K = 3.0
CORRELATION_MIN_PAIRS = 3
CORRELATION_REQUIRED_METRICS = 2

USER_DIMENSIONS = ("city", "sex", "constellation")
# 查询结果中的已知元数据列（非指标、非维度），完整性校验时应排除
META_COLUMNS = {"window_label"}

_CAUSALITY_LIMITATION = "相关性/趋势为统计描述，不代表因果关系"
_ANOMALY_LIMITATION = "异常点为统计偏离（稳健 MAD），未归因于任何外部事件"
_CITY_LIMITATION = "city 为数字编码，无官方名称映射，禁止推断真实城市名称"

# 聚合操作 → 中文措辞（描述性结论必须与实际 SQL 聚合一致）
_OP_LABELS = {
    "SUM": "合计",
    "AVG": "平均值",
    "COUNT": "计数",
    "COUNT_DISTINCT": "去重计数",
    "MIN": "最小值",
    "MAX": "最大值",
}

_MONEY_METRICS = {"purchase_amount", "redeem_amount", "net_inflow"}
_TREND_METRICS = _MONEY_METRICS | {"active_user_count", "daily_yield",
                                   "seven_day_yield", "shibor_rate"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _to_iso(v) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _parse_iso_date(raw) -> date | None:
    """把 YYYY-MM-DD 文本解析为 date；无法解析时返回 None（不猜测）。"""
    if isinstance(raw, date):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _to_float(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def _metric_column_keys(columns: list[str], plan) -> dict[str, tuple[str, str | None]]:
    """列名 → (指标 ID, 期限) 的**精确**映射（07.5.2）。

    匹配规则（不依赖模糊前缀推断）：
    - 参数化指标（term 非空）：列名必须等于 `{metric_id}_{term}_{op}`；
    - 普通指标：列名等于 `{metric_id}_{op}` 或（用户计数类）`{metric_id}`；
    - 一个列若被两个 (指标, 期限) 命中 → 视为歧义，**不映射**
      （后续完整性校验会拒绝分析）。
    """
    mapping: dict[str, tuple[str, str | None]] = {}
    if plan is None:
        return mapping
    for spec in plan.metrics:
        mid, term = spec.metric_id, spec.term
        try:
            allowed = {str(op).lower() for op in mc.get_metric(mid).allowed_operations}
        except (KeyError, ValueError):
            allowed = set()
        candidates: list[str] = []
        if term:
            candidates = [f"{mid}_{term.lower()}_{op}" for op in sorted(allowed)]
        else:
            candidates = [f"{mid}_{op}" for op in sorted(allowed)] + [mid]
        for col in columns:
            if col in candidates and col not in mapping:
                mapping[col] = (mid, term)
                continue
            # DuckDB 自动去重产生的 `X_1` / `X_2` 列：视为同一口径的重复列，
            # 一并映射，使完整性校验报出“同指标多聚合列”并拒绝（不删除、不消歧）。
            m = re.match(r"^(.*)_" + r"\d+$", col)
            if m and m.group(1) in candidates:
                mapping[col] = (mid, term)
    return mapping


def _metric_column_map(columns: list[str], plan) -> dict[str, str]:
    """列名 → 指标 ID（兼容旧接口；期限信息见 `_metric_column_keys`）。"""
    return {col: mid for col, (mid, _term) in
            _metric_column_keys(columns, plan).items()}


def _metric_info(metric_id: str) -> MetricInfo:
    try:
        m = mc.get_metric(metric_id)
        return MetricInfo(metric_id=m.metric_id, name_zh=m.name_zh,
                          unit=m.unit, formula=m.formula)
    except (KeyError, ValueError):
        return MetricInfo(metric_id=metric_id, name_zh=metric_id, unit="",
                          formula="（未在指标目录中登记）")


def _additivity(metric_id: str) -> tuple[bool, str]:
    """从指标目录读取可加性（不可加时返回口径说明）。"""
    try:
        m = mc.get_metric(metric_id)
        return getattr(m, "additive", True), getattr(m, "additivity_note", "")
    except (KeyError, ValueError):
        return True, ""


def _dim_columns(columns: list[str], group_by: list[str],
                 metric_cols: dict[str, str]) -> list[str]:
    time_fields = {"report_date", "mfd_date"}
    return [c for c in columns if c in set(group_by) | time_fields
            and c not in metric_cols]


def _date_column(columns: list[str], group_by: list[str],
                 metric_cols: dict[str, str]) -> str | None:
    for c in ("report_date", "mfd_date"):
        if c in columns:
            return c
    dims = _dim_columns(columns, group_by, metric_cols)
    return dims[0] if dims else None


def _missing_dates(dates: list[date]) -> list[str]:
    """序列内部缺失的自然日（仅记录缺口；**不补零**）。"""
    if len(dates) < 2:
        return []
    present = set(dates)
    out: list[str] = []
    cur, end = min(dates), max(dates)
    while cur <= end:
        if cur not in present:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _group_key(row, dims: list[str], col_index: dict[str, int]) -> dict[str, str]:
    return {d: str(row[col_index[d]]) for d in dims}


def _group_label(key: dict[str, str]) -> str:
    if not key:
        return "全部"
    return " / ".join(f"{k}={v}" for k, v in key.items())


def _fmt(x: float | None, unit: str = "") -> str:
    if x is None:
        return "（不可计算）"
    if unit == "元" and abs(x) >= 1e8:
        return f"{x / 1e8:.2f} 亿元"
    return f"{x:,.2f}{unit}"


def _infer_op(column: str, metric_id: str | None = None, plan=None) -> str | None:
    """推断列的实际聚合操作（措辞必须与真实 SQL 聚合一致）。

    优先级：
    1. 列名中**非歧义**后缀（`_sum`/`_avg`/`_min`/`_max`/`_count_distinct`）——
       列名由生成器按实际聚合写出，最可靠；
    2. 歧义后缀 `_count`（可能是指标名的一部分，如 `active_user_count`）→
       以指标目录口径为准；
    3. 无后缀：指标目录的固定聚合口径，或计划中唯一的聚合操作；
    4. 无法确定 → None（调用方不得生成措辞结论）。
    """
    lower = column.lower()
    for suffix, op in (("count_distinct", "COUNT_DISTINCT"), ("sum", "SUM"),
                       ("avg", "AVG"), ("min", "MIN"), ("max", "MAX")):
        if lower.endswith("_" + suffix):
            return op

    catalog_agg: str | None = None
    if metric_id:
        try:
            m = mc.get_metric(metric_id)
            catalog_agg = m.aggregation
        except (KeyError, ValueError):
            catalog_agg = None

    if lower.endswith("_count"):
        if catalog_agg == "COUNT_DISTINCT":
            return "COUNT_DISTINCT"
        return "COUNT"

    if catalog_agg == "COUNT_DISTINCT":
        return "COUNT_DISTINCT"
    if catalog_agg in _OP_LABELS:
        declared = ([op.value for op in plan.statistics if op.value in _OP_LABELS]
                    if plan is not None else [])
        if not declared or len(declared) == 1:
            return catalog_agg
    if plan is not None:
        declared = [op.value for op in plan.statistics if op.value in _OP_LABELS]
        if len(declared) == 1:
            return declared[0]
    return None


def _effective_window(execution_result, window_label: str | None = None
                      ) -> tuple[str, str] | None:
    """从查询**结构化元数据**取实际执行窗口（截取后的有效范围）。

    不解析自由文本、不推断；无法确认时返回 None（调用方不得伪造日期）。
    """
    for q in execution_result.queries:
        if window_label is not None and q.window_label != window_label:
            continue
        if q.window_start and q.window_end:
            return (q.window_start, q.window_end)
    return None


def _check_metrics_complete(plan, metric_cols: dict[str, str], columns: list[str],
                            group_by: list[str], analysis_type: str,
                            single_metric_types: tuple[str, ...] = ()
                            ) -> tuple[bool, str | None]:
    """校验请求指标完整性（任务 06.2-A）。

    - 计划中的每个指标都必须能唯一对应到结果列（缺失 → 拒绝）；
    - 结果中不得出现未识别的额外列（无法确认口径 → 拒绝）；
    - 仅支持单指标的分析类型遇到多指标计划 → 拒绝（不得只处理第一列）。
    """
    if plan is None:
        return True, None
    # 请求身份 = (metric_id, term)；同一指标的不同期限必须分别检查（07.5.3）。
    requested_keys = list(dict.fromkeys(
        (m.metric_id, m.term) for m in plan.metrics))
    # 实际存在的身份来自**精确列映射**（不依赖模糊前缀推断）
    column_keys = _metric_column_keys(columns, plan)
    found_keys = list(dict.fromkeys(column_keys.values()))
    missing = [k for k in requested_keys if k not in found_keys]
    if missing:
        def _fmt(key: tuple[str, str | None]) -> str:
            mid, term = key
            return f"{mid}（期限 {term}）" if term else mid
        return False, ("请求指标/期限未出现在查询结果中："
                       + "、".join(_fmt(k) for k in missing)
                       + f"（结果实际包含: "
                       + ("、".join(_fmt(k) for k in found_keys) or "无") + "）")
    # 每个请求指标（按 metric_id + term）必须唯一对应一个结果列
    # （多聚合口径 → 拒绝；同一指标的不同 term 属不同口径，合法）
    per_metric: dict[tuple[str, str | None], list[str]] = {}
    for col, key in _metric_column_keys(columns, plan).items():
        per_metric.setdefault(key, []).append(col)
    ambiguous = {key: cols for key, cols in per_metric.items() if len(cols) > 1}
    if ambiguous:
        return False, ("指标在查询结果中出现多个聚合口径列，无法唯一确定使用哪一个："
                       + "；".join(f"{mid}{'/' + term if term else ''}={cols}"
                                   for (mid, term), cols in ambiguous.items()))
    dim_cols = set(_dim_columns(columns, group_by, metric_cols)) | META_COLUMNS
    extra = [c for c in columns if c not in metric_cols and c not in dim_cols]
    if extra:
        return False, f"查询结果包含未识别的额外列 {extra}，无法确认指标口径"
    if analysis_type in single_metric_types and len(requested_keys) > 1:
        return False, (f"{analysis_type} 仅支持单指标分析（当前请求 {len(requested_keys)} 个："
                       f"{[f'{mid}/{term}' if term else mid for mid, term in requested_keys]}"
                       "）；请分别提交单指标计划")
    return True, None


def _requested_window(plan) -> tuple[str, str] | None:
    tr = plan.time_range if plan is not None else None
    if tr and tr.resolved_start_date and tr.resolved_end_date:
        return (tr.resolved_start_date.isoformat(), tr.resolved_end_date.isoformat())
    return None


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------

def compute_trend(points: list[tuple[str, float]],
                  metric: MetricInfo | None = None,
                  group: dict[str, str] | None = None,
                  additive: bool = True,
                  additivity_note: str = "",
                  term: str | None = None) -> TrendStats:
    """计算日趋势：排序、日环比、变化率、可加性受控的合计/期间均值、缺失日期。

    `additive=False`（收益率/利率/每日去重用户数/静态快照）时：
    - `total` 保持 None，`total_note` 说明原因；
    - 改给 `period_mean`（期间平均值），**不输出"合计"**。
    """
    metric = metric or MetricInfo(metric_id="", name_zh="", unit="", formula="")
    by_date: dict[str, float] = {}
    duplicates: list[str] = []
    for d, v in points:
        if d in by_date:
            duplicates.append(d)
            continue
        by_date[d] = v
    sorted_dates = sorted(by_date)
    series = [SeriesPoint(date=d, value=by_date[d]) for d in sorted_dates]

    changes: list[DailyChange] = []
    for i in range(1, len(sorted_dates)):
        prev_d, cur_d = sorted_dates[i - 1], sorted_dates[i]
        prev, cur = by_date[prev_d], by_date[cur_d]
        diff = cur - prev
        if prev == 0:
            rate, note = None, "上一期值为 0，变化率不可计算（不返回无穷大或伪造百分比）"
        else:
            rate, note = (cur - prev) / prev, None
            if prev < 0:
                note = "上一期值为负，变化率按 (本期-上期)/上期 计算，方向解释需谨慎"
        changes.append(DailyChange(date=cur_d, previous_value=prev,
                                   current_value=cur, abs_diff=diff,
                                   change_rate=rate, rate_note=note))

    values = [by_date[d] for d in sorted_dates]
    if additive and values:
        total: float | None = sum(values)
        total_note: str | None = None
        period_mean: float | None = None
    else:
        total = None
        period_mean = fmean(values) if values else None
        total_note = (additivity_note or "该指标不可跨日相加，未计算区间合计")

    parsed_dates: list[date] = []
    for d in sorted_dates:
        try:
            parsed_dates.append(date.fromisoformat(d))
        except ValueError:
            pass
    return TrendStats(
        metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
        term=term, unit=metric.unit, points=series, daily_changes=changes,
        additive=additive, total=total, total_note=total_note,
        period_mean=period_mean,
        cumulative_last=(series[-1].value if series else None),
        missing_dates=_missing_dates(parsed_dates),
        duplicate_dates=sorted(set(duplicates)), group=group or {})


def _trend_conclusions(stats: TrendStats, query_name: str,
                       date_range: tuple[str, str] | None,
                       requested_range: tuple[str, str] | None = None
                       ) -> list[Conclusion]:
    if not stats.points:
        return []
    metric = MetricInfo(metric_id=stats.metric_id, name_zh=stats.metric_name_zh or stats.metric_id,
                        unit=stats.unit, formula="")
    first, last = stats.points[0], stats.points[-1]
    label = _group_label(stats.group)
    term_tag = f"（期限 {stats.term}）" if stats.term else ""
    formula = "日环比 Δ=x_t−x_{t−1}；变化率=(x_t−x_{t−1})/x_{t−1}"
    evidence = Evidence(
        query_name=query_name, metric_id=metric.metric_id,
        metric_name_zh=metric.name_zh, unit=metric.unit,
        date_range=date_range, requested_range=requested_range,
        data_points=len(stats.points), formula=formula,
        input_summary=(f"{len(stats.points)} 个日值；首值 {first.date}={first.value}；"
                       f"末值 {last.date}={last.value}"),
        method="daily_trend",
        warnings=(["序列存在缺失日期（未补零）：" + ", ".join(stats.missing_dates[:5])]
                  if stats.missing_dates else []))

    # 可加性措辞：可加 → 合计；不可加 → 期间均值（绝不称"合计"）
    if stats.additive and stats.total is not None:
        aggregate_text = f"，区间合计 {_fmt(stats.total, metric.unit)}"
        aggregate_lim: list[str] = []
    else:
        aggregate_text = (f"，期间日均 {_fmt(stats.period_mean, metric.unit)}"
                          if stats.period_mean is not None else "")
        aggregate_lim = [stats.total_note or "该指标不可跨日相加，未计算区间合计"]

    conclusions = [Conclusion(
        text=(f"{label}：{metric.name_zh}{term_tag}在 {first.date} 至 {last.date} 共 "
              f"{len(stats.points)} 个数据点，由 {_fmt(first.value, metric.unit)} "
              f"变化到 {_fmt(last.value, metric.unit)}{aggregate_text}"),
        metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
        unit=metric.unit, query_name=query_name, date_range=date_range,
        data_points=len(stats.points), formula=formula,
        evidence_strength=EvidenceStrength.DIRECT_COMPUTATION, evidence=evidence,
        warnings=list(evidence.warnings),
        limitations=[_CAUSALITY_LIMITATION] + aggregate_lim)]

    rated = [c for c in stats.daily_changes if c.change_rate is not None]
    if rated:
        # 三类极值**分别**计算（07.6-B），口径互不混用：
        #   最大降幅 = 有效负变化率中最负的一项
        #   最大涨幅 = 有效正变化率中最大的一项
        #   最大绝对波动 = 有效变化率绝对值最大的一项
        # 前值为 0 的项变化率为 None，不参与任何变化率极值比较。
        negatives = [c for c in rated if c.change_rate < 0]
        positives = [c for c in rated if c.change_rate > 0]

        def _extremum_conclusion(item, kind: str, formula_text: str):
            return Conclusion(
                text=(f"{label}：{item.date} 的{metric.name_zh}日环比{kind}"
                      f"（变化率 {item.change_rate:+.2%}；前值 "
                      f"{_fmt(item.previous_value, metric.unit)} → 本期 "
                      f"{_fmt(item.current_value, metric.unit)}）"),
                metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
                unit=metric.unit, query_name=query_name, date_range=date_range,
                data_points=len(stats.daily_changes), formula=formula_text,
                evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
                evidence=evidence, warnings=list(evidence.warnings),
                limitations=[_CAUSALITY_LIMITATION])

        change_formula = "变化率=(x_t−x_{t−1})/x_{t−1}（分母为 0 时不可计算）"
        if negatives:
            max_drop = min(negatives, key=lambda c: c.change_rate)
            conclusions.append(_extremum_conclusion(
                max_drop, "降幅最大", change_formula))
        if positives:
            max_rise = max(positives, key=lambda c: c.change_rate)
            conclusions.append(_extremum_conclusion(
                max_rise, "涨幅最大", change_formula))
        max_abs = max(rated, key=lambda c: abs(c.change_rate))
        conclusions.append(Conclusion(
            text=(f"{label}：{max_abs.date} 的{metric.name_zh}日环比绝对波动最大"
                  f"（变化率 {max_abs.change_rate:+.2%}，绝对幅度 "
                  f"{abs(max_abs.change_rate):.2%}）"),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name, date_range=date_range,
            data_points=len(stats.daily_changes), formula=change_formula,
            evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
            evidence=evidence, warnings=list(evidence.warnings),
            limitations=[_CAUSALITY_LIMITATION]))
    zero_base = [c for c in stats.daily_changes if c.change_rate is None]
    if zero_base:
        conclusions.append(Conclusion(
            text=(f"{label}：{metric.name_zh}{term_tag}共 {len(zero_base)} 个日期的"
                  "变化率不可计算（上一期值为 0）"),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name, date_range=date_range,
            data_points=len(zero_base),
            formula="变化率=(x_t−x_{t−1})/x_{t−1}；分母为 0 时不可计算",
            evidence_strength=EvidenceStrength.OBSERVATION, evidence=evidence,
            warnings=[zero_base[0].rate_note or ""],
            limitations=["变化率不可计算不影响其余统计"]))
    return conclusions


# ---------------------------------------------------------------------------
# 窗口对比
# ---------------------------------------------------------------------------

def compare_windows(main_rows, comp_rows, main_columns, comp_columns,
                    dims: list[str], metric_col: str,
                    main_window: tuple[str, str],
                    comp_window: tuple[str, str],
                    unit: str = "",
                    alignment: str = "exact_group") -> WindowComparisonStats:
    """匹配两窗口记录（不配对不同分组、不混窗口、不把缺失组当零）。

    alignment="day_of_month"：按**月内日期**（1—31）对齐（跨年同月逐日比较），
    同时保留两窗口的原始完整日期；缺失日期不补零、不伪造。
    """
    align_day = (alignment == "day_of_month")
    date_dim = dims[0] if (align_day and dims) else None

    def _day_key(raw: str) -> tuple[str, str] | None:
        """把 YYYY-MM-DD 拆成 (对齐键, 原始日期)。无法解析时返回 None。"""
        try:
            d = _parse_iso_date(raw)
        except Exception:      # noqa: BLE001 - 非日期值不参与月内对齐
            return None
        if d is None:
            return None
        return (f"{d.month:02d}-{d.day:02d}", d.isoformat())

    def index(rows, columns):
        idx = {c: i for i, c in enumerate(columns)}
        out: dict[tuple, float] = {}
        dates: dict[tuple, str] = {}
        for row in rows:
            if align_day:
                raw = str(row[idx[date_dim]])
                parsed = _day_key(raw)
                if parsed is None:
                    continue
                key = (parsed[0],)
                dates.setdefault(key, parsed[1])
            else:
                key = tuple(str(row[idx[d]]) for d in dims) if dims else ()
            v = _to_float(row[idx[metric_col]])
            if v is not None:
                out[key] = v
        return out, dates

    main_map, main_dates = index(main_rows, main_columns)
    comp_map, comp_dates = index(comp_rows, comp_columns)

    # 跨年逐日：分别保存两个年份的每日原始金额（月内日期 -> 值）。
    # 仅 day_of_month 对齐时有意义；单边缺失的日期也保留在各自字典里，
    # 供双折线图单独成线、缺失侧显示 NaN（不丢值、不补零）。
    main_daily: dict[int, float] = {}
    comp_daily: dict[int, float] = {}
    if align_day:
        for key, v in main_map.items():
            try:
                main_daily[int(str(key[0]).split("-")[1])] = v
            except (ValueError, IndexError):
                pass
        for key, v in comp_map.items():
            try:
                comp_daily[int(str(key[0]).split("-")[1])] = v
            except (ValueError, IndexError):
                pass

    def key_to_dict(key: tuple) -> dict[str, str]:
        if align_day:
            # 展示用分组标签：主窗口的原始完整日期
            return {date_dim: main_dates.get(key, key[0])}
        return {d: v for d, v in zip(dims, key)} if dims else {}

    pairs: list[WindowPair] = []
    missing_in_main: list[dict[str, str]] = []
    missing_in_comp: list[dict[str, str]] = []

    for key in sorted(set(main_map) | set(comp_map)):
        kd = key_to_dict(key)
        if key not in comp_map:
            missing_in_comp.append(kd)
            continue
        if key not in main_map:
            missing_in_main.append(kd)
            continue
        main_v, comp_v = main_map[key], comp_map[key]
        diff = main_v - comp_v
        align_key = key[0] if align_day else None
        main_date = main_dates.get(key) if align_day else None
        comp_date = comp_dates.get(key) if align_day else None
        if comp_v == 0:
            rate, note = None, "对比窗口值为 0，变化率不可计算（不生成虚假百分比）"
        else:
            rate, note = diff / comp_v, None
            if comp_v < 0:
                note = "对比窗口值为负，变化率按此基数计算，方向解释需谨慎"
        direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
        pairs.append(WindowPair(group=kd, main_value=main_v, comparison_value=comp_v,
                                abs_diff=diff, change_rate=rate, rate_note=note,
                                direction=direction, align_key=align_key,
                                main_date=main_date, comparison_date=comp_date))

    by_abs = sorted([p for p in pairs if p.abs_diff < 0], key=lambda p: p.abs_diff)
    for rank, p in enumerate(by_abs, start=1):
        p.main_rank_by_abs_decline = rank
    by_rate = sorted([p for p in pairs if p.change_rate is not None and p.change_rate < 0],
                     key=lambda p: p.change_rate)
    for rank, p in enumerate(by_rate, start=1):
        p.main_rank_by_rate = rank

    note = None
    if by_abs and by_rate:
        top_abs, top_rate = by_abs[0], by_rate[0]
        if top_abs.group != top_rate.group:
            note = ("绝对下降幅度与相对变化率的排名第一不一致："
                    f"绝对下降第一为 {_group_label(top_abs.group)}，"
                    f"相对变化率第一为 {_group_label(top_rate.group)}——两种口径分别展示")
    stats = WindowComparisonStats(
        pairs=pairs, main_window=main_window, comparison_window=comp_window,
        missing_in_main=missing_in_main, missing_in_comparison=missing_in_comp,
        ranking_note=note, alignment=alignment,
        main_daily_amounts=main_daily, comparison_daily_amounts=comp_daily)

    # ---- 月度合计与配对覆盖率（与"仅配对日期合计"严格区分）----
    main_total = sum(main_map.values()) if main_map else None
    comp_total = sum(comp_map.values()) if comp_map else None
    stats.main_total = main_total
    stats.comparison_total = comp_total
    stats.main_days = len(main_map)
    stats.comparison_days = len(comp_map)
    stats.paired_days = len(pairs)
    if main_total is not None and comp_total is not None:
        stats.total_abs_diff = main_total - comp_total
        if comp_total == 0:
            stats.total_change_rate = None
            stats.total_rate_note = "对比窗口合计为 0，月度同比变化率不可计算（不生成虚假百分比）"
        else:
            stats.total_change_rate = (main_total - comp_total) / comp_total
            stats.total_rate_note = None
    stats.paired_main_total = (sum(p.main_value for p in pairs) if pairs else None)
    stats.paired_comparison_total = (sum(p.comparison_value for p in pairs)
                                      if pairs else None)
    if stats.main_days:
        stats.coverage_rate = len(pairs) / stats.main_days
    return stats


def _cross_year_daily_conclusions(stats: WindowComparisonStats, metric: MetricInfo,
                                   query_name: str,
                                   eff_main: tuple[str, str] | None,
                                   eff_comp: tuple[str, str] | None,
                                   req_main: tuple[str, str] | None,
                                   req_comp: tuple[str, str] | None) -> list[Conclusion]:
    """跨年同月逐日对比结论：月度合计 + 极值 + 覆盖情况（不逐日罗列缺失）。"""
    conclusions: list[Conclusion] = []
    formula = "每日差值 = 主窗口当日值 − 对比窗口同月同日值；变化率 = 差值 / 对比窗口当日值"

    def _win_label(window, effective: bool, which: str) -> str:
        if window is None:
            return which + "（日期未知）"
        if effective:
            return f"{which} {window[0]} 至 {window[1]}"
        return f"{which}（请求范围）{window[0]} 至 {window[1]}"

    def _mk_evidence(summary: str, data_points: int) -> Evidence:
        return Evidence(
            query_name=query_name, metric_id=metric.metric_id,
            metric_name_zh=metric.name_zh, unit=metric.unit,
            date_range=None,
            main_window=eff_main, comparison_window=eff_comp,
            requested_main_window=req_main, requested_comparison_window=req_comp,
            data_points=data_points, formula=formula,
            input_summary=summary, method="cross_year_daily_comparison",
            warnings=[stats.total_rate_note] if stats.total_rate_note else [])

    # ① 月度合计
    if stats.main_total is not None and stats.comparison_total is not None:
        rate_text = (f"，同比变化率 {stats.total_change_rate:+.2%}"
                     if stats.total_change_rate is not None
                     else f"（{stats.total_rate_note}）")
        summary = (f"主窗口 {stats.main_window[0]} 至 {stats.main_window[1]} 合计="
                   f"{stats.main_total}；对比窗口 {stats.comparison_window[0]} 至 "
                   f"{stats.comparison_window[1]} 合计={stats.comparison_total}")
        conclusions.append(Conclusion(
            text=(f"月度合计：{metric.name_zh}在"
                  + _win_label(stats.comparison_window,
                               stats.comparison_window_from_effective, "对比窗口")
                  + f" 合计 {_fmt(stats.comparison_total, metric.unit)}，在"
                  + _win_label(stats.main_window,
                               stats.main_window_from_effective, "主窗口")
                  + f" 合计 {_fmt(stats.main_total, metric.unit)}，"
                  f"绝对变化 {_fmt(stats.total_abs_diff, metric.unit)}{rate_text}"),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name,
            date_range=None, main_window=eff_main, comparison_window=eff_comp,
            data_points=stats.main_days, formula=formula,
            evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
            evidence=_mk_evidence(summary, stats.main_days),
            warnings=[stats.total_rate_note] if stats.total_rate_note else [],
            limitations=[_CAUSALITY_LIMITATION]))

    # ② 逐日最大增幅 / 最大降幅
    ups = [p for p in stats.pairs if p.abs_diff > 0]
    downs = [p for p in stats.pairs if p.abs_diff < 0]
    if ups:
        top = max(ups, key=lambda p: p.abs_diff)
        rate = (f"，变化率 {top.change_rate:+.2%}"
                if top.change_rate is not None else "")
        conclusions.append(Conclusion(
            text=(f"单日最大增幅：{top.align_key}（{top.comparison_date} → {top.main_date}）"
                  f"增加 {_fmt(top.abs_diff, metric.unit)}{rate}"),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name,
            date_range=None, main_window=eff_main, comparison_window=eff_comp,
            data_points=2, formula=formula,
            evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
            evidence=_mk_evidence(
                f"对齐键 {top.align_key}：对比窗口 {top.comparison_value} → "
                f"主窗口 {top.main_value}", 2),
            limitations=[_CAUSALITY_LIMITATION]))
    if downs:
        bottom = min(downs, key=lambda p: p.abs_diff)
        rate = (f"，变化率 {bottom.change_rate:+.2%}"
                if bottom.change_rate is not None else "")
        conclusions.append(Conclusion(
            text=(f"单日最大降幅：{bottom.align_key}（{bottom.comparison_date} → "
                  f"{bottom.main_date}）减少 {_fmt(abs(bottom.abs_diff), metric.unit)}{rate}"),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name,
            date_range=None, main_window=eff_main, comparison_window=eff_comp,
            data_points=2, formula=formula,
            evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
            evidence=_mk_evidence(
                f"对齐键 {bottom.align_key}：对比窗口 {bottom.comparison_value} → "
                f"主窗口 {bottom.main_value}", 2),
            limitations=[_CAUSALITY_LIMITATION]))

    # ③ 覆盖情况（汇总，不逐日罗列）
    unpaired = len(stats.missing_in_comparison) + len(stats.missing_in_main)
    coverage_text = (f"按月内日期（1—31）对齐，成功配对 {stats.paired_days} 天"
                     f"（主窗口观测 {stats.main_days} 天、对比窗口观测 "
                     f"{stats.comparison_days} 天）")
    if stats.coverage_rate is not None:
        coverage_text += f"，配对覆盖率 {stats.coverage_rate:.1%}"
    only_main = len(stats.missing_in_comparison)
    only_comp = len(stats.missing_in_main)
    if unpaired:
        detail = []
        if only_main:
            detail.append(f"{only_main} 天仅主窗口有观测")
        if only_comp:
            detail.append(f"{only_comp} 天仅对比窗口有观测")
        coverage_text += ("；" + "、".join(detail)
                          + "（未按 0 处理，图中不连线）")
    else:
        coverage_text += "；两窗口全部观测均可配对"
    conclusions.append(Conclusion(
        text=coverage_text,
        metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
        unit=metric.unit, query_name=query_name, data_points=stats.paired_days,
        formula="对齐键 = 月内日期（1—31）",
        evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
        evidence=_mk_evidence(
            f"配对 {stats.paired_days} 天；仅配对日期合计：主窗口="
            f"{stats.paired_main_total}，对比窗口={stats.paired_comparison_total}",
            stats.paired_days),
        limitations=[_CAUSALITY_LIMITATION]))
    return conclusions


def _window_conclusions(stats: WindowComparisonStats, metric: MetricInfo,
                        query_name: str,
                        eff_main: tuple[str, str] | None,
                        eff_comp: tuple[str, str] | None,
                        req_main: tuple[str, str] | None,
                        req_comp: tuple[str, str] | None) -> list[Conclusion]:
    """窗口对比结论：两窗口分别记录，**不合并**为连续 date_range（任务 06.2-C）。"""
    conclusions: list[Conclusion] = []
    formula = "差值 = 主窗口 − 对比窗口；变化率 = 差值 / 对比窗口值"

    def _win_label(window, effective: bool, which: str) -> str:
        if window is None:
            return which + "（日期未知）"
        if effective:
            return f"{which} {window[0]} 至 {window[1]}"
        return f"{which}（请求范围）{window[0]} 至 {window[1]}"

    for pair in stats.pairs:
        if pair.abs_diff == 0:
            continue
        label = _group_label(pair.group)
        rate_text = (f"，变化率 {pair.change_rate:+.2%}"
                     if pair.change_rate is not None else f"（{pair.rate_note}）")
        evidence = Evidence(
            query_name=query_name, metric_id=metric.metric_id,
            metric_name_zh=metric.name_zh, unit=metric.unit,
            date_range=None,      # 不连续窗口不得合并为连续范围
            main_window=eff_main, comparison_window=eff_comp,
            requested_main_window=req_main, requested_comparison_window=req_comp,
            data_points=2, formula=formula,
            input_summary=(f"主窗口 {stats.main_window[0]} 至 {stats.main_window[1]}="
                           f"{pair.main_value}；对比窗口 {stats.comparison_window[0]} 至 "
                           f"{stats.comparison_window[1]}={pair.comparison_value}"),
            method="window_comparison",
            warnings=[pair.rate_note] if pair.rate_note else [])
        conclusions.append(Conclusion(
            text=(f"{label}：{metric.name_zh}从"
                  + _win_label(stats.comparison_window,
                               stats.comparison_window_from_effective, "对比窗口")
                  + f" 的 {_fmt(pair.comparison_value, metric.unit)} 变为"
                  + _win_label(stats.main_window,
                               stats.main_window_from_effective, "主窗口")
                  + f" 的 {_fmt(pair.main_value, metric.unit)}，绝对变化 "
                  + f"{_fmt(pair.abs_diff, metric.unit)}{rate_text}"),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name,
            date_range=None, main_window=eff_main, comparison_window=eff_comp,
            data_points=2, formula=formula,
            evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
            evidence=evidence, warnings=list(evidence.warnings),
            limitations=[_CAUSALITY_LIMITATION]))

    if stats.missing_in_comparison:
        conclusions.append(Conclusion(
            text=("以下分组只出现在主窗口、对比窗口无对应记录（未按 0 处理）："
                  + "、".join(_group_label(g) for g in stats.missing_in_comparison)),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name, data_points=0,
            formula="不适用（缺失分组）",
            evidence_strength=EvidenceStrength.OBSERVATION,
            evidence=Evidence(query_name=query_name, metric_id=metric.metric_id,
                              metric_name_zh=metric.name_zh, unit=metric.unit,
                              main_window=eff_main, comparison_window=eff_comp,
                              data_points=0, formula="不适用（缺失分组）",
                              input_summary="主窗口有、对比窗口无"),
            warnings=["缺失分组不视为 0；不计算其变化率"],
            limitations=["如需比较请确认该分组在对比窗口是否确实无业务记录"]))
    if stats.ranking_note:
        conclusions.append(Conclusion(
            text=stats.ranking_note, metric_id=metric.metric_id,
            metric_name_zh=metric.name_zh, unit=metric.unit,
            query_name=query_name, data_points=len(stats.pairs),
            formula="按绝对差值与按变化率分别排序",
            evidence_strength=EvidenceStrength.OBSERVATION,
            evidence=Evidence(query_name=query_name, metric_id=metric.metric_id,
                              metric_name_zh=metric.name_zh, unit=metric.unit,
                              main_window=eff_main, comparison_window=eff_comp,
                              data_points=len(stats.pairs),
                              formula="排名口径", input_summary=stats.ranking_note),
            warnings=[], limitations=["排名依据不同口径，结论需说明所用口径"]))
    return conclusions


# ---------------------------------------------------------------------------
# 排名 / 分布 / 描述性
# ---------------------------------------------------------------------------

def rank_groups(rows, columns, dims: list[str], metric_col: str,
                metric: MetricInfo, order: str = "desc") -> RankingStats:
    """分组排名：保留编码、明确排序；**仅可加指标**计算占比。"""
    idx = {c: i for i, c in enumerate(columns)}
    entries: list[tuple[dict[str, str], float]] = []
    for row in rows:
        key = _group_key(row, dims, idx) if dims else {}
        v = _to_float(row[idx[metric_col]])
        if v is None:
            continue
        entries.append((key, v))
    entries.sort(key=lambda kv: kv[1], reverse=(order == "desc"))

    additive, add_note = _additivity(metric.metric_id)
    total = sum(v for _, v in entries)
    if not additive:
        allow_share = False
        share_note = (add_note or "非加性指标不计算占比")
    elif not all(v >= 0 for _, v in entries) or total <= 0:
        allow_share = False
        share_note = "分组值含负值或合计为 0，不计算占比"
    else:
        allow_share = True
        share_note = "占比 = 该分组值 / 全部分组值之和（分母：本次查询返回的全部分组）"
    out = [
        RankingEntry(group=key, value=v, rank=i + 1,
                     share=(v / total if allow_share else None))
        for i, (key, v) in enumerate(entries)
    ]
    return RankingStats(entries=out, order=order, metric_id=metric.metric_id,
                        unit=metric.unit, share_note=share_note)


def _ranking_conclusions(stats: RankingStats, metric: MetricInfo,
                         query_name: str) -> list[Conclusion]:
    if not stats.entries:
        return []
    top = stats.entries[0]
    formula = f"按 {metric.name_zh} {'降序' if stats.order == 'desc' else '升序'}排序"
    share_text = f"，占比 {top.share:.2%}" if top.share is not None else ""
    share_lim = [] if top.share is not None else [stats.share_note]
    evidence = Evidence(
        query_name=query_name, metric_id=metric.metric_id,
        metric_name_zh=metric.name_zh, unit=metric.unit,
        data_points=len(stats.entries), formula=formula,
        input_summary=f"{len(stats.entries)} 个分组；第一名 {_group_label(top.group)}="
                      f"{top.value}",
        method="group_ranking", warnings=list(share_lim))
    return [Conclusion(
        text=(f"按{metric.name_zh}排名第一的是 {_group_label(top.group)}，"
              f"为 {_fmt(top.value, metric.unit)}{share_text}"
              f"（共 {len(stats.entries)} 个分组）"),
        metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
        unit=metric.unit, query_name=query_name,
        data_points=len(stats.entries), formula=formula,
        evidence_strength=EvidenceStrength.DIRECT_COMPUTATION, evidence=evidence,
        warnings=list(share_lim), limitations=share_lim
        + ([_CITY_LIMITATION] if any("city" in e.group for e in stats.entries) else []))]


def compute_distribution(rows, columns, dims: list[str], count_col: str,
                         share_note: str | None = None) -> DistributionStats:
    idx = {c: i for i, c in enumerate(columns)}
    cats: list[DistributionCategory] = []
    missing = 0
    for row in rows:
        key = _group_key(row, dims, idx) if dims else {}
        raw = row[idx[count_col]]
        if raw is None:
            missing += 1
            continue
        v = _to_float(raw)
        if v is None:
            missing += 1
            continue
        cats.append(DistributionCategory(group=key, count=int(v)))
    total = sum(c.count for c in cats)
    for c in cats:
        c.share = (c.count / total) if total > 0 else None
    values = [c.count for c in cats]
    value_stats = None
    if values:
        value_stats = {"min": min(values), "max": max(values),
                       "mean": fmean(values), "median": median(values)}
    return DistributionStats(
        categories=cats, total_count=total, sample_size=len(cats),
        missing_count=missing,
        share_note=share_note or "占比 = 该类别计数 / 全部类别计数之和",
        value_stats=value_stats)


def compute_descriptive(rows, columns, metric_cols: dict[str, str],
                        plan=None) -> DescriptiveStats:
    """单行汇总；记录每列的实际聚合操作（用于措辞一致性）。"""
    values: dict[str, float] = {}
    ops: dict[str, str] = {}
    if not rows:
        return DescriptiveStats(values={}, columns=[], column_ops={}, data_points=0)
    for col in columns:
        if col not in metric_cols:
            continue
        v = _to_float(rows[0][columns.index(col)])
        if v is None:
            continue
        values[col] = v
        inferred = _infer_op(col, metric_cols[col], plan)
        if inferred:
            ops[col] = inferred
    return DescriptiveStats(values=values, columns=list(values),
                            column_ops=ops, data_points=len(rows))


# ---------------------------------------------------------------------------
# 异常检测（稳健 MAD）
# ---------------------------------------------------------------------------

def detect_anomalies(points: list[tuple[str, float]],
                     threshold_k: float = ANOMALY_THRESHOLD_K,
                     min_points: int = ANOMALY_MIN_POINTS) -> AnomalyStats:
    """稳健 MAD 异常检测（center=median，scaled=1.4826·MAD，|robust_z|>k）。"""
    series = sorted(points, key=lambda kv: kv[0])
    xs = [v for _, v in series]
    n = len(xs)
    if n < min_points:
        return AnomalyStats(sample_size=n, threshold_k=threshold_k,
                            min_points=min_points,
                            note=f"样本数 {n} < 最小样本数 {min_points}，无法进行稳健异常检测")
    center = median(xs)
    mad = median([abs(x - center) for x in xs])
    scaled = 1.4826 * mad
    if scaled == 0:
        return AnomalyStats(sample_size=n, threshold_k=threshold_k,
                            min_points=min_points, center=center, mad=mad,
                            scaled_mad=scaled,
                            note="MAD 为 0（序列基本恒定），稳健 z 无法计算；"
                                 "未判定任何异常点")
    pts: list[AnomalyPoint] = []
    for d, v in series:
        z = (v - center) / scaled
        pts.append(AnomalyPoint(date=d, value=v, center=center, deviation=v - center,
                                robust_z=z, is_anomaly=abs(z) > threshold_k))
    anomalies = [p for p in pts if p.is_anomaly]
    note = (f"阈值 k={threshold_k}，最小样本数 {min_points}；"
            f"共判定 {len(anomalies)} 个异常点（仅统计偏离，未归因外部事件）")
    return AnomalyStats(sample_size=n, threshold_k=threshold_k,
                        min_points=min_points, center=center, mad=mad,
                        scaled_mad=scaled, points=pts, anomalies=anomalies, note=note)


def _anomaly_conclusions(stats: AnomalyStats, metric: MetricInfo,
                         query_name: str, date_range,
                         requested_range=None) -> list[Conclusion]:
    conclusions: list[Conclusion] = []
    formula = ("center=median(x)；MAD=median(|x−center|)；scaled_mad=1.4826·MAD；"
               f"robust_z=(x−center)/scaled_mad；|robust_z|>{stats.threshold_k} 判为异常")
    evidence = Evidence(
        query_name=query_name, metric_id=metric.metric_id,
        metric_name_zh=metric.name_zh, unit=metric.unit, date_range=date_range,
        requested_range=requested_range,
        data_points=stats.sample_size, formula=formula,
        input_summary=(f"样本 {stats.sample_size} 个；center={stats.center}；"
                       f"MAD={stats.mad}；scaled_mad={stats.scaled_mad}"),
        method="robust_median_mad")
    if stats.center is None or stats.scaled_mad in (None, 0):
        conclusions.append(Conclusion(
            text=f"异常检测未执行：{stats.note}", metric_id=metric.metric_id,
            metric_name_zh=metric.name_zh, unit=metric.unit, query_name=query_name,
            date_range=date_range, data_points=stats.sample_size, formula=formula,
            evidence_strength=EvidenceStrength.OBSERVATION, evidence=evidence,
            warnings=[stats.note], limitations=[_ANOMALY_LIMITATION]))
        return conclusions
    for p in stats.anomalies:
        conclusions.append(Conclusion(
            text=(f"{p.date} 的{metric.name_zh}为 {_fmt(p.value, metric.unit)}，"
                  f"偏离中位数 {_fmt(p.center, metric.unit)}（robust_z={p.robust_z:.2f}，"
                  f"阈值 {stats.threshold_k}），判定为统计异常点"),
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name, date_range=date_range,
            data_points=stats.sample_size, formula=formula,
            evidence_strength=EvidenceStrength.DIRECT_COMPUTATION, evidence=evidence,
            warnings=[], limitations=[_ANOMALY_LIMITATION]))
    if not stats.anomalies:
        conclusions.append(Conclusion(
            text=f"在 {stats.sample_size} 个数据点中未检出超过阈值 "
                 f"{stats.threshold_k} 的统计异常",
            metric_id=metric.metric_id, metric_name_zh=metric.name_zh,
            unit=metric.unit, query_name=query_name, date_range=date_range,
            data_points=stats.sample_size, formula=formula,
            evidence_strength=EvidenceStrength.OBSERVATION, evidence=evidence,
            warnings=[], limitations=[_ANOMALY_LIMITATION]))
    return conclusions


# ---------------------------------------------------------------------------
# 相关性
# ---------------------------------------------------------------------------

def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = fmean(xs), fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    if den == 0:
        return None
    return num / den


def _average_ranks(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def compute_correlation(series_a: list[tuple[str, float]],
                        series_b: list[tuple[str, float]],
                        metric_a: str, metric_b: str,
                        method: str = "pearson") -> CorrelationStats:
    """按日期对齐计算相关系数（inner join；记录缺失日期与配对样本数）。"""
    map_a = dict(series_a)
    map_b = dict(series_b)
    common = sorted(set(map_a) & set(map_b))
    only_a = sorted(set(map_a) - set(map_b))
    only_b = sorted(set(map_b) - set(map_a))
    xs = [map_a[d] for d in common]
    ys = [map_b[d] for d in common]
    pairs = [{"date": d, "a": map_a[d], "b": map_b[d]} for d in common]
    missing = sorted(set(only_a) | set(only_b))

    if len(common) < CORRELATION_MIN_PAIRS:
        return CorrelationStats(method=method, metric_a=metric_a, metric_b=metric_b,
                                n_pairs=len(common), coefficient=None,
                                status_note=(f"配对样本数 {len(common)} < "
                                             f"{CORRELATION_MIN_PAIRS}，无法计算相关系数"),
                                missing_dates=missing, pairs=pairs)
    if method == "spearman":
        coef = _pearson(_average_ranks(xs), _average_ranks(ys))
    else:
        coef = _pearson(xs, ys)
    if coef is None:
        return CorrelationStats(method=method, metric_a=metric_a, metric_b=metric_b,
                                n_pairs=len(common), coefficient=None,
                                status_note="至少一个序列为常量（方差为 0），相关系数不可计算",
                                missing_dates=missing, pairs=pairs)
    return CorrelationStats(method=method, metric_a=metric_a, metric_b=metric_b,
                            n_pairs=len(common), coefficient=coef,
                            missing_dates=missing, pairs=pairs)


def _correlation_conclusions(stats: CorrelationStats, query_name: str,
                             unit_a: str, unit_b: str) -> list[Conclusion]:
    formula = ("Pearson r=Σ((x−x̄)(y−ȳ))/sqrt(Σ(x−x̄)²·Σ(y−ȳ)²)"
               if stats.method == "pearson" else "Spearman：对平均秩计算 Pearson")
    evidence = Evidence(
        query_name=query_name, metric_id=f"{stats.metric_a}|{stats.metric_b}",
        metric_name_zh=f"{stats.metric_a} 与 {stats.metric_b}",
        unit=f"{unit_a} / {unit_b}", data_points=stats.n_pairs, formula=formula,
        input_summary=f"配对样本 {stats.n_pairs} 个；缺失日期 {len(stats.missing_dates)} 个",
        method=f"correlation_{stats.method}",
        warnings=[f"以下日期只在单侧序列中出现：{', '.join(stats.missing_dates[:5])}"]
        if stats.missing_dates else [])
    if stats.coefficient is None:
        return [Conclusion(
            text=f"相关性不可计算：{stats.status_note}",
            metric_id=evidence.metric_id, metric_name_zh=evidence.metric_name_zh,
            unit=evidence.unit, query_name=query_name,
            data_points=stats.n_pairs, formula=formula,
            evidence_strength=EvidenceStrength.OBSERVATION, evidence=evidence,
            warnings=[stats.status_note or ""],
            limitations=[_CAUSALITY_LIMITATION])]
    return [Conclusion(
        text=(f"{stats.metric_a} 与 {stats.metric_b} 在 {stats.n_pairs} 个配对日期上的"
              f"{stats.method} 相关系数为 {stats.coefficient:.4f}"
              "（相关不等于因果）"),
        metric_id=evidence.metric_id, metric_name_zh=evidence.metric_name_zh,
        unit=evidence.unit, query_name=query_name, data_points=stats.n_pairs,
        formula=formula, evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
        evidence=evidence, warnings=list(evidence.warnings),
        limitations=[_CAUSALITY_LIMITATION])]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _base_result(plan, execution_result) -> dict:
    tr = plan.time_range if plan is not None else None
    requested = {
        "type": tr.type.value if tr else None,
        "relative_period": tr.relative_period if tr else None,
        "start": (tr.resolved_start_date.isoformat()
                  if tr and tr.resolved_start_date else None),
        "end": (tr.resolved_end_date.isoformat()
                if tr and tr.resolved_end_date else None),
        "comparison_start": (tr.comparison_start_date.isoformat()
                             if tr and tr.comparison_start_date else None),
        "comparison_end": (tr.comparison_end_date.isoformat()
                           if tr and tr.comparison_end_date else None),
        "anchor": tr.anchor if tr else None,
    }
    # 有效窗口：来自查询元数据（截取后）；无法确认则为 None（不伪造）
    # 窗口对比必须按 window_label 隔离，**不得**用另一窗口的日期填充缺失窗口
    labeled: dict[str, tuple[str, str]] = {}
    duplicate_labels: set[str] = set()
    unlabeled: list[tuple[str, str]] = []
    for q in execution_result.queries:
        if not (q.window_start and q.window_end):
            continue
        win = (q.window_start, q.window_end)
        if q.window_label:
            if q.window_label in labeled:
                duplicate_labels.add(q.window_label)
            else:
                labeled[q.window_label] = win
        else:
            unlabeled.append(win)

    is_window_comparison = (plan is not None
                            and plan.analysis_type.value == "window_comparison")
    effective_note: str | None = None
    if is_window_comparison:
        effective = labeled.get("main")
        comp_effective = labeled.get("comparison")
        if duplicate_labels:
            effective_note = ("存在重复窗口标签，窗口元数据不唯一："
                              f"{sorted(duplicate_labels)}")
        elif "main" not in labeled and "comparison" in labeled:
            effective_note = ("主窗口缺少实际执行窗口元数据（effective=None）；"
                              "不会用对比窗口日期填充")
        elif "comparison" not in labeled and "main" in labeled:
            effective_note = ("对比窗口缺少实际执行窗口元数据"
                              "（effective_comparison=None）")
    else:
        effective = ({"start": unlabeled[0][0], "end": unlabeled[0][1]}
                     if unlabeled else None)
        comp_effective = None

    effective = ({"start": effective[0], "end": effective[1]}
                 if isinstance(effective, tuple) else effective)
    comp_effective = ({"start": comp_effective[0], "end": comp_effective[1]}
                      if isinstance(comp_effective, tuple) else comp_effective)
    return {
        "question": plan.question if plan is not None else "",
        "analysis_type": (plan.analysis_type.value if plan is not None else "unknown"),
        "metrics": [_metric_info(m.metric_id) for m in plan.metrics]
        if plan is not None else [],
        "time_range": {
            "requested": requested,
            "effective": effective,
            "effective_comparison": comp_effective,
            "effective_note": effective_note,
            "data_coverage": {
                "min": mc.DATA_DATE_MIN, "max": mc.DATA_DATE_MAX,
                "note": "数据集实际覆盖范围（请求超出时被截取或拒绝）",
            },
        },
        "group_by": list(plan.group_by) if plan is not None else [],
    }


def _failed(plan, execution_result, status: AnalysisStatus, reason: str) -> AnalysisResult:
    base = _base_result(plan, execution_result)
    return AnalysisResult(status=status, status_reason=reason,
                          query_names=[q.name for q in execution_result.queries],
                          warnings=list(execution_result.warnings), **base)


def _trend_branch(plan, execution_result, base, analysis_type) -> AnalysisResult:
    q0 = execution_result.queries[0]
    metric_cols = _metric_column_map(q0.columns, plan)
    ok, reason = _check_metrics_complete(plan, metric_cols, q0.columns,
                                         base["group_by"], analysis_type)
    if not ok:
        return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC, reason)
    if not metric_cols:
        return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                       "查询结果中没有可识别的指标列")
    date_col = _date_column(q0.columns, base["group_by"], metric_cols)
    if date_col is None:
        return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                       "查询结果中没有日期列，无法进行时间序列分析")

    per_metric_cols: dict[str, list[str]] = {}
    for col, mid in metric_cols.items():
        per_metric_cols.setdefault(mid, []).append(col)

    for mid in per_metric_cols:
        if mid not in _TREND_METRICS:
            return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                           f"指标 {mid} 不支持时间趋势分析")

    idx = {c: i for i, c in enumerate(q0.columns)}
    dims = [d for d in base["group_by"] if d in q0.columns]
    # 时间字段是趋势序列的“轴”，不能同时作为分组键——否则每个分组只剩 1 个点，
    # 会把本来有多个有效日期的数据误判为数据不足（阶段 07.5 根因 D 类）。
    ignored_time_dims = [d for d in dims if d in ("report_date", "mfd_date")]
    dims = [d for d in dims if d not in ("report_date", "mfd_date")]
    req = base["time_range"]["requested"]
    requested_range = ((req["start"], req["end"])
                       if req["start"] and req["end"] else None)
    eff = base["time_range"]["effective"]
    # 只使用实际执行窗口；元数据缺失时保持 None（不得用请求范围冒充）
    date_range = (eff["start"], eff["end"]) if eff else None
    range_warning = None
    if eff is None:
        range_warning = ("无法确认实际执行窗口（查询缺少窗口元数据）；"
                         "证据不标注日期范围，观测值范围仅供参照")
    elif requested_range and date_range != requested_range:
        range_warning = (f"请求区间 {requested_range[0]} 至 {requested_range[1]} "
                         f"超出数据覆盖范围，实际执行窗口为 {date_range[0]} 至 {date_range[1]}")

    # 每个指标 × 每个分组维度组合 → 独立序列
    series: list[TrendStats] = []
    warnings: list[str] = [range_warning] if range_warning else []
    if ignored_time_dims:
        warnings.append(
            "已忽略按日期分组（" + "、".join(ignored_time_dims)
            + "）：日期是趋势序列的时间轴，不作为分组维度")
    analyzed_metrics: list[str] = []
    empty_metrics: list[str] = []          # 整列无有效数值的指标
    per_metric_groups: dict[str, set[tuple]] = {}
    metric_keys = _metric_column_keys(q0.columns, plan)
    for metric_col, mid in metric_cols.items():
        term = metric_keys.get(metric_col, (mid, None))[1]
        info = _metric_info(mid)
        additive, add_note = _additivity(mid)
        buckets: dict[tuple, list[tuple[str, float]]] = {}
        for row in q0.rows:
            key = tuple(str(row[idx[d]]) for d in dims) if dims else ()
            v = _to_float(row[idx[metric_col]])
            if v is None:
                continue
            buckets.setdefault(key, []).append((_to_iso(row[idx[date_col]]), v))
        if not buckets:
            empty_metrics.append(f"{mid}/{term}" if term else mid)
            continue
        per_metric_groups[mid] = set(buckets)
        for key in sorted(buckets):
            group = {d: v for d, v in zip(dims, key)} if dims else {}
            series.append(compute_trend(buckets[key], info, group, additive,
                                        add_note, term))
        analyzed_metrics.append(f"{mid}/{term}" if term else mid)

    # 整列为空：不静默跳过（部分空 → 数据不足；全空 → 空结果）
    if empty_metrics:
        if len(empty_metrics) == len(metric_cols):
            return _failed(
                plan, execution_result, AnalysisStatus.EMPTY_RESULT,
                f"所有请求指标的结果列均无有效数值：{empty_metrics}；"
                "无法进行趋势分析（不会将缺失值填零）")
        return _failed(
            plan, execution_result, AnalysisStatus.INSUFFICIENT_DATA,
            f"以下指标的结果列整列为空、无有效数值：{empty_metrics}"
            f"（其余指标: {analyzed_metrics}）；分析将不完整，拒绝返回成功结论")
    if not series:
        return _failed(plan, execution_result, AnalysisStatus.EMPTY_RESULT,
                       "所有指标列均为空值，无法进行趋势分析")

    # 多维分组下某指标缺某个分组：不虚构零值，明确提示
    if dims and per_metric_groups:
        all_groups: set[tuple] = set()
        for gs in per_metric_groups.values():
            all_groups |= gs
        for mid, gs in per_metric_groups.items():
            missing_groups = sorted(all_groups - gs)
            if missing_groups:
                readable = ["/".join(f"{d}={v}" for d, v in zip(dims, key))
                            for key in missing_groups[:5]]
                warnings.append(
                    f"指标 {mid} 在以下分组无有效数据（未虚构零值）："
                    + "、".join(readable))

    stats_out = series[0] if len(series) == 1 else TrendSetStats(
        series=series, group_by=dims, metric_ids=analyzed_metrics)
    warnings.extend(w for w in execution_result.warnings)

    if analysis_type == "anomaly":
        if len(series) != 1:
            return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                           "异常检测要求单条时间序列（当前结果为多指标或多分组）")
        stats = detect_anomalies([(p.date, p.value) for p in series[0].points])
        status = AnalysisStatus.OK
        reason = None
        if stats.center is None or stats.scaled_mad in (None, 0):
            status = (AnalysisStatus.INSUFFICIENT_DATA
                      if stats.sample_size < stats.min_points
                      else AnalysisStatus.METHOD_NOT_APPLICABLE)
            reason = stats.note
        metric = MetricInfo(metric_id=series[0].metric_id,
                            name_zh=series[0].metric_name_zh, unit=series[0].unit,
                            formula="")
        return AnalysisResult(
            status=status, status_reason=reason, statistics=stats,
            conclusions=_anomaly_conclusions(stats, metric,
                                             execution_result.queries[0].name,
                                             date_range, requested_range),
            method="robust_median_mad",
            method_params={"threshold_k": stats.threshold_k,
                           "min_points": stats.min_points},
            query_names=[q.name for q in execution_result.queries],
            warnings=warnings + ([stats.note] if reason else []),
            limitations=[_ANOMALY_LIMITATION, _CAUSALITY_LIMITATION], **base)

    conclusions: list[Conclusion] = []
    for s in series:
        conclusions.extend(_trend_conclusions(s, execution_result.queries[0].name,
                                             date_range, requested_range))
    status = AnalysisStatus.OK
    reason = None
    if all(len(s.points) < 2 for s in series):
        status = AnalysisStatus.INSUFFICIENT_DATA
        detail = "；".join(
            f"{s.metric_id}{(' / ' + '/'.join(f'{k}={v}' for k, v in s.group.items())) if s.group else ''}"
            f"={len(s.points)} 点" for s in series)
        reason = (f"有效数据点少于 2 个，无法计算日环比（各序列有效点数：{detail}）"
                  "；请检查时间范围或筛选条件是否过窄")
    if date_range is None:
        obs = sorted({p.date for s in series for p in s.points})
        if obs:
            warnings.append(f"观测值范围 {obs[0]} 至 {obs[-1]}"
                            "（仅供参照，不代表实际查询窗口）")
    missing = sorted({d for s in series for d in s.missing_dates})
    if missing:
        warnings.append(f"序列内部存在 {len(missing)} 个缺失日期（未补零）")
    dup = sorted({d for s in series for d in s.duplicate_dates})
    if dup:
        warnings.append(f"存在重复日期（保留首条）：{dup[:5]}")
    return AnalysisResult(
        status=status, status_reason=reason, statistics=stats_out,
        conclusions=conclusions, method="daily_trend",
        method_params={"metrics": analyzed_metrics, "dims": dims},
        query_names=[q.name for q in execution_result.queries],
        warnings=warnings,
        limitations=[_CAUSALITY_LIMITATION]
        + ([_CITY_LIMITATION] if "city" in dims else []),
        **base)


def analyze_execution_result(execution_result, plan=None) -> AnalysisResult:
    """把阶段五的执行结果转换为证据化分析结果（不连接数据库、不生成 SQL）。"""
    from .analysis_runner import ExecutionStatus

    if execution_result.status in (ExecutionStatus.NEEDS_CLARIFICATION,
                                   ExecutionStatus.PLAN_REJECTED,
                                   ExecutionStatus.SQL_REJECTED,
                                   ExecutionStatus.EXECUTION_FAILED,
                                   ExecutionStatus.TIMEOUT):
        return _failed(plan, execution_result, AnalysisStatus.EXECUTION_FAILED,
                       f"上游执行未成功（status={execution_result.status}）："
                       f"{execution_result.error}")
    if not execution_result.queries:
        return _failed(plan, execution_result, AnalysisStatus.EMPTY_RESULT,
                       "上游没有任何查询结果")
    if any(q.status != "ok" for q in execution_result.queries):
        return _failed(plan, execution_result, AnalysisStatus.EXECUTION_FAILED,
                       "部分或全部查询失败：" + "；".join(
                           f"{q.name}={q.status}" for q in execution_result.queries))
    if any(q.truncated for q in execution_result.queries):
        return _failed(plan, execution_result, AnalysisStatus.TRUNCATED,
                       "查询结果被行数上限截断，不足以生成完整结论；请缩小时间范围或提高上限")
    if any(not q.rows for q in execution_result.queries):
        return _failed(plan, execution_result, AnalysisStatus.EMPTY_RESULT,
                       "查询结果为空（该时间范围/筛选条件下无数据）")

    base = _base_result(plan, execution_result)
    query_names = [q.name for q in execution_result.queries]
    analysis_type = base["analysis_type"]
    metric_ids = [m.metric_id for m in plan.metrics] if plan else []
    metric_primary = (_metric_info(metric_ids[0]) if metric_ids
                      else MetricInfo(metric_id="unknown", name_zh="未知指标",
                                      unit="", formula=""))

    if analysis_type in ("time_trend", "anomaly"):
        return _trend_branch(plan, execution_result, base, analysis_type)

    if analysis_type == "window_comparison":
        if len(execution_result.queries) < 2:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC,
                           "窗口对比需要主窗口与对比窗口两条查询结果")
        main_q, comp_q = execution_result.queries[0], execution_result.queries[1]
        # 两窗口必须分别通过契约校验（列结构 / 指标映射 / 聚合口径）
        if set(main_q.columns) != set(comp_q.columns):
            return _failed(
                plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                "主窗口与对比窗口的列结构不一致，拒绝比较"
                f"（主: {list(main_q.columns)}；对比: {list(comp_q.columns)}）")
        metric_cols_main = _metric_column_map(main_q.columns, plan)
        metric_cols_comp = _metric_column_map(comp_q.columns, plan)
        if metric_cols_main != metric_cols_comp:
            return _failed(
                plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                "主窗口与对比窗口的指标映射/聚合口径不一致，拒绝比较"
                f"（主: {metric_cols_main}；对比: {metric_cols_comp}）")
        if not metric_cols_main:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC,
                           "窗口对比结果中没有可识别的指标列")
        for window_name, cols, mcols in (("主窗口", main_q.columns, metric_cols_main),
                                         ("对比窗口", comp_q.columns, metric_cols_comp)):
            ok_w, reason_w = _check_metrics_complete(
                plan, mcols, cols, base["group_by"], analysis_type,
                single_metric_types=("window_comparison",))
            if not ok_w:
                return _failed(plan, execution_result,
                               AnalysisStatus.INCOMPATIBLE_METRIC,
                               f"{window_name}校验失败：{reason_w}")
        metric_cols = metric_cols_main
        metric_col = next((c for c in main_q.columns if c in metric_cols), None)
        metric = _metric_info(metric_cols[metric_col])
        dims = [d for d in base["group_by"] if d in main_q.columns]
        eff_main = _effective_window(execution_result, "main")
        eff_comp = _effective_window(execution_result, "comparison")
        req = base["time_range"]["requested"]
        req_main = ((req["start"], req["end"]) if req["start"] and req["end"] else None)
        req_comp = ((req["comparison_start"], req["comparison_end"])
                    if req["comparison_start"] and req["comparison_end"] else None)
        # 标注窗口：优先实际元数据；缺失时用请求范围并标注来源
        main_window = eff_main or req_main
        comp_window = eff_comp or req_comp
        if main_window is None or comp_window is None:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC,
                           "无法确认窗口日期（既无窗口元数据也无请求范围），"
                           "拒绝生成窗口对比结论")
        alignment = getattr(plan, "comparison_alignment", None)
        align_value = getattr(alignment, "value", alignment) or "exact_group"
        stats = compare_windows(main_q.rows, comp_q.rows, main_q.columns,
                                comp_q.columns, dims, metric_col,
                                main_window, comp_window, metric.unit,
                                alignment=align_value)
        stats.main_window_from_effective = eff_main is not None
        stats.comparison_window_from_effective = eff_comp is not None
        # 任务 D：两窗口没有任何可配对记录时**不得**返回成功
        if not stats.pairs:
            unpaired = (len(stats.missing_in_comparison)
                        + len(stats.missing_in_main))
            reason = ("两窗口之间没有可配对的记录"
                      + (f"（{unpaired} 条记录各自只出现在一个窗口）"
                         if unpaired else "")
                      + "，无法进行对比分析")
            if align_value == "day_of_month":
                reason = ("按月内日期（1—31）对齐后两窗口没有可配对的日期，"
                          "无法进行逐日同比对比")
            return _failed(plan, execution_result,
                           AnalysisStatus.INSUFFICIENT_DATA, reason)
        warnings: list[str] = []
        # 部分配对：明确说明未配对数量与覆盖率
        if stats.missing_in_comparison or stats.missing_in_main:
            unpaired = (len(stats.missing_in_comparison)
                        + len(stats.missing_in_main))
            cov = (f"，配对覆盖率 {stats.coverage_rate:.1%}"
                   if stats.coverage_rate is not None else "")
            warnings.append(f"有 {unpaired} 条记录在另一窗口没有对应观测"
                            f"（未按 0 处理）{cov}")
        if eff_main is None:
            warnings.append("无法确认主窗口的实际执行范围（缺少窗口元数据）；"
                            "标注使用请求范围")
        elif req_main and eff_main != req_main:
            warnings.append(f"主窗口请求 {req_main[0]} 至 {req_main[1]}，"
                            f"实际执行 {eff_main[0]} 至 {eff_main[1]}")
        if eff_comp is None:
            warnings.append("无法确认对比窗口的实际执行范围（缺少窗口元数据）；"
                            "标注使用请求范围")
        elif req_comp and eff_comp != req_comp:
            warnings.append(f"对比窗口请求 {req_comp[0]} 至 {req_comp[1]}，"
                            f"实际执行 {eff_comp[0]} 至 {eff_comp[1]}")
        if align_value == "day_of_month":
            conclusions = _cross_year_daily_conclusions(
                stats, metric, query_names[0], eff_main, eff_comp,
                req_main, req_comp)
        else:
            conclusions = _window_conclusions(stats, metric, query_names[0],
                                              eff_main, eff_comp,
                                              req_main, req_comp)
        return AnalysisResult(
            status=AnalysisStatus.OK, statistics=stats,
            conclusions=conclusions,
            method=("cross_year_daily_comparison" if align_value == "day_of_month"
                    else "window_comparison"),
            method_params={"metric": metric.metric_id, "dims": dims,
                           "alignment": align_value},
            query_names=query_names, warnings=warnings + list(execution_result.warnings),
            limitations=[_CAUSALITY_LIMITATION]
            + ([_CITY_LIMITATION] if "city" in dims else []),
            **base)

    if analysis_type == "group_ranking":
        q0 = execution_result.queries[0]
        metric_cols = _metric_column_map(q0.columns, plan)
        ok, reason = _check_metrics_complete(plan, metric_cols, q0.columns,
                                             base["group_by"], analysis_type)
        if not ok:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC, reason)
        if not metric_cols:
            return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                           "排名结果中没有指标列")
        dims = [d for d in base["group_by"] if d in q0.columns]
        if not dims:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC,
                           "排名结果中没有分组维度列")
        # 多指标：每个指标独立排名（不混用、不只取第一列）
        rankings: list[RankingStats] = []
        conclusions: list[Conclusion] = []
        ranked_ids: list[str] = []
        for metric_col, mid in metric_cols.items():
            info = _metric_info(mid)
            rk = rank_groups(q0.rows, q0.columns, dims, metric_col, info, "desc")
            rankings.append(rk)
            conclusions.extend(_ranking_conclusions(rk, info, query_names[0]))
            ranked_ids.append(mid)
        stats_out = rankings[0] if len(rankings) == 1 else RankingSetStats(
            series=rankings, metric_ids=ranked_ids)
        return AnalysisResult(
            status=AnalysisStatus.OK, statistics=stats_out,
            conclusions=conclusions,
            method="group_ranking",
            method_params={"metrics": ranked_ids, "dims": dims, "order": "desc"},
            query_names=query_names,
            warnings=list(execution_result.warnings),
            limitations=[_CITY_LIMITATION] if "city" in dims else [],
            **base)

    if analysis_type == "distribution":
        q0 = execution_result.queries[0]
        metric_cols = _metric_column_map(q0.columns, plan)
        ok, reason = _check_metrics_complete(
            plan, metric_cols, q0.columns, base["group_by"], analysis_type,
            single_metric_types=("distribution",))
        if not ok:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC, reason)
        dims = [d for d in base["group_by"] if d in q0.columns]
        count_col = next((c for c in q0.columns if c in metric_cols), None)
        if count_col is None:
            return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                           "分布结果中没有计数列")
        stats = compute_distribution(q0.rows, q0.columns, dims, count_col)
        conclusions = []
        if stats.categories:
            max_share = max((c.share or 0) for c in stats.categories)
            conclusions.append(Conclusion(
                text=(f"共 {stats.sample_size} 个类别、合计 {stats.total_count}；"
                      f"最大类别占比 {max_share:.2%}"),
                metric_id=metric_primary.metric_id,
                metric_name_zh=metric_primary.name_zh, unit=metric_primary.unit,
                query_name=query_names[0], data_points=stats.sample_size,
                formula=stats.share_note,
                evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
                evidence=Evidence(query_name=query_names[0],
                                  metric_id=metric_primary.metric_id,
                                  metric_name_zh=metric_primary.name_zh,
                                  unit=metric_primary.unit,
                                  data_points=stats.sample_size,
                                  formula=stats.share_note,
                                  input_summary=f"{stats.sample_size} 个类别",
                                  method="distribution"),
                warnings=[], limitations=[]))
        return AnalysisResult(
            status=AnalysisStatus.OK, statistics=stats, conclusions=conclusions,
            method="distribution", method_params={"dims": dims, "count_col": count_col},
            query_names=query_names, warnings=list(execution_result.warnings), **base)

    if analysis_type == "descriptive":
        q0 = execution_result.queries[0]
        metric_cols = _metric_column_map(q0.columns, plan)
        ok, reason = _check_metrics_complete(plan, metric_cols, q0.columns,
                                             base["group_by"], analysis_type)
        if not ok:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC, reason)
        stats = compute_descriptive(q0.rows, q0.columns, metric_cols, plan)
        conclusions = []
        warnings: list[str] = []
        for col, v in stats.values.items():
            info = _metric_info(metric_cols[col])
            op = stats.column_ops.get(col)
            if op is None:
                warnings.append(f"列 {col} 的聚合操作无法确定，未生成措辞结论")
                continue
            label = _OP_LABELS.get(op, op)
            conclusions.append(Conclusion(
                text=f"{info.name_zh}（{col}）的{label}为 {_fmt(v, info.unit)}",
                metric_id=info.metric_id, metric_name_zh=info.name_zh,
                unit=info.unit, query_name=query_names[0],
                data_points=len(q0.rows),
                formula=f"{op}（实际 SQL 聚合操作）",
                evidence_strength=EvidenceStrength.DIRECT_COMPUTATION,
                evidence=Evidence(query_name=query_names[0],
                                  metric_id=info.metric_id,
                                  metric_name_zh=info.name_zh, unit=info.unit,
                                  data_points=len(q0.rows),
                                  formula=f"{op}（实际 SQL 聚合操作）",
                                  input_summary=f"单行汇总结果 {col}={v}",
                                  method="descriptive"),
                warnings=[], limitations=[]))
        return AnalysisResult(
            status=AnalysisStatus.OK, statistics=stats, conclusions=conclusions,
            method="descriptive", method_params={}, query_names=query_names,
            warnings=warnings + list(execution_result.warnings), **base)

    if analysis_type == "correlation":
        q0 = execution_result.queries[0]
        metric_cols = _metric_column_map(q0.columns, plan)
        if len(metric_cols) != CORRELATION_REQUIRED_METRICS:
            return _failed(
                plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                f"相关性分析要求恰好 {CORRELATION_REQUIRED_METRICS} 个指标"
                f"（当前结果含 {len(metric_cols)} 个指标列：{list(metric_cols.values())}）")
        dims = [d for d in base["group_by"] if d in q0.columns]
        if any(d in USER_DIMENSIONS for d in dims):
            return _failed(
                plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                "相关性分析暂不支持用户属性分组（同日期多行会被覆盖）；"
                "请仅按日期对齐")
        date_col = _date_column(q0.columns, base["group_by"], metric_cols)
        if date_col is None:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC,
                           "相关性分析需要日期列以对齐两个指标")
        idx = {c: i for i, c in enumerate(q0.columns)}
        # 同一日期多行 → 无法按日期对齐（拒绝，不静默覆盖）
        seen: set[str] = set()
        duplicated_dates: list[str] = []
        for row in q0.rows:
            d = _to_iso(row[idx[date_col]])
            if d in seen:
                duplicated_dates.append(d)
            seen.add(d)
        if duplicated_dates:
            return _failed(
                plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                "查询结果中同一日期存在多行（可能包含未声明的分组维度），"
                "无法按日期可靠对齐；已拒绝以免静默覆盖",
                )

        ok, reason = _check_metrics_complete(plan, metric_cols, q0.columns,
                                             base["group_by"], analysis_type)
        if not ok:
            return _failed(plan, execution_result,
                           AnalysisStatus.INCOMPATIBLE_METRIC, reason)
        a_col, b_col = list(metric_cols)[0], list(metric_cols)[1]
        sa, sb = [], []
        for row in q0.rows:
            d = _to_iso(row[idx[date_col]])
            va, vb = _to_float(row[idx[a_col]]), _to_float(row[idx[b_col]])
            if va is not None:
                sa.append((d, va))
            if vb is not None:
                sb.append((d, vb))
        stats = compute_correlation(sa, sb, metric_cols[a_col], metric_cols[b_col],
                                    "pearson")
        status = AnalysisStatus.OK
        reason = None
        if stats.coefficient is None:
            status = (AnalysisStatus.INSUFFICIENT_DATA
                      if stats.n_pairs < CORRELATION_MIN_PAIRS
                      else AnalysisStatus.METHOD_NOT_APPLICABLE)
            reason = stats.status_note
        info_a, info_b = _metric_info(metric_cols[a_col]), _metric_info(metric_cols[b_col])
        return AnalysisResult(
            status=status, status_reason=reason, statistics=stats,
            conclusions=_correlation_conclusions(stats, query_names[0],
                                                 info_a.unit, info_b.unit),
            method="correlation_pearson",
            method_params={"metric_a": info_a.metric_id,
                           "metric_b": info_b.metric_id,
                           "min_pairs": CORRELATION_MIN_PAIRS},
            query_names=query_names,
            warnings=([stats.status_note] if reason else [])
            + list(execution_result.warnings),
            limitations=[_CAUSALITY_LIMITATION], **base)

    return _failed(plan, execution_result, AnalysisStatus.INCOMPATIBLE_METRIC,
                   f"不支持的分析类型: {analysis_type}")
