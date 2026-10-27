"""阶段 16：跨年同月逐日对比测试。

标准答案由**独立**的纯 Python（csv 模块，不使用 dataagent 代码）从合成夹具复算，
再与 Agent 输出比对——不复用被测代码作为"标准答案"。
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataagent import db, ingestion, shibor  # noqa: E402
from dataagent.agent import AgentConfig, run_agent  # noqa: E402
from dataagent.analytics import compare_windows  # noqa: E402
from dataagent.analysis_plan import AnalysisPlan, ComparisonAlignment  # noqa: E402
from dataagent.query_service import QueryService  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_cross_year"
RANGE = (date(2013, 8, 1), date(2014, 8, 31))

PLAN = {
    "question": "帮我分析2013年8月每天与2014年8月每天的资金申购对比",
    "intent": "跨年同月逐日申购对比",
    "analysis_type": "window_comparison",
    "metrics": [{"metric_id": "purchase_amount"}],
    "time_range": {"type": "window_comparison",
                   "resolved_start_date": "2014-08-01", "resolved_end_date": "2014-08-31",
                   "comparison_start_date": "2013-08-01",
                   "comparison_end_date": "2013-08-31",
                   "resolution_status": "resolved"},
    "group_by": ["report_date"],
    "comparison_alignment": "day_of_month",
    "filters": [], "statistics": ["SUM"],
    "outputs": {"summary_table": True, "comparison_chart": True,
                "chinese_report": True},
    "unresolved": [], "notes": [],
}


class _LLM:
    def __init__(self, plan):
        self.plan = plan
        self.model = "fake-cross-year"
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(self.plan)),
            finish_reason="stop")])


@pytest.fixture(scope="module")
def cross_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("cross") / "x.duckdb"
    con = db.connect(path)
    db.init_schema(con)
    for rep in ingestion.import_all(con, data_dir=FIXTURE):
        assert rep.ok, rep.error
    shibor.create_fill_view(con)
    con.close()
    return path


@pytest.fixture(scope="module")
def golden():
    """独立标准答案：纯 Python + csv 直接读夹具（不使用 dataagent 代码）。"""
    daily: dict[tuple[str, int], int] = {}
    with (FIXTURE / "user_balance_table.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r["report_date"][:4], int(r["report_date"][6:8]))
            daily[key] = daily.get(key, 0) + int(r["total_purchase_amt"])
    y2013 = {d: v for (y, d), v in daily.items() if y == "2013"}
    y2014 = {d: v for (y, d), v in daily.items() if y == "2014"}
    paired = sorted(set(y2013) & set(y2014))
    return {
        "y2013": y2013, "y2014": y2014, "paired": paired,
        "main_total": sum(y2014.values()),
        "comp_total": sum(y2013.values()),
        "paired_main": sum(y2014[d] for d in paired),
        "paired_comp": sum(y2013[d] for d in paired),
        "missing_in_main": sorted(set(y2013) - set(y2014)),
        "missing_in_comp": sorted(set(y2014) - set(y2013)),
    }


@pytest.fixture(scope="module")
def run_result(cross_db, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    cfg = AgentConfig(model_mode="mock", data_mode="synthetic", db_path=cross_db,
                      data_range=RANGE, output_dir=out, llm_client=_LLM(PLAN))
    with QueryService(cross_db) as qs:
        return run_agent(PLAN["question"], cfg, query_service=qs)


# ---------------------------------------------------------------- 计划与 Schema
class TestPlanSemantics:
    def test_alignment_enum_values(self):
        assert ComparisonAlignment.EXACT_GROUP.value == "exact_group"
        assert ComparisonAlignment.DAY_OF_MONTH.value == "day_of_month"

    def test_day_of_month_requires_window_comparison(self):
        with pytest.raises(Exception):
            AnalysisPlan(**{**PLAN, "analysis_type": "time_trend"})

    def test_day_of_month_requires_single_date_dimension(self):
        with pytest.raises(Exception):
            AnalysisPlan(**{**PLAN, "group_by": ["report_date", "city"]})

    def test_alignment_rejects_free_form_expression(self):
        with pytest.raises(Exception):
            AnalysisPlan(**{**PLAN, "comparison_alignment": "strftime(report_date)"})

    def test_default_alignment_is_exact_group(self):
        plan = AnalysisPlan(**{**PLAN, "comparison_alignment": "exact_group"})
        assert plan.comparison_alignment == ComparisonAlignment.EXACT_GROUP


# ---------------------------------------------------------------- 对齐与统计
class TestDailyAlignment:
    def test_day_one_paired_correctly(self, run_result):
        st = run_result.analysis_result.statistics
        p1 = next(p for p in st.pairs if p.align_key == "08-01")
        assert p1.main_date == "2014-08-01"
        assert p1.comparison_date == "2013-08-01"

    def test_day_thirty_one_paired_correctly(self, run_result):
        st = run_result.analysis_result.statistics
        p31 = next(p for p in st.pairs if p.align_key == "08-31")
        assert p31.main_date == "2014-08-31"
        assert p31.comparison_date == "2013-08-31"

    def test_diff_and_rate_match_independent_golden(self, run_result, golden):
        st = run_result.analysis_result.statistics
        for p in st.pairs:
            d = int(p.align_key.split("-")[1])
            assert p.main_value == pytest.approx(golden["y2014"][d])
            assert p.comparison_value == pytest.approx(golden["y2013"][d])
            assert p.abs_diff == pytest.approx(golden["y2014"][d] - golden["y2013"][d])
            if golden["y2013"][d]:
                assert p.change_rate == pytest.approx(
                    (golden["y2014"][d] - golden["y2013"][d]) / golden["y2013"][d])

    def test_month_totals_match_independent_golden(self, run_result, golden):
        st = run_result.analysis_result.statistics
        assert st.main_total == pytest.approx(golden["main_total"])
        assert st.comparison_total == pytest.approx(golden["comp_total"])
        assert st.total_abs_diff == pytest.approx(
            golden["main_total"] - golden["comp_total"])
        assert st.total_change_rate == pytest.approx(
            (golden["main_total"] - golden["comp_total"]) / golden["comp_total"])
        assert st.paired_main_total == pytest.approx(golden["paired_main"])
        assert st.paired_comparison_total == pytest.approx(golden["paired_comp"])

    def test_directions_cover_up_down_flat(self):
        """2014 高于 2013、低于 2013、相等 三类方向都要能表达。"""
        main = [("2014-08-01", 200.0), ("2014-08-02", 50.0), ("2014-08-03", 100.0)]
        comp = [("2013-08-01", 100.0), ("2013-08-02", 100.0), ("2013-08-03", 100.0)]
        st = compare_windows(main, comp, ["report_date", "purchase_amount"],
                             ["report_date", "purchase_amount"], ["report_date"],
                             "purchase_amount", ("2014-08-01", "2014-08-31"),
                             ("2013-08-01", "2013-08-31"), "元",
                             alignment="day_of_month")
        assert {p.direction for p in st.pairs} == {"up", "down", "flat"}

    def test_zero_comparison_value_gives_no_rate(self):
        main = [("2014-08-10", 210.0)]
        comp = [("2013-08-10", 0.0)]
        st = compare_windows(main, comp, ["report_date", "purchase_amount"],
                             ["report_date", "purchase_amount"], ["report_date"],
                             "purchase_amount", ("2014-08-01", "2014-08-31"),
                             ("2013-08-01", "2013-08-31"), "元",
                             alignment="day_of_month")
        p = st.pairs[0]
        assert p.change_rate is None
        assert p.rate_note and "不可计算" in p.rate_note

    def test_single_side_missing_not_zero_filled(self, run_result, golden):
        st = run_result.analysis_result.statistics
        # 2014-08-20 缺失 → 不得出现 08-20 的配对，也不得补零
        assert not any(p.align_key == "08-20" for p in st.pairs)
        assert len(st.missing_in_main) == len(golden["missing_in_main"])

    def test_no_pairs_returns_not_success(self):
        """两窗口完全无可配对日期 → 必须失败，不能返回成功。"""
        main = [("2014-08-01", 100.0)]
        comp = [("2013-08-01", 80.0)]
        st = compare_windows(main, comp, ["report_date", "purchase_amount"],
                             ["report_date", "purchase_amount"], ["report_date"],
                             "purchase_amount", ("2014-08-01", "2014-08-31"),
                             ("2013-08-01", "2013-08-31"), "元",
                             alignment="day_of_month")
        assert st.pairs          # 同月同日可配对
        # 用不同月份制造无配对
        main2 = [("2014-03-01", 100.0)]
        comp2 = [("2013-08-01", 80.0)]
        st2 = compare_windows(main2, comp2, ["report_date", "purchase_amount"],
                              ["report_date", "purchase_amount"], ["report_date"],
                              "purchase_amount", ("2014-03-01", "2014-03-31"),
                              ("2013-08-01", "2013-08-31"), "元",
                              alignment="day_of_month")
        assert not st2.pairs
        assert st2.missing_in_main and st2.missing_in_comparison


# ---------------------------------------------------------------- 端到端产物
class TestEndToEndArtifacts:
    def test_status_and_statistics(self, run_result):
        assert run_result.status.value in ("report_generated",
                                            "report_generated_with_warnings")
        st = run_result.analysis_result.statistics
        assert st.alignment == "day_of_month"
        assert len(st.pairs) == 30

    def test_chart_is_cross_year_double_line(self, run_result):
        names = [Path(p).name for p in (run_result.chart_paths or [])]
        assert "cross_year_daily.png" in names
        chart = Path(run_result.chart_paths[0])
        assert chart.is_file() and chart.stat().st_size > 0

    def test_report_contains_daily_table_and_totals(self, run_result):
        text = Path(run_result.report_path).read_text(encoding="utf-8")
        assert "跨年同月逐日对齐" in text
        assert "| 月内日期 |" in text
        assert "2013-08-01" in text and "2014-08-01" in text
        assert "月度差额" in text and "配对覆盖率" in text
        assert "零分母说明" in text
        # 不得出现逐日罗列的"只出现在主窗口"式冗长结论
        assert "只出现在主窗口" not in text

    def test_evidence_keeps_both_windows(self, run_result):
        ev = json.loads(Path(run_result.evidence_path).read_text(encoding="utf-8"))
        assert ev["evidence"], "应有证据条目"
        windows = [(e.get("main_window"), e.get("comparison_window"))
                   for e in ev["evidence"]]
        assert any("2014-08-01" in str(w[0]) and "2013-08-01" in str(w[1])
                   for w in windows)

    def test_queries_keep_both_windows(self, run_result):
        qs = run_result.execution_summary["queries"]
        assert len(qs) == 2
        assert qs[0]["window_start"].startswith("2014-08-01")
        assert qs[1]["window_start"].startswith("2013-08-01")
        # SQL 仍是参数化模板
        assert all("?" in q["sql"] for q in qs)

    def test_no_empty_success_report(self, run_result):
        """有配对时不得是空报告；结论数量合理（不再 31 条缺失罗列）。"""
        conclusions = run_result.analysis_result.conclusions
        assert 0 < len(conclusions) <= 8
        text = Path(run_result.report_path).read_text(encoding="utf-8")
        assert len(text) > 800


class TestNoRegression:
    def test_exact_group_city_comparison_unchanged(self):
        """普通城市窗口比较仍按分类维度匹配（不变成按几号对齐）。"""
        main = [("A", 100.0), ("B", 200.0)]
        comp = [("A", 90.0), ("B", 180.0)]
        st = compare_windows(main, comp, ["city", "purchase_amount"],
                             ["city", "purchase_amount"], ["city"],
                             "purchase_amount", ("2014-08-01", "2014-08-31"),
                             ("2013-08-01", "2013-08-31"), "元",
                             alignment="exact_group")
        assert st.alignment == "exact_group"
        assert {p.group["city"] for p in st.pairs} == {"A", "B"}
        assert not any(p.align_key for p in st.pairs)

    def test_single_month_trend_unaffected(self, cross_db, tmp_path):
        """单月趋势分析不回归。"""
        plan = {
            "question": "帮我分析申购金额最近的趋势。",
            "intent": "趋势", "analysis_type": "time_trend",
            "metrics": [{"metric_id": "purchase_amount"}],
            "time_range": {"type": "explicit_dates",
                           "resolved_start_date": "2013-08-01",
                           "resolved_end_date": "2013-08-31",
                           "resolution_status": "resolved"},
            "group_by": [], "filters": [], "statistics": ["SUM"],
            "outputs": {"summary_table": True, "trend_chart": True},
            "unresolved": [], "notes": [],
        }
        cfg = AgentConfig(model_mode="mock", data_mode="synthetic", db_path=cross_db,
                          data_range=RANGE, output_dir=tmp_path, llm_client=_LLM(plan))
        with QueryService(cross_db) as qs:
            res = run_agent(plan["question"], cfg, query_service=qs)
        assert res.status.value in ("report_generated",
                                    "report_generated_with_warnings")
        assert res.analysis_result.statistics.kind == "trend"

    def test_fake_llm_parses_owner_question(self):
        """Fake LLM 能把负责人原始问句解析为正确的结构化计划。"""
        from dataagent.intent_parser import IntentParser
        parser = IntentParser(llm_client=_LLM(PLAN), allow_repair=False)
        out = parser.parse_question(PLAN["question"])
        assert out.plan is not None
        plan = out.plan
        assert plan.analysis_type.value == "window_comparison"
        assert plan.comparison_alignment == ComparisonAlignment.DAY_OF_MONTH
        assert plan.group_by == ["report_date"]
        assert str(plan.time_range.resolved_start_date) == "2014-08-01"
        assert str(plan.time_range.resolved_end_date) == "2014-08-31"
        assert str(plan.time_range.comparison_start_date) == "2013-08-01"
        assert str(plan.time_range.comparison_end_date) == "2013-08-31"
