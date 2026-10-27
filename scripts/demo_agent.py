#!/usr/bin/env python
"""演示：一个自然语言问题 → 结构化计划 → 安全 SQL → 统计 → 图表 → 中文报告。

四种组合（**Mock 只能配合成数据；真实数据库必须配 `--real`**）：
1. **默认（合成数据 + Mock 解析）**：不需要网络与 API Key；使用临时合成数据库
   （演示结束自动清理），SQL 与查询结果都来自真实执行的合成库。
2. `--real`：使用真实大模型解析（需 `DATAAGENT_LLM_API_KEY`）；未配置时明确报错。
3. `--real` + 数据源：`--project-db`（项目固定路径）或 `--db PATH`（指定 DuckDB 文件），
   都是**只读**、不重建。
4. ⚠️ `--db PATH` / `--project-db` **单独使用（不配 `--real`）会被拒绝**并退出码 2：
   Mock 的内置计划固定为 2024 年合成数据，与真实库时间范围不匹配，
   系统不会悄悄改写计划日期。

输出：Markdown 报告 + JSON 证据文件 + PNG 图表（默认 `artifacts/generated/`）。

用法：
    python scripts/demo_agent.py                      # 合成数据 + Mock（推荐先跑；8 个内置场景）
    python scripts/demo_agent.py --question "各城市申购金额排名。"   # Mock 单问（须精确命中内置场景）
    python scripts/demo_agent.py --real --question "分析 2014 年 8 月各城市的净资金流入"  # 真实模型 + 合成库
    python scripts/demo_agent.py --real --project-db --question "分析 2014 年 8 月的申购和赎回趋势"
    python scripts/demo_agent.py --real --db data/processed/dataagent_demo.duckdb --question "..."
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent import config, db, ingestion, shibor, visualization  # noqa: E402
from dataagent.agent import AgentConfig, AgentStatus, run_agent  # noqa: E402
from dataagent.query_service import QueryService  # noqa: E402

SYNTH_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "synthetic"
SYNTH_RANGE = (date(2024, 1, 2), date(2024, 1, 12))

# ---- 演示问题与对应的 Mock 解析结果（结构同真实模型输出）----
DEMOS = [
    # 说明：本清单是 Mock 演示场景的**唯一数据源**（Web 页面 / 命令行演示 / 文档均由此派生）。
    # kind="success" 为可完整运行的分析；kind="boundary" 为**预期**失败/澄清的边界演示。
    {
        "case_id": "T03",
        "kind": "success",
        "label": "多指标趋势（申购 + 赎回）",
        "expected": "report_generated",
        "expected_note": "生成完整报告、趋势图与证据（推荐首次体验）",
        "question": "帮我分析申购和赎回金额最近的趋势。",
        "plan":         {
            "intent": "benchmark",
            "analysis_type": "time_trend",
            "metrics": [
                {
                    "metric_id": "purchase_amount"
                },
                {
                    "metric_id": "redeem_amount"
                }
            ],
            "time_range": {
                "type": "explicit_dates",
                "resolved_start_date": "2024-01-02",
                "resolved_end_date": "2024-01-12",
                "resolution_status": "resolved"
            },
            "group_by": [],
            "filters": [],
            "statistics": [
                "SUM"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [],
            "notes": []
        },
    },
    {
        "case_id": "W01",
        "kind": "success",
        "label": "城市净资金流入窗口对比",
        "expected": "report_generated",
        "expected_note": "生成两窗口对比报告与对比图",
        "question": "比较最近几天和之前几天各城市的净资金流入。",
        "plan":         {
            "intent": "benchmark",
            "analysis_type": "window_comparison",
            "metrics": [
                {
                    "metric_id": "net_inflow"
                }
            ],
            "time_range": {
                "type": "window_comparison",
                "resolved_start_date": "2024-01-08",
                "resolved_end_date": "2024-01-12",
                "comparison_start_date": "2024-01-02",
                "comparison_end_date": "2024-01-07",
                "resolution_status": "resolved"
            },
            "group_by": [
                "city"
            ],
            "filters": [],
            "statistics": [
                "SUM",
                "WINDOW_DIFF"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [],
            "notes": []
        },
    },
    {
        "case_id": "R01",
        "kind": "success",
        "label": "各城市申购金额排名",
        "expected": "report_generated",
        "expected_note": "生成排名结果（含占比说明）",
        "question": "各城市申购金额排名。",
        "plan":         {
            "intent": "benchmark",
            "analysis_type": "group_ranking",
            "metrics": [
                {
                    "metric_id": "purchase_amount"
                }
            ],
            "time_range": {
                "type": "explicit_dates",
                "resolved_start_date": "2024-01-02",
                "resolved_end_date": "2024-01-12",
                "resolution_status": "resolved"
            },
            "group_by": [
                "city"
            ],
            "filters": [],
            "statistics": [
                "SUM"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [],
            "notes": [],
            "top_n": 10
        },
    },
    {
        "case_id": "A01",
        "kind": "success",
        "label": "申购金额异常日期识别（MAD）",
        "expected": "report_generated",
        "expected_note": "生成稳健异常识别结果（样本不足时会明确提示）",
        "question": "申购金额有没有异常日期？",
        "plan":         {
            "intent": "benchmark",
            "analysis_type": "anomaly",
            "metrics": [
                {
                    "metric_id": "purchase_amount"
                }
            ],
            "time_range": {
                "type": "explicit_dates",
                "resolved_start_date": "2024-01-02",
                "resolved_end_date": "2024-01-12",
                "resolution_status": "resolved"
            },
            "group_by": [],
            "filters": [],
            "statistics": [
                "SUM"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [],
            "notes": []
        },
    },
    {
        "case_id": "S01",
        "kind": "success",
        "label": "观察期申购金额汇总",
        "expected": "report_generated",
        "expected_note": "生成描述性汇总报告",
        "question": "观察期内申购金额的总体情况。",
        "plan":         {
            "intent": "benchmark",
            "analysis_type": "descriptive",
            "metrics": [
                {
                    "metric_id": "purchase_amount"
                }
            ],
            "time_range": {
                "type": "explicit_dates",
                "resolved_start_date": "2024-01-02",
                "resolved_end_date": "2024-01-12",
                "resolution_status": "resolved"
            },
            "group_by": [],
            "filters": [],
            "statistics": [
                "SUM"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [],
            "notes": []
        },
    },
    {
        "case_id": "B1",
        "kind": "boundary",
        "label": "不支持的指标（销售额）",
        "expected": "unsupported_metric",
        "expected_note": "预期被拒绝：销售额不在受支持指标目录内",
        "question": "帮我分析销售额的变化。",
        "plan":         {
            "intent": "销售额分析",
            "analysis_type": "time_trend",
            "metrics": [
                {
                    "metric_id": "purchase_amount"
                }
            ],
            "time_range": {
                "type": "explicit_dates",
                "resolved_start_date": "2024-01-02",
                "resolved_end_date": "2024-01-12",
                "resolution_status": "resolved"
            },
            "group_by": [],
            "filters": [],
            "statistics": [
                "SUM"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [
                "没有经确认的商品销售额指标"
            ],
            "notes": []
        },
    },
    {
        "case_id": "B2",
        "kind": "boundary",
        "label": "需要澄清的问题",
        "expected": "needs_clarification",
        "expected_note": "预期需要补充信息：问题缺少指标与时间范围",
        "question": "分析一下。",
        "plan":         {
            "intent": "分析一下",
            "analysis_type": "time_trend",
            "metrics": [
                {
                    "metric_id": "purchase_amount"
                }
            ],
            "time_range": {
                "type": "last_n_months"
            },
            "group_by": [],
            "filters": [],
            "statistics": [
                "SUM"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [],
            "notes": []
        },
    },
    {
        "case_id": "B3",
        "kind": "boundary",
        "label": "数据不足（单日趋势）",
        "expected": "insufficient_data",
        "expected_note": "预期数据不足：单日无法计算日环比",
        "question": "分析 2024-01-02 当天的申购金额趋势。",
        "plan":         {
            "intent": "单日趋势",
            "analysis_type": "time_trend",
            "metrics": [
                {
                    "metric_id": "purchase_amount"
                }
            ],
            "time_range": {
                "type": "explicit_dates",
                "resolved_start_date": "2024-01-02",
                "resolved_end_date": "2024-01-02",
                "resolution_status": "resolved"
            },
            "group_by": [],
            "filters": [],
            "statistics": [
                "SUM"
            ],
            "outputs": {
                "summary_table": True
            },
            "unresolved": [],
            "notes": []
        },
    },
]


class MockCompletions:
    def __init__(self, idx: int):
        self.idx = idx

    def create(self, **kwargs):
        payload = json.dumps(DEMOS[self.idx]["plan"], ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=payload))])


_PUNCT = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":",
    "（": "(", "）": ")", "　": " ", "、": ",", "～": "~",
})


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _display(path) -> str:
    """展示用路径：项目内相对路径，项目外只显示文件名（不暴露本机绝对路径）。"""
    p = Path(path)
    try:
        return p.resolve().relative_to(_PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return p.name


def _normalize(question: str) -> str:
    """规范化：去全部空白、统一全角标点、去句末标点（仅容错，不做子串匹配）。"""
    q = (question or "").translate(_PUNCT)
    q = "".join(q.split())
    return q.rstrip(".!?;,")


def _match_demo(question: str) -> dict | None:
    """在 DEMOS 中精确匹配（规范化后全等）；未命中返回 None。"""
    norm = _normalize(question)
    if not norm:
        return None
    for idx, item in enumerate(DEMOS):
        if _normalize(item["question"]) == norm:
            return {"index": idx, "label": item["label"],
                    "expected": item["expected"], "kind": item["kind"]}
    return None


def mock_client(idx: int):
    return SimpleNamespace(chat=SimpleNamespace(completions=MockCompletions(idx)))


def prepare_synthetic_db(tmpdir: Path) -> Path:
    db_path = tmpdir / "demo_agent.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
    assert all(r.ok for r in reports), [r.error for r in reports if r.error]
    shibor.create_fill_view(con)
    con.close()
    return db_path


def _print_result(result) -> None:
    print(f"状态：{result.status.value}")
    print(f"模型模式：{result.model_mode} | 数据模式：{result.data_mode}")
    if result.plan:
        metrics = [m.get("metric_id") for m in result.plan.get("metrics", [])]
        print(f"计划：{result.plan.get('analysis_type')} | 指标 {metrics}")
    for q in (result.clarification_questions or [])[:3]:
        print(f"澄清：{q}")
    if result.error:
        print(f"错误类别：{result.error_code}")
        print(f"错误信息：{result.error[:200]}")
    if result.execution_summary:
        qs_rows = result.execution_summary.get("queries", [])
        print(f"查询：{len(qs_rows)} 条 | "
              f"{result.execution_summary.get('total_ms')} ms")
    if result.analysis_summary:
        print(f"统计：{result.analysis_summary.get('status')} / "
              f"{result.analysis_summary.get('method')} / "
              f"结论 {result.analysis_summary.get('conclusion_count')} 条")
    if result.report_path:
        print(f"报告：{result.report_path}")
        print(f"证据：{result.evidence_path}")
        print(f"叙述来源：{result.narrative_source}")
    for chart in result.chart_paths:
        print(f"图表：{chart}")
    for w in (result.warnings or [])[:3]:
        print(f"警告：{w}")


def _print_generated(generated: list[str]) -> None:
    print()
    print("=" * 80)
    print("生成的文件：")
    for item in generated:
        exists = "OK" if Path(item).exists() else "MISSING"
        print(f"  [{exists}] {item}")
    print()
    print("说明：报告中已标注模型模式与数据模式；"
          "Mock/合成数据不会被标记为真实业务结果。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段七：Agent 全流程演示")
    ap.add_argument("--real", action="store_true", help="使用真实大模型解析（需 API Key）")
    ap.add_argument("--project-db", action="store_true", help="使用项目真实数据库（只读）")
    ap.add_argument("--db", type=Path, default=None, help="指定数据库路径")
    ap.add_argument("--out", type=Path, default=None, help="输出目录")
    ap.add_argument("--llm-narrative", action="store_true",
                    help="启用 LLM 叙述层（需真实模型客户端）")
    ap.add_argument("--question", type=str, default=None,
                    help="只运行一个自定义问题（真实模型验证入口）；"
                         "不指定则运行全部 Mock 演示场景")
    args = ap.parse_args(argv)

    out_dir = args.out or visualization.DEFAULT_OUTPUT_DIR
    tmpdir: Path | None = None
    data_range = None

    if args.db is not None:
        db_path, data_mode = args.db, "project_db"
    elif args.project_db:
        db_path, data_mode = Path(config.DB_PATH), "project_db"
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix="dataagent_demo7_"))
        db_path = prepare_synthetic_db(tmpdir)
        data_mode, data_range = "synthetic", SYNTH_RANGE
    if not db_path.exists():
        print(f"[错误] 数据库不存在: {db_path}")
        return 1

    model_mode = "real" if args.real else "mock"
    print("=" * 80)
    # P1-1（阶段 13.2 加固）：按**最终解析出的 data_mode** 判断，而非某个开关。
    # `--db PATH` 与 `--project-db` 都会进入 project_db 模式，两者都必须被拒绝。
    if not args.real and data_mode != "synthetic":
        source = "--db <路径>" if args.db is not None else "--project-db"
        print(f"[配置不匹配] {source} 搭配 Mock 模式不可用：")
        print("             Mock 内置示例的计划时间为 2024-01-02 至 2024-01-12，")
        print("             而你指定的是非合成数据源（真实业务库覆盖 2013-07 至 2014-08）。")
        print("             系统不会自动改写计划日期。请改为：")
        print("               ① 去掉 --db/--project-db，使用内置合成数据；")
        print("               ② 加 --real 用真实模型分析你的数据库（会产生 API 费用）。")
        return 2

    print(f"模型模式：{model_mode}"
          + ("（真实大模型，需 DATAAGENT_LLM_API_KEY）" if args.real
             else "（Mock 解析，不需要 API Key）"))
    if args.real:
        print("费用提示：真实模式每个问题至少 1 次模型调用；若模型输出不合规，"
              "系统会做**一次** Schema 修复重试（可能额外产生 1 次调用与费用）；"
              "启用 --llm-narrative 还会增加 1 次叙述调用。")
    print(f"数据模式：{data_mode}"
          + ("（临时合成库，结束自动清理）" if data_mode == "synthetic"
             else "（项目数据库，只读）"))
    print(f"数据库：{_display(db_path)}")
    print(f"输出目录：{_display(out_dir)}")
    print("=" * 80)

    generated: list[str] = []
    try:
        if args.question:
            print()
            print("── 自定义问题 " + "─" * 50)
            print(f"问题：{args.question}")
            if args.real:
                cfg = AgentConfig(model_mode="real", data_mode=data_mode,
                                  db_path=db_path, data_range=data_range,
                                  output_dir=out_dir,
                                  use_llm_narrative=args.llm_narrative)
            else:
                print("[提示] 未指定 --real：使用 Mock 解析器（不调用真实 API）。")
                # P0（阶段 13.2）：Mock **不再**对任意问题套用第一个预设计划。
                # 只允许与内置演示场景规范化后**完全一致**的问题；其余明确拒绝。
                detail = _match_demo(args.question)
                if detail is None:
                    print()
                    print("[Mock 限制] 未匹配到内置演示场景（不是后端故障）。")
                    print("           Mock 使用事先准备的分析计划，不能自由理解任意中文问题；")
                    print("           请使用下列场景之一，或加 --real 自由提问（会产生 API 费用）：")
                    for item in DEMOS:
                        tag = "正常分析" if item["kind"] == "success" else "边界演示"
                        print(f"             · 【{tag}】{item['question']}"
                              f"（预期 {item['expected']}）")
                    return 2
                idx = detail["index"]
                print(f"[Mock 场景] {detail['label']}"
                      f"（预期 {detail['expected']}）")
                cfg = AgentConfig(model_mode="mock", data_mode=data_mode,
                                  db_path=db_path, data_range=data_range,
                                  output_dir=out_dir,
                                  llm_client=mock_client(idx),
                                  use_llm_narrative=False)
            with QueryService(db_path) as qs:
                one = run_agent(args.question, cfg, query_service=qs)
            _print_result(one)
            if one.report_path:
                generated.extend([one.report_path, one.evidence_path])
            generated.extend(one.chart_paths)
            _print_generated(generated)
            return 0

        for idx, demo in enumerate(DEMOS):
            if args.real:
                cfg = AgentConfig(model_mode="real", data_mode=data_mode,
                                  db_path=db_path, data_range=data_range,
                                  output_dir=out_dir,
                                  use_llm_narrative=args.llm_narrative)
            else:
                cfg = AgentConfig(model_mode="mock", data_mode=data_mode,
                                  db_path=db_path, data_range=data_range,
                                  output_dir=out_dir, llm_client=mock_client(idx),
                                  use_llm_narrative=False)
            print(f"\n── 场景 {idx + 1}：{demo['label']} ─" + "─" * 40)
            print(f"问题：{demo['question']}")
            with QueryService(db_path) as qs:
                result = run_agent(demo["question"], cfg, query_service=qs)
            _print_result(result)
            if result.report_path:
                generated.extend([result.report_path, result.evidence_path])
            generated.extend(result.chart_paths)
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("\n[清理] 已删除临时合成库")

    _print_generated(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
