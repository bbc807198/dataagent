"""Web 层包（阶段 12，本地单用户演示）。

注意：本包只做**展示与编排**，所有分析逻辑都在 `dataagent` 包内；
不复制统计/SQL 逻辑、不执行未验证 SQL、不写数据库。
"""

from .service import (  # noqa: F401
    DATA_MODES,
    MODEL_MODES,
    STATUS_TEXT,
    SyntheticDB,
    build_config,
    data_coverage,
    evidence_rows,
    is_real_mode_configured,
    load_benchmark,
    model_label,
    run_question,
)

__all__ = [
    "DATA_MODES", "MODEL_MODES", "STATUS_TEXT",
    "SyntheticDB", "build_config", "data_coverage", "evidence_rows",
    "is_real_mode_configured", "load_benchmark", "model_label", "run_question",
]
