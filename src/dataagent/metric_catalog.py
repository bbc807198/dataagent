"""指标与数据字典目录（机器可读，第四阶段）。

以 `docs/data_dictionary.md`（阶段一审计结论）与 `db.py` 的实际 Schema 为依据，
为自然语言解析层提供**封闭的指标/维度白名单**——大模型只能引用本目录中的条目，
不得发明字段或指标。

关键业务口径（阶段一已审计确认）：
- 申购/赎回是**资金流**口径，不得称为"销售额"；
- `city` 是 7 个数字编码，**无官方名称映射**，禁止虚构城市名称；
- `tBalance`/`yBalance` 语义未确认，**不启用**相关业务指标；
- SHIBOR 仅工作日有原始观测（294/427 天），截止 **2014-08-29**；
  分析应使用 `v_mfd_bank_shibor_daily_filled` 前向填充视图；
- 相对时间范围以**数据集实际最大日期**为锚点（当前数据截止 2014 年），
  不以系统当前日期为锚点。

目录与实际 Schema 的一致性由 `tests/test_metric_catalog.py` 固化。
"""

from __future__ import annotations

from dataclasses import dataclass

from .sql_guard import TABLE_COLUMNS

# ---------------------------------------------------------------------------
# 数据集日期范围（阶段一审计实测；查询层可用只读连接动态复核）
# ---------------------------------------------------------------------------

DATA_DATE_MIN = "2013-07-01"
DATA_DATE_MAX = "2014-08-31"
SHIBOR_LAST_OBS_DATE = "2014-08-29"   # SHIBOR 最后实际观测日

# 允许的分组维度（字段 → 来源表）。user_id 不是维度。
ALLOWED_DIMENSIONS: dict[str, str] = {
    "report_date": "user_balance_table",
    "mfd_date": "mfd_day_share_interest",
    "city": "user_profile_table",
    "sex": "user_profile_table",
    "constellation": "user_profile_table",
}

# 维度中文名（用于澄清问题与报告表述）
DIMENSION_LABELS: dict[str, str] = {
    "report_date": "日期",
    "mfd_date": "日期",
    "city": "城市编码",
    "sex": "性别类别",
    "constellation": "星座类别",
}


# ---------------------------------------------------------------------------
# 类别字段取值域（任务 05-A：机器可读，来自数据字典与实际数据审计）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CategoryDomain:
    """类别字段的已知取值域。

    - values：规范化字符串表示（比较用）；
    - value_type：SQL 参数绑定的 Python 类型（"int"/"str"）；
    - semantics_confirmed：编码的业务含义是否已经确认（未确认时只校验编码合法性，
      不得在报告/澄清中虚构含义）。
    """

    name: str
    values: frozenset[str]
    value_type: str          # "int" | "str"
    semantics_confirmed: bool
    notes: str

    def normalize(self, v) -> str | None:
        """把输入值规范化为字符串；不在域内返回 None。"""
        if isinstance(v, bool):
            return None
        s = str(v).strip()
        return s if s in self.values else None

    def to_binding(self, v):
        """转换为参数绑定值（按 value_type）。"""
        s = self.normalize(v)
        if s is None:
            raise ValueError(f"类别字段 {self.name} 的值 {v!r} 不在已知取值域内")
        return int(s) if self.value_type == "int" else s

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "values": sorted(self.values),
            "value_type": self.value_type,
            "semantics_confirmed": self.semantics_confirmed,
            "notes": self.notes,
        }


# 实际取值来自阶段一审计与本地数据库只读 DISTINCT 实测（2026-10-25 复核）。
CATEGORY_DOMAINS: dict[str, CategoryDomain] = {
    "city": CategoryDomain(
        name="city",
        values=frozenset({
            "6081949", "6281949", "6301949", "6411949",
            "6412149", "6481949", "6581949",
        }),
        value_type="str",
        semantics_confirmed=True,   # 编码取值已确认；但**无官方名称映射**
        notes="7 个数字类别编码（非连续数值，不得当作数值排序或运算）；"
              "无官方城市名称映射，禁止虚构城市名称。",
    ),
    "sex": CategoryDomain(
        name="sex",
        values=frozenset({"0", "1"}),
        value_type="int",
        semantics_confirmed=False,  # 编码方向的业务含义未确认
        notes="已观测编码 0 与 1；编码方向（何者为男/女）的业务含义尚未确认，"
              "不得在输出或报告中虚构。",
    ),
    "constellation": CategoryDomain(
        name="constellation",
        values=frozenset({
            "白羊座", "金牛座", "双子座", "巨蟹座", "狮子座", "处女座",
            "天秤座", "天蝎座", "射手座", "摩羯座", "水瓶座", "双鱼座",
        }),
        value_type="str",
        semantics_confirmed=True,   # 12 个实际取值（数据库 DISTINCT 实测）
        notes="12 个星座类别，取值为数据库中实际存在的 12 个中文类别。",
    ),
}

# SHIBOR 期限 → 视图列名
SHIBOR_TERM_COLUMNS: dict[str, str] = {
    "O_N": "Interest_O_N", "1W": "Interest_1_W", "2W": "Interest_2_W",
    "1M": "Interest_1_M", "3M": "Interest_3_M", "6M": "Interest_6_M",
    "9M": "Interest_9_M", "1Y": "Interest_1_Y",
}


@dataclass(frozen=True)
class MetricDefinition:
    """指标定义（白名单条目）。"""

    metric_id: str
    name_zh: str
    table: str
    fields: tuple[str, ...]
    unit: str
    aggregation: str            # SUM / AVG / RAW（原值）
    formula: str                # 人类可读的计算规则
    time_field: str
    allowed_dimensions: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    restrictions: tuple[str, ...]
    semantics_confirmed: bool
    enabled: bool = True        # 语义未确认的指标不启用
    additive: bool = True       # 跨日是否可加总（False=比率/快照/每日去重计数）
    additivity_note: str = ""   # 不可加时的口径说明

    def as_dict(self) -> dict:
        return {
            "metric_id": self.metric_id,
            "name_zh": self.name_zh,
            "table": self.table,
            "fields": list(self.fields),
            "unit": self.unit,
            "aggregation": self.aggregation,
            "formula": self.formula,
            "time_field": self.time_field,
            "allowed_dimensions": list(self.allowed_dimensions),
            "allowed_operations": list(self.allowed_operations),
            "restrictions": list(self.restrictions),
            "semantics_confirmed": self.semantics_confirmed,
            "enabled": self.enabled,
            "additive": self.additive,
            "additivity_note": self.additivity_note,
        }


# 常用统计操作集合（按指标类型区分）
_MONEY_OPS = ("SUM", "AVG", "MIN", "MAX", "COUNT",
              "PERIOD_OVER_PERIOD", "CHANGE_RATE", "WINDOW_DIFF", "TOP_N")
_RATE_OPS = ("AVG", "MIN", "MAX", "PERIOD_OVER_PERIOD", "CHANGE_RATE", "TOP_N")

METRIC_CATALOG: dict[str, MetricDefinition] = {
    "purchase_amount": MetricDefinition(
        metric_id="purchase_amount",
        name_zh="申购金额",
        table="user_balance_table",
        fields=("total_purchase_amt",),
        unit="元",
        aggregation="SUM",
        formula="总申购 = 直接购买 + 收益再投（资金流口径，不是商品销售额）",
        time_field="report_date",
        allowed_dimensions=("report_date", "city", "sex", "constellation"),
        allowed_operations=_MONEY_OPS,
        restrictions=(
            "资金流入口径，不得称为销售额；",
            "须以聚合方式使用（SUM/AVG 等），不得导出逐条用户记录。",
        ),
        semantics_confirmed=True,
    ),
    "redeem_amount": MetricDefinition(
        metric_id="redeem_amount",
        name_zh="赎回金额",
        table="user_balance_table",
        fields=("total_redeem_amt",),
        unit="元",
        aggregation="SUM",
        formula="总赎回 = 消费 + 转出（资金流口径）",
        time_field="report_date",
        allowed_dimensions=("report_date", "city", "sex", "constellation"),
        allowed_operations=_MONEY_OPS,
        restrictions=("资金流出口径，不得称为销售额或退款；",),
        semantics_confirmed=True,
    ),
    "net_inflow": MetricDefinition(
        metric_id="net_inflow",
        name_zh="净资金流入",
        table="user_balance_table",
        fields=("total_purchase_amt", "total_redeem_amt"),
        unit="元",
        aggregation="SUM",
        formula="净流入 = SUM(total_purchase_amt) - SUM(total_redeem_amt)",
        time_field="report_date",
        allowed_dimensions=("report_date", "city", "sex", "constellation"),
        allowed_operations=_MONEY_OPS,
        restrictions=("派生指标，依赖申购与赎回两个字段；",),
        semantics_confirmed=True,
    ),
    "daily_yield": MetricDefinition(
        metric_id="daily_yield",
        name_zh="每日收益率（万份收益）",
        table="mfd_day_share_interest",
        fields=("mfd_daily_yield",),
        unit="元/万份",
        aggregation="RAW",
        formula="原值序列（不做跨行汇总；按日均值可作趋势参考）",
        time_field="mfd_date",
        allowed_dimensions=("mfd_date", "report_date"),
        allowed_operations=_RATE_OPS,
        restrictions=(
            "市场指标，非用户级数据；",
            "与资金流关联时按 mfd_date = report_date 对齐；"
            "此时可使用 report_date 作为分组维度（JOIN 保证对齐）。",
        ),
        semantics_confirmed=True,
        additive=False,
        additivity_note="每日收益率为比率指标，跨日相加无业务含义；可计算期间平均值",
    ),
    "seven_day_yield": MetricDefinition(
        metric_id="seven_day_yield",
        name_zh="七日年化收益率",
        table="mfd_day_share_interest",
        fields=("mfd_7daily_yield",),
        unit="%",
        aggregation="RAW",
        formula="原值序列",
        time_field="mfd_date",
        allowed_dimensions=("mfd_date", "report_date"),
        allowed_operations=_RATE_OPS,
        restrictions=("市场指标，非用户级数据；与资金流关联时可用 report_date 对齐分组。",),
        semantics_confirmed=True,
        additive=False,
        additivity_note="七日年化收益率为比率指标，跨日相加无业务含义；可计算期间平均值",
    ),
    "shibor_rate": MetricDefinition(
        metric_id="shibor_rate",
        name_zh="SHIBOR 利率",
        table="v_mfd_bank_shibor_daily_filled",
        fields=tuple(SHIBOR_TERM_COLUMNS.values()),
        unit="%",
        aggregation="RAW",
        formula="指定期限利率原值序列（期限由参数 term 指定：O_N/1W/2W/1M/3M/6M/9M/1Y）",
        time_field="mfd_date",
        allowed_dimensions=("mfd_date", "report_date"),
        allowed_operations=_RATE_OPS,
        restrictions=(
            "必须通过 term 参数指定期限；",
            "SHIBOR 仅工作日有原始观测，最后实际观测日 2014-08-29；"
            "2014-08-30/31 由 08-29 前向填充，涉及截止日的分析需披露；",
            "应使用前向填充视图 v_mfd_bank_shibor_daily_filled。",
        ),
        semantics_confirmed=True,
        additive=False,
        additivity_note="SHIBOR 为利率指标，跨日相加无业务含义；可计算期间平均值",
    ),
    "user_count": MetricDefinition(
        metric_id="user_count",
        name_zh="用户画像总人数（静态快照）",
        table="user_profile_table",
        fields=("user_id",),
        unit="人",
        aggregation="COUNT_DISTINCT",
        formula="COUNT(DISTINCT user_id)——用户画像表中的去重总人数（静态快照，"
                "不代表任何时间窗口内的活跃用户数）",
        time_field="",   # 用户画像表无日期字段
        allowed_dimensions=("city", "sex", "constellation"),
        allowed_operations=("COUNT_DISTINCT", "COUNT"),
        additive=False,
        additivity_note="用户画像为静态快照，不存在跨日求和语义",
        restrictions=(
            "**静态口径**：用户画像表无日期字段，统计结果不代表任何时间段的用户数；",
            "只允许 full_range（全期）计划；若用户指定了有限时间窗口，"
            "须改用 active_user_count（期间去重用户数）或请求澄清；",
            "只允许去重计数口径（COUNT(DISTINCT user_id)），不得输出或分组 user_id。",
        ),
        semantics_confirmed=True,
    ),
    "active_user_count": MetricDefinition(
        metric_id="active_user_count",
        name_zh="期间去重用户数",
        table="user_balance_table",
        fields=("user_id",),
        unit="人",
        aggregation="COUNT_DISTINCT",
        formula="在指定时间窗口内出现过资金记录（资金表 report_date 范围内）的"
                "去重用户数：COUNT(DISTINCT b.user_id)，同一用户多天出现只计一次",
        time_field="report_date",
        allowed_dimensions=("report_date", "city", "sex", "constellation"),
        allowed_operations=("COUNT_DISTINCT", "COUNT"),
        additive=False,
        additivity_note="每日去重用户数不可跨日相加；跨日求和得到的是"
                        "“每日用户计数的累计”，不是期间去重用户总数",
        restrictions=(
            "**期间口径**：必须提供有限时间窗口（起止日期），不得用 COUNT(*) 代替去重计数；",
            "按城市/性别/星座分组时经已审核的 user_id JOIN 关联用户画像；",
            "同一用户多天出现只计一次（COUNT DISTINCT）。",
        ),
        semantics_confirmed=True,
    ),
    "balance": MetricDefinition(
        metric_id="balance",
        name_zh="账户余额（语义未确认）",
        table="user_balance_table",
        fields=("tBalance", "yBalance"),
        unit="元",
        aggregation="RAW",
        formula="未定义——tBalance/yBalance 的业务语义尚未经数据字典确认",
        time_field="report_date",
        allowed_dimensions=(),
        allowed_operations=(),
        restrictions=(
            "tBalance/yBalance 语义未确认（与 share_amt 的差值关系不稳定）；",
            "本指标未启用，不得用于分析计划。",
        ),
        semantics_confirmed=False,
        enabled=False,
    ),
}


# ---------------------------------------------------------------------------
# 访问接口
# ---------------------------------------------------------------------------

def get_metric(metric_id: str) -> MetricDefinition:
    """按 ID 取指标定义；未知或未启用指标抛 KeyError/ValueError。"""
    if metric_id not in METRIC_CATALOG:
        raise KeyError(f"未知指标: {metric_id}")
    m = METRIC_CATALOG[metric_id]
    if not m.enabled:
        raise ValueError(f"指标 {metric_id} 未启用（业务语义未确认）")
    return m


def list_available_metrics() -> list[MetricDefinition]:
    return [m for m in METRIC_CATALOG.values() if m.enabled]


def is_known_dimension(field_name: str) -> bool:
    return field_name in ALLOWED_DIMENSIONS


def dimension_label(field_name: str) -> str:
    return DIMENSION_LABELS.get(field_name, field_name)


def shibor_column(term: str) -> str:
    if term not in SHIBOR_TERM_COLUMNS:
        raise KeyError(f"未知 SHIBOR 期限: {term}（可选 {sorted(SHIBOR_TERM_COLUMNS)}）")
    return SHIBOR_TERM_COLUMNS[term]


def validate_catalog_against_schema() -> list[str]:
    """核对指标目录引用的表/字段与 sql_guard 的 Schema 白名单一致。

    返回问题列表（空 = 一致）。由测试调用，防止目录与 Schema 漂移。
    """
    problems: list[str] = []
    for m in METRIC_CATALOG.values():
        if m.table not in TABLE_COLUMNS:
            problems.append(f"{m.metric_id}: 表 {m.table} 不在 Schema 白名单")
            continue
        for f in m.fields:
            if f not in TABLE_COLUMNS[m.table]:
                problems.append(f"{m.metric_id}: 字段 {f} 不在 {m.table} 的 Schema 中")
        for d in m.allowed_dimensions:
            if d not in ALLOWED_DIMENSIONS:
                problems.append(f"{m.metric_id}: 维度 {d} 不在允许维度表")
            elif ALLOWED_DIMENSIONS[d] not in TABLE_COLUMNS:
                problems.append(f"{m.metric_id}: 维度 {d} 的来源表不在白名单")
    return problems
