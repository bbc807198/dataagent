"""DataAgent 评测模块（阶段 11）。

职责：加载固定测试集与独立核算的标准答案，按案例注入 Fake 模型输出并真实执行
`AnalysisPlan → QueryBuilder → SQLGuard → QueryService → Analytics → ReportGenerator`，
计算查询成功率/答案正确率/正确拒绝率/响应时间分位，并输出结构化日志。

**离线模式衡量的只是"确定性执行链路"，不代表真实模型的自然语言理解准确率。**
"""

from .models import (  # noqa: F401
    CaseResult,
    CheckResult,
    CostInfo,
    EvaluationSummary,
    RunLogRecord,
    TokenUsage,
)
from .runner import EvaluationRunner, load_benchmark  # noqa: F401

__all__ = [
    "CaseResult", "CheckResult", "CostInfo", "EvaluationSummary",
    "RunLogRecord", "TokenUsage", "EvaluationRunner", "load_benchmark",
]
