# 真实模型 Web 手工验收指南

> **本文是给项目负责人手工执行的验收清单**，不是自动化测试结果。
> 执行方（AI 助手）在本阶段**没有调用真实 API**，因此下列各项标记为「负责人手工验证」或「尚未验证」。
> 仅凭历史报告**不能**宣称新版 Web 已通过真实模型验收。

## 0. 前提

| 项 | 要求 |
|---|---|
| Python | 3.11+（本项目在 3.13 验证） |
| 依赖 | `pip install -e ".[dev,web]"` |
| API Key | 任意 OpenAI 兼容服务（如 DeepSeek）的 Key，**不要**写进任何文件 |
| 网络 | 能访问所配置的 `DATAAGENT_LLM_BASE_URL` |

## 1. 配置环境变量（安全步骤）

```powershell
# 1) 在项目根目录打开 PowerShell（不要用管理员权限，也不要把 Key 写进脚本）
cd <你的克隆目录>

# 2) 仅当前会话生效（关闭窗口即失效）
$secure = Read-Host -AsSecureString "请输入 API Key"
$env:DATAAGENT_LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
# ↑ 兼容 Windows PowerShell 5.1 与 PowerShell 7；避免 Key 留在命令历史里

$env:DATAAGENT_LLM_BASE_URL = "https://api.deepseek.com"      # 按服务商文档填写
$env:DATAAGENT_LLM_MODEL    = "<服务商文档中的模型名>"

# 3) 自查（只打印是否已设置，不打印值）
if ($env:DATAAGENT_LLM_API_KEY) { "KEY: 已设置" } else { "KEY: 未设置" }
"BASE_URL: $env:DATAAGENT_LLM_BASE_URL"
"MODEL: $env:DATAAGENT_LLM_MODEL"
```

> **不要**把 Key 写进 `.env`（项目不会读取）、源码、测试或截图。
> 提交前用 `git status` 确认没有 `.env`。

## 2. 启动 Streamlit

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 <http://localhost:8501>。关闭：在启动的终端按 `Ctrl + C`。

## 3. Real + Synthetic 标准测试（先跑这个）

侧边栏：**模型模式 = Real**，**数据模式 = 合成测试数据**。

| 步骤 | 问题 | 期望 |
|---|---|---|
| 3.1 | `分析2024年1月2日至2024年1月12日的申购和赎回趋势` | 状态"已完成，报告已生成"；两条指标序列；含 `[E00x]` 证据 |
| 3.2 | `比较最近几天和之前几天各城市的净资金流入` | 窗口对比报告，含 `main_window`/`comparison_window` |
| 3.3 | `帮我分析销售额的变化` | **被拒绝**（`unsupported_metric` / `plan_rejected`），**无成功报告** |

**成功判定条件（全部满足才算通过）**：

1. 状态为"已完成，报告已生成"（或带警告的完成）；
2. 报告包含 14 节结构，核心结论每条带 `[E00x]`；
3. 证据表行数与报告引用 ID 一致（页面"证据"表格）；
4. 图表至少 1 张且在页面可显示；
5. SQL 区块显示**参数化模板**（含 `?` 占位），不含具体日期/数值字面量；
6. 下载的 Markdown 与页面显示内容一致；下载的 JSON 含 `evidence` 数组。

**需要核对的统计值（合成数据）**：

| 指标 | 期望值 |
|---|---|
| 申购金额合计（2024-01-02~01-12） | **2,850.00 元** |
| 赎回金额合计 | **1,850.00 元** |
| 净资金流入合计 | **1,000.00 元** |
| 数据点数 | 11（每日一条） |
| 最大降幅 | 2024-01-05，−100.00%（前值 200 → 本期 0） |
| 窗口对比（主 0108-0112 / 对比 0102-0107）申购 | 1,750 / 1,100 |

> 若真实模型给出的计划与上述参数不同（例如自己选了别的时间范围），
> 数值会相应变化——此时以**报告第 5 节的执行窗口**为准，核对"窗口内合计"是否自洽。

## 4. Real + Project DB 标准测试（需自备数据库）

侧边栏：**模型模式 = Real**，**数据模式 = 项目自带演示数据库（只读）**。

前提：已按 README 运行 `python scripts\build_demo_db.py --verify` 构建 `data/processed/dataagent_demo.duckdb`（原始授权的资金流入流出数据不随仓库发布）。

| 步骤 | 问题 | 期望 |
|---|---|---|
| 4.1 | `分析2014年8月1日至2014年8月31日的申购和赎回趋势` | 成功；执行窗口为 2014-08-01~08-31 |
| 4.2 | `比较7月和8月各城市的净资金流入` | 窗口对比；城市为**数字编码**（无名称映射） |
| 4.3 | `各城市申购金额排名` | 排名结果，含占比说明 |

**需要核对的统计值（演示数据库）**：

| 项 | 参考值 | 状态 |
|---|---|---|
| 数据覆盖 | 2013-07-01 至 2014-08-31（427 天） | 已由阶段一审计记录 |
| 2014-08 月申购合计 | **7,921,353,364 元（约 79.21 亿元）** | 来源：负责人 2014-08 真实模型 + Project DB 手工验收记录；**本阶段未复核** |
| 2014-08 月赎回合计 | **7,770,619,181 元（约 77.71 亿元）** | 同上 |
| 查询结果日期数 | **31** 个（2014-08-01 至 2014-08-31 每日一条） | 同上 |
| 城市编码 | 7 个数字编码（无官方名称映射） | 阶段一审计 |
| SHIBOR | 仅工作日；08-30/08-31 由 08-29 前向填充（报告会标注） | 阶段一审计 |

> ⚠️ **本表不再把"85.6 / 85.3 亿元"作为验收标准**。该数值来自早期内部推算，
> 与负责人 2014-08 实测值（79.21 / 77.71 亿元）差额为**申购 6.39 亿元、赎回 7.59 亿元**（相对差异约 8%），**原因尚未确认**，
> 已标记为**待核实**（原因尚未确认，需在真实数据上后续复核）。
>
> **判断方法（不依赖上表）**：
> 1. 看报告"实际执行窗口"是否为 2014-08-01 至 2014-08-31（31 个数据点）；
> 2. 看"区间合计"是否与报告内图表/证据一致（自洽即可，不要求等于某个外部数字）；
> 3. 需要交叉验证时，请用**独立 SQL**（不经本系统）核对：
>
> ```sql
> -- 申购合计（含直接购买与收益再投）
> SELECT SUM(total_purchase_amt) FROM user_balance_table
>  WHERE report_date BETWEEN DATE '2014-08-01' AND DATE '2014-08-31';
> -- 赎回合计
> SELECT SUM(total_redeem_amt) FROM user_balance_table
>  WHERE report_date BETWEEN DATE '2014-08-01' AND DATE '2014-08-31';
> -- 日期数
> SELECT COUNT(DISTINCT report_date) FROM user_balance_table
>  WHERE report_date BETWEEN DATE '2014-08-01' AND DATE '2014-08-31';
> ```
>
> 若上式结果与页面报告不同，请把这三条 SQL 的结果（非敏感聚合值）提供给执行方复盘。

## 5. 失败时需要保存的信息（非敏感）

出现异常时，请保存：

1. 页面状态文字与错误类别（如 `llm_not_configured`、`plan_rejected`、`timeout`）；
2. 问题原文（**可公开的示例问题**，不要贴含业务机密的真实问题）；
3. 报告的 `.md` 与 `.evidence.json`（若已生成）；
4. 终端最后 20 行输出（**注意**：若终端里有 `set/$env:` 命令，先删掉再分享）；
5. 时间戳与所配置的 `MODEL` / `BASE_URL` 域名（**不要**贴 Key）。

**不要保存/上传**：API Key、`.env`、数据库文件、原始 CSV、含用户级记录的查询结果、终端截图中的密钥。

## 6. 观察调用次数与费用

| 观察点 | 方法 |
|---|---|
| 调用次数（Web） | 每次点击「运行分析」= 1 次解析调用；若模型输出不合规，会追加**1 次** Schema 修复重试 |
| 叙述层 | 页面默认**不启用** LLM 叙述层（不产生额外调用） |
| 离线评测的调用记录 | `artifacts/evaluation/latest/run_log.jsonl` 的 `request_count` / `response_count` / `token_usage` |
| 真实评测预算 | `python scripts/evaluate_agent.py --real --limit 5 --max-requests 5`（超出即 `skipped_budget`） |
| 账单核对 | 以服务商控制台为准；本项目只记录可观察 token，无价格表时成本显示 `N/A` |

## 7. 不得上传或公开的文件

- `Purchase Redemption Data/`（原始授权的资金流入流出数据）
- `data/processed/*.duckdb`（本地数据库）
- `.env`、任何含 Key 的脚本或笔记
- `artifacts/`（生成的报告/图表/评测结果/审查包）
- 终端截图里出现 `$env:DATAAGENT_LLM_API_KEY = ...` 的画面

## 8. API Key 泄露时的处理

1. **立即**到服务商控制台吊销该 Key；
2. 生成新 Key，并只在本地会话中设置；
3. 检查是否已写入文件或提交：`git log -p | Select-String "sk-"`（本仓库目前**零提交**）；
4. 若已推送到远端，需重写历史（`git filter-repo`）并强制推送——**本项目当前未推送**；
5. 复核账单是否有异常调用。

## 9. Windows PowerShell 编码问题与独立核对脚本

### 9.1 为什么会出现 `????`

在 Windows PowerShell 里把**中文 Python 代码通过管道**传给解释器时，例如：

```powershell
"print('申购合计')" | python -
```

可能出现标签显示成 `????`。原因是**控制台代码页（code page）与管道编码**不匹配
（PowerShell 5.1 默认 GBK/936，而 Python 期望 UTF-8），属于**显示 / 传输层的编码问题**，
**与数据计算结果无关**：

- 数字（金额、天数）仍然正确，只是**中文标签**被替换成了 `?`；
- 换终端（Windows Terminal / cmd / WSL）、执行 `chcp 65001`、
  或把代码写进 `.py` 文件再执行，都不会出现该现象；
- 因此**看到 `????` 不等于计算错误**，请以数字为准。

### 9.2 推荐：用英文标签的独立核对脚本

仓库提供 `scripts/verify_month_aggregate.py`：**只读**打开 DuckDB、只执行 `SELECT`、
输出**纯 ASCII 标签**（任何终端都不乱码），并且**使用完整请求日期窗口**（不做截断）。

```powershell
# 2014 年 8 月申购合计（示例）
.\.venv\Scripts\python.exe scripts\verify_month_aggregate.py `
    --db data\processed\dataagent_demo.duckdb `
    --start 2014-08-01 --end 2014-08-31 --metric purchase

# 其他口径：--metric redeem（赎回） / --metric net（净流入）
```

输出示例（标签全英文）：

```
metric            : purchase
date_from         : 2014-08-01
date_to           : 2014-08-31
total_amount      : <合计金额>
distinct_dates    : <日期数>
covered_by_db     : yes
db_date_min       : 2013-07-01
db_date_max       : 2014-08-31
sql               : SELECT COALESCE(SUM(total_purchase_amt), 0) ...
note              : read-only query; full requested window used (no truncation applied)
```

判读要点：

1. `covered_by_db = yes` 表示请求窗口完全落在数据覆盖范围内；
2. `distinct_dates` 应等于窗口内自然日数（2014-08 为 31）；
3. `total_amount` 应与页面报告的"区间合计"一致；**若不一致**，请把这三项
   （`total_amount` / `distinct_dates` / `covered_by_db`，均为非敏感聚合值）提供给执行方复盘；
4. 请求窗口超出数据覆盖时，脚本**不会**自动改日期，而是给出 `covered_by_db = no`
   并打印库的实际起止日期。

## 10. 验收结论填写模板

```
验收人：____          日期：____
Real + Synthetic：通过 / 未通过（原因：____）
Real + Project DB：通过 / 未通过（原因：____）
核对项：申购合计 ___ / 赎回合计 ___ / 证据数 ___ / 图表数 ___
调用次数（服务商后台）：___      费用：___
备注：____
```
