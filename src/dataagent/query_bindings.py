"""SQL 绑定参数的可公开展示（白名单）。

安全约定：

1. **执行方式不变**：SQL 仍使用 `?` 参数化绑定，这里只记录"已经绑定好的值"，
   绝不改变执行路径，也绝不把值拼回 SQL。
2. **白名单制**：只有下列标签允许展示，其余一律隐藏（`value=None`）。
3. **不编造**：若某个参数没有可靠来源（例如未记录标签），显示"未记录"，
   绝不用用户原始问题反推。
4. **不展示敏感值**：API Key、用户级标识（user_id 等）永远不在白名单内。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# 允许公开的**字面标签**（与 query_builder 生成的 param_labels 对应）
PUBLIC_PARAM_LABELS = frozenset({"start_date", "end_date", "limit"})

# 允许公开的**筛选字段**（类别/画像维度，取值为编码或枚举，不含个人身份）
PUBLIC_FILTER_FIELDS = frozenset({"city", "sex", "constellation"})

_HIDDEN = {"public": False, "kind": "hidden", "value": None}


def _is_date_like(value: Any) -> bool:
    if isinstance(value, (date, datetime)):
        return True
    if isinstance(value, str):
        # 严格日期形状：YYYY-MM-DD
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            return all(value[i].isdigit() for i in (0, 1, 2, 3, 5, 6, 8, 9))
    return False


def _is_date_label(label: str) -> bool:
    return label in ("start_date", "end_date") or label.endswith("_start_date") \
        or label.endswith("_end_date")


def public_bindings(param_labels: list[str] | None,
                    params: list | None) -> list[dict]:
    """按白名单生成可展示的绑定参数列表。

    返回每项形如：
        {"position": 1, "label": "start_date", "kind": "date",
         "value": "2024-01-02", "public": True}
    非白名单项：{"position": n, "label": ..., "kind": "hidden",
         "value": None, "public": False}
    """
    if params is None:
        return []
    labels = list(param_labels or [])
    out: list[dict] = []
    for idx, value in enumerate(params, 1):
        label = labels[idx - 1] if idx - 1 < len(labels) else None
        if not label:
            # 无标签来源 → 不推断，仅标记"未记录"
            out.append({"position": idx, "label": None, "kind": "unrecorded",
                        "value": None, "public": False})
            continue
        if _is_date_label(label):
            # 日期类：必须是日期形状，否则不公开（不猜测）
            if _is_date_like(value):
                shown = value.isoformat() if isinstance(value, (date, datetime)) \
                    else str(value)
                out.append({"position": idx, "label": label, "kind": "date",
                            "value": shown, "public": True})
            else:
                out.append({"position": idx, "label": label, "kind": "hidden",
                            "value": None, "public": False})
            continue
        if label == "limit":
            # 行数上限：整数且非负即可公开（非敏感）
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out.append({"position": idx, "label": label, "kind": "limit",
                            "value": int(value), "public": True})
            else:
                out.append({"position": idx, "label": label, "kind": "hidden",
                            "value": None, "public": False})
            continue
        if label in PUBLIC_PARAM_LABELS:
            out.append({"position": idx, "label": label, "kind": "hidden",
                        "value": None, "public": False})
            continue
        if label.startswith("filter:"):
            field = label.split(":", 1)[1]
            if field in PUBLIC_FILTER_FIELDS:
                out.append({"position": idx, "label": label, "kind": "filter",
                            "value": str(value), "public": True})
            else:
                out.append({"position": idx, "label": label, "kind": "hidden",
                            "value": None, "public": False})
            continue
        out.append({"position": idx, "label": label, **_HIDDEN})
    return out
