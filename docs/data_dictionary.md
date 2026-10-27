# 数据字典

> 依据：阶段一审计报告（2026-10-24，已审核）与第二阶段导入实测。
> 单位约定：所有金额字段为**元**（整数口径，已审计无小数）；日期为 DATE。
> 语义确认状态分三级：✅ 已由审计恒等式确认；⚠️ 部分确认/有条件；❓ 未确认，需业务数据字典。

## 1. user_balance_table（用户×日期资金流水，2,840,421 行）

粒度：每用户每天至多 1 条（`(user_id, report_date)` 唯一，已审计 0 重复）。
用户 28,041 个；日期 427 天（2013-07-01 ~ 2014-08-31，无缺失）。
业务含义：货币基金平台（余额宝类）用户的申购/赎回/消费/转出流水，**不是商品销售数据**。

| 字段 | 类型 | 业务含义 | 单位/粒度 | 缺失值特征 | 语义状态 |
|---|---|---|---|---|---|
| user_id | BIGINT | 用户标识（1~28,041 连续） | 用户 | 无缺失 | ✅ |
| report_date | DATE | 资金记录日期（源为 YYYYMMDD 整数，已规范化） | 日 | 无缺失、无重复 | ✅ |
| tBalance | BIGINT | 账户余额（口径待确认，疑为总余额） | 元 | 无缺失；0 值 52,719 行；范围 0~104,285,082 | ❓ 语义未确认 |
| yBalance | BIGINT | 账户余额（口径待确认） | 元 | 无缺失；0 值 64,742 行 | ❓ 语义未确认 |
| total_purchase_amt | BIGINT | 总申购 = 直接购买 + 收益再投 | 元 | 无缺失；0 值 900,058 行 | ✅ 恒等式确认 |
| direct_purchase_amt | BIGINT | 直接购买 = 余额购 + 银行卡购 | 元 | 无缺失 | ✅ |
| purchase_bal_amt | BIGINT | 余额申购（从账户余额购买） | 元 | 无缺失 | ✅ |
| purchase_bank_amt | BIGINT | 银行卡申购 | 元 | 无缺失 | ✅ |
| total_redeem_amt | BIGINT | 总赎回 = 消费 + 转出 | 元 | 无缺失 | ✅ |
| consume_amt | BIGINT | 消费（支付） | 元 | 无缺失；0 值 2,666,682 行 | ✅ |
| transfer_amt | BIGINT | 转出 = 到余额 + 到银行卡 | 元 | 无缺失 | ✅ |
| tftobal_amt | BIGINT | 转出到余额 | 元 | 无缺失 | ✅ |
| tftocard_amt | BIGINT | 转出到银行卡 | 元 | 无缺失 | ✅ |
| share_amt | BIGINT | 收益再投（万份收益×份额折算的元金额；全期合计 6.88 亿，占申购 0.74%） | 元 | 无缺失；0 值 943,768 行 | ✅ 恒等式确认 |
| category1~4 | BIGINT | 消费分项金额；consume=category1+2+3+4 | 元 | **设计性空值**：consume=0 时四列全为空（2,666,682 行）；consume>0 时（173,739 行）全部非空 | ✅ 空值为业务特征，非数据错误 |

已知恒等式（原始金额精度，284 万行逐行 0 违例）：
1. `total_redeem_amt = consume_amt + transfer_amt`
2. `transfer_amt = tftobal_amt + tftocard_amt`
3. `direct_purchase_amt = purchase_bal_amt + purchase_bank_amt`
4. `consume_amt = category1+2+3+4`（category 空值按 0）
5. `total_purchase_amt = direct_purchase_amt + share_amt`

诊断项（非断言）：`tBalance(t) − tBalance(t−1) = total_purchase(t) − total_redeem(t)`
在同一用户连续活跃日上成立，2,795,780 个连续日过渡中仅 1 例违例（残差 100 元）。
（连续日过渡数经 DuckDB 窗口函数与独立 oracle 双向核实；早期 pandas 流式口径因假设文件按用户有序而少计 1,323，已弃用。）

## 2. user_profile_table（用户属性，28,041 行）

| 字段 | 类型 | 业务含义 | 缺失值特征 | 语义状态 |
|---|---|---|---|---|
| user_id | BIGINT | 用户标识，唯一（PK） | 无缺失 | ✅ |
| sex | BIGINT | 性别编码（0/1 二值；0=1,352？语义以数据字典为准） | 无缺失；取值 1（14,489）/ 0（13,552） | ⚠️ 编码方向未确认 |
| city | VARCHAR(16) | 城市**类别编码**（7 个值：6081949/6281949/6301949/6411949/6412149/6481949/6581949）；**不可作数值运算，无官方名称映射** | 无缺失 | ❓ 编码→名称映射缺失 |
| constellation | VARCHAR(16) | 星座（12 个值） | 无缺失 | ✅ |

## 3. mfd_day_share_interest（货币基金收益，427 行）

| 字段 | 类型 | 业务含义 | 单位 | 缺失值特征 | 语义状态 |
|---|---|---|---|---|---|
| mfd_date | DATE | 日期（PK，YYYYMMDD 已规范化） | 日 | 无缺失、无重复；覆盖资金表全部 427 天 | ✅ |
| mfd_daily_yield | DOUBLE | 万份收益 | 元/万份 | 无缺失；1.0993~1.822 | ✅ |
| mfd_7daily_yield | DOUBLE | 七日年化收益率 | % | 无缺失；4.116~6.763 | ✅ |

## 4. mfd_bank_shibor（Shibor 市场利率，294 行）

| 字段 | 类型 | 业务含义 | 单位 | 缺失值特征 | 语义状态 |
|---|---|---|---|---|---|
| mfd_date | DATE | 日期（PK） | 日 | 无重复；**仅工作日有观测**（294/427 天；2013-07-01~2014-08-29） | ✅ |
| Interest_O_N … Interest_1_Y | DOUBLE | 隔夜/1周/2周/1月/3月/6月/9月/1年 Shibor | % | 294 行内无缺失 | ✅ |

填充规则：缺失日期（周末/节假日及数据截止后的 2014-08-30/31）在分析视图
`v_mfd_bank_shibor_daily_filled` 中按 **LOCF**（此前最近有效观测）填充，
携带 `source_obs_date` 与 `is_filled`；原始表不改动。
**SHIBOR 最后实际观测日为 2014-08-29**；2014-08-30/31 的填充值来源即该日。

## 5. 不导入的表

`comp_predict_table.csv`：无表头、3 行常数占位（2014-09-01~09-03，申购 40000000/赎回 30000000），
为比赛提交模板，**不作为业务数据**，不参与任何资金流统计。
