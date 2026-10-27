"""Web 层业务封装（阶段 12）。

**只调用现有受控接口**：`AgentConfig` / `run_agent` / `QueryService` / `display_path`。
不复制任何统计、SQL 生成或安全校验逻辑；不执行未验证 SQL；不写数据库。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dataagent import config, db, ingestion, shibor, visualization
from dataagent.agent import AgentConfig, AgentStatus, run_agent
from dataagent.query_service import QueryService
from dataagent.report_generator import display_path

ROOT = Path(__file__).resolve().parents[1]
SYNTH_DIR = ROOT / "tests" / "fixtures" / "synthetic"
SYNTH_RANGE = (date(2024, 1, 2), date(2024, 1, 12))
BENCH_LATEST = ROOT / "artifacts" / "evaluation" / "latest"

MODEL_MODES = ("mock", "real")
DATA_MODES = ("synthetic", "project_db")
# 展示用标签（内部 key 不变，避免影响既有逻辑与测试）
DATA_MODE_LABELS = {
    "synthetic": "合成测试数据（仓库内小样本，非真实业务数据，无需建库）",
    "project_db": "项目自带经授权的非真实业务演示数据（2013-07 至 2014-08，脱敏改造）",
}

# 状态 → 中文说明（任务 D-3：分类易懂、Warning 不等于致命错误）
# 状态 → 中文说明（阶段 13.1：区分"预期边界"与"真实故障"，并给出下一步建议）
STATUS_TEXT = {
    "report_generated": "已完成，报告已生成",
    "report_generated_with_warnings":
        "已完成，但有数据质量提示——请查看下方警告，确认数据覆盖、缺失值与"
        "窗口截取对结论适用范围的影响",
    "needs_clarification":
        "需要补充信息（不是故障）：问题缺少关键要素，请补充**指标**（如申购金额）与"
        "**时间范围**（如 2024-01-02 至 2024-01-12）",
    "plan_rejected":
        "无法执行：分析计划不符合受支持范围（未确认指标、口径冲突或多指标不兼容）",
    "sql_rejected":
        "已安全拒绝：生成的 SQL 未通过安全校验，**未执行**（系统保护行为）",
    "execution_failed": "数据库执行失败：查询过程中出错（不是数据问题）",
    "timeout": "执行超时：查询耗时超过限制，已被安全取消；可缩小时间范围重试",
    "insufficient_data":
        "数据不足：有效数据点太少，无法给出可靠统计；建议**扩大时间范围**"
        "（单日无法计算日环比属于预期边界）",
    "incompatible_metric": "指标口径不匹配：请求指标无法唯一对应结果列",
    "llm_not_configured":
        "未配置模型：Real 模式需要在启动前设置 `DATAAGENT_LLM_API_KEY`；"
        "也可切换回 Mock 模式体验内置演示",
    "unsupported_metric":
        "不支持的指标：该指标不在受支持目录内（如“销售额”无确认口径）；"
        "请改用受支持指标，或在 Real 模式下换一种问法",
    "mock_project_db_mismatch":
        "配置不匹配：Mock 内置示例基于 2024 年合成数据，无法用于真实业务库；"
        "请把数据模式切回「合成测试数据」，或改用 Real 模式分析真实库",
    "mock_scenario_unknown":
        "Mock 模式的使用限制（不是后端故障）：Mock 只识别内置演示场景，"
        "无法自由理解任意问题；请从示例中选择，或切换到 Real 模式",
}

SUCCESS_STATUSES = {AgentStatus.REPORT_GENERATED,
                    AgentStatus.REPORT_GENERATED_WITH_WARNINGS}

# 首页示例问题（全部为受支持的分析类型，使用合成数据即可回答）
# 示例问题不再硬编码：统一从 `scripts/demo_agent.py` 的 DEMOS 派生
# （见 mock_catalog() / mock_examples()），避免与演示场景不同步。


@dataclass
class RunOutput:
    """一次运行的展示用结果（不含敏感信息）。"""

    ok: bool
    status: str
    status_text: str
    question: str
    model_mode: str
    data_mode: str
    plan: dict = field(default_factory=dict)
    metrics: list[str] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    charts: list[str] = field(default_factory=list)
    report_text: str = ""
    evidence: dict = field(default_factory=dict)
    report_name: str = ""
    evidence_name: str = ""
    error: str = ""
    error_code: str = ""
    narrative_source: str = ""
    latency_hint: str = ""


class SyntheticDB:
    """合成数据库（会话级缓存；演示结束由调用方清理）。"""

    _dir: Path | None = None
    _path: Path | None = None

    @classmethod
    def path(cls) -> Path:
        if cls._path is None or not cls._path.is_file():
            cls._dir = Path(tempfile.mkdtemp(prefix="dataagent_web_"))
            cls._path = cls._dir / "web.duckdb"
            con = db.connect(cls._path)
            db.init_schema(con)
            reports = ingestion.import_all(con, data_dir=SYNTH_DIR)
            assert all(r.ok for r in reports), [r.error for r in reports if not r.ok]
            shibor.create_fill_view(con)
            con.close()
        return cls._path

    @classmethod
    def cleanup(cls) -> None:
        if cls._dir is not None:
            shutil.rmtree(cls._dir, ignore_errors=True)
        cls._dir = None
        cls._path = None


_DEMO_MODULE = None


def _demo_module():
    """加载 `scripts/demo_agent.py`（复用其演示场景与 Mock 客户端）。"""
    global _DEMO_MODULE
    if _DEMO_MODULE is None:
        path = ROOT / "scripts" / "demo_agent.py"
        spec = importlib.util.spec_from_file_location("_dataagent_demo", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _DEMO_MODULE = mod
    return _DEMO_MODULE


_PUNCT = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":",
    "（": "(", "）": ")", "　": " ", "、": ",", "～": "~", "至": "至",
})


def _normalize(question: str) -> str:
    """规范化：去全部空白、统一全角标点、去尾部句末标点。

    这样"帮我分析申购和赎回金额最近的趋势。"与不带句号/带空格的写法等价，
    但**不**做子串匹配——避免"申购""趋势"等片段意外命中预设计划。
    """
    q = (question or "").translate(_PUNCT)
    q = "".join(q.split())
    return q.rstrip(".!?;,")


def mock_catalog() -> list[dict]:
    """Mock 场景目录（唯一数据源 = scripts/demo_agent.py 的 DEMOS）。"""
    demo = _demo_module()
    out = []
    for idx, item in enumerate(demo.DEMOS):
        out.append({
            "index": idx,
            "case_id": item.get("case_id", f"S{idx}"),
            "kind": item.get("kind", "success"),
            "label": item.get("label", f"场景 {idx + 1}"),
            "question": item["question"],
            "expected": item.get("expected", "report_generated"),
            "expected_note": item.get("expected_note", ""),
        })
    return out


def mock_examples(kind: str | None = None) -> list[str]:
    """示例问题（可按 success / boundary 过滤）。"""
    return [c["question"] for c in mock_catalog()
            if kind is None or c["kind"] == kind]


def mock_scenario_detail(question: str) -> dict | None:
    """精确匹配（规范化后全等）；未命中返回 None。"""
    norm = _normalize(question)
    if not norm:
        return None
    for item in mock_catalog():
        if _normalize(item["question"]) == norm:
            demo = _demo_module()
            item = dict(item)
            item["client"] = demo.mock_client(item["index"])
            return item
    return None


def mock_scenario(question: str):
    """兼容接口：返回 (client, label) 或 (None, None)。"""
    detail = mock_scenario_detail(question)
    if detail is None:
        return None, None
    return detail["client"], detail["label"]


def data_coverage(model_mode: str, data_mode: str) -> str:
    """侧边栏显示的数据覆盖范围（避免暴露本机绝对路径）。"""
    if data_mode == "synthetic":
        return f"{SYNTH_RANGE[0]} 至 {SYNTH_RANGE[1]}（合成测试数据，非真实业务数据）"
    p = Path(config.DB_PATH)
    if not p.is_file():
        return "项目演示数据库未就绪（请先运行 python scripts/build_demo_db.py --verify）"
    return f"项目演示数据库：{display_path(p)}（只读）"


def build_config(model_mode: str, data_mode: str, output_dir: Path,
                 use_llm_narrative: bool = False,
                 llm_client: object | None = None) -> AgentConfig:
    """构建运行配置。**不接受任意数据库路径**（仅合成库或项目固定路径）。"""
    if model_mode not in MODEL_MODES or data_mode not in DATA_MODES:
        raise ValueError("非法的模型/数据模式")
    if data_mode == "synthetic":
        db_path = SyntheticDB.path()
        data_range = SYNTH_RANGE
    else:
        db_path = Path(config.DB_PATH)
        data_range = None
        if not db_path.is_file():
            # 演示数据库缺失 → 明确指引（不生成"不存在的数据源"的报告）
            raise FileNotFoundError(
                f"演示数据库不存在：{display_path(db_path)}\n"
                "请先在项目根目录执行：python scripts/build_demo_db.py\n"
                "（使用仓库自带的 demo_data/*.parquet 离线构建，无需下载外部数据）")
    return AgentConfig(
        model_mode=model_mode, data_mode=data_mode, db_path=db_path,
        data_range=data_range, output_dir=Path(output_dir),
        llm_client=llm_client,
        use_llm_narrative=bool(use_llm_narrative and model_mode == "real"),
    )


def run_question(question: str, model_mode: str, data_mode: str,
                 output_dir: Path | None = None,
                 progress=None) -> RunOutput:
    """执行一次分析（只调用 run_agent；数据库始终只读）。"""
    question = (question or "").strip()
    out_dir = Path(output_dir) if output_dir else visualization.DEFAULT_OUTPUT_DIR
    if not question:
        return RunOutput(ok=False, status="empty_question",
                         status_text="请输入一个分析问题", question=question,
                         model_mode=model_mode, data_mode=data_mode,
                         error="问题为空")

    # P1-1：Mock 内置示例固定使用 2024 年合成数据，不能用于真实业务库
    # （绝不自动改写计划里的日期——那会造成"看起来成功"的假结果）
    if model_mode == "mock" and data_mode == "project_db":
        return RunOutput(
            ok=False, status="mock_project_db_mismatch",
            status_text=STATUS_TEXT["mock_project_db_mismatch"],
            question=question, model_mode=model_mode, data_mode=data_mode,
            error="Mock 内置示例的计划时间范围为 2024-01-02 至 2024-01-12，"
                  "与真实业务库（2013-07 至 2014-08）不匹配",
            error_code="mock_project_db_mismatch")

    mock_client = None
    mock_label = None
    if model_mode == "mock":
        mock_client, mock_label = mock_scenario(question)
        if mock_client is None:
            return RunOutput(
                ok=False, status="mock_scenario_unknown",
                status_text=STATUS_TEXT["mock_scenario_unknown"],
                question=question, model_mode=model_mode, data_mode=data_mode,
                error="未匹配到演示场景：请从示例问题中选择，或切换为 Real 模式",
                error_code="mock_scenario_unknown")

    try:
        cfg = build_config(model_mode, data_mode, out_dir, llm_client=mock_client)
    except FileNotFoundError as e:
        return RunOutput(ok=False, status="db_missing", status_text=str(e),
                         question=question, model_mode=model_mode,
                         data_mode=data_mode, error=str(e))
    except ValueError as e:
        return RunOutput(ok=False, status="invalid_config", status_text=str(e),
                         question=question, model_mode=model_mode,
                         data_mode=data_mode, error=str(e))

    if progress:
        progress("正在解析问题并生成分析计划…")
    try:
        with QueryService(cfg.db_path) as qs:
            result = run_agent(question, cfg, query_service=qs)
    except Exception as e:      # noqa: BLE001 - 页面不得展示堆栈细节
        return RunOutput(ok=False, status="exception",
                         status_text=f"运行时出现异常（{type(e).__name__}）",
                         question=question, model_mode=model_mode,
                         data_mode=data_mode, error=type(e).__name__)

    ok = result.status in SUCCESS_STATUSES
    report_text = ""
    evidence: dict = {}
    report_name = evidence_name = ""
    if result.report_path and Path(result.report_path).is_file():
        report_text = Path(result.report_path).read_text(encoding="utf-8")
        report_name = Path(result.report_path).name
    if result.evidence_path and Path(result.evidence_path).is_file():
        try:
            evidence = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            evidence = {}
        evidence_name = Path(result.evidence_path).name

    plan = result.plan or {}
    metrics = []
    for m in plan.get("metrics", []):
        mid = m.get("metric_id")
        term = m.get("term")
        metrics.append(f"{mid}:{term}" if term else str(mid))

    queries = []
    for q in (result.execution_summary or {}).get("queries", []):
        eff = (q.get("window_start"), q.get("window_end"))
        req = (q.get("requested_window_start"), q.get("requested_window_end"))
        clipped = bool(eff[0] and req[0] and (eff[0] != req[0] or eff[1] != req[1]))
        queries.append({
            "name": q.get("name"), "status": q.get("status"),
            "row_count": q.get("row_count"),
            "window": f"{eff[0]} 至 {eff[1]}" if eff[0] else "—",
            "requested_window": f"{req[0]} 至 {req[1]}" if req[0] else None,
            "window_clipped": clipped,
            "sql": q.get("sql") or "",
            # 已绑定的参数（白名单过滤；不可展示项 value=None）
            "bindings": list(q.get("bindings") or []),
        })

    return RunOutput(
        ok=ok, status=result.status.value,
        status_text=STATUS_TEXT.get(result.status.value, result.status.value),
        question=question, model_mode=result.model_mode,
        data_mode=result.data_mode, plan=plan, metrics=metrics,
        queries=queries,
        conclusions=[c.text for c in
                     (getattr(result.analysis_result, "conclusions", None) or [])],
        warnings=list(result.warnings or []),
        limitations=list(result.limitations or []),
        charts=[display_path(p) for p in (result.chart_paths or [])],
        report_text=report_text, evidence=evidence,
        report_name=report_name, evidence_name=evidence_name,
        error=result.error or "", error_code=result.error_code or "",
        narrative_source=result.narrative_source or "deterministic",
    )


def evidence_rows(evidence: dict) -> list[dict]:
    """证据 ID → 指标/口径摘要（供表格展示）。"""
    rows = []
    for e in (evidence.get("evidence") or []):
        rng = e.get("date_range") or []
        rows.append({
            "证据 ID": f"[{e.get('id')}]",
            "指标": e.get("metric_id") or "",
            "单位": e.get("unit") or "",
            "查询": e.get("query_name") or "",
            "日期范围": " 至 ".join(str(x) for x in rng) if rng else "—",
            "数据点数": e.get("data_points"),
            "证据强度": e.get("evidence_strength") or "",
        })
    return rows


def load_benchmark() -> dict | None:
    """读取已有的离线评测结果（不重新运行评测）。"""
    res_file = BENCH_LATEST / "evaluation_results.json"
    if not res_file.is_file():
        return None
    try:
        data = json.loads(res_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    summary_md = BENCH_LATEST / "summary.md"
    data["summary_md"] = (summary_md.read_text(encoding="utf-8")
                          if summary_md.is_file() else "")
    return data


def is_real_mode_configured() -> bool:
    import os
    return bool(os.environ.get("DATAAGENT_LLM_API_KEY"))


def package_version() -> str:
    """版本号：优先取安装元数据（与 pyproject 一致），失败回退包内常量。"""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("dataagent")
        except PackageNotFoundError:
            pass
    except Exception:      # noqa: BLE001
        pass
    from dataagent import __version__ as v
    return v


def model_label(model_mode: str) -> str:
    import os
    if model_mode != "real":
        return "Mock（内置演示解析器，不调用外部 API）"
    model = os.environ.get("DATAAGENT_LLM_MODEL") or "（代码默认兜底模型）"
    base = os.environ.get("DATAAGENT_LLM_BASE_URL") or "（默认 OpenAI 兼容地址）"
    return f"真实模型：`{model}` @ {base}"
