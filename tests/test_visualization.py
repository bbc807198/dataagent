"""可视化测试（阶段六）：PNG 生成、路径/标签一致性、中文字体警告与失败隔离。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dataagent import analytics, visualization
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
)
from dataagent.analysis_runner import AnalysisExecutionResult, QueryExecution


def _plan(**kw):
    base = dict(
        question="绘图测试", intent="绘图",
        analysis_type=AnalysisType.TIME_TREND,
        metrics=[MetricSpec(metric_id="purchase_amount")],
        time_range=TimeRangeSpec(
            type="explicit_dates",
            resolved_start_date=date(2024, 1, 1),
            resolved_end_date=date(2024, 1, 5),
            resolution_status="resolved"),
        group_by=[], statistics=[StatOperation.SUM])
    base.update(kw)
    return AnalysisPlan(**base)


def _exec(queries):
    return AnalysisExecutionResult(status="ok", plan_summary={}, queries=queries)


def _q(name, columns, rows, **kw):
    return QueryExecution(name=name, status="ok", columns=columns, rows=rows,
                          row_count=len(rows), **kw)


def _d(s):
    return date.fromisoformat(s)


@pytest.fixture(autouse=True)
def _reset_font(monkeypatch):
    visualization._reset_font_cache_for_tests()
    yield
    visualization._reset_font_cache_for_tests()


class TestTrendChart:
    def test_chart_generated(self, tmp_path):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0),
                (_d("2024-01-03"), 90.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        res = visualization.generate_charts(r, tmp_path)
        assert len(res) == 1
        assert res[0].kind == "trend"
        assert res[0].status == "ok"
        p = Path(res[0].path)
        assert p.exists() and p.stat().st_size > 1000   # 非空 PNG
        assert p.suffix == ".png"

    def test_title_contains_metric_unit_and_range(self, tmp_path):
        rows = [(_d("2024-01-01"), 100.0), (_d("2024-01-02"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        title = visualization._title("申购金额", "元", ("2024-01-01", "2024-01-02"),
                                     "时间趋势")
        assert "申购金额" in title and "元" in title and "2024-01-01" in title
        # 图表的实际标题由同一函数生成（数值来自分析结果）
        c = visualization.plot_trend(r, tmp_path)
        assert c.status == "ok"


class TestOtherCharts:
    def test_window_chart(self, tmp_path):
        main = [("6081949", 80.0), ("6281949", 200.0)]
        comp = [("6081949", 100.0), ("6281949", 150.0)]
        p = _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                  metrics=[MetricSpec(metric_id="net_inflow")],
                  group_by=["city"],
                  time_range=TimeRangeSpec(
                      type="window_comparison",
                      resolved_start_date=date(2024, 2, 1),
                      resolved_end_date=date(2024, 2, 29),
                      comparison_start_date=date(2024, 1, 1),
                      comparison_end_date=date(2024, 1, 31),
                      resolution_status="resolved"),
                  statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])
        r = analytics.analyze_execution_result(
            _exec([_q("m", ["city", "net_inflow"], main),
                   _q("c", ["city", "net_inflow"], comp)]), p)
        res = visualization.generate_charts(r, tmp_path)
        assert res[0].kind == "window_comparison"
        assert res[0].status == "ok" and Path(res[0].path).exists()

    def test_ranking_chart(self, tmp_path):
        rows = [("6081949", 1100.0), ("6281949", 1500.0), ("6301949", 250.0)]
        p = _plan(analysis_type=AnalysisType.GROUP_RANKING, group_by=["city"],
                  statistics=[StatOperation.SUM, StatOperation.TOP_N])
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["city", "purchase_amount"], rows)]), p)
        res = visualization.generate_charts(r, tmp_path)
        assert res[0].kind == "ranking" and res[0].status == "ok"
        assert Path(res[0].path).exists()

    def test_anomaly_chart(self, tmp_path):
        vals = [10, 11, 10, 12, 10, 11, 40, 10, 11, 10]
        rows = [(_d(f"2024-03-{i:02d}"), float(v)) for i, v in enumerate(vals, 1)]
        r = analytics.analyze_execution_result(
            _exec([_q("a", ["report_date", "purchase_amount"], rows)]),
            _plan(analysis_type=AnalysisType.ANOMALY))
        res = visualization.generate_charts(r, tmp_path)
        assert res[0].kind == "anomaly" and res[0].status == "ok"
        assert Path(res[0].path).exists()

    def test_correlation_chart(self, tmp_path):
        rows = [(_d("2024-04-01"), 1.0, 2.0), (_d("2024-04-02"), 2.0, 4.0),
                (_d("2024-04-03"), 3.0, 6.0)]
        p = _plan(analysis_type=AnalysisType.CORRELATION,
                  metrics=[MetricSpec(metric_id="purchase_amount"),
                           MetricSpec(metric_id="daily_yield")],
                  group_by=["report_date"],
                  statistics=[StatOperation.SUM, StatOperation.AVG])
        r = analytics.analyze_execution_result(
            _exec([_q("c", ["report_date", "purchase_amount_sum",
                           "daily_yield_avg"], rows)]), p)
        res = visualization.generate_charts(r, tmp_path)
        assert res[0].kind == "correlation" and res[0].status == "ok"


class TestFontAndFailure:
    def test_chinese_font_detected(self):
        font, warnings = visualization.ensure_chinese_font()
        if font is None:
            # 无中文字体环境：必须给出警告，且标签回退英文
            assert warnings and "中文字体" in warnings[0]
            assert visualization._label("中文", "English") == "English"
        else:
            assert font in visualization.CHINESE_FONT_CANDIDATES
            assert visualization._label("中文", "English") == "中文"

    def test_missing_font_warns_and_falls_back(self, monkeypatch):
        """模拟无中文字体：必须有警告，且回退英文标签（不产生乱码）。"""
        monkeypatch.setattr(visualization, "CHINESE_FONT_CANDIDATES", ())
        visualization._reset_font_cache_for_tests()
        font, warnings = visualization.ensure_chinese_font()
        assert font is None
        assert any("中文字体" in w for w in warnings)
        assert visualization._label("中文标签", "English label") == "English label"

    def test_chart_failure_is_structured(self, tmp_path):
        """无匹配统计结果时返回结构化失败，不抛异常。"""
        rows = [(_d("2024-01-01"), 1.0), (_d("2024-01-02"), 2.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        # 强行调用排名绘图（统计类型不匹配）
        res = visualization.plot_ranking(r, tmp_path)
        assert res.status == "failed"
        assert res.path is None
        assert "没有排名统计数据" in res.error

    def test_chart_failure_does_not_affect_statistics(self, tmp_path):
        rows = [(_d("2024-01-01"), 10.0), (_d("2024-01-02"), 20.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        before = r.statistics.total
        visualization.plot_ranking(r, tmp_path)      # 失败调用
        assert r.statistics.total == before           # 统计结果不变


# ===========================================================================
# 任务 06.2-B/C：图表日期范围与双窗口标题
# ===========================================================================

class TestChartWindowSemantics:
    def test_trend_without_metadata_no_fake_range(self, tmp_path):
        rows = [(_d("2024-01-05"), 100.0), (_d("2024-01-09"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        res = visualization.plot_trend(r, tmp_path)
        assert res.status == "ok"
        assert any("实际执行窗口未知" in w for w in res.warnings)

    def test_trend_with_metadata_uses_effective(self, tmp_path):
        rows = [(_d("2024-01-02"), 100.0), (_d("2024-01-03"), 110.0)]
        q = _q("q", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-05")
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        res = visualization.plot_trend(r, tmp_path)
        assert res.status == "ok"
        assert visualization._result_window(r) == ("2024-01-01", "2024-01-05")

    def test_window_chart_title_distinguishes_windows(self, tmp_path):
        main = [("6081949", 80.0)]
        comp = [("6081949", 100.0)]
        q_main = _q("m", ["city", "net_inflow"], main, window_label="main",
                    window_start="2024-03-01", window_end="2024-03-31")
        q_comp = _q("c", ["city", "net_inflow"], comp, window_label="comparison",
                    window_start="2024-01-01", window_end="2024-01-31")
        p = _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                  metrics=[MetricSpec(metric_id="net_inflow")],
                  group_by=["city"],
                  time_range=TimeRangeSpec(
                      type="window_comparison",
                      resolved_start_date=date(2024, 3, 1),
                      resolved_end_date=date(2024, 3, 31),
                      comparison_start_date=date(2024, 1, 1),
                      comparison_end_date=date(2024, 1, 31),
                      resolution_status="resolved"),
                  statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])
        r = analytics.analyze_execution_result(_exec([q_main, q_comp]), p)
        res = visualization.plot_window_comparison(r, tmp_path)
        assert res.status == "ok"
        # 双窗口分别记录（不合并）
        assert r.conclusions[0].main_window == ("2024-03-01", "2024-03-31")
        assert r.conclusions[0].comparison_window == ("2024-01-01", "2024-01-31")

    def test_window_chart_warns_when_metadata_missing(self, tmp_path):
        main = [("6081949", 80.0)]
        comp = [("6081949", 100.0)]
        q_main = _q("m", ["city", "net_inflow"], main, window_label="main")
        q_comp = _q("c", ["city", "net_inflow"], comp, window_label="comparison")
        p = _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                  metrics=[MetricSpec(metric_id="net_inflow")],
                  group_by=["city"],
                  time_range=TimeRangeSpec(
                      type="window_comparison",
                      resolved_start_date=date(2024, 2, 1),
                      resolved_end_date=date(2024, 2, 29),
                      comparison_start_date=date(2024, 1, 1),
                      comparison_end_date=date(2024, 1, 31),
                      resolution_status="resolved"),
                  statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])
        r = analytics.analyze_execution_result(_exec([q_main, q_comp]), p)
        res = visualization.plot_window_comparison(r, tmp_path)
        assert res.status == "ok"
        assert any("请求范围" in w for w in res.warnings)


# ===========================================================================
# 任务 06.2-B/C：图表日期范围与双窗口标题
# ===========================================================================

class TestChartWindowSemantics:
    def test_trend_without_metadata_no_fake_range(self, tmp_path):
        rows = [(_d("2024-01-05"), 100.0), (_d("2024-01-09"), 110.0)]
        r = analytics.analyze_execution_result(
            _exec([_q("q", ["report_date", "purchase_amount"], rows)]), _plan())
        res = visualization.plot_trend(r, tmp_path)
        assert res.status == "ok"
        assert any("实际执行窗口未知" in w for w in res.warnings)

    def test_trend_with_metadata_uses_effective(self, tmp_path):
        rows = [(_d("2024-01-02"), 100.0), (_d("2024-01-03"), 110.0)]
        q = _q("q", ["report_date", "purchase_amount"], rows,
               window_start="2024-01-01", window_end="2024-01-05")
        r = analytics.analyze_execution_result(_exec([q]), _plan())
        res = visualization.plot_trend(r, tmp_path)
        assert res.status == "ok"
        assert visualization._result_window(r) == ("2024-01-01", "2024-01-05")

    def test_window_chart_title_distinguishes_windows(self, tmp_path):
        main = [("6081949", 80.0)]
        comp = [("6081949", 100.0)]
        q_main = _q("m", ["city", "net_inflow"], main, window_label="main",
                    window_start="2024-03-01", window_end="2024-03-31")
        q_comp = _q("c", ["city", "net_inflow"], comp, window_label="comparison",
                    window_start="2024-01-01", window_end="2024-01-31")
        p = _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                  metrics=[MetricSpec(metric_id="net_inflow")],
                  group_by=["city"],
                  time_range=TimeRangeSpec(
                      type="window_comparison",
                      resolved_start_date=date(2024, 3, 1),
                      resolved_end_date=date(2024, 3, 31),
                      comparison_start_date=date(2024, 1, 1),
                      comparison_end_date=date(2024, 1, 31),
                      resolution_status="resolved"),
                  statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])
        r = analytics.analyze_execution_result(_exec([q_main, q_comp]), p)
        res = visualization.plot_window_comparison(r, tmp_path)
        assert res.status == "ok"
        # 双窗口分别记录（不合并）
        assert r.conclusions[0].main_window == ("2024-03-01", "2024-03-31")
        assert r.conclusions[0].comparison_window == ("2024-01-01", "2024-01-31")

    def test_window_chart_warns_when_metadata_missing(self, tmp_path):
        main = [("6081949", 80.0)]
        comp = [("6081949", 100.0)]
        q_main = _q("m", ["city", "net_inflow"], main, window_label="main")
        q_comp = _q("c", ["city", "net_inflow"], comp, window_label="comparison")
        p = _plan(analysis_type=AnalysisType.WINDOW_COMPARISON,
                  metrics=[MetricSpec(metric_id="net_inflow")],
                  group_by=["city"],
                  time_range=TimeRangeSpec(
                      type="window_comparison",
                      resolved_start_date=date(2024, 2, 1),
                      resolved_end_date=date(2024, 2, 29),
                      comparison_start_date=date(2024, 1, 1),
                      comparison_end_date=date(2024, 1, 31),
                      resolution_status="resolved"),
                  statistics=[StatOperation.SUM, StatOperation.WINDOW_DIFF])
        r = analytics.analyze_execution_result(_exec([q_main, q_comp]), p)
        res = visualization.plot_window_comparison(r, tmp_path)
        assert res.status == "ok"
        assert any("请求范围" in w for w in res.warnings)
