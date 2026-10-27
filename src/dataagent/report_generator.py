"""证据化中文分析报告生成器（阶段七）。

两层结构：

1. **确定性报告层**（始终可用）：负责数字、日期、指标、统计方法、证据与警告——
   全部直接取自 `AnalysisResult`（其中每条结论都带 `Evidence`）；不存在自由文本推测。
2. **可选 LLM 叙述层**（`llm_narrator` 提供时启用）：只允许"组织文字"，
   输入是结构化证据包；输出必须满足：
   - 引用至少一个已有证据 ID（`[E001]` 形式）；
   - 出现的每个数字都能在证据包中找到（不得引入新数字）；
   - 不得改写指标口径（不得把申购金额称为销售额）；
   - 不得把相关性表述为因果、不得把异常点归因外部事件。
   任一条不满足 → **拒绝该段叙述并回退确定性内容**（`narrative_source` 标注）。

输出：
- Markdown 报告（14 节结构）
- JSON 证据文件（结构化事实 + 证据清单，报告中每个 `[E00x]` 都能在其中找到）
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config as app_config
from . import metric_catalog as mc
from . import visualization
from .analytics_models import AnalysisStatus, EvidenceStrength

log = logging.getLogger(__name__)

_NOT_AVAILABLE = "数据不足 / 不适用"

# LLM 叙述中的禁用表述（无法由数据支持）
_FORBIDDEN_PATTERNS = (
    (r"销售额", "指标口径错误：数据中没有经确认的销售额指标"),
    (r"因果关系|导致|归因于|因为.{0,10}造成", "不得表述因果关系或外部归因"),
    (r"城市名称|北京|上海|广州|深圳|杭州", "不得虚构城市名称"),
)


@dataclass
class ReportResult:
    status: str                       # generated / generated_with_warnings / failed
    markdown_path: str | None = None
    evidence_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    narrative_source: str = "deterministic"   # deterministic / llm / llm_rejected_fallback
    narrative_fallback_reason: str | None = None    # 回退原因分类（见 NARRATIVE_FALLBACK_REASONS）
    narrative_fallback_detail: str | None = None    # 回退细节（可读，不含密钥）
    evidence_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status, "markdown_path": self.markdown_path,
            "evidence_path": self.evidence_path, "warnings": self.warnings,
            "error": self.error, "narrative_source": self.narrative_source,
            "narrative_fallback_reason": self.narrative_fallback_reason,
            "narrative_fallback_detail": self.narrative_fallback_detail,
            "evidence_ids": self.evidence_ids,
        }


# ---------------------------------------------------------------------------
# 事实与证据包
# ---------------------------------------------------------------------------

def _iso(v):
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def build_report_facts(agent_result) -> dict:
    """构造结构化事实与证据包（报告的唯一致据来源）。"""
    analysis = agent_result.analysis_result
    if analysis is None:
        return {"error": "没有可用的分析结果", "evidence": [], "evidence_numbers": []}

    evidence: list[dict] = []
    for i, c in enumerate(analysis.conclusions, start=1):
        eid = f"E{i:03d}"
        ev = c.evidence
        evidence.append({
            "id": eid,
            "text": c.text,
            "metric_id": c.metric_id,
            "metric_name_zh": c.metric_name_zh,
            "unit": c.unit,
            "query_name": c.query_name,
            "date_range": list(c.date_range) if c.date_range else None,
            "main_window": list(c.main_window) if c.main_window else None,
            "comparison_window": list(c.comparison_window) if c.comparison_window else None,
            "requested_range": list(ev.requested_range) if ev.requested_range else None,
            "requested_main_window": (list(ev.requested_main_window)
                                      if ev.requested_main_window else None),
            "requested_comparison_window": (list(ev.requested_comparison_window)
                                            if ev.requested_comparison_window else None),
            "data_points": c.data_points,
            "formula": c.formula,
            "evidence_strength": c.evidence_strength.value,
            "input_summary": ev.input_summary,
            "warnings": list(c.warnings),
            "limitations": list(c.limitations),
        })

    stats = analysis.statistics
    statistics_dict = stats.model_dump(mode="json") if stats is not None else None
    charts = [{"path": display_path(p), "kind": "chart"}
              for p in agent_result.chart_paths]

    facts = {
        "metadata": {
            "title": f"数据分析报告：{agent_result.question}",
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"),
            "run_status": agent_result.status.value,
            "model_mode": agent_result.model_mode,
            "data_mode": agent_result.data_mode,
            "narrative_source": agent_result.narrative_source or "deterministic",
            "llm_model": getattr(agent_result, "llm_model", None),
        },
        "question": agent_result.question,
        "plan": agent_result.plan,
        "execution_summary": agent_result.execution_summary,
        "time_range": analysis.time_range,
        "metrics": [m.model_dump() for m in analysis.metrics],
        "method": analysis.method,
        "method_params": analysis.method_params,
        "statistics": statistics_dict,
        "evidence": evidence,
        "warnings": list(dict.fromkeys(agent_result.warnings + list(analysis.warnings))),
        "limitations": list(dict.fromkeys(analysis.limitations)),
        "charts": charts,
        "data_coverage": (analysis.time_range or {}).get("data_coverage"),
    }
    facts["evidence_numbers"] = sorted(_collect_numbers(facts))
    return facts


def _collect_numbers(facts: dict) -> set[float]:
    """收集证据包中出现的全部数值（LLM 叙述只允许复述这些数字）。"""
    nums: set[float] = set()

    def walk(node):
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            nums.add(round(float(node), 6))
        elif isinstance(node, str):
            for x in re.findall(r"-?\d+(?:\.\d+)?", node):
                try:
                    nums.add(round(float(x.replace(",", "")), 6))
                except ValueError:
                    pass
            for x in re.findall(r"-?\d[\d,]*\.?\d*", node):
                try:
                    nums.add(round(float(x.replace(",", "")), 6))
                except ValueError:
                    pass
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(facts.get("evidence"))
    walk(facts.get("statistics"))
    walk(facts.get("time_range"))
    walk(facts.get("plan"))
    walk(facts.get("execution_summary"))
    return nums


# ---------------------------------------------------------------------------
# LLM 叙述校验
# ---------------------------------------------------------------------------

def _extract_numbers(text: str) -> list[float]:
    t = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)   # 日期不计入
    t = re.sub(r"E\d{3}", " ", t)
    out = []
    for x in re.findall(r"-?\d[\d,]*\.?\d*", t):
        try:
            out.append(round(float(x.replace(",", "")), 6))
        except ValueError:
            pass
    return out


# 句子/标点层面的完整性规则（07.6-A）
_DANGLING_CONNECTIVES = (
    "此外", "并且", "同时", "而", "其中", "以及", "但是", "然而", "因此", "所以",
    "然后", "另外", "以及", "并且", "与", "和", "的", "了", "在", "为", "对", "按",
    "较", "更", "最", "但", "或", "及", "即", "则", "由", "从", "向", "对",
)
_SENTENCE_END = "。！？!?….;；"
# 可出现在句末的闭合符号（**不能替代**句末标点；检查时先剥离再看是否以标点结束）
_CLOSING_SYMBOLS = "）〕】」』”’\"')"
# 逐类配对检查的括号/引号（任一类计数不等即视为未闭合）
_PAIRED_SYMBOLS = (
    ("（", "）", "中文圆括号（）"),
    ("(", ")", "英文圆括号()"),
    ("「", "」", "直角引号「」"),
    ("『", "』", "双直角引号『』"),
    ("“", "”", "弯引号“”"),
    ("‘", "’", "单弯引号‘’"),
)


def _fallback_detail_summary(detail: str | None) -> str | None:
    """把回退细节归纳为**问题类别**（不含被拒原文/数字，可安全写入报告）。

    被拒绝的叙述内容（如证据外数字、幻觉口径）**不得回显到报告**，
    以免把不可信内容混入正式报告；此处只输出类别标签。
    """
    if not detail:
        return None
    mapping = (
        ("不存在的数字", "证据外数字"),
        ("未知证据", "未知证据引用"),
        ("未引用任何证据", "缺少证据引用"),
        ("口径", "指标口径问题"),
        ("因果", "因果表述问题"),
        ("括号", "结构未闭合"),
        ("引号", "结构未闭合"),
        ("标点", "句子未完成"),
        ("句末标点", "句子未完成"),
        ("连接词", "句子未完成"),
    )
    labels: list[str] = []
    for key, label in mapping:
        if key in detail and label not in labels:
            labels.append(label)
    if not labels:
        labels.append("其他约束不满足")
    return "、".join(labels)


# 每类回退原因的**报告表述**（07.6.3-B）：区分"未发起调用"与"调用后校验失败"
_NARRATIVE_FALLBACK_DESC = {
    "no_model": "未提供模型名称，**未发起**叙述层调用，保留确定性报告",
    "api_error": "叙述层模型 **API 调用失败**，保留确定性报告",
    "truncated": "模型输出达到**生成长度上限**（可能被截断），保留确定性报告",
    "incomplete": "摘要**完整性检查**失败（句子未完成或引号/括号未闭合），保留确定性报告",
    "evidence": "证据**一致性检查**失败，保留确定性报告",
}


# 叙述层回退原因分类（07.6.2-B）：结构化记录，便于报告与告警可读呈现
NARRATIVE_FALLBACK_REASONS = {
    "no_model": "未提供模型名称（未调用叙述层）",
    "api_error": "模型 API 调用失败",
    "truncated": "模型输出被截断（达到生成长度上限）",
    "incomplete": "摘要完整性校验失败（句子未完成或引号/括号未闭合）",
    "evidence": "证据一致性校验失败（引用、数字或口径约束不满足）",
}


def validate_narrative_completeness(text: str) -> tuple[bool, list[str]]:
    """检查 LLM 摘要是否完整（07.6-A）。

    关注**完整性**而非长度：短摘要只要句子完整即接受。
    判定为不完整的情形：
    - 以逗号/顿号/分号/冒号结尾（句子被切断）；
    - 未以句末标点结束；
    - 以悬空连接词结尾（"此外，申购金额有4个日期、赎回"）；
    - 括号/引号未闭合。
    """
    problems: list[str] = []
    t = (text or "").strip()
    if not t:
        return False, ["摘要为空"]
    if t[-1] in "，,、；;：:":
        problems.append("摘要以标点结尾，句子未完成")
    else:
        # 句末标点检查：闭合括号/引号**不能替代**句末标点
        # （"……（说明）" 视为句子未结束，"……（说明）。" 才算完整）
        core = t.rstrip(_CLOSING_SYMBOLS)
        if not core or core[-1] not in _SENTENCE_END:
            problems.append("摘要未以句末标点结束")
    tail = t.rstrip("，,、；;：: 。.!！?？")
    for word in _DANGLING_CONNECTIVES:
        if tail.endswith(word):
            problems.append(f"摘要以悬空连接词结尾（{word}）")
            break
    # 括号与引号**逐类**配对检查（任一类不匹配即报告）
    for opener, closer, name in _PAIRED_SYMBOLS:
        if t.count(opener) != t.count(closer):
            problems.append(f"摘要存在未闭合的{name}")
    return (not problems), problems


def validate_narrative(text: str, facts: dict) -> tuple[bool, list[str]]:
    """校验 LLM 叙述是否受证据约束（引用完整、无新数字、无越界表述）。"""
    problems: list[str] = []
    if not text or not text.strip():
        return False, ["叙述为空"]

    ids = {e["id"] for e in facts.get("evidence", [])}
    cited = set(re.findall(r"\[E\d{3}\]", text))
    if not cited:
        problems.append("叙述未引用任何证据 ID（应形如 [E001]）")
    unknown = {c.strip("[]") for c in cited} - ids
    if unknown:
        problems.append(f"叙述引用了不存在的证据 ID: {sorted(unknown)}")

    allowed = {round(float(x), 6) for x in facts.get("evidence_numbers", [])}
    novel = sorted({n for n in _extract_numbers(text) if n not in allowed})
    if novel:
        problems.append(f"叙述引入了证据包中不存在的数字: {novel[:5]}")

    for pattern, reason in _FORBIDDEN_PATTERNS:
        if re.search(pattern, text):
            problems.append(reason)

    return (not problems), problems


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def display_path(path: str | Path) -> str:
    """把本机绝对路径转为项目内相对路径（避免报告泄露本地目录结构）。"""
    try:
        p = Path(path).resolve()
        root = Path(app_config.PROJECT_ROOT).resolve()
        return str(p.relative_to(root)).replace("\\", "/")
    except (ValueError, OSError):
        return Path(path).name


def _fmt_range(rng) -> str:
    if not rng:
        return "无法确认"
    start, end = rng[0], rng[1]
    return f"{start} 至 {end}"


def _synthetic_coverage_lines(time_range: dict) -> list[str]:
    """第 6 节「合成数据实际覆盖范围」渲染。

    当分析含对比窗口（跨年逐日对比等）时，必须**分别列出主窗口与对比窗口**，
    避免把主窗口单独当作“本次合成数据的全部覆盖范围”。
    """
    tr = time_range or {}
    eff = tr.get("effective") or {}
    eff_comp = tr.get("effective_comparison") or {}
    has_comp = bool(eff_comp.get("start"))
    lines: list[str] = []
    if eff.get("start"):
        suffix = "（主窗口）" if has_comp else ""
        lines.append("- **本次合成测试数据的实际覆盖范围" + suffix + "**："
                     f"{eff.get('start')} 至 {eff.get('end')}")
    if has_comp:
        lines.append("- **本次合成测试数据的实际覆盖范围（对比窗口）**："
                     f"{eff_comp.get('start')} 至 {eff_comp.get('end')}")
    if not lines:
        return ["- **本次合成测试数据的实际覆盖范围**：以第 5 节执行窗口为准"
                "（合成数据仅覆盖上述日期，非真实业务数据）"]
    if has_comp:
        lines.append("（合成数据覆盖上述两个窗口的日期，非真实业务数据）")
    else:
        lines[0] += "（合成数据仅覆盖上述日期，非真实业务数据）"
    return lines


def _render_time_range_section(facts: dict) -> list[str]:
    tr = facts.get("time_range") or {}
    requested = tr.get("requested") or {}
    effective = tr.get("effective")
    eff_comp = tr.get("effective_comparison")
    note = tr.get("effective_note")
    lines = ["## 5. 实际查询时间范围", ""]
    if effective:
        lines.append(f"- 实际执行窗口：{effective['start']} 至 {effective['end']}")
    elif facts.get("analysis_type") == "window_comparison" or eff_comp:
        lines.append("- 主窗口实际执行窗口：无法确认（缺少窗口元数据）")
    else:
        lines.append("- 实际执行窗口：**无法确认**（查询结果缺少窗口元数据）")
    if eff_comp:
        lines.append(f"- 对比窗口实际执行窗口：{eff_comp['start']} 至 {eff_comp['end']}")
    if requested.get("start"):
        lines.append(f"- 原始请求范围：{requested['start']} 至 {requested.get('end')}")
    if requested.get("comparison_start"):
        lines.append(f"- 原始请求对比窗口：{requested['comparison_start']} 至 "
                     f"{requested.get('comparison_end')}")
    if note:
        lines.append(f"- 元数据说明：{note}")
    lines.append("")
    return lines


def _render_markdown(facts: dict, narrative: str | None,
                     narrative_note: str | None) -> str:
    md: list[str] = []
    meta = facts.get("metadata", {})
    md.append(f"# {meta.get('title', '数据分析报告')}")
    md.append("")
    md.append(f"> 生成时间：{meta.get('generated_at')}　|　"
              f"模型模式：**{meta.get('model_mode')}**　|　"
              f"数据模式：**{meta.get('data_mode')}**　|　"
              f"报告叙述：{meta.get('narrative_source')}")
    md.append("")

    # 2 用户问题
    md.append("## 2. 用户原始问题")
    md.append("")
    md.append(f"> {facts.get('question', '')}")
    md.append("")

    # 3 执行状态与数据来源
    md.append("## 3. 执行状态与数据来源")
    md.append("")
    md.append(f"- 运行状态：`{meta.get('run_status')}`")
    md.append(f"- 模型模式：`{meta.get('model_mode')}`"
              + ("（Mock 解析结果，**非真实模型调用**）"
                 if meta.get("model_mode") == "mock" else ""))
    md.append(f"- 数据模式：`{meta.get('data_mode')}`"
              + ("（合成测试数据，**非真实业务数据**）"
                 if meta.get("data_mode") == "synthetic" else "（项目数据库，只读）"))
    md.append("")

    # 4 分析目标与指标定义
    md.append("## 4. 分析目标与指标定义")
    md.append("")
    md.append(f"- 分析方法：`{facts.get('method')}`")
    if facts.get("method_params"):
        md.append(f"- 方法参数：`{json.dumps(facts['method_params'], ensure_ascii=False)}`")
    md.append("")
    md.append("| 指标 ID | 中文名称 | 单位 | 计算口径 |")
    md.append("|---|---|---|---|")
    for m in facts.get("metrics", []):
        md.append(f"| `{m['metric_id']}` | {m['name_zh']} | {m['unit']} | {m['formula']} |")
    md.append("")

    # 5 实际查询时间范围
    md.extend(_render_time_range_section(facts))

    # 6 数据覆盖范围
    md.append("## 6. 数据覆盖范围与截取说明")
    md.append("")
    cov = facts.get("data_coverage") or {}
    if cov:
        md.append(f"- 数据集覆盖（配置常量）：{cov.get('min')} 至 {cov.get('max')}")
        if cov.get("note"):
            md.append(f"- 说明：{cov['note']}")
        if (facts.get("metadata") or {}).get("data_mode") == "synthetic":
            md.extend(_synthetic_coverage_lines(facts.get("time_range") or {}))
            md.append("- **真实业务数据集的配置覆盖范围**（项目常量，本次未使用）："
                      f"{cov.get('min')} 至 {cov.get('max')}")
        md.append("- **请求日期超出覆盖范围时的处理规则**：查询层按数据实际覆盖范围"
                  "**截取**请求窗口，并在计划、证据与报告中记录**有效窗口**与警告；"
                  "若请求窗口与覆盖范围**完全无交集**，则拒绝执行并返回明确错误"
                  "（不补零、不伪造观测）")
    else:
        md.append(f"- {_NOT_AVAILABLE}")
    md.append("")

    # 7 核心结论
    md.append("## 7. 核心结论")
    md.append("")
    evidence = facts.get("evidence", [])
    if not narrative:
        fb_code = (facts.get("metadata") or {}).get("narrative_fallback_reason")
        if fb_code:
            fb_desc = _NARRATIVE_FALLBACK_DESC.get(fb_code, fb_code)
            extra = ""
            if fb_code == "evidence":
                fb_summary = _fallback_detail_summary(
                    (facts.get("metadata") or {}).get("narrative_fallback_detail"))
                if fb_summary:
                    extra = f"｜问题类别：{fb_summary}"
            md.append(f"> 叙述层已回退确定性模板（{fb_desc}）{extra}")
            md.append("")
    if narrative:
        md.append("### 7.1 叙述摘要（受证据约束）")
        md.append("")
        md.append(narrative)
        md.append("")
        if narrative_note:
            md.append(f"> 说明：{narrative_note}")
            md.append("")
        md.append("### 7.2 结论清单（确定性生成）")
        md.append("")
    if not evidence:
        md.append(f"- {_NOT_AVAILABLE}")
    for e in evidence:
        md.append(f"- **[{e['id']}]** {e['text']}")
    md.append("")

    # 8 统计结果与计算口径
    md.append("## 8. 统计结果与计算口径")
    md.append("")
    md.append(f"- 统计方法：`{facts.get('method')}`")
    md.append(f"- 方法参数：`{json.dumps(facts.get('method_params') or {}, ensure_ascii=False)}`")
    stats = facts.get("statistics")
    if stats:
        kind = stats.get("kind")
        md.append(f"- 统计结构类型：`{kind}`")
        if kind == "trend":
            md.append(f"- 数据点数：{len(stats.get('points', []))}")
            if stats.get("additive"):
                md.append(f"- 区间合计：{stats.get('total')}")
                md.append("- 期间日均：不适用（可加指标以区间合计为准）")
            else:
                md.append("- 区间合计：不适用（不可加指标，不跨日求和）")
                md.append(f"- 期间日均：{stats.get('period_mean')}")
                if stats.get("total_note"):
                    md.append(f"- 口径说明：{stats['total_note']}")
        elif kind == "trend_set":
            md.append(f"- 序列数：{len(stats.get('series', []))}"
                      f"（指标：{', '.join(stats.get('metric_ids', []))}）")
            for ser in stats.get("series", []):
                if ser.get("additive"):
                    detail = f"合计 {ser.get('total')}"
                else:
                    detail = f"期间日均 {ser.get('period_mean')}（不可加）"
                md.append(f"  - `{ser.get('metric_id')}` / "
                          f"{ser.get('group') or '全部'}："
                          f"{len(ser.get('points', []))} 点，{detail}")
        elif kind in ("ranking", "ranking_set"):
            series = stats.get("series") or [stats]
            for rk in series:
                md.append(f"- 指标 `{rk.get('metric_id')}` 排名前 3：")
                for entry in rk.get("entries", [])[:3]:
                    md.append(f"  - 第 {entry['rank']} 名 {entry['group']}："
                              f"{entry['value']}（占比 {entry.get('share')}）")
        elif kind == "window_comparison":
            md.append(f"- 主窗口：{_fmt_range(stats.get('main_window'))}"
                      f"（实际元数据：{stats.get('main_window_from_effective')}）")
            md.append(f"- 对比窗口：{_fmt_range(stats.get('comparison_window'))}"
                      f"（实际元数据：{stats.get('comparison_window_from_effective')}）")
            md.append(f"- 配对分组数：{len(stats.get('pairs', []))}")
            if stats.get("alignment") == "day_of_month":
                _append_cross_year_daily_table(md, stats)
    else:
        md.append(f"- {_NOT_AVAILABLE}")
    md.append("")

    # 9 图表
    md.append("## 9. 图表与说明")
    md.append("")
    charts = facts.get("charts", [])
    if charts:
        for c in charts:
            md.append(f"- `{c['path']}`（数值与第 7、8 节一致）")
    else:
        md.append(f"- {_NOT_AVAILABLE}（未生成图表）")
    md.append("")

    # 10 关键数据证据
    md.append("## 10. 关键数据证据")
    md.append("")
    if evidence:
        md.append("| 证据 ID | 指标 | 单位 | 查询名称 | 实际日期范围 | 数据点 | 公式 | 证据强度 |")
        md.append("|---|---|---|---|---|---|---|---|")
        for e in evidence:
            date_text = (_fmt_range(e.get("date_range")) if e.get("date_range")
                         else ("主窗口 " + _fmt_range(e.get("main_window"))
                               + "；对比窗口 " + _fmt_range(e.get("comparison_window"))
                               if e.get("main_window") or e.get("comparison_window")
                               else "无法确认"))
            md.append(f"| {e['id']} | {e['metric_name_zh']}（`{e['metric_id']}`） | "
                      f"{e['unit']} | `{e['query_name']}` | {date_text} | "
                      f"{e['data_points']} | {e['formula']} | {e['evidence_strength']} |")
    else:
        md.append(f"- {_NOT_AVAILABLE}")
    md.append("")

    # 11 数据质量警告
    md.append("## 11. 异常、缺失值与数据质量警告")
    md.append("")
    warnings = facts.get("warnings") or []
    if warnings:
        for w in warnings:
            md.append(f"- {w}")
    else:
        md.append("- 无额外警告")
    md.append("")

    # 12 可能的解释与待验证假设
    md.append("## 12. 可能的解释与待验证假设")
    md.append("")
    hypotheses = [e for e in evidence
                  if e.get("evidence_strength") == EvidenceStrength.HYPOTHESIS.value]
    md.append("- 本报告仅包含由查询结果直接计算或观察得到的结论；"
              "**不包含经数据验证的因果解释**。")
    if hypotheses:
        for e in hypotheses:
            md.append(f"- 待验证假设（证据强度：hypothesis_to_verify）：{e['text']}")
    else:
        md.append("- 无待验证假设（现有数据不足以区分原因）。")
    md.append("")

    # 13 结论局限
    md.append("## 13. 结论局限")
    md.append("")
    limitations = facts.get("limitations") or []
    for lim in limitations:
        md.append(f"- {lim}")
    md.append("- 细粒度统计可能存在小样本披露风险；系统**未实现 k-匿名小样本抑制**，"
              "不提供正式的统计披露控制保证。")
    md.append("")

    # 14 复现信息
    md.append("## 14. 本次分析的复现信息")
    md.append("")
    md.append(f"- 原始问题：`{facts.get('question')}`")
    plan = facts.get("plan") or {}
    if plan:
        md.append(f"- 分析类型：`{plan.get('analysis_type')}`")
        md.append(f"- 指标：`{[m.get('metric_id') for m in plan.get('metrics', [])]}`")
        md.append(f"- 统计操作：`{plan.get('statistics')}`")
        md.append(f"- 分组维度：`{plan.get('group_by')}`")
        tr = plan.get("time_range") or {}
        md.append(f"- 计划时间范围：`{tr.get('type')}` "
                  f"（{tr.get('resolved_start_date')} 至 {tr.get('resolved_end_date')}）")
    qs = (facts.get("execution_summary") or {}).get("queries") or []
    if qs:
        md.append("- 查询清单：")
        for q in qs:
            md.append(f"  - `{q['name']}`：状态 `{q['status']}`，"
                      f"{q['row_count']} 行，窗口 {q.get('window_start')} 至 "
                      f"{q.get('window_end')}（标签 {q.get('window_label')}）")
    narrative_src = meta.get("narrative_source") or "deterministic"
    # 模型名只来自实际生效配置；无法确定时显示“未记录”（不猜测、不硬编码）
    model_name = meta.get("llm_model") or "未记录"
    reason_code = meta.get("narrative_fallback_reason")
    reason_label = (NARRATIVE_FALLBACK_REASONS.get(reason_code, reason_code)
                    if reason_code else None)
    reason_desc = _NARRATIVE_FALLBACK_DESC.get(
        reason_code or "", f"输出未通过校验（原因：{reason_label or '未记录'}）")
    narrative_desc = {
        "deterministic": "确定性模板（未调用 LLM 叙述层）",
        "llm": f"LLM 叙述层（模型 `{model_name}`）",
        "llm_rejected_fallback": (
            f"LLM 叙述层（模型 `{model_name}`）**已回退确定性模板**（{reason_desc}）"),
    }.get(narrative_src, narrative_src)
    # 仅对"证据一致性"类失败展示问题类别（不含被拒原文/数字）
    detail_summary = (_fallback_detail_summary(
        meta.get("narrative_fallback_detail")) if reason_code == "evidence" else None)
    if reason_code == "evidence" and detail_summary:
        narrative_desc += f"｜问题类别：{detail_summary}"
    md.append("- 本次**有效运行配置**（系统实际生效的配置，"
              "非 CLI 原始命令行的逐字记录）：")
    md.append(f"  - 模型模式：`{meta.get('model_mode')}`"
              + ("（Mock 解析结果，**非真实模型调用**）"
                 if meta.get("model_mode") == "mock" else "（真实大模型调用）"))
    md.append(f"  - 数据模式：`{meta.get('data_mode')}`"
              + ("（合成测试数据，**非真实业务数据**）"
                 if meta.get("data_mode") == "synthetic" else "（真实业务数据）"))
    md.append(f"  - 报告叙述：{narrative_desc}")
    md.append("- 提示：真实模型调用 ≠ 真实业务数据；两者由上面两行分别说明。")
    md.append("")
    md.append("---")
    md.append("")
    md.append("*本报告由确定性模板基于结构化证据生成；所有数值、日期与单位均来自"
              "实际查询结果与 Python 统计，未经人工改写。*")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

_DEFAULT_NARRATIVE_PROMPT = """你是数据分析报告的文字组织者。只能使用给定证据包中的事实与数字。
要求：
1. 每段陈述必须引用证据 ID（形如 [E001]）；不得引用不存在的 ID。
2. 不得引入证据包之外的任何数字、日期、指标或城市名称。
3. 不得把申购金额称为销售额；不得把相关性说成因果关系；不得把异常点归因于外部事件。
4. 不得给出数据不支持的原因；如需说明只能写“待验证假设”。
5. 用中文输出 3-5 句概述，不要输出表格或 Markdown 标题。"""


def _llm_narrative(llm_narrator, facts: dict, llm_model: str | None = None
                   ) -> tuple[str | None, str | None, list[str], str | None,
                              str | None]:
    """调用 LLM 生成叙述并校验。

    返回 (narrative, note, warnings, fallback_reason, fallback_detail)；
    `fallback_reason` 取值见 `NARRATIVE_FALLBACK_REASONS`（结构化分类），
    `fallback_detail` 为可读细节（**不含密钥**）。

    模型名称必须来自实际配置（`llm_model` 或客户端属性）；不得使用占位名。
    """
    model_name = llm_model or getattr(llm_narrator, "model", None)
    if not model_name:
        return (None, None,
                ["未提供 LLM 模型名称（llm_model / 客户端 model 均为空），"
                 "已跳过叙述层并保留确定性报告"],
                "no_model", "未提供模型名称，未发起模型调用")
    payload = {
        "question": facts.get("question"),
        "method": facts.get("method"),
        "metrics": facts.get("metrics"),
        "evidence": [{k: e[k] for k in ("id", "text", "metric_id", "unit",
                                       "date_range", "main_window",
                                       "comparison_window", "evidence_strength")}
                     for e in facts.get("evidence", [])],
        "limitations": facts.get("limitations"),
        "warnings": facts.get("warnings"),
    }
    try:
        resp = llm_narrator.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": _DEFAULT_NARRATIVE_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False,
                                                       default=str)},
            ],
            temperature=0,
            max_tokens=600,
        )
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        finish_reason = getattr(choice, "finish_reason", None)
    except Exception as e:  # noqa: BLE001 - 叙述失败不影响确定性报告
        return (None, None,
                [f"LLM 叙述调用失败（{type(e).__name__}），已回退确定性报告"],
                "api_error", f"模型 API 调用异常：{type(e).__name__}")

    # ① API 明确表示达到长度上限 → 视为可能被截断（07.6-A）
    if str(finish_reason or "").lower() == "length":
        return (None, None,
                ["LLM 叙述达到生成长度上限（可能被截断），已回退确定性报告"],
                "truncated", "API 返回 finish_reason=length（输出可能被截断）")

    # ② 完整性检查（短但完整则接受，不因过短判失败）
    complete_ok, complete_problems = validate_narrative_completeness(text)
    if not complete_ok:
        return (None, None,
                ["LLM 叙述疑似截断或句子未完成，已回退确定性报告："
                 + "；".join(complete_problems)],
                "incomplete", "；".join(complete_problems))

    # ③ 证据一致性校验
    ok, problems = validate_narrative(text, facts)
    if not ok:
        return (None, None,
                ["LLM 叙述未通过证据校验，已回退确定性报告：" + "；".join(problems)],
                "evidence", "；".join(problems))
    return (text, "该段由 LLM 组织文字，数字与结论均来自上方证据（未新增事实）",
            [], None, None)


def _append_cross_year_daily_table(md: list[str], stats: dict) -> None:
    """跨年同月逐日对比：月度合计 + 每日对齐表 + 对齐/缺失规则说明。"""
    main_total = stats.get("main_total")
    comp_total = stats.get("comparison_total")
    diff = stats.get("total_abs_diff")
    rate = stats.get("total_change_rate")
    rate_note = stats.get("total_rate_note")
    md.append("")
    md.append("#### 跨年同月逐日对齐（月内日期 1—31）")
    md.append("")
    if main_total is not None and comp_total is not None:
        rate_text = (f"{rate:+.2%}" if rate is not None
                     else (rate_note or "不可计算"))
        md.append(f"- 两月合计：主窗口 {main_total}，对比窗口 {comp_total}；"
                  f"月度差额 {diff}，月度同比 {rate_text}")
    paired_total_main = stats.get("paired_main_total")
    paired_total_comp = stats.get("paired_comparison_total")
    if paired_total_main is not None and paired_total_comp is not None:
        md.append(f"- 仅配对日期上的合计：主窗口 {paired_total_main}，"
                  f"对比窗口 {paired_total_comp}"
                  "（与上面的**全窗口合计**口径不同，不可混用）")
    md.append(f"- 配对情况：成功配对 {stats.get('paired_days', 0)} 天"
              f"（主窗口观测 {stats.get('main_days', 0)} 天、"
              f"对比窗口观测 {stats.get('comparison_days', 0)} 天）")
    cov = stats.get("coverage_rate")
    if cov is not None:
        md.append(f"- 配对覆盖率：{cov:.1%}")
    md.append("- 对齐规则：按**月内日期**（1—31）一一对应；"
              "缺失日期不补零、不连线、不参与变化率计算")
    md.append("")
    md.append("| 月内日期 | 主窗口日期 | 主窗口金额 | 对比窗口日期 | 对比金额 | 差额 | 同比变化率 |")
    md.append("|---|---|---|---|---|---|---|")
    for pr in stats.get("pairs", []):
        rate_cell = (f"{pr['change_rate']:+.2%}"
                     if pr.get("change_rate") is not None
                     else (pr.get("rate_note") or "N/A"))
        md.append(f"| {pr.get('align_key') or '—'} | {pr.get('main_date') or '—'} | "
                  f"{pr.get('main_value')} | {pr.get('comparison_date') or '—'} | "
                  f"{pr.get('comparison_value')} | {pr.get('abs_diff')} | {rate_cell} |")
    md.append("")
    only_main = len(stats.get("missing_in_comparison") or [])
    only_comp = len(stats.get("missing_in_main") or [])
    if only_main or only_comp:
        md.append(f"- 未配对说明：{only_main} 天仅主窗口有观测、"
                  f"{only_comp} 天仅对比窗口有观测（未按 0 处理）")
    zero_base = [pr for pr in stats.get("pairs", [])
                 if pr.get("comparison_value") == 0]
    if zero_base:
        md.append(f"- 零分母说明：{len(zero_base)} 天对比窗口金额为 0，"
                  "同比变化率不可计算（N/A），不生成虚假百分比")
    md.append("")


def generate_report(agent_result, output_dir=None,
                    llm_narrator=None, llm_model: str | None = None) -> ReportResult:
    """生成 Markdown 报告 + JSON 证据文件。"""
    out_dir = Path(output_dir) if output_dir else visualization.DEFAULT_OUTPUT_DIR
    try:
        facts = build_report_facts(agent_result)
    except Exception as e:  # noqa: BLE001
        return ReportResult(status="failed", error=f"报告事实构建失败：{e}")

    if facts.get("error"):
        return ReportResult(status="failed", error=facts["error"])

    narrative = None
    narrative_note = None
    warnings: list[str] = []
    source = "deterministic"
    fallback_reason: str | None = None
    fallback_detail: str | None = None
    if llm_narrator is not None:
        (narrative, narrative_note, warn,
         fallback_reason, fallback_detail) = _llm_narrative(
            llm_narrator, facts, llm_model)
        warnings.extend(warn)
        source = "llm" if narrative else "llm_rejected_fallback"

    facts["metadata"]["narrative_source"] = source
    facts["metadata"]["narrative_fallback_reason"] = fallback_reason
    facts["metadata"]["narrative_fallback_detail"] = fallback_detail
    try:
        markdown = _render_markdown(facts, narrative, narrative_note)
    except Exception as e:  # noqa: BLE001
        return ReportResult(status="failed", error=f"Markdown 渲染失败：{e}")

    stem = "report_" + re.sub(r"[^\w\u4e00-\u9fa5-]+", "_",
                              agent_result.question)[:40].strip("_")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"{stem}.md"
        md_path.write_text(markdown, encoding="utf-8")
        ev_path = out_dir / f"{stem}.evidence.json"
        ev_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return ReportResult(status="failed", error=f"报告写入失败：{e}")

    status = "generated_with_warnings" if (warnings or facts.get("warnings")) else "generated"
    return ReportResult(
        status=status, markdown_path=str(md_path), evidence_path=str(ev_path),
        warnings=warnings, narrative_source=source,
        narrative_fallback_reason=fallback_reason,
        narrative_fallback_detail=fallback_detail,
        evidence_ids=[e["id"] for e in facts.get("evidence", [])])
