"""阶段 07.4 测试：绝对日期范围解析、锚点语义与字段级修复重试。

全部使用 Fake 模型客户端与合成数据库，不调用真实 API。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataagent import db, ingestion, shibor
from dataagent.agent import AgentConfig, AgentStatus, run_agent
from dataagent.analysis_plan import (
    AnalysisPlan,
    AnalysisType,
    MetricSpec,
    StatOperation,
    TimeRangeSpec,
    TimeRangeType,
)
from dataagent.intent_parser import IntentParser, ParseStatus
from dataagent.analysis_runner import ExecutionStatus
from dataagent.analysis_runner import ExecutionStatus
from dataagent.query_builder import QueryBuildError, build_queries
from dataagent.query_service import QueryService

SYNTH_DIR = Path(__file__).parent / "fixtures" / "synthetic"
DATA_RANGE = (date(2024, 1, 2), date(2024, 1, 12))


def explicit_plan(start: str, end: str, *, question="日期测试",
                  metrics=("purchase_amount", "redeem_amount")):
    return {
        "intent": "趋势", "analysis_type": "time_trend",
        "metrics": [{"metric_id": m} for m in metrics],
        "time_range": {"type": "explicit_dates",
                       "resolved_start_date": start,
                       "resolved_end_date": end,
                       "resolution_status": "resolved"},
        "group_by": [], "filters": [], "statistics": ["SUM"],
        "outputs": {"summary_table": True}, "unresolved": [], "notes": [],
    }


class FakeParserClient:
    """按脚本顺序返回模型输出的假客户端（记录调用与模型名）。"""

    def __init__(self, script: list[str | Exception]):
        self.script = list(script)
        self.calls: list[dict] = []
        self.model = "fake-date-model"
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kw):
        self.calls.append({"model": kw.get("model"),
                           "messages": kw.get("messages") or []})
        item = self.script.pop(0) if self.script else self.script[-1]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=item))])


def parse_with(script: list[str], question: str,
               anchor_end=date(2014, 8, 31)) -> object:
    parser = IntentParser(llm_client=FakeParserClient(script),
                          anchor_end=anchor_end, data_start=date(2013, 7, 1))
    return parser.parse_question(question)


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory) -> Path:
    db_path = tmp_path_factory.mktemp("s74") / "dates.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
    assert all(r.ok for r in reports)
    shibor.create_fill_view(con)
    con.close()
    return db_path


# ---------------------------------------------------------------------------
# 1-3：绝对日期范围解析
# ---------------------------------------------------------------------------

class TestAbsoluteDateParsing:
    @pytest.mark.parametrize("question,start,end", [
        ("分析2024年1月2日至2024年1月12日的申购和赎回趋势",
         "2024-01-02", "2024-01-12"),
        ("2024-01-02到2024-01-12的申购赎回趋势", "2024-01-02", "2024-01-12"),
        ("2024/01/02至2024/01/12的申购赎回趋势", "2024-01-02", "2024-01-12"),
    ])
    def test_absolute_range_parsed(self, synth_db, question, start, end):
        """Fake 模型按提示词把绝对日期转为 ISO resolved_* 字段。"""
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(explicit_plan(start, end), ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question(question)
        assert r.status == ParseStatus.OK, r.errors
        plan = r.plan
        tr = plan.time_range
        assert tr.type == TimeRangeType.EXPLICIT_DATES
        assert tr.resolved_start_date.isoformat() == start
        assert tr.resolved_end_date.isoformat() == end
        # 6) 绝对日期不得写入 anchor
        assert tr.anchor == "max_available_data_date"
        # 10) 双指标保留
        assert plan.metric_ids() == ["purchase_amount", "redeem_amount"]

    def test_anchor_never_contains_date_range(self, synth_db):
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(explicit_plan("2024-01-02", "2024-01-12"),
                        ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert "至" not in r.plan.time_range.anchor
        assert "2024" not in r.plan.time_range.anchor


# ---------------------------------------------------------------------------
# 4-5：相对时间锚点
# ---------------------------------------------------------------------------

class TestRelativeAnchor:
    def test_relative_time_uses_data_max_anchor(self, synth_db):
        plan_json = {
            "intent": "相对趋势", "analysis_type": "time_trend",
            "metrics": [{"metric_id": "purchase_amount"}],
            "time_range": {"type": "last_n_months",
                           "relative_period": "最近三个月", "n": 3},
            "group_by": [], "filters": [], "statistics": ["SUM"],
            "outputs": {"summary_table": True}, "unresolved": [], "notes": [],
        }
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(plan_json, ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("最近三个月的申购趋势")
        tr = r.plan.time_range
        # 锚点 = 数据集实际最大日期（2014-08-31），非系统当前日期
        assert tr.resolved_end_date == date(2014, 8, 31)
        assert tr.resolved_start_date == date(2014, 6, 1)
        assert tr.anchor == "max_available_data_date"

    def test_invalid_anchor_rejected_at_schema(self, synth_db):
        """模型把日期范围写入 anchor → Schema 拒绝（不放宽白名单）。

        语义澄清（07.4.1-A）：由于存在一次修复重试，"首次非法"本身不构成最终
        状态；最终拒绝语义由 Schema 层直构断言 + 修复失败的 API_ERROR
        （test_repair_api_failure_returns_api_error）共同覆盖。
        """
        with pytest.raises(Exception, match="不支持的锚点定义"):
            TimeRangeSpec(type=TimeRangeType.EXPLICIT_DATES,
                          resolved_start_date=date(2024, 1, 2),
                          resolved_end_date=date(2024, 1, 12),
                          resolution_status="resolved",
                          anchor="2024-01-02至2024-01-12")


# ---------------------------------------------------------------------------
# 5')：字段级修复重试
# ---------------------------------------------------------------------------

class TestRepairRetry:
    def test_repair_retry_fixes_anchor_error(self, synth_db):
        """第一次把日期写入 anchor（非法）→ 字段级反馈 → 第二次修正成功。"""
        bad = explicit_plan("2024-01-02", "2024-01-12")
        bad["time_range"]["anchor"] = "2024-01-02至2024-01-12"
        good = explicit_plan("2024-01-02", "2024-01-12")
        client = FakeParserClient([
            json.dumps(bad, ensure_ascii=False),               # 第一次：非法
            json.dumps(good, ensure_ascii=False),              # 修复后
        ])
        parser = IntentParser(llm_client=client,
                              anchor_end=date(2014, 8, 31),
                              data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.OK
        assert len(client.calls) == 2                          # 恰好一次修复重试
        # 修复消息包含字段级错误与锚点说明
        repair = client.calls[1]["messages"][-1]["content"]
        assert "字段级错误" in repair and "max_available_data_date" in repair
        # 修复消息保留用户原始问题（对话第二条 user 消息）
        assert "2024年1月2日至2024年1月12日" in client.calls[1]["messages"][1]["content"]
        # 最终计划正确且 anchor 干净
        assert r.plan.time_range.resolved_start_date.isoformat() == "2024-01-02"
        assert r.plan.time_range.anchor == "max_available_data_date"

    def test_repair_exhausted_goes_to_clarification(self, synth_db):
        """情况③：原始校验失败且修复结果仍不合法 → 精确进入 CLARIFICATION。"""
        bad = explicit_plan("2024-01-02", "2024-01-12")
        bad["time_range"]["anchor"] = "2024-01-02至2024-01-12"
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(bad, ensure_ascii=False),
             json.dumps(bad, ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.CLARIFICATION      # 精确状态（非宽泛断言）
        # 字段级错误保留（含锚点原因），日期信息未丢失
        assert any("锚点" in e and "2024-01-02至2024-01-12" in e
                   for e in r.errors)

    def test_repair_api_failure_returns_api_error(self, synth_db):
        """情况②：修复调用 API 失败 → 独立 API_ERROR（不进入澄清路径），
        且保留原始字段级错误（不丢失日期信息）。"""
        bad = explicit_plan("2024-01-02", "2024-01-12")
        bad["time_range"]["anchor"] = "2024-01-02至2024-01-12"

        class FailOnSecond:
            def __init__(self):
                self.n = 0
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):
                self.n += 1
                if self.n == 1:
                    return SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(bad, ensure_ascii=False)))])
                raise ConnectionError("repair api down")

        parser = IntentParser(llm_client=FailOnSecond(),
                              anchor_end=date(2014, 8, 31),
                              data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.API_ERROR          # 精确状态
        assert any("锚点" in e for e in r.errors)          # 原始字段错误保留
        assert any("修复重试调用失败" in e for e in r.errors)
        assert any("重新提交" in e for e in r.errors)       # 允许用户重新提交

    def test_repair_success_returns_ok(self, synth_db):
        """情况①+②：首次非法 → 修复成功 → OK（不落入任何失败状态）。"""
        bad = explicit_plan("2024-01-02", "2024-01-12")
        bad["time_range"]["anchor"] = "2024-01-02至2024-01-12"
        good = explicit_plan("2024-01-02", "2024-01-12")
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(bad, ensure_ascii=False),
             json.dumps(good, ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.OK
        assert r.plan.time_range.anchor == "max_available_data_date"
        assert r.plan.time_range.resolved_start_date.isoformat() == "2024-01-02"


# ---------------------------------------------------------------------------
# 7-9：边界
# ---------------------------------------------------------------------------

class TestDateBoundaries:
    def test_start_after_end_rejected_at_schema(self):
        with pytest.raises(Exception, match="不得晚于"):
            TimeRangeSpec(type=TimeRangeType.EXPLICIT_DATES,
                          resolved_start_date=date(2024, 1, 12),
                          resolved_end_date=date(2024, 1, 2),
                          resolution_status="resolved")

    def test_missing_dates_pending_then_clarification(self, synth_db):
        parser = IntentParser(llm_client=FakeParserClient([json.dumps({
            "intent": "趋势", "analysis_type": "time_trend",
            "metrics": [{"metric_id": "purchase_amount"}],
            "time_range": {"type": "explicit_dates"},       # 无日期
            "group_by": [], "filters": [], "statistics": ["SUM"],
            "outputs": {"summary_table": True}, "unresolved": ["缺少日期"],
            "notes": [],
        }, ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("分析趋势")
        # pending 无日期 → 解析为澄清路径
        assert r.status in (ParseStatus.CLARIFICATION, ParseStatus.INVALID)

    def test_out_of_range_clipped_by_query_builder(self, synth_db):
        """绝对范围超出数据覆盖 → 计划仍可生成，执行层截取并记录有效范围。"""
        plan = AnalysisPlan(
            question="2014 年趋势", intent="趋势",
            analysis_type=AnalysisType.TIME_TREND,
            metrics=[MetricSpec(metric_id="purchase_amount")],
            time_range=TimeRangeSpec(
                type=TimeRangeType.EXPLICIT_DATES,
                resolved_start_date=date(2024, 1, 1),
                resolved_end_date=date(2024, 3, 31),    # 起始早于 2024-01-02
                resolution_status="resolved"),
            group_by=[], statistics=[StatOperation.SUM])
        bundle = build_queries(plan, data_range=DATA_RANGE)
        assert any("截取" in n for n in bundle.notes)
        q = bundle.queries[0]
        assert q.params == ["2024-01-02", "2024-01-12"]   # 截取后的有效窗口

        with QueryService(synth_db) as qs:
            from dataagent.analysis_runner import run_analysis
            execution = run_analysis(plan, qs, data_range=DATA_RANGE)
            assert execution.status == ExecutionStatus.OK
            assert execution.queries[0].window_end == "2024-01-12"


# ===========================================================================
# 任务 07.4.2-A：API 调用失败 vs 修复输出无效 的精确分类
# ===========================================================================

def _bad_anchor_plan():
    bad = explicit_plan("2024-01-02", "2024-01-12")
    bad["time_range"]["anchor"] = "2024-01-02至2024-01-12"
    return bad


class TestRepairOutcomeClassification:
    """严格区分：API 调用阶段异常（api_error）与修复输出无效（澄清/无效路径）。"""

    def test_api_network_exception_is_api_error(self):
        """① 修复调用抛出网络异常 → 精确 api_error。"""
        class FailOnSecond:
            def __init__(self):
                self.n = 0
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):
                self.n += 1
                if self.n == 1:
                    return SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(_bad_anchor_plan(),
                                               ensure_ascii=False)))])
                raise ConnectionError("repair api down")

        parser = IntentParser(llm_client=FailOnSecond(),
                              anchor_end=date(2014, 8, 31),
                              data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.API_ERROR          # 精确（非宽泛）
        assert any("锚点" in e for e in r.errors)
        assert any("修复重试调用失败: ConnectionError" in e for e in r.errors)

    def test_invalid_json_repair_is_not_api_error(self):
        """② 修复响应为非法 JSON → 不得 api_error，进入无效修复结果路径。"""
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(_bad_anchor_plan(), ensure_ascii=False),
             "这不是 JSON"]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status != ParseStatus.API_ERROR          # 关键：不伪装为 API 故障
        assert r.status == ParseStatus.CLARIFICATION      # 精确状态
        assert any("合法 JSON" in e for e in r.errors)
        assert any("锚点" in e and "2024-01-02至2024-01-12" in e for e in r.errors)

    def test_missing_structure_repair_is_not_api_error(self):
        """③ 修复响应结构缺失（JSON 数组 / 空内容）→ 不得 api_error。"""
        for payload in ("[]", ""):
            parser = IntentParser(llm_client=FakeParserClient(
                [json.dumps(_bad_anchor_plan(), ensure_ascii=False),
                 payload]),
                anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
            r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
            assert r.status != ParseStatus.API_ERROR
            assert r.status == ParseStatus.CLARIFICATION
            assert any(("结构缺失" in e or "缺少有效内容" in e)
                       for e in r.errors)

    def test_schema_invalid_repair_has_precise_errors(self):
        """④ 修复响应 Schema 仍不通过 → 精确最终状态与错误信息。"""
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(_bad_anchor_plan(), ensure_ascii=False),
             json.dumps(_bad_anchor_plan(), ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.CLARIFICATION
        assert any("不支持的锚点定义" in e for e in r.errors)
        assert any("2024-01-02至2024-01-12" in e for e in r.errors)

    def test_repair_success_keeps_dates_and_anchor(self):
        """⑤ 修复成功 → ok，日期与 anchor 字段正确。"""
        good = explicit_plan("2024-01-02", "2024-01-12")
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(_bad_anchor_plan(), ensure_ascii=False),
             json.dumps(good, ensure_ascii=False)]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.OK
        tr = r.plan.time_range
        assert tr.resolved_start_date.isoformat() == "2024-01-02"
        assert tr.resolved_end_date.isoformat() == "2024-01-12"
        assert tr.anchor == "max_available_data_date"
        # 用户原始问题保留
        assert r.plan.question == "分析2024年1月2日至2024年1月12日的趋势"

    def test_original_context_preserved_on_failure(self):
        """⑥ 修复失败时，原始问题与字段级日期错误仍然保留。"""
        parser = IntentParser(llm_client=FakeParserClient(
            [json.dumps(_bad_anchor_plan(), ensure_ascii=False),
             "坏的 JSON"]),
            anchor_end=date(2014, 8, 31), data_start=date(2013, 7, 1))
        question = "分析2024年1月2日至2024年1月12日的申购和赎回趋势"
        r = parser.parse_question(question)
        # 字段级错误（含日期字符串）保留
        assert any("2024-01-02至2024-01-12" in e for e in r.errors)
        assert r.status == ParseStatus.CLARIFICATION
        # 澄清对象仍存在（上下文未丢弃）：问题列表非空
        if r.clarification is not None:
            assert r.clarification.needed
            assert r.clarification.questions

    def test_repair_retried_at_most_once(self):
        """⑦ 修复重试最多一次（总调用次数 == 2），不得无限重试。"""
        good = explicit_plan("2024-01-02", "2024-01-12")
        client = FakeParserClient([
            json.dumps(_bad_anchor_plan(), ensure_ascii=False),
            json.dumps(good, ensure_ascii=False),
            json.dumps(good, ensure_ascii=False),      # 不应被使用
        ])
        parser = IntentParser(llm_client=client,
                              anchor_end=date(2014, 8, 31),
                              data_start=date(2013, 7, 1))
        r = parser.parse_question("分析2024年1月2日至2024年1月12日的趋势")
        assert r.status == ParseStatus.OK
        assert len(client.calls) == 2
        assert len(client.script) == 1                  # 第三个响应未被消费
