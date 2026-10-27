"""Matplotlib 可视化（阶段六）。

只使用 Matplotlib（不使用 Seaborn），输出 PNG 到 `artifacts/generated/`。

原则：
- 图表数值**全部来自 `AnalysisResult`**（即真实查询结果的统计），不手工绘图；
- 标题包含指标中文名、单位与日期范围；坐标轴/图例与统计结果一致；
- 中文字体：自动检测（Microsoft YaHei / SimHei / Noto Sans CJK 等）；找不到时
  **给出警告并回退到英文标签**（不静默产生乱码）；
- 图表生成失败返回结构化 `ChartResult(status="failed")`，不影响已完成的统计结果。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")   # 无显示环境

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from . import config  # noqa: E402
from .analytics_models import (  # noqa: E402
    AnalysisResult,
    AnomalyStats,
    CorrelationStats,
    RankingSetStats,
    RankingStats,
    TrendSetStats,
    TrendStats,
    WindowComparisonStats,
)

CHINESE_FONT_CANDIDATES = (
    "Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC",
    "Source Han Sans SC", "WenQuanYi Micro Hei", "PingFang SC", "Heiti SC",
)

DEFAULT_OUTPUT_DIR = Path(config.PROJECT_ROOT) / "artifacts" / "generated"


@dataclass
class ChartResult:
    kind: str
    path: str | None
    status: str                       # "ok" | "failed"
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "status": self.status,
                "error": self.error, "warnings": self.warnings}


_font_state: dict = {"checked": False, "font": None, "warnings": []}


def ensure_chinese_font() -> tuple[str | None, list[str]]:
    """检测并配置中文字体；返回（字体名或 None，警告列表）。"""
    if not _font_state["checked"]:
        available = {f.name for f in font_manager.fontManager.ttflist}
        found = next((c for c in CHINESE_FONT_CANDIDATES if c in available), None)
        warnings: list[str] = []
        if found:
            plt.rcParams["font.sans-serif"] = [found] + list(
                plt.rcParams.get("font.sans-serif", []))
            plt.rcParams["axes.unicode_minus"] = False
        else:
            warnings.append(
                "未找到可用中文字体（候选: " + "、".join(CHINESE_FONT_CANDIDATES)
                + "）；图表标签将回退为英文，避免乱码")
        _font_state.update({"checked": True, "font": found, "warnings": warnings})
    return _font_state["font"], list(_font_state["warnings"])


def _reset_font_cache_for_tests() -> None:
    """测试用：重置字体检测缓存。"""
    _font_state.update({"checked": False, "font": None, "warnings": []})


def _label(zh: str, en: str) -> str:
    font, _ = ensure_chinese_font()
    return zh if font else en


def _save(fig, out_dir: Path, filename: str) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path)


def _result_window(result: AnalysisResult) -> tuple[str, str] | None:
    """从分析结果取**实际执行**的有效窗口（结构化元数据，不推断）。"""
    eff = (result.time_range or {}).get("effective") or {}
    if eff.get("start") and eff.get("end"):
        return (eff["start"], eff["end"])
    return None


def _title(metric_name: str, unit: str, date_range: tuple[str, str] | None,
           kind_zh: str) -> str:
    unit_text = f"，单位 {unit}" if unit else ""
    range_text = f"，{date_range[0]} 至 {date_range[1]}" if date_range else ""
    return f"{kind_zh}：{metric_name}{unit_text}{range_text}"


def _safe(kind: str, fn, *args, **kwargs) -> ChartResult:
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - 图表失败不影响统计结果
        return ChartResult(kind=kind, path=None, status="failed",
                           error=f"图表生成失败: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 各图表
# ---------------------------------------------------------------------------

def _trend_series(stats) -> list[TrendStats]:
    if isinstance(stats, TrendSetStats):
        return list(stats.series)
    if isinstance(stats, TrendStats):
        return [stats]
    return []


def plot_trend(result: AnalysisResult, out_dir: Path | None = None) -> ChartResult:
    """时间趋势折线图。

    - 单指标：一张图，多分组时逐组画线（图例为分组）；
    - **多指标**：每个指标一个子图（各自标题/单位），不会用第一个指标的名称/单位
      标注其他指标。
    """
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR
    stats = result.statistics
    series = _trend_series(stats)
    if not series:
        return ChartResult(kind="trend", path=None, status="failed",
                           error="结果中没有趋势统计数据")
    warnings = list(ensure_chinese_font()[1])
    date_range = _result_window(result)
    if date_range is None:
        # 无有效窗口元数据：标题不标注查询范围（观测值首末日期不是查询窗口）
        warnings.append("实际执行窗口未知（缺少窗口元数据）：图表标题不标注查询日期范围")

    metric_ids: list[str] = []
    for s in series:
        if s.metric_id not in metric_ids:
            metric_ids.append(s.metric_id)

    if len(metric_ids) <= 1:
        s0 = series[0]
        name = s0.metric_name_zh or (result.metrics[0].name_zh if result.metrics else "指标")
        unit = s0.unit or (result.metrics[0].unit if result.metrics else "")
        fig, ax = plt.subplots(figsize=(10, 4.8))
        axes = [ax]
        groups = series
        panels = [(ax, name, unit, groups)]
    else:
        fig, axes_arr = plt.subplots(len(metric_ids), 1, figsize=(10, 3.6 * len(metric_ids)),
                                     sharex=True, squeeze=False)
        axes = [row[0] for row in axes_arr]
        panels = []
        for i, mid in enumerate(metric_ids):
            group_series = [s for s in series if s.metric_id == mid]
            name = group_series[0].metric_name_zh or mid
            unit = group_series[0].unit
            panels.append((axes[i], name, unit, group_series))

    for ax, name, unit, group_series in panels:
        for s in group_series:
            xs = [p.date for p in s.points]
            ys = [p.value for p in s.points]
            label = " / ".join(f"{k}={v}" for k, v in s.group.items()) or name
            ax.plot(xs, ys, marker="o", markersize=2.5, linewidth=1.4, label=label)
        ax.set_title(_title(name, unit, date_range, _label("时间趋势", "Time trend")))
        ax.set_ylabel(f"{name}（{unit}）" if unit else name)
        ax.grid(alpha=0.3)
        if len(group_series) > 1:
            ax.legend(fontsize=8)
        xs_all = [p.date for s in group_series for p in s.points]
        if xs_all:
            step = max(1, len(xs_all) // 10)
            ax.set_xticks(xs_all[::step])
    if date_range is None:
        obs = sorted({p.date for s in series for p in s.points})
        if obs:
            axes[-1].set_xlabel(
                _label(f"日期（观测值范围 {obs[0]} 至 {obs[-1]}，非查询范围）",
                       f"Date (observed span {obs[0]} 至 {obs[-1]}, not query window)"))
        else:
            axes[-1].set_xlabel(_label("日期", "Date"))
    else:
        axes[-1].set_xlabel(_label("日期", "Date"))
    fig.autofmt_xdate(rotation=30)
    fname = ("trend_multi_" + "_".join(_slug(m) for m in metric_ids) + ".png"
             if len(metric_ids) > 1 else f"trend_{_slug(result.analysis_type)}.png")
    path = _save(fig, out_dir, fname)
    return ChartResult(kind="trend", path=path, status="ok", warnings=warnings)


def plot_window_comparison(result: AnalysisResult,
                           out_dir: Path | None = None) -> ChartResult:
    """主窗口 vs 对比窗口的分组条形图。"""
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR
    stats = result.statistics
    if not isinstance(stats, WindowComparisonStats) or not stats.pairs:
        return ChartResult(kind="window_comparison", path=None, status="failed",
                           error="结果中没有窗口对比统计数据")
    metric = result.metrics[0] if result.metrics else None
    name = metric.name_zh if metric else "指标"
    unit = metric.unit if metric else ""
    warnings = list(ensure_chinese_font()[1])

    labels = [" / ".join(f"{k}={v}" for k, v in p.group.items()) or _label("全部", "all")
              for p in stats.pairs]
    main_vals = [p.main_value for p in stats.pairs]
    comp_vals = [p.comparison_value for p in stats.pairs]

    fig, ax = plt.subplots(figsize=(10, 4.8))
    x = range(len(labels))
    width = 0.38
    ax.bar([i - width / 2 for i in x], comp_vals, width,
           label=_label("对比窗口", "Comparison"), color="#8ab4f8")
    ax.bar([i + width / 2 for i in x], main_vals, width,
           label=_label("主窗口", "Main"), color="#f28b82")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=8)
    def _win(win, effective: bool, which_zh: str, which_en: str) -> str:
        tag = _label(which_zh, which_en)
        if not effective:
            tag += _label("（请求范围）", " (requested)")
        return f"{tag} {win[0]} 至 {win[1]}"

    main_txt = _win(stats.main_window, stats.main_window_from_effective,
                    "主窗口", "main")
    comp_txt = _win(stats.comparison_window,
                    stats.comparison_window_from_effective, "对比窗口", "comparison")
    win_warnings = []
    if not stats.main_window_from_effective or not stats.comparison_window_from_effective:
        win_warnings.append("部分窗口缺少实际执行元数据，标题中标注为请求范围")
    warnings.extend(win_warnings)
    ax.set_title(f"{_label('窗口对比', 'Window comparison')}：{name}"
                 + (f"，单位 {unit}" if unit else "")
                 + f"\n{main_txt} vs {comp_txt}")
    ax.set_ylabel(f"{name}（{unit}）" if unit else name)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    path = _save(fig, out_dir, "window_comparison.png")
    return ChartResult(kind="window_comparison", path=path, status="ok",
                       warnings=warnings)


def plot_ranking(result: AnalysisResult, out_dir: Path | None = None,
                 top_n: int = 10) -> ChartResult:
    """分组排名水平条形图（保留原始类别编码）。"""
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR
    stats = result.statistics
    if not isinstance(stats, RankingStats) or not stats.entries:
        return ChartResult(kind="ranking", path=None, status="failed",
                           error="结果中没有排名统计数据")
    metric = result.metrics[0] if result.metrics else None
    name = metric.name_zh if metric else "指标"
    unit = metric.unit if metric else ""
    warnings = list(ensure_chinese_font()[1])

    entries = stats.entries[:top_n]
    labels = [" / ".join(f"{k}={v}" for k, v in e.group.items()) for e in entries]
    values = [e.value for e in entries]

    fig, ax = plt.subplots(figsize=(9, max(3.0, 0.42 * len(entries) + 1.6)))
    y = list(range(len(entries)))[::-1]
    ax.barh(y, values, color="#f6aea9")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_title(_title(name, unit, _result_window(result),
                        _label("分组排名", "Ranking")))
    ax.set_xlabel(f"{name}（{unit}）" if unit else name)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.grid(axis="x", alpha=0.3)
    path = _save(fig, out_dir, "ranking.png")
    return ChartResult(kind="ranking", path=path, status="ok", warnings=warnings)


def plot_ranking_set(result: AnalysisResult, out_dir: Path | None = None,
                     top_n: int = 10) -> ChartResult:
    """多指标分组排名：每个指标一个子图（标题/单位各自独立）。"""
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR
    stats = result.statistics
    if not isinstance(stats, RankingSetStats) or not stats.series:
        return ChartResult(kind="ranking", path=None, status="failed",
                           error="结果中没有多指标排名数据")
    warnings = list(ensure_chinese_font()[1])
    n = len(stats.series)
    fig, axes_arr = plt.subplots(n, 1, figsize=(9, max(3.0, 3.4 * n)),
                                 squeeze=False)
    for i, rk in enumerate(stats.series):
        ax = axes_arr[i][0]
        entries = rk.entries[:top_n]
        labels = [" / ".join(f"{k}={v}" for k, v in e.group.items()) for e in entries]
        values = [e.value for e in entries]
        y = list(range(len(entries)))[::-1]
        ax.barh(y, values, color="#f6aea9")
        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=9)
        name = next((m.name_zh for m in result.metrics
                     if m.metric_id == rk.metric_id), rk.metric_id)
        ax.set_title(_title(name, rk.unit, _result_window(result),
                            _label("分组排名", "Ranking")))
        ax.set_xlabel(f"{name}（{rk.unit}）" if rk.unit else name)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.grid(axis="x", alpha=0.3)
    path = _save(fig, out_dir, "ranking_multi_" + "_".join(_slug(m) for m in stats.metric_ids) + ".png")
    return ChartResult(kind="ranking", path=path, status="ok", warnings=warnings)


def plot_anomalies(result: AnalysisResult, out_dir: Path | None = None) -> ChartResult:
    """异常点标记图（折线 + 红色异常点）。"""
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR
    stats = result.statistics
    if not isinstance(stats, AnomalyStats) or not stats.points:
        return ChartResult(kind="anomaly", path=None, status="failed",
                           error="结果中没有异常检测数据（样本不足或序列恒定）")
    metric = result.metrics[0] if result.metrics else None
    name = metric.name_zh if metric else "指标"
    unit = metric.unit if metric else ""
    warnings = list(ensure_chinese_font()[1])

    xs = [p.date for p in stats.points]
    ys = [p.value for p in stats.points]
    ax_x = [p.date for p in stats.anomalies]
    ax_y = [p.value for p in stats.anomalies]

    fig, ax = plt.subplots(figsize=(10, 4.6))
    ax.plot(xs, ys, marker="o", markersize=2.5, linewidth=1.2,
            label=_label("观测值", "Observed"))
    if stats.center is not None:
        ax.axhline(stats.center, color="gray", linestyle="--", linewidth=1,
                   label=_label("中位数", "Median"))
    if ax_x:
        ax.scatter(ax_x, ax_y, color="#c62828", s=55, zorder=5,
                   label=_label(f"异常点（k={stats.threshold_k}）",
                                f"Anomalies (k={stats.threshold_k})"))
    win = _result_window(result)
    if win is None:
        warnings.append("实际执行窗口未知（缺少窗口元数据）：标题不标注查询日期范围")
    ax.set_title(_title(name, unit, win,
                        _label("异常点检测（稳健 MAD）", "Anomaly detection (robust MAD)")))
    ax.set_xlabel(_label("日期", "Date"))
    ax.set_ylabel(f"{name}（{unit}）" if unit else name)
    ax.grid(alpha=0.3)
    step = max(1, len(xs) // 10)
    ax.set_xticks(xs[::step])
    fig.autofmt_xdate(rotation=30)
    ax.legend(fontsize=8)
    path = _save(fig, out_dir, "anomaly.png")
    return ChartResult(kind="anomaly", path=path, status="ok", warnings=warnings)


def plot_correlation(result: AnalysisResult, out_dir: Path | None = None) -> ChartResult:
    """两指标相关性散点图。"""
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR
    stats = result.statistics
    if not isinstance(stats, CorrelationStats) or not stats.pairs:
        return ChartResult(kind="correlation", path=None, status="failed",
                           error="结果中没有相关性配对数据")
    warnings = list(ensure_chinese_font()[1])
    xs = [p["a"] for p in stats.pairs]
    ys = [p["b"] for p in stats.pairs]

    fig, ax = plt.subplots(figsize=(6.4, 5.2))
    ax.scatter(xs, ys, s=22, alpha=0.8, color="#f6aea9")
    coef = ("r=%.4f" % stats.coefficient) if stats.coefficient is not None else "r 不可计算"
    ax.set_title(f"{stats.metric_a} vs {stats.metric_b}\n"
                 f"{stats.method} {coef}，n={stats.n_pairs}")
    ax.set_xlabel(stats.metric_a)
    ax.set_ylabel(stats.metric_b)
    ax.grid(alpha=0.3)
    path = _save(fig, out_dir, "correlation.png")
    return ChartResult(kind="correlation", path=path, status="ok", warnings=warnings)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:40]


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def cross_year_series(stats) -> tuple[list[int], list[float], list[float]]:
    """构造跨年逐日图的数据数组（可测试）。

    返回 (横轴月内日期, 主窗口序列, 对比窗口序列)：
    - 横轴为两窗口所属月份的**完整日历天数**（即使 31 日无观测也占位到月末）；
    - 各年份每日原始金额**独立保存**，单边缺失时另一边的观测值仍显示、缺失侧用 NaN；
    - 缺失观测用 NaN（**不以 0 替代**，绘图时自动断开，不跨缺失日连线）；
    - 两个年份各自成线，不混在同一条序列里。
    """
    # 优先使用两个年份各自独立的每日原始金额；否则回退到配对结果
    main_pts: dict[int, float] = {}
    comp_pts: dict[int, float] = {}
    md = getattr(stats, "main_daily_amounts", None)
    cd = getattr(stats, "comparison_daily_amounts", None)
    if md or cd:
        main_pts = {int(k): float(v) for k, v in (md or {}).items()}
        comp_pts = {int(k): float(v) for k, v in (cd or {}).items()}
    else:
        for pair in getattr(stats, "pairs", []) or []:
            key = getattr(pair, "align_key", None)
            if not key or "-" not in key:
                continue
            try:
                day = int(key.split("-", 1)[1])
            except (TypeError, ValueError):
                continue
            main_pts[day] = pair.main_value
            comp_pts[day] = pair.comparison_value

    # 横轴：两窗口所属月份的完整日历天数（缺失 31 日也显示到月末）
    max_day = 0
    for win in (getattr(stats, "main_window", None),
                getattr(stats, "comparison_window", None)):
        if not win or not win[0]:
            continue
        try:
            y, m, _ = str(win[0]).split("-")
            max_day = max(max_day, calendar.monthrange(int(y), int(m))[1])
        except (ValueError, IndexError, AttributeError):
            pass
    if max_day == 0:
        observed = list(main_pts) + list(comp_pts)
        max_day = max(observed) if observed else 0
    if max_day == 0:
        return [], [], []

    days = list(range(1, max_day + 1))
    main_vals = [(main_pts[d] if d in main_pts else float("nan")) for d in days]
    comp_vals = [(comp_pts[d] if d in comp_pts else float("nan")) for d in days]
    return days, main_vals, comp_vals


def plot_cross_year_daily(result: AnalysisResult,
                         out_dir: Path | None = None) -> ChartResult:
    """跨年同月逐日对比：两条折线（主窗口 vs 对比窗口）。

    - 横坐标：月内日期（1—31）；缺失观测用 NaN 表示（**不补零、不连线**）；
    - 纵坐标：每日指标金额（必要时按万元缩放并标注）；
    - 图例区分两个窗口（用各自窗口的年份标注）。
    """
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUTPUT_DIR
    stats = result.statistics
    if not isinstance(stats, WindowComparisonStats) or not stats.pairs:
        return ChartResult(kind="cross_year_daily", path=None, status="failed",
                           error="结果中没有可用的逐日对比数据")
    metric = result.metrics[0] if result.metrics else None
    name = metric.name_zh if metric else "指标"
    unit = metric.unit if metric else ""
    warnings = list(ensure_chinese_font()[1])

    # 对齐键 → 月内日期（1—31）
    def _day_of(align_key: str | None) -> int | None:
        if not align_key or "-" not in align_key:
            return None
        try:
            _month, d = align_key.split("-", 1)
            return int(d)
        except (TypeError, ValueError):
            return None

    # 数据数组（完整横轴 + 缺失 NaN）由 cross_year_series 统一构造
    all_days, main_series, comp_series = cross_year_series(stats)
    if not all_days:
        return ChartResult(kind="cross_year_daily", path=None, status="failed",
                           error="逐日对齐结果为空")

    finite = [v for v in (main_series + comp_series) if v == v]
    max_val = max(finite) if finite else 0.0
    scale = 1.0
    scale_label = unit
    if unit == "元" and max_val >= 1_000_000:
        scale = 10_000.0
        scale_label = "万元"

    def _scaled(values: list[float]) -> list[float]:
        return [(v / scale) if v == v else float("nan") for v in values]

    main_year = (stats.main_window[0][:4] if stats.main_window else "")
    comp_year = (stats.comparison_window[0][:4] if stats.comparison_window else "")

    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(all_days, _scaled(comp_series), marker="o", markersize=3, linewidth=1.6,
            color="#8ab4f8",
            label=_label(f"对比窗口 {comp_year}", f"Comparison {comp_year}"))
    ax.plot(all_days, _scaled(main_series), marker="o", markersize=3, linewidth=1.6,
            color="#f28b82",
            label=_label(f"主窗口 {main_year}", f"Main {main_year}"))
    ax.set_xlabel(_label("月内日期（1—31）", "Day of month (1-31)"))
    ax.set_ylabel(f"{name}（{scale_label}）" if scale_label else name)
    ax.set_title(f"{_label('跨年同月逐日对比', 'Cross-year daily comparison')}：{name}"
                 + (f"，单位 {scale_label}" if scale_label else "")
                 + f"\n{stats.main_window[0]} 至 {stats.main_window[1]}"
                 + f" vs {stats.comparison_window[0]} 至 {stats.comparison_window[1]}")
    ax.set_xticks(all_days)
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    path = _save(fig, out_dir, "cross_year_daily.png")
    if stats.paired_days < stats.main_days:
        warnings.append(f"部分日期在对比窗口没有对应观测（未补零、未连线）："
                        f"配对 {stats.paired_days}/{stats.main_days} 天")
    return ChartResult(kind="cross_year_daily", path=path, status="ok",
                       warnings=warnings)


def generate_charts(result: AnalysisResult, out_dir: Path | None = None) -> list[ChartResult]:
    """按统计结果类型生成对应图表（失败不影响统计结果）。"""
    stats = result.statistics
    results: list[ChartResult] = []
    if isinstance(stats, (TrendStats, TrendSetStats)):
        results.append(_safe("trend", plot_trend, result, out_dir))
    if isinstance(stats, WindowComparisonStats):
        if getattr(stats, "alignment", "exact_group") == "day_of_month":
            results.append(_safe("cross_year_daily", plot_cross_year_daily,
                                 result, out_dir))
        else:
            results.append(_safe("window_comparison", plot_window_comparison,
                                 result, out_dir))
    if isinstance(stats, RankingStats):
        results.append(_safe("ranking", plot_ranking, result, out_dir))
    if isinstance(stats, RankingSetStats):
        results.append(_safe("ranking", plot_ranking_set, result, out_dir))
    if isinstance(stats, AnomalyStats):
        results.append(_safe("anomaly", plot_anomalies, result, out_dir))
    if isinstance(stats, CorrelationStats):
        results.append(_safe("correlation", plot_correlation, result, out_dir))
    return results
