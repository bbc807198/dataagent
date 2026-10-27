"""澄清机制（第四阶段）。

当问题存在实质性歧义时，生成面向用户的中文澄清问题；
对已经足够明确的问题不追加无谓追问。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import metric_catalog as mc


@dataclass
class ClarificationResult:
    needed: bool
    missing_info: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)     # 面向用户的中文问题
    confirmed: list[str] = field(default_factory=list)     # 已识别且可靠的部分
    unconfirmed: list[str] = field(default_factory=list)   # 暂时无法确认的部分

    def as_dict(self) -> dict:
        return {
            "needed": self.needed,
            "missing_info": self.missing_info,
            "questions": self.questions,
            "confirmed": self.confirmed,
            "unconfirmed": self.unconfirmed,
        }


# 常见业务概念 → 现有指标的映射提示（防止把申购/赎回说成销售额）
_CONCEPT_HINTS: dict[str, str] = {
    "销售额": "当前数据仅包含资金申购/赎回流水，没有经确认的商品销售额指标。"
              "是否改为分析申购金额（资金流入）或赎回金额（资金流出）？",
    "销量": "当前数据仅包含资金流水，没有商品销量指标。是否改为分析申购金额？",
    "收入": "当前数据为货币基金资金流，没有营业收入指标。是否改为分析申购金额？",
    "利润": "当前数据为货币基金资金流，没有利润指标。是否改为分析净资金流入？",
}


def detect_concept_mismatch(question: str) -> str | None:
    """检测用户问题中的业务概念是否与现有指标不匹配。"""
    for concept, hint in _CONCEPT_HINTS.items():
        if concept in question:
            return hint
    return None


def build_clarification(
    question: str,
    validation_errors: list[str] | None = None,
    plan=None,
) -> ClarificationResult:
    """根据解析结果构造澄清。

    优先级：
    1. 业务概念不匹配（如销售额）→ 指标替换建议；
    2. Pydantic 校验错误 → 转成中文澄清问题；
    3. 计划层面的缺失（指标/时间/维度）→ 定向追问。
    """
    errors = list(validation_errors or [])
    result = ClarificationResult(needed=False)

    # 1) 业务概念不匹配
    hint = detect_concept_mismatch(question)
    if hint:
        result.needed = True
        result.missing_info.append("经确认的分析指标")
        result.questions.append(hint)
        result.unconfirmed.append("用户所指指标与现有数据口径的对应关系")

    # 2) 校验错误转换
    if errors:
        result.needed = True
        result.missing_info.append("可校验的分析计划要素")
        for e in errors[:5]:
            result.questions.append(f"解析结果存在问题：{e}。请补充或修正后重试。")

    # 3) 计划层面的缺失
    if plan is not None:
        if not plan.metrics:
            result.needed = True
            result.missing_info.append("分析指标")
            result.questions.append(
                "请指明要分析的指标，例如申购金额、赎回金额、净资金流入、收益率或 SHIBOR 利率。")
        if not plan.time_range.is_resolved and not plan.time_range.relative_period:
            result.needed = True
            result.missing_info.append("时间范围")
            result.questions.append(
                "请指明时间范围，例如“最近三个月”“2014 年 6 月至 8 月”或“全期”。")
        if (plan.analysis_type.value == "group_ranking" and not plan.group_by):
            result.needed = True
            result.missing_info.append("分组维度")
            result.questions.append(
                "请指明按什么维度排名（可选：城市编码、性别类别、星座类别、日期）。")
        # 已识别且可靠的部分
        if plan.metrics:
            result.confirmed.append(
                "指标：" + "、".join(mc.get_metric(m.metric_id).name_zh for m in plan.metrics))
        if plan.time_range.relative_period:
            result.confirmed.append(f"时间表述：{plan.time_range.relative_period}")
        if plan.group_by:
            result.confirmed.append(
                "分组维度：" + "、".join(mc.dimension_label(d) for d in plan.group_by))

    return result
