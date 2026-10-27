r"""DataAgent 本地 Web 界面（Streamlit，单用户演示）。

启动：
    .\.venv\Scripts\python.exe -m streamlit run app.py

设计约束（阶段 12）：
- 只调用 `dataagent` 的受控接口（AgentConfig / run_agent / QueryService），不复制业务逻辑；
- 数据库**始终只读**，且只允许"合成库 / 项目固定路径"两种选择，不接受任意路径输入；
- 页面上的 SQL 仅用于查看（参数化模板，不含参数值），不可编辑执行；
- 默认 Mock + Synthetic：无 API Key、无本地业务库也能完整体验；
- 刷新页面不会重复调用模型（结果缓存在 session_state，仅在点击按钮时运行）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import dataagent  # noqa: E402
from web import service  # noqa: E402

PAGE_TITLE = "DataAgent · 本地数据分析演示"


def _sidebar() -> dict:
    with st.sidebar:
        st.markdown("### 运行配置")
        model_mode = st.radio(
            "模型模式", service.MODEL_MODES, index=0, key="model_mode",
            format_func=lambda m: "Mock（默认，无需 Key）" if m == "mock" else "Real（真实大模型）",
            help="Mock 使用内置演示解析器，不发起任何外部调用。")
        data_mode = st.radio(
            "数据模式", service.DATA_MODES, index=0, key="data_mode",
            format_func=lambda d: "合成测试数据（默认）" if d == "synthetic"
            else "项目自带演示数据库（只读）",
            help="Synthetic=人工构造的测试样本（非真实业务数据）；"
                 "Project DB=项目自带经授权的非真实业务演示数据（脱敏改造，离线构建）。")

        st.markdown("#### 当前配置")
        st.caption(service.model_label(model_mode))
        st.caption(f"数据范围：{service.data_coverage(model_mode, data_mode)}")

        if model_mode == "real":
            if service.is_real_mode_configured():
                st.warning(
                    "真实模式会产生 API 费用（按服务商计费）。每次点击「运行分析」才发起调用；"
                    "正常情况下为 1 次解析调用，若模型输出不合规则会追加**一次** Schema 修复重试"
                    "（可能额外计费）。")
            else:
                st.error("未检测到 `DATAAGENT_LLM_API_KEY`。"
                         "真实模式需要在启动前设置该环境变量。")

        with st.expander("功能说明"):
            st.markdown(
                "- 自然语言 → 结构化分析计划（受指标白名单约束）\n"
                "- 确定性 SQL 生成 + AST 安全校验 + 只读执行\n"
                "- Python 统计 + 图表 + 证据化中文报告\n"
                "- LLM 仅负责理解与叙述，**不执行任意 SQL**")
        with st.expander("安全与隐私提示"):
            st.markdown(
                "- 数据库**只读**，页面不能修改任何数据\n"
                "- SQL 仅可查看，不能编辑后执行\n"
                "- 本页面为**本地单用户演示**，未实现鉴权、会话隔离与正式隐私保护\n"
                "- 未实现 k-匿名小样本抑制；细粒度统计存在披露风险\n"
                "- 不要把 API Key 写入代码或提交到 Git")
        st.caption(f"版本 {service.package_version()} · MIT License")
    return {"model_mode": model_mode, "data_mode": data_mode}


def _render_result(out, is_stale: bool = False) -> None:
    st.subheader("分析结果（历史）" if is_stale else "分析结果")
    if out.ok:
        st.success(f"状态：{out.status_text}")
    else:
        st.error(f"状态：{out.status_text}")
        if out.error:
            st.caption(f"错误类别：{out.error_code or '—'}｜详情：{out.error[:300]}")
        st.info("系统不会把失败伪装成成功报告：请根据上面的原因调整问题或配置。")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("分析类型", str(out.plan.get("analysis_type") or "—"))
    c2.metric("指标", "、".join(out.metrics) or "—")
    c3.metric("查询条数", len(out.queries))

    if out.warnings:
        with st.expander(f"数据质量警告（{len(out.warnings)} 条）", expanded=False):
            st.caption("这些提示说明数据覆盖、缺失值、口径调整或窗口截取的实际影响；"
                       "请结合报告第 5–6 节的执行窗口与覆盖范围判断结论适用范围。")
            for w in out.warnings:
                st.write(f"- {w}")

    st.markdown("#### 核心结论")
    for i, text in enumerate(out.conclusions[:12], 1):
        st.markdown(f"{i}. {text}")
    if not out.conclusions:
        st.info("本次分析没有生成结论（可能数据不足或方法不适用）。")

    if out.charts:
        st.markdown("#### 图表")
        for p in out.charts:
            f = ROOT / p
            if f.is_file():
                st.image(str(f), caption=p, width="stretch")
            else:
                st.caption(f"图表文件缺失：{p}")

    st.markdown("#### SQL 查询（仅供查看，不可编辑执行）")
    st.caption("SQL 始终以参数化模板执行（`?` 为占位符，值由后端绑定，"
               "不拼接用户输入）。下方同时给出**本次实际绑定的参数**与**执行范围**。")
    for q in out.queries:
        st.caption(f"{q['name']}｜状态 {q['status']}｜{q['row_count']} 行｜"
                   f"实际窗口 {q['window']}")
        if q.get("window_clipped") and q.get("requested_window"):
            st.warning(f"日期窗口已截取：原始请求窗口 **{q['requested_window']}** → "
                       f"实际执行窗口 **{q['window']}**（受数据覆盖范围限制）")
        if q["sql"]:
            st.code(q["sql"], language="sql")
        bindings = q.get("bindings") or []
        if bindings:
            st.markdown("**本次实际绑定的参数**")
            rows = []
            for b in bindings:
                if b.get("public"):
                    rows.append({"位置": f"?{b['position']}", "含义": b.get("label"),
                                 "类型": b.get("kind"), "绑定值": b.get("value")})
                else:
                    kind = b.get("kind")
                    shown = "未记录" if kind == "unrecorded" else "不可展示（不在白名单）"
                    rows.append({"位置": f"?{b['position']}",
                                 "含义": b.get("label") or "—",
                                 "类型": kind, "绑定值": shown})
            st.dataframe(rows, width="stretch", hide_index=True)
        else:
            st.caption("本次查询没有绑定参数。")

    rows = service.evidence_rows(out.evidence)
    st.markdown("#### 证据（可追溯到查询与口径）")
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.caption("本次无证据条目。")

    st.markdown("#### 完整报告（Markdown）")
    if out.report_text:
        with st.expander("展开查看报告全文", expanded=False):
            st.markdown(out.report_text)
        st.download_button("下载 Markdown 报告", out.report_text,
                           file_name=out.report_name or "report.md",
                           mime="text/markdown", key="dl_md")
        if out.evidence:
            import json
            st.download_button("下载 JSON 证据",
                               json.dumps(out.evidence, ensure_ascii=False, indent=2),
                               file_name=out.evidence_name or "evidence.json",
                               mime="application/json", key="dl_json")
    else:
        st.caption("本次未生成报告文件。")


def _view_analysis(cfg: dict) -> None:
    st.title(PAGE_TITLE)
    st.caption("用自然语言提问，得到带证据的中文分析报告。默认使用合成数据与 Mock 解析器，"
               "无需 API Key、无需本地业务数据库。")

    # A1：首次打开即理解 Mock 的含义（不必读 README）
    if cfg["model_mode"] == "mock":
        st.info(
            "**当前为 Mock 演示模式（不调用大模型 API）。**\n\n"
            "系统使用**事先准备好的分析计划**运行真实的 SQL、统计与报告生成流程——"
            "也就是说，图表、数字与证据都是真算出来的，但**问题理解是固定的**："
            "它不能自由理解任意中文问题。\n\n"
            "想自由提问？请在左侧切换到 **Real** 模式并配置 "
            "`DATAAGENT_LLM_API_KEY`（会产生 API 费用）。")

    catalog = service.mock_catalog()
    examples = [c["question"] for c in catalog]

    # 显示用标签（值仍是问题文本，便于 session_state 同步）
    label_map = {}
    for c in catalog:
        tag = "正常分析" if c["kind"] == "success" else "边界演示"
        label_map[c["question"]] = f"【{tag}】{c['label']} — 预期 {c['expected']}"

    def _sync_example() -> None:
        picked = st.session_state.get("example_pick") or "（自定义）"
        if picked == "（自定义）":
            st.session_state["question_input"] = ""
            return
        detail = service.mock_scenario_detail(picked)
        st.session_state["question_input"] = detail["question"] if detail else picked

    # A2：默认推荐一个可成功的正常分析场景（首次体验直接点"运行分析"即可看到报告）
    default_index = 1 if (cfg["model_mode"] == "mock" and examples) else 0
    st.selectbox(
        "示例场景（Mock 模式仅支持以下内置场景）",
        ["（自定义）"] + examples, index=default_index, key="example_pick",
        on_change=_sync_example,
        format_func=lambda q: q if q == "（自定义）" else label_map.get(q, q),
        help="【正常分析】可完整生成报告与图表；【边界演示】是**预期**的拒绝/澄清，"
             "用于展示系统的失败关闭行为。")

    picked_detail = service.mock_scenario_detail(
        st.session_state.get("question_input") or "")
    if picked_detail:
        if picked_detail["kind"] == "success":
            st.caption(f"✅ 正常分析 · 预期结果：`{picked_detail['expected']}`"
                       f" — {picked_detail['expected_note']}")
        else:
            st.caption(f"⚠️ 边界演示 · **预期结果：`{picked_detail['expected']}`**"
                       f" — {picked_detail['expected_note']}"
                       "（这是系统的保护行为，不是故障）")
    if "question_input" not in st.session_state:
        st.session_state["question_input"] = (examples[0]
                                              if (cfg["model_mode"] == "mock" and examples)
                                              else "")
    question = st.text_area("你的分析问题", height=90, key="question_input",
                            placeholder="例如：帮我分析申购和赎回金额最近的趋势。")

    mismatch = (cfg["model_mode"] == "mock" and cfg["data_mode"] == "project_db")
    if mismatch:
        st.error(
            "**配置不匹配：Mock + 项目数据库不可组合。**\n\n"
            "Mock 的内置示例使用 2024 年**合成数据**的分析计划；真实业务库覆盖 2013-07 至 2014-08。"
            "系统**不会**自动把示例日期改成你的库日期（那会产生看似成功的错误结果）。\n\n"
            "请任选其一：① 数据模式切回 **合成测试数据**；② 模型模式切到 **Real** 分析真实库。")

    col_a, col_b = st.columns([1, 3])
    run_clicked = col_a.button("运行分析", type="primary", key="run_btn",
                              disabled=mismatch)
    st.caption("提示：可手动编辑上方问题；Mock 模式只会识别内置演示场景。")
    with col_b:
        if cfg["model_mode"] == "real":
            st.caption("⚠️ 真实模式每次运行会产生一次模型调用（可能产生费用）。")

    if cfg["model_mode"] == "real" and not service.is_real_mode_configured():
        st.error("真实模式未配置 API Key：请设置 `DATAAGENT_LLM_API_KEY` 后重启页面，"
                 "或切换为 Mock 模式。")
        return

    if run_clicked:
        if mismatch:
            st.error("请先解决上面的配置不匹配问题（Mock 只用于合成数据）。")
            return
        if not question.strip():
            st.warning("请先输入一个分析问题。")
            return
        # 只在用户点击时运行一次（防重复扣费/重复调用）
        with st.status("正在运行分析…", expanded=True) as status:
            st.write("1/3 解析问题 → 生成受约束的分析计划")
            st.write("2/3 生成 SQL → 安全校验 → 只读执行")
            st.write("3/3 统计计算 → 图表 → 证据化报告")
            out = service.run_question(question, cfg["model_mode"], cfg["data_mode"])
            status.update(label="运行结束", state="complete")
        st.session_state["result"] = out
        st.session_state["result_sig"] = (question.strip(), cfg["model_mode"],
                                          cfg["data_mode"])

    out = st.session_state.get("result")
    if out is None:
        st.info("输入问题后点击「运行分析」。首次体验建议直接用上面的示例问题。")
        return
    sig = st.session_state.get("result_sig")
    current = (question.strip(), cfg["model_mode"], cfg["data_mode"])
    stale = bool(sig) and sig != current
    if stale:
        st.warning(
            "下方是**上一次运行的历史结果**（问题或配置已变化）。"
            f"历史问题：`{sig[0][:40]}`（模型 {sig[1]} / 数据 {sig[2]}）。"
            "点击「运行分析」以获取当前输入的结果——页面不会自动重跑，也不会产生额外调用。")
    _render_result(out, is_stale=stale)


def _view_benchmark() -> None:
    data = service.load_benchmark()
    mode = ((data or {}).get("summary") or {}).get("model_mode")
    title = ("Benchmark · 离线确定性执行链路" if mode == "mock"
             else "Benchmark · 真实 LLM 评测" if mode == "real" else "Benchmark 质量指标")
    st.title(title)
    data = service.load_benchmark()
    if not data:
        st.warning("尚未生成离线评测结果。请先运行：")
        st.code("python scripts/evaluate_agent.py", language="powershell")
        return

    summary = data.get("summary") or {}
    st.caption(f"run_id：`{summary.get('run_id')}`｜模型模式：`{summary.get('model_mode')}`"
               f"｜数据模式：`{summary.get('data_mode')}`")
    if mode == "mock":
        st.warning(
            "本批次为**离线确定性执行链路评测**（逐例注入计划，不调用真实模型）。"
            "它验证 SQL 生成、安全校验、查询、统计与报告链路，"
            "**不代表真实 DeepSeek/其他模型的自然语言理解准确率**。")
    elif mode == "real":
        st.info("本批次为**真实 LLM 评测**（显式启用、有调用预算；"
                "拒绝结论来自真实模型输出的解析结果）。")
    else:
        st.caption("未识别模型模式。")

    def pct(v):
        return "N/A" if v is None else f"{v * 100:.1f}%"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("执行成功率", pct(summary.get("query_success_rate")),
              help=f"{summary.get('query_success')}/{summary.get('valid_cases')} 有效案例")
    c2.metric("答案正确率", pct(summary.get("answer_accuracy")),
              help=f"{summary.get('answers_correct')}/{summary.get('verifiable_cases')}"
                   "（分母由标准答案事前声明）")
    c3.metric("行为正确率", pct(summary.get("behavior_accuracy")),
              help=f"全部 {summary.get('total_cases')} 个案例")
    c4.metric("正确拒绝率", pct(summary.get("negative_accuracy")),
              help=f"{summary.get('negative_correct')}/{summary.get('negative_cases')}"
                   " 负面案例")

    def _ms(v):
        return "N/A" if v is None else f"{float(v):.0f} ms"

    c5, c6, c7 = st.columns(3)
    c5.metric("P50 响应", _ms(summary.get("latency_p50_ms")))
    c6.metric("P95 响应", _ms(summary.get("latency_p95_ms")))
    cost = summary.get("cost_total")
    token = summary.get("token_total")
    currency = summary.get("cost_currency") or ""
    cost_text = ("N/A" if cost in (None, "N/A")
                 else f"{cost} {currency}".strip())
    c7.metric("Token / 成本",
              f"{token if token is not None else 'N/A'} / {cost_text}",
              help="离线模式未调用真实 API，Token 与费用记为 N/A（不填 0）")
    st.caption(f"成本说明：{summary.get('cost_note') or '—'}")
    if summary.get("token_incomplete_cases"):
        st.warning(f"有 {summary['token_incomplete_cases']} 个案例的响应缺少 usage，"
                   "Token 与成本统计**不完整**（未按 0 计）。")
    st.caption(f"调用统计：请求 {summary.get('token_calls') or 0} 次响应；"
               f"案例数 {summary.get('total_cases')}。")

    with st.expander("按分析类型表现", expanded=True):
        rows = [{"分析类型": k, "总数": v["total"], "行为符合": v["behavior_correct"],
                 "可验证": v["answers_verified"], "答案正确": v["answers_correct"]}
                for k, v in sorted((summary.get("by_analysis_type") or {}).items())]
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)

    failures = summary.get("failures") or []
    st.markdown(f"#### 失败案例（{len(failures)}）")
    if failures:
        st.dataframe([{"case_id": f.get("case_id"), "类别": f.get("category"),
                       "期望行为": f.get("expected_behavior"), "实际状态": f.get("status"),
                       "原因": (f.get("failure_reason") or "")[:120]} for f in failures],
                     width="stretch", hide_index=True)
    else:
        st.success("本批次无失败案例。")

    if data.get("summary_md"):
        with st.expander("查看完整 Markdown 汇总"):
            st.markdown(data["summary_md"])


def _view_about() -> None:
    st.title("项目信息")
    st.markdown(
        "**DataAgent** 是一个本地命令行/网页数据分析 Agent：把自然语言问题转换成"
        "受约束的分析计划，在封闭指标目录上生成 SQL，经 AST 安全校验后只读执行，"
        "再用 Python 做确定性统计，最终产出带证据的中文报告。")

    st.markdown("### 架构与流程")
    st.code(
        "用户问题\n"
        "  → IntentParser（LLM 仅做理解，输出 Pydantic 白名单计划）\n"
        "  → QueryBuilder（确定性 SQL 编译：模板 + 参数绑定）\n"
        "  → SQLGuard（AST 校验：只读/表白名单/用户级数据保护）\n"
        "  → QueryService（只读连接、行数上限、超时取消、审计）\n"
        "  → Analytics（Python 确定性统计 + 证据）\n"
        "  → ReportGenerator（14 节 Markdown + JSON 证据；可选 LLM 叙述）\n"
        "  → 报告 .md + 证据 .json + 图表 .png",
        language="text")

    st.markdown("### 与“LLM 直接生成 SQL”的区别")
    st.markdown(
        "| 维度 | 常见做法（LLM 直接写 SQL） | 本项目 |\n"
        "|---|---|---|\n"
        "| SQL 来源 | 模型自由生成 | 封闭模板编译，用户输入只做参数绑定 |\n"
        "| 安全 | 依赖提示词约束 | AST 级白名单 + 只读执行 + 行数/超时限制 |\n"
        "| 数字可信度 | 模型可能口算/编造 | 全部由确定性统计产生，逐条可追溯证据 |\n"
        "| 失败处理 | 容易静默降级 | 显式状态（澄清/拒绝/超时/数据不足），不伪装成功 |\n"
        "| 叙述 | 无约束叙述 | LLM 只能复述证据，四类失败明确回退 |")

    st.markdown("### 技术栈")
    st.markdown("Python 3.11+ · Streamlit（本地界面）· Pydantic（计划 Schema）· "
                "SQLGlot（AST 校验）· DuckDB（只读查询）· Matplotlib（图表）· "
                "OpenAI 兼容 SDK（可选真实模型）")

    st.markdown("### 数据来源与边界")
    st.markdown(
        "- 项目原始授权的资金流入流出数据**不随仓库发布**（仅负责人本地使用）；仓库随附"
        "**经授权的非真实业务演示数据**（脱敏改造，可离线构建演示库）\n"
        "- Mock 模式默认使用人工构造的合成测试样本（**非真实业务数据**）；"
        "Real + 项目库使用随附的演示数据库\n"
        "- 本页面为**本地单用户演示**：无鉴权、无会话隔离、不适用于公网多用户场景\n"
        "- 未实现 k-匿名小样本抑制，不提供正式统计披露控制保证\n"
        "- 统计趋势与相关性**不代表因果关系**")
    st.caption("MIT License · Copyright (c) 2026 dataagent project contributors")


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, page_icon="📊", layout="wide")
    cfg = _sidebar()
    view = st.sidebar.radio("页面", ["数据分析", "Benchmark", "项目信息"],
                            index=0, key="view")
    if view == "数据分析":
        _view_analysis(cfg)
    elif view == "Benchmark":
        _view_benchmark()
    else:
        _view_about()


main()
