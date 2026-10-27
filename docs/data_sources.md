# 数据来源、脱敏与使用说明

## 一句话

仓库随附的是**经授权发布的非真实业务演示数据，已进行脱敏改造，仅用于技术演示、研究与学习。**
数据不代表任何主体的真实经营情况；请勿用于商业决策、对外披露或任何对外承诺。

## 概念区分：Mock/Real 与 Synthetic/演示数据库

为避免误解，先澄清两组常被混淆的概念：

| 维度 | 选项 | 含义 |
|---|---|---|
| **模型模式** | Mock | 使用**事先准备好的分析计划**运行真实的 SQL/统计/报告链路；不调用大模型 API，不能自由理解任意中文问题 |
| | Real | 调用真实 LLM（需自备 API Key）理解问题并产出计划 |
| **数据模式** | Synthetic（合成测试数据） | `tests/fixtures/synthetic/` 下**人工构造的小样本**，仅用于 Mock 内置场景；**不是**真实业务数据，也不来自任何真实数据集 |
| | 演示数据库（Demo Database） | 仓库随附 `demo_data/*.parquet` 经 **`scripts/build_demo_db.py --verify`** 离线构建的 DuckDB；属**经授权脱敏的非真实业务演示数据** |

要点：

- 默认体验（Mock + Synthetic）**无需 API Key、无需任何外部数据**；
- 演示数据库的数据**不是独立随机生成的**，而是由授权数据**脱敏改造**而来，保留了业务恒等式
  （如总赎回 = 消费 + 转出），因此金额未缩放、未扰动；
- 所有数据均**不代表任何主体的真实经营情况**，仅用于技术演示、研究与学习。

## 1. 数据文件清单

随仓库发布（`demo_data/`）：

| 文件 | 大小 | 内容 | SHA-256（见 `demo_data/SHA256SUMS.txt`） |
|---|---|---|---|
| `user_balance_table.parquet` | ≈ 29.3 MB | 用户每日资金余额与变动（2,840,421 行） | `c3462ee6…`（以清单为准） |
| `user_profile_table.parquet` | ≈ 0.1 MB | 用户静态画像（28,041 行） | 见清单 |
| `mfd_day_share_interest.parquet` | < 0.1 MB | 基金日收益率（427 行） | 见清单 |
| `mfd_bank_shibor.parquet` | < 0.1 MB | SHIBOR 利率（294 行） | 见清单 |

**权威校验清单**：`demo_data/SHA256SUMS.txt`（逐文件完整 SHA-256）。
校验命令：

```powershell
python scripts\build_demo_db.py --verify
```

或（跨平台）：

```bash
cd demo_data && sha256sum -c SHA256SUMS.txt
```

## 2. 表结构与覆盖范围

| 表 | 行数 | 主键/维度 | 覆盖范围 |
|---|---|---|---|
| `user_balance_table` | 2,840,421 | `user_id` + `report_date` | 2013-07-01 ~ 2014-08-31（427 天） |
| `user_profile_table` | 28,041 | `user_id` | 静态快照 |
| `mfd_day_share_interest` | 427 | `mfd_date` | 2013-07-01 ~ 2014-08-31 |
| `mfd_bank_shibor` | 294 | `mfd_date` | 2013-07-01 ~ 2014-08-29（仅工作日） |

### 字段与类型

`user_balance_table`（全部为整数金额/计数，`report_date` 为 DATE）：

```
user_id BIGINT, report_date DATE, tBalance BIGINT, yBalance BIGINT,
total_purchase_amt BIGINT, direct_purchase_amt BIGINT, purchase_bal_amt BIGINT,
purchase_bank_amt BIGINT, total_redeem_amt BIGINT, consume_amt BIGINT,
transfer_amt BIGINT, tftobal_amt BIGINT, tftocard_amt BIGINT, share_amt BIGINT,
category1 BIGINT, category2 BIGINT, category3 BIGINT, category4 BIGINT
```

`user_profile_table`：`user_id BIGINT, sex BIGINT, city VARCHAR, constellation VARCHAR`
（`city` 为**数字编码**，无官方名称映射；`constellation` 为中文星座枚举）

`mfd_day_share_interest`：`mfd_date DATE, mfd_daily_yield DOUBLE, mfd_7daily_yield DOUBLE`

`mfd_bank_shibor`：`mfd_date DATE` + 期限利率列
（`Interest_O_N`、`Interest_1_W`、`Interest_2_W`、`Interest_1_M`、`Interest_3_M`、
`Interest_6_M`、`Interest_9_M`、`Interest_1_Y`）

## 3. 指标口径（与代码一致）

| 指标 | 口径 |
|---|---|
| 申购金额 | `SUM(total_purchase_amt)`；总申购 = 直接购买 + 收益再投（**资金流口径，非商品销售额**） |
| 赎回金额 | `SUM(total_redeem_amt)`；总赎回 = 消费 + 转出（资金流口径） |
| 净资金流入 | `SUM(total_purchase_amt) - SUM(total_redeem_amt)` |
| 每日收益率 | `mfd_daily_yield` 原值序列（不做跨行汇总） |
| 七日年化收益率 | `mfd_7daily_yield` 原值序列 |
| SHIBOR | 指定期限的原值序列（期限由参数 `term` 指定：`O_N/1W/2W/1M/3M/6M/9M/1Y`） |
| 用户画像总人数 | `COUNT(DISTINCT user_id)`（静态快照） |
| 期间去重用户数 | 时间窗口内出现过资金记录的去重用户数 |

## 4. 脱敏改造说明

| 项目 | 处理 | 原因 |
|---|---|---|
| `user_id` | **稳定哈希重编号**（SHA-256 排序后映射为 1..N，映射可复现） | 切断与原始数据集中用户编号的可关联性；聚合分析不受影响（每用户每天至多一条记录） |
| `city` / `sex` / `constellation` | 保留（原始脱敏数据中的**编码与枚举**，无姓名、联系方式或地址） | 分类维度分析需要；本身不含直接标识 |
| 金额字段 | **未缩放、未扰动** | 保持业务恒等式（总赎回 = 消费 + 转出；总申购 = 直接购买 + 收益再投；余额连续性）与已核对基准（2014-08 申购合计 7,921,353,364 元） |
| 时间 | 保留原始日期 | 趋势/同比/收益率分析需要 |
| 其它 | 未发布任何日志、视图、中间表、原始备份 | 仓库只含四张业务表数据 |

> **关于"非真实业务数据"**：该数据集本身即为公开可得并经授权使用的**非真实业务演示数据**
> （脱敏后的模拟资金流水），不是任何现实机构的经营数据。本仓库的改造仅去除可关联标识。

> **脱敏局限（重要）**：第 4 节中的 `user_id` 重编号为**可复现的哈希映射**（并非不可逆匿名化），
> 映射关系在掌握方法后可被还原；本项目**未实现 k-匿名 / 最小分组抑制**，细粒度分组
> （如个别城市 × 星座）仍存在披露风险。请勿将任何统计结果用于对外披露或再识别推断。

## 5. 构建与使用

```powershell
# 离线构建（不联网、不需要任何外部下载）
python scripts\build_demo_db.py --verify

# 产物（本地生成，已被 .gitignore 排除）
data/processed/dataagent_demo.duckdb
```

- 默认数据库路径由 `src/dataagent/config.py` 从**项目相对路径**解析；
- 想用自己的库：设置环境变量 `DATAAGENT_DB_PATH=/path/to/your.duckdb`；
- 查询连接**始终只读**；不允许通过界面执行任意 SQL。

## 6. 授权与使用限制

1. 本数据经权利人授权发布，仅用于**技术演示、研究与学习**；
2. 不得用于商业决策、对外披露、任何形式的业绩承诺或再分发中的署名主张；
3. 转发/引用时请保留本文件与数据出处说明；
4. 项目代码以 MIT 许可发布；数据授权条款以本节为准（与代码许可相互独立）。

## 7. 校验与复现

- 数据完整性：仓库内测试 `tests/test_demo_data_delivery.py` 会校验 SHA-256 清单、四表存在性、
  关键字段、覆盖范围、只读性与 2014-08 口径基准；
- 一键重建：删除 `data/processed/dataagent_demo.duckdb` 后重新执行建库脚本即可复现同样的库。
