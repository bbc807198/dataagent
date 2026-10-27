# DataAgent 本地网页使用指南

本指南说明如何使用 **本地单用户** Streamlit 界面（阶段 12）。所有命令在 **Windows PowerShell** 下验证；
Linux/macOS 将 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python` 即可。

> 适用范围：**本机单人使用**。本界面**没有**登录鉴权、会话隔离、限流与正式隐私保护，
> **不要**直接暴露到公网或多用户环境。

## 1. 创建虚拟环境

```powershell
# 在项目根目录
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
```

## 2. 安装依赖（网页界面 + 开发测试）

本指南面向 **Streamlit 网页界面**，请一次性安装网页 + 开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
```

> · 仅需命令行 / 开发测试（不使用网页界面）：`.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`
> · 已装 `.[dev]` 后仅补装 Streamlit：`.\.venv\Scripts\python.exe -m pip install -e ".[web]"`

## 3. 启动与关闭

```powershell
# 启动（默认 http://localhost:8501，浏览器一般会自动打开）
.\.venv\Scripts\python.exe -m streamlit run app.py

# 指定端口
.\.venv\Scripts\python.exe -m streamlit run app.py --server.port 8502

# 关闭：在启动的终端窗口按 Ctrl + C
```

页面结构：

| 区域 | 内容 |
|---|---|
| 左侧栏 | 模型模式（Mock/Real）、数据模式（Synthetic/Project DB）、当前模型与数据范围、功能说明、安全提示 |
| 数据分析 | 问题输入、示例问题、运行按钮、执行状态、结论/图表/报告/SQL/证据、下载按钮 |
| Benchmark | 读取已有离线评测结果：四项比率、P50/P95、Token 与成本、分类表现、失败案例 |
| 项目信息 | 架构流程、与「LLM 直接生成 SQL」的区别、技术栈、数据来源与边界 |

## 4. 无需 API Key 的体验（默认路径）

打开页面即为 **Mock + Synthetic**，不需要 Key、不需要业务数据：

1. 在「示例问题」下拉中选择一条（Mock 模式只支持这些内置演示场景）；
2. 点击 **运行分析**；
3. 查看结论、图表、报告、SQL 与证据表，可下载 Markdown 报告与 JSON 证据。

示例场景（共 8 个，与 `scripts/demo_agent.py` 的 DEMOS 同源）：

| 场景 | 问题 | 预期结果 |
|---|---|---|
| 【正常分析】多指标趋势 | 帮我分析申购和赎回金额最近的趋势。 | `report_generated`（报告 + 图表 + 证据） |
| 【正常分析】城市窗口对比 | 比较最近几天和之前几天各城市的净资金流入。 | `report_generated`（两窗口对比） |
| 【正常分析】城市排名 | 各城市申购金额排名。 | `report_generated`（排名 + 占比） |
| 【正常分析】异常识别 | 申购金额有没有异常日期？ | `report_generated`（MAD 稳健异常） |
| 【正常分析】描述性汇总 | 观察期内申购金额的总体情况。 | `report_generated`（汇总） |
| 【边界演示】不支持指标 | 帮我分析销售额的变化。 | **预期** `unsupported_metric`（拒绝） |
| 【边界演示】需澄清 | 分析一下。 | **预期** `needs_clarification`（追问） |
| 【边界演示】数据不足 | 分析 2024-01-02 当天的申购金额趋势。 | **预期** `insufficient_data` |

> 以上 8 个场景是 `scripts/demo_agent.py` 中 `DEMOS` 的**唯一数据源**派生物：
> Web 页面、命令行演示与本表内容一致。Mock 采用**精确匹配**（仅容忍空格与句末标点差异），
> 片段输入（如"申购""趋势"）不会被识别。

> **Mock 模式不代表真实语言理解能力**：它把问题匹配到内置演示场景的计划。
> 输入与示例无关的问题会提示"未匹配到演示场景"，请改用 Real 模式。

## 4.1 跨年同月逐日对比

在 Real 模式下可直接提问：`帮我分析2013年8月每天与2014年8月每天的资金申购对比`。
系统会按**月内日期（1—31）**对齐两个年份的 8 月，生成双折线图与逐日同比表；
缺失日期不补零，无可配对日期时显示"数据不足"。

## 5. 配置 DeepSeek（或其他 OpenAI 兼容服务）

```powershell
# 当前 PowerShell 会话有效；新窗口需重设
# 安全输入（Key 不进入命令历史，也不会显示在屏幕上；兼容 PowerShell 5.1 与 7）
$secure = Read-Host -AsSecureString "请输入 API Key"
$env:DATAAGENT_LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
$env:DATAAGENT_LLM_BASE_URL = "https://api.deepseek.com"
$env:DATAAGENT_LLM_MODEL    = "<服务商文档中的模型名>"

# 重新启动页面（环境变量只在启动时读取）
.\.venv\Scripts\python.exe -m streamlit run app.py
```

然后在侧边栏把「模型模式」切换到 **Real**。此时：

- 每次点击 **运行分析** 才会发起**一次**模型调用；
- 页面顶部会提示"真实模式会产生 API 费用"；
- 未配置 Key 时页面显示明确错误，不会静默回退成 Mock 结果；
- 页面刷新**不会**重复调用（结果缓存在会话中，只有点击按钮才运行）。

## 6. 连接只读数据库

侧边栏「数据模式」选择 **项目自带演示数据库（只读）**。该模式只使用**项目固定路径**
（`data/processed/dataagent_demo.duckdb`，可用 `DATAAGENT_DB_PATH` 覆盖），
**页面不提供任意路径输入，也不支持远程连接**。

```powershell
# 若尚未构建演示数据库（克隆后推荐，使用仓库自带的经授权脱敏演示数据，离线、无需外部下载）
.\.venv\Scripts\python.exe scripts\build_demo_db.py --verify
```

> 该命令使用仓库自带的 `demo_data/*.parquet`（**经授权的非真实业务演示数据**）离线构建，
> **无需手动导入任何原始数据**。手动从原始授权 CSV 自建（`scripts\build_database.py` + `scripts\audit_database.py`）
> 属于**高级选项**，仅面向持有原始授权数据、要重建自有库的用户，并非使用本项目的必经步骤。

安全约束：

- 数据库以**只读连接**打开，页面提供的任何操作都不会写库；
- 页面上的 SQL 是**参数化模板**（值以 `?` 占位），**只能查看，不能编辑执行**；
- 所有查询仍经过 `SQLGuard` 与 `QueryService`，页面无法绕过。

## 7. 查看 Benchmark

1. 先在命令行生成/更新评测结果：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_agent.py
.\.venv\Scripts\python.exe scripts\verify_golden.py
```

2. 页面左侧选择 **Benchmark**，会读取 `artifacts/evaluation/latest/` 的最近一次结果。

页面展示：执行成功率 / 答案正确率 / 行为正确率 / 正确拒绝率、P50/P95 响应时间、
Token 与成本（离线为 `N/A`）、各分析类型表现、失败案例清单。

> 页面明确标注：**离线评测为确定性执行链路评测，不代表真实模型（DeepSeek）的
> 自然语言理解准确率**；Mock 模式的费用不会被显示成真实调用成本。

## 8. 常见错误与解决

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'streamlit'` | 未安装 web 依赖 | `.\\.venv\\Scripts\\python.exe -m pip install -e ".[web]"` |
| 页面显示"未配置模型：真实模式需要 DATAAGENT_LLM_API_KEY" | Real 模式缺 Key | 设置环境变量后**重启**页面，或切回 Mock |
| 页面显示"Mock 模式只支持内置演示场景" | Mock 不能理解任意问题 | 从示例问题中选择，或切换 Real 模式 |
| 页面显示"项目数据库不存在，请先构建" | 未生成 `data/processed/dataagent_demo.duckdb` | 运行 `python scripts\build_demo_db.py --verify`，或切回 Synthetic |
| 端口 8501 被占用 | 已有实例在运行 | 关闭旧窗口，或用 `--server.port 8502` |
| 中文图表显示方框/英文 | 缺少中文字体 | 安装中文字体（如 Microsoft YaHei/SimHei）；不影响统计与报告 |
| 运行后状态为"需要补充信息" | 问题缺指标/时间范围 | 按页面提示补充（如明确指出指标与日期） |
| 运行后状态为"数据不足" | 时间范围过窄或无数据 | 扩大时间范围 |
| 状态为"已安全拒绝" | 生成的 SQL 未通过安全校验 | 属预期保护，调整问题；页面不会执行该 SQL |
| 页面报"依赖损坏"（`h11`/`jiter` 缺失等） | 安装过程被中断 | 按 README「依赖损坏与虚拟环境重建」重建 `.venv` |

> **Warning 不等于致命错误**：数据质量警告（如 SHIBOR 前向填充、窗口被截取）
> 显示在"数据质量警告"折叠区，报告仍可正常使用。

## 9. 避免泄露 API Key

1. **不要**把 Key 写进 `app.py`、测试、文档或任何会提交的文件；
2. 只用环境变量（当前会话）注入，关闭终端即失效；
3. `.env` 已被 `.gitignore` 排除，但项目**不会自动读取 `.env`**——改用环境变量；
4. 页面不会显示 Key、数据库原始用户记录或异常堆栈中的敏感内容；
5. 分享截图时注意不要截到终端里的 `set/$env:` 命令；
6. 若怀疑泄露，立即在服务商后台吊销并重新生成。

## 10. 相关文档

- [README](../README.md)：项目全景、架构、安全与许可证
- [docs/user_guide.md](./user_guide.md)：命令行三种使用模式与故障排查
- [docs/benchmark.md](./benchmark.md)：评测口径与复现命令
- [docs/architecture.md](./architecture.md)：组件调用关系（含 Web 层）
