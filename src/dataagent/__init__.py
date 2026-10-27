"""dataagent：AI 数据分析 Agent 的工程骨架与数据底座（阶段二）。

当前已实现：
- config：路径解析（动态推导 + 环境变量覆盖，不硬编码用户目录）
- db：DuckDB 连接、四张业务表 Schema、显式重建策略
- ingestion：原始 CSV 校验与导入、日期规范化、导入后质量检查
- data_contracts：阶段一审计结论固化为契约断言（原始金额精度）
- shibor：SHIBOR 前向填充分析视图（ASOF/LOCF）与逐格验证

尚未实现（后续阶段）：自然语言理解、SQL 生成与安全校验、Python 统计分析、
图表与报告生成、大模型解释层。
"""

from . import config  # noqa: F401

__version__ = "0.7.1"

__all__ = ["config", "__version__"]
