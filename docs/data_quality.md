# 数据质量报告

> 本报告记录 DuckDB 数据底座的实际导入结果与质量验证。
> 实测数字来自 `scripts/audit_database.py` 与 `pytest` 的真实运行结果；下表数值与已审核的审计结论一致。

## 1. 导入结果

| 表 | 期望行数 | 实际行数 | 日期范围 | 主键唯一性 |
|---|---:|---:|---|---|
| user_balance_table | 2,840,421 | 2,840,421 | 2013-07-01 ~ 2014-08-31（427 天无缺失） | (user_id, report_date) 0 重复 |
| user_profile_table | 28,041 | 28,041 | 不适用（无日期字段） | user_id 唯一 |
| mfd_day_share_interest | 427 | 427 | 2013-07-01 ~ 2014-08-31 | mfd_date 唯一 |
| mfd_bank_shibor | 294 | 294 | 2013-07-01 ~ 2014-08-29（仅工作日） | mfd_date 唯一 |

## 2. 关键质量指标

- **负值**：四个表的金额/余额列均无负值（0 行）。
- **缺失**：14 个数值列 0 缺失；`category1~4` 空值当且仅当 `consume_amt=0`（设计性空值，
  2,666,682 行），`consume>0` 的 173,739 行全部非空。
- **整数口径**：两个金额列经逐块校验全部为整数（元），按 BIGINT 存储。
- **重复键**：0。
- **日期格式**：源数据为 YYYYMMDD 整数字符串，导入时规范化为 DATE；未规范化直接做
  字符串日期比较会静默出错（阶段二实测教训）。

## 3. 关联检查

| 关联 | 结果 |
|---|---|
| balance.user_id ↔ profile.user_id | 28,041/28,041 **100% 双向匹配**，无孤儿；1:1，关联不膨胀 |
| balance.report_date ↔ yield.mfd_date | 427/427 **100%** 覆盖 |
| balance.report_date ↔ shibor.mfd_date | 294/427（68.9%）；133 个日期无 SHIBOR 观测（周末/节假日 + 截止后两天） |

## 4. 业务恒等式（原始金额精度，逐行 0 违例）

1. `total_redeem_amt = consume_amt + transfer_amt`
2. `transfer_amt = tftobal_amt + tftocard_amt`
3. `direct_purchase_amt = purchase_bal_amt + purchase_bank_amt`
4. `consume_amt = category1+2+3+4`（category 空值按 0）
5. `total_purchase_amt = direct_purchase_amt + share_amt`

## 5. SHIBOR 前向填充

- 视图 `v_mfd_bank_shibor_daily_filled`：427 行（脊柱范围 = 资金表实际日期范围，动态推导）。
- 填充 133 行 / 原始观测 294 行；全部填充值经独立 LOCF 重算**逐格一致**（含 pandas 交叉验证）。
- 来源日期间隔：最短 1 天、最长 7 天、平均 1.79 天；无任何未来观测被用于过去填充。
- **数据截止日：SHIBOR 最后实际观测 2014-08-29**；2014-08-30/31 由该日承接（`is_filled=1`，
  `source_obs_date=2014-08-29`）。
- 明确不使用累计最大值（cummax）替代：实测 cummax 在全部 133 个填充日均产生错误值
  （最差例：2014-02-22 应填 1.764，cummax 给 5.23——该值仅 2013-10-30 观测过，陈旧 115 天）。

## 6. 已知局限（分析时必须披露）

1. `tBalance`/`yBalance` 语义未确认（差值≠share_amt 稳定成立，残差可达 ±9,726 万）；
   连续日余额流恒等式有 1 例 100 元残差（诊断项，非核心断言）。
2. `city` 为 7 个数字编码，无名称映射；城市级结论只能以编码呈现。
3. 数据高度集中：Top5% 用户占 67.4% 申购 / 72.4% 赎回；均值类指标需配中位数/去尖峰口径。
4. 宏观相关为趋势型伪相关：收益率/SHIBOR 与日度净流入一阶差分后 |r|≤0.16；
   不能把相关性当因果。
5. 2014-08-30/31 无 SHIBOR 观测（承接 08-29），涉及跨截止日的利率分析需注意。
6. `sex` 编码方向（0/1 哪个性别）未确认。
7. 金额展示规则：原始精度计算，亿元两位小数展示，列间独立舍入可有 ±0.01 亿差异。

## 7. 复现方式

```powershell
.\.venv\Scripts\python.exe scripts\build_database.py
.\.venv\Scripts\python.exe scripts\audit_database.py --json data\processed\audit_result.json
.\.venv\Scripts\python.exe -m pytest
```
