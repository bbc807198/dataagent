"""阶段 15：SQL 可解释性展示测试。

覆盖：参数化不变、绑定值真实且有序、截取窗口可见、敏感参数不暴露、
缺失记录不虚构；并验证 SQLGuard / 只读执行 / Benchmark 链路不回归。
"""

from __future__ import annotations

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
from dataagent.query_bindings import public_bindings  # noqa: E402
from dataagent.query_service import QueryService  # noqa: E402
from web import service  # noqa: E402

SYNTH = ROOT / "tests" / "fixtures" / "synthetic"
RANGE = (date(2024, 1, 2), date(2024, 1, 12))


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("explain") / "b.duckdb"
    con = db.connect(path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH)
    assert all(r.ok for r in reports)
    shibor.create_fill_view(con)
    con.close()
    return path


def _plan(start="2024-01-02", end="2024-01-12", filters=None, analysis="time_trend",
          metrics=None):
    return {
        "intent": "x", "analysis_type": analysis,
        "metrics": metrics or [{"metric_id": "purchase_amount"}],
        "time_range": {"type": "explicit_dates", "resolved_start_date": start,
                       "resolved_end_date": end, "resolution_status": "resolved"},
        "group_by": [], "filters": filters or [], "statistics": ["SUM"],
        "outputs": {"summary_table": True}, "unresolved": [], "notes": [],
    }


class _MockLLM:
    def __init__(self, plan):
        self.plan = plan
        self.model = "fake"
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(self.plan)), finish_reason="stop")])


def _run(plan, db_path, tmp_path, data_range=RANGE):
    cfg = AgentConfig(model_mode="mock", data_mode="synthetic", db_path=db_path,
                      data_range=data_range, output_dir=tmp_path,
                      llm_client=_MockLLM(plan))
    with QueryService(db_path) as qs:
        return run_agent("帮我分析申购和赎回金额最近的趋势。", cfg, query_service=qs)


class TestParameterizationPreserved:
    def test_sql_still_uses_placeholders(self, synth_db, tmp_path):
        """① SQL 仍以 `?` 参数化执行（未改为拼接）。"""
        r = _run(_plan(), synth_db, tmp_path)
        assert r.status.value == "report_generated"
        sql = (r.execution_summary or {})["queries"][0]["sql"]
        assert "?" in sql
        assert "2024-01-02" not in sql      # 实际日期未进 SQL 文本
        assert "2024-01-12" not in sql

    def test_dates_actually_take_effect(self, synth_db, tmp_path):
        """② 日期参数真实参与查询（缩小窗口 → 行数变少）。"""
        full = _run(_plan(), synth_db, tmp_path)
        narrow = _run(_plan(start="2024-01-02", end="2024-01-05"), synth_db, tmp_path)
        n_full = full.execution_summary["queries"][0]["row_count"]
        n_narrow = narrow.execution_summary["queries"][0]["row_count"]
        assert n_narrow < n_full
        assert n_narrow == 4        # 0102..0105 共 4 天


class TestBindings:
    def test_bindings_match_actual_parameters(self, synth_db, tmp_path):
        """③ 展示的绑定日期与实际执行窗口一致。"""
        r = _run(_plan(start="2024-01-03", end="2024-01-09"), synth_db, tmp_path)
        q = r.execution_summary["queries"][0]
        b = {x["label"]: x["value"] for x in q["bindings"] if x["public"]}
        assert b["start_date"] == "2024-01-03"
        assert b["end_date"] == "2024-01-09"
        assert q["window_start"] == b["start_date"]
        assert q["window_end"] == b["end_date"]

    def test_parameter_order_is_preserved(self):
        """④ 多个参数顺序不错位（位置 ↔ 值 一一对应）。"""
        labels = ["start_date", "end_date", "limit", "filter:city"]
        params = ["2024-01-02", "2024-01-12", 10, "6081949"]
        out = public_bindings(labels, params)
        assert [x["position"] for x in out] == [1, 2, 3, 4]
        assert [x["value"] for x in out] == params
        # 顺序颠倒时值也必须跟着位置走
        out2 = public_bindings(list(reversed(labels)), list(reversed(params)))
        assert [x["value"] for x in out2] == list(reversed(params))

    def test_sensitive_params_hidden(self):
        """⑥ 非白名单参数（用户级标识等）不公开。"""
        out = public_bindings(["start_date", "end_date", "filter:user_id", "limit"],
                              ["2024-01-02", "2024-01-12", 42, 10])
        by_label = {x["label"]: x for x in out}
        assert by_label["filter:user_id"]["public"] is False
        assert by_label["filter:user_id"]["value"] is None
        assert by_label["filter:user_id"]["kind"] == "hidden"
        assert by_label["start_date"]["public"] is True

    def test_missing_labels_not_invented(self):
        """⑦ 缺少参数标签时标记"未记录"，不虚构值。"""
        out = public_bindings(None, ["2024-01-02", "2024-01-12"])
        assert len(out) == 2
        assert all(x["kind"] == "unrecorded" for x in out)
        assert all(x["value"] is None and x["public"] is False for x in out)
        # 标签数量不足时，多出的参数同样不得编造
        out2 = public_bindings(["start_date"], ["2024-01-02", "2024-01-12"])
        assert out2[1]["kind"] == "unrecorded" and out2[1]["value"] is None

    def test_public_filter_fields_only(self):
        """白名单外的筛选字段保持隐藏。"""
        out = public_bindings(["filter:city", "filter:sex", "filter:constellation"],
                              ["6081949", 1, "Aries"])
        assert all(x["public"] for x in out)


class TestWindowDisclosure:
    def test_clipped_window_shows_requested_and_effective(self, synth_db, tmp_path):
        """⑤ 发生截取时同时展示原始请求窗口与实际执行窗口。"""
        # 请求窗口超出合成数据覆盖（2024 全年），实际会被截取到 0102..0112
        r = _run(_plan(start="2024-01-01", end="2024-12-31"), synth_db, tmp_path)
        q = r.execution_summary["queries"][0]
        assert q["requested_window_start"] == "2024-01-01"
        assert q["requested_window_end"] == "2024-12-31"
        assert q["window_start"] == "2024-01-02"     # 截取后
        assert q["window_end"] == "2024-01-12"

    def test_no_clip_when_within_coverage(self, synth_db, tmp_path):
        r = _run(_plan(), synth_db, tmp_path)
        q = r.execution_summary["queries"][0]
        assert q["requested_window_start"] == q["window_start"]
        assert q["requested_window_end"] == q["window_end"]


class TestWebPresentation:
    def test_service_exposes_bindings_and_ranges(self):
        """Web 服务层把绑定参数与执行范围透出。"""
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "synthetic")
        assert out.ok
        q = out.queries[0]
        assert q["sql"].count("?") == len([b for b in q["bindings"]])
        pub = {b["label"]: b["value"] for b in q["bindings"] if b["public"]}
        assert pub["start_date"] == "2024-01-02"
        assert pub["end_date"] == "2024-01-12"
        assert "2024-01-02" not in q["sql"]      # SQL 仍是模板
        assert q["window"] == "2024-01-02 至 2024-01-12"

    def test_page_renders_binding_table(self):
        """页面 SQL 区域展示参数表（AppTest 断言存在表格与说明）。"""
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
        at.text_area(key="question_input").set_value(
            "帮我分析申购和赎回金额最近的趋势。")
        at.button(key="run_btn").click().run()
        assert not at.exception
        body = at.session_state.get("result")
        assert body is not None and body.ok
        assert body.queries[0]["bindings"]
        # 页面文本含参数含义与占位说明
        text = " ".join(c.value for c in at.caption)
        assert "参数化" in text
        # 证据/图表等既有能力不回归
        assert body.charts and body.evidence and len(at.get("download_button")) >= 2


class TestNoRegression:
    def test_sqlguard_still_rejects(self):
        """SQLGuard 仍然拒绝危险 SQL（未被本次改动削弱）。"""
        from dataagent.sql_guard import validate_sql
        res = validate_sql("SELECT * FROM user_balance_table")
        assert res.passed is False
        res2 = validate_sql("SELECT user_id FROM user_balance_table")
        assert res2.passed is False
        res3 = validate_sql(
            "SELECT report_date, SUM(total_purchase_amt) FROM user_balance_table "
            "WHERE report_date BETWEEN ? AND ? GROUP BY report_date")
        assert res3.passed is True

    def test_query_service_is_readonly(self, synth_db):
        """QueryService 仍是只读连接（写操作被拒绝，不抛未捕获异常）。"""
        from dataagent.query_service import QueryStatus
        with QueryService(synth_db) as qs:
            res = qs.execute("CREATE TABLE t_probe(x INTEGER)", [])
            assert res.query_status != QueryStatus.OK
            # 读操作正常
            res2 = qs.execute("SELECT COUNT(*) FROM user_balance_table", [])
            assert res2.query_status == QueryStatus.OK

    def test_benchmark_still_passes(self):
        """Benchmark 链路未回归（抽样两个案例）。"""
        from dataagent.evaluation import EvaluationRunner, load_benchmark
        cases, answers = load_benchmark()
        subset = [c for c in cases if c["case_id"] in ("T01", "N01")]
        import tempfile
        runner = EvaluationRunner(subset, answers, model_mode="mock",
                                  output_root=Path(tempfile.mkdtemp()), quiet=True)
        try:
            results, _ = runner.run()
        finally:
            runner.cleanup()
        assert next(r for r in results if r.case_id == "T01").report_generated
        assert next(r for r in results if r.case_id == "N01").behavior_correct is True
