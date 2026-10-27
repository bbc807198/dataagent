# DataAgent 使用指南

本指南面向**第一次接触本项目**的使用者，提供三种可复制的操作方式与常见故障排查。所有命令均在 **Windows PowerShell** 下验证；Linux/macOS 将 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python` 即可。

> 前置条件：已完成 [README Quick Start](../README.md#quick-start不需要-api-key不需要原始数据) 的安装（虚拟环境 + `pip install -e ".[dev]"`；README 默认的 `.[dev,web]` 已包含 dev 依赖，同样适用本指南）。

---

## 模式 1：无 API Key 快速体验（合成数据 + Mock 模型）

**适用**：想先看看项目能做什么，还没有 API Key，也不想准备业务数据。

```powershell
# 在项目根目录执行
.\.venv\Scripts\python.exe scripts\demo_agent.py
```

会发生什么：

1. 在系统临时目录创建一个小型**合成 DuckDB 数据库**（来自 `tests/fixtures/synthetic/`，演示结束自动删除）；
2. 依次运行 **8 个场景**：**5 个正常分析**（均生成完整报告与图表）+ **3 个边界演示**（预期失败/澄清）：

| 分类 | 场景 | 预期结果 |
|---|---|---|
| 正常分析 | 多指标趋势（申购 + 赎回） | `report_generated`，报告 + JSON 证据 + 趋势图 |
| 正常分析 | 城市净资金流入窗口对比 | `report_generated`，报告 + 证据 + 对比图 |
| 正常分析 | 各城市申购金额排名 | `report_generated`，排名结果（含占比说明） |
| 正常分析 | 申购金额异常识别（MAD） | `report_generated`，稳健异常结果 |
| 正常分析 | 观察期申购金额汇总 | `report_generated`，描述性汇总 |
| 边界演示 | 不支持的指标（销售额） | `unsupported_metric`（拒绝，不生成报告） |
| 边界演示 | 需要澄清（“分析一下。”） | `needs_clarification`，返回中文追问 |
| 边界演示 | 数据不足（单日趋势） | `insufficient_data`，说明无法计算环比 |

3. 产物输出到 `artifacts/generated/`：
   - `report_<问题>.md`：14 节中文报告；
   - `report_<问题>.evidence.json`：与报告 `[E00x]` 一一对应的证据文件；
   - `*.png`：图表。

**失败场景是故意设计的**：它们用于验证系统不会把错误伪装成成功。看到 `plan_rejected` / `insufficient_data` 属于预期行为。

## 模式 2：真实 LLM + 合成数据

**适用**：已有 OpenAI 兼容 API Key（如 DeepSeek），想验证真实模型解析链路，但暂不连接业务数据。

### 2.1 配置环境变量

```powershell
# 当前 PowerShell 会话有效（新窗口需重新设置）
# 安全输入（Key 不进入命令历史，也不会显示在屏幕上；兼容 PowerShell 5.1 与 7）
$secure = Read-Host -AsSecureString "请输入 API Key"
$env:DATAAGENT_LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
$env:DATAAGENT_LLM_BASE_URL = "https://api.deepseek.com"   # 其他 OpenAI 兼容服务填对应地址
$env:DATAAGENT_LLM_MODEL    = "<服务商文档中的模型名>"
```

三个变量的用途：

| 变量 | 用途 | 说明 |
|---|---|---|
| `DATAAGENT_LLM_API_KEY` | API 密钥 | 只从进程环境变量读取；**项目不读取 `.env` 文件**，仅创建 `.env` 不会生效 |
| `DATAAGENT_LLM_MODEL` | 模型名称 | 优先级：调用方显式传入 > 本变量 > 代码默认（兜底） |
| `DATAAGENT_LLM_BASE_URL` | 兼容接口地址 | 默认 `https://api.openai.com/v1`；DeepSeek 等填服务商地址 |

> **禁止把真实密钥写进任何会提交的文件**（源码、测试、示例、文档）。`.env` 已被 `.gitignore` 排除；`.env.example` 只是清单模板。

### 2.2 运行

```powershell
# 真实模型解析 + 临时合成库（无需业务数据）
.\.venv\Scripts\python.exe scripts\demo_agent.py --real --question "分析 2024 年 1 月 2 日至 1 月 12 日的申购和赎回趋势"

# 启用 LLM 叙述层（额外一次模型调用；只允许复述证据，失败自动回退确定性文本）
.\.venv\Scripts\python.exe scripts\demo_agent.py --real --llm-narrative --question "分析 2024 年 1 月 2 日至 1 月 12 日的申购和赎回趋势"
```

报告头部会标注 `模型模式：real（真实大模型调用）` 与 `数据模式：synthetic（合成测试数据，非真实业务数据）`——**真实模型调用 ≠ 真实业务数据**，两者分别标注。

## 模式 3：真实 LLM + 本地项目数据库

**适用**：已有自己的 DuckDB 业务库，要做真实分析。

> **默认演示库无需手动准备**：克隆仓库后运行 `python scripts\build_demo_db.py --verify`
> 即可离线构建随附的演示数据库（经授权的非真实业务演示数据），**无需导入任何原始数据**。
> 下方的「从原始授权数据自建」仅面向持有原始授权数据的高级用户，并非使用本项目的必经步骤。

> **业务数据库由使用者自行准备。** 项目原始授权的资金流入流出数据**不随仓库公开**。

### 3.1 准备数据库

```powershell
# 方式 A：直接使用已有的 DuckDB 文件
.\.venv\Scripts\python.exe scripts\demo_agent.py --db "D:\你的目录\your.duckdb"

# 方式 B：使用项目约定路径（默认 <项目根>/data/processed/dataagent_demo.duckdb，
#         可用 DATAAGENT_DB_PATH 覆盖）
.\.venv\Scripts\python.exe scripts\demo_agent.py --project-db
```

如需从零构建项目自有库（授权原始四表）：将 `user_balance_table.csv`、`user_profile_table.csv`、`mfd_day_share_interest.csv`、`mfd_bank_shibor.csv` 放入 `Purchase Redemption Data/`（`comp_predict_table.csv` 是比赛提交模板，不作为业务数据导入），然后：

```powershell
.\.venv\Scripts\python.exe scripts\build_database.py   # 构建（已存在且含业务表时默认拒绝，--rebuild/--force 显式覆盖）
.\.venv\Scripts\python.exe scripts\audit_database.py   # 契约/恒等式/SHIBOR 填充验证
```

### 3.2 真实模型 + 项目库 + 自定义问题

```powershell
# 先按模式 2 配置好三个环境变量，然后：
.\.venv\Scripts\python.exe scripts\demo_agent.py --real --project-db --question "分析最近三个月各城市的申购和赎回趋势"
```

查询始终以**只读连接**执行；不会修改、重建或删除你的数据库。

---

## 常用命令速查

| 目的 | 命令 |
|---|---|
| 默认演示（无 Key） | `.\.venv\Scripts\python.exe scripts\demo_agent.py` |
| 只看帮助 | `.\.venv\Scripts\python.exe scripts\demo_agent.py --help` |
| 真实模型 + 合成库（**会跑全部 8 个场景**） | `... --real` |
| 真实模型 + 叙述层 | `... --real --llm-narrative` |
| 指定数据库 | `... --db <路径>` 或 `... --project-db` |
| 自定义问题（推荐） | `... --real --question "你的问题"`（单次调用，费用可控） |
| 指定输出目录 | `... --out <目录>`（默认 `artifacts/generated/`） |
| 全部测试 | `.\.venv\Scripts\python.exe -m pytest` |
| 只跑合成测试 | `.\.venv\Scripts\python.exe -m pytest -m "not integration"` |

## 输出解读

```
状态：report_generated                 # 或 report_generated_with_warnings
模型模式：real | 数据模式：synthetic    # 头部与报告均有标注
计划：time_trend | 指标 ['purchase_amount', 'redeem_amount']
查询：1 条 | 12.4 ms
统计：ok / daily_trend / 结论 8 条
报告：artifacts/generated/report_xxx.md
证据：artifacts/generated/report_xxx.evidence.json
图表：artifacts/generated/chart_xxx.png
叙述来源：deterministic                # 或 llm / llm_rejected_fallback（含原因）
```

- **报告与证据的对应**：报告中每条结论前缀 `[E001]`、`[E002]`…，在 `.evidence.json` 的 `evidence` 数组中以 `id` 字段一一对应，含指标、单位、查询名、实际日期范围、数据点数、公式与警告；
- **警告（Warning）不等于失败**：如“SHIBOR 期限利率仅工作日观测，已前向填充”“请求窗口被数据覆盖范围截取”等会记录在报告数据质量节，分析照常完成。

## 状态码与失败语义

| 状态 | 含义 | 处理 |
|---|---|---|
| `report_generated` / `report_generated_with_warnings` | 成功（后者有可继续使用的警告） | 正常阅读报告 |
| `needs_clarification` | 计划缺关键信息 | 按澄清问题补充后重试 |
| `plan_rejected` | 计划不满足白名单/可执行性（如未确认指标、多指标冲突） | 换用受支持的指标或拆分问题 |
| `sql_rejected` | 生成的 SQL 未通过安全校验 | 不会执行；属于失败关闭 |
| `insufficient_data` | 有效数据点不足（如单日趋势无法算环比） | 扩大时间范围 |
| `incompatible_metric` | 请求指标在结果中无法唯一对应 | 检查指标口径冲突 |
| `execution_failed` / `timeout` | 查询失败/超时 | 检查数据库与资源 |
| `llm_not_configured` | `--real` 未配置 API Key | 按模式 2 配置 |

**关于“report_date 被识别为时间轴”的 Warning**：当模型把 `report_date` 同时放进 `group_by` 时，系统会忽略该分组并给出提示。这是**预期的规范化行为**——时间字段本来就是趋势序列的“轴”，若当作分组维度会导致每组只剩一个数据点、有效数据被误判为不足。分析结果不受影响，无需处理。

## 故障排查

| 现象 | 原因与解决 |
|---|---|
| `ModuleNotFoundError: No module named 'dataagent'` | 未安装项目或未用项目虚拟环境。在项目根目录执行 `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"` |
| OpenAI SDK 能导入但无法初始化 | ① 先确认 `DATAAGENT_LLM_BASE_URL` 含协议与路径（如 `https://api.deepseek.com`）且网络/代理可达；② 若报 `ModuleNotFoundError` / `ImportError`（如 `No module named 'h11'`、`No module named 'jiter'`、`cannot import name 'URL' from 'httpx'`），说明依赖安装不完整或已损坏——多为安装过程被中断（磁盘空间不足、Ctrl+C）导致文件截断；③ 修复方式：`.\\.venv\\Scripts\\python.exe -m pip install --force-reinstall "h11" "jiter" "httpx" "httpcore" "anyio"`（或直接重建虚拟环境，见下） |
| API 认证失败（401） | Key 错误或未生效：环境变量只在**当前终端会话**有效，新窗口需重新设置；确认没有多余空格 |
| 模型名称错误（404/model not found） | `DATAAGENT_LLM_MODEL` 与服务商文档不一致；留空则使用代码默认（兜底），但建议按服务商文档显式设置 |
| `[错误] 数据库不存在: ...` | `--db` 路径写错或文件未生成；用 `--project-db`（演示库）前先运行 `python scripts\build_demo_db.py --verify`；如需从原始授权数据自建才用 `scripts\build_database.py`，或确认 `DATAAGENT_DB_PATH` |
| 不支持的指标或问题（如“销售额”） | 指标白名单只包含已确认口径（申购/赎回/净流入/收益率/SHIBOR/用户计数）；`tBalance`/`yBalance` 语义未确认未启用。换成受支持指标，或按澄清提示补充 |
| `plan_rejected` | 计划缺关键信息或组合不合法（如静态 `user_count` 与资金指标同查询、非 SHIBOR 指标填期限）。按错误信息拆分或修改问题 |
| `insufficient_data` | 时间范围过窄或筛选后无数据。扩大范围、放宽筛选；单日无法计算日环比属于预期 |
| 报告生成了但想复现 | 报告第 14 节“复现信息”记录了本次有效运行配置（模型模式/数据模式/叙述来源/计划/查询窗口）；Mock 演示本身可重复运行 |
| 中文图表变英文标签 | 系统未找到中文字体（如 Microsoft YaHei/SimHei）。安装中文字体或忽略（不影响统计与报告） |

## 依赖损坏与虚拟环境重建

现象：原本能跑的代码突然报 `ModuleNotFoundError` / `ImportError` / `AttributeError: module 'X' has no attribute 'Y'`，
常见于**安装或升级过程被中断**（磁盘写满、Ctrl+C、`pip install` 中途退出），导致包文件被截断——
此时模块往往还能 `import`，但缺关键属性，报错信息具有迷惑性。

典型受损包：`h11`、`jiter`、`httpx`、`anyio`、`numpy`、`pandas`、`python-dateutil`、`matplotlib`、`cycler`、`duckdb`、`openai`。

### 快速修复（逐个强制重装）

```powershell
# 按报错信息定位缺失/损坏的包，逐个强制重装（不升级其他依赖）
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "h11"
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "jiter"
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "httpx"

# OpenAI SDK 相关（缺 anyio/httpx 时会连锁失败）
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "httpcore" "anyio"
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "openai>=1.40,<2.0"

# 数据与绘图链（numpy 损坏会导致 pandas、matplotlib 连锁报错）
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "numpy"
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "pandas"
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "matplotlib"
```

### 彻底重建虚拟环境（推荐，最省心）

```powershell
# 1. 确认磁盘有足够空间（安装约需 1GB 以上余量）
# 2. 删除并重建虚拟环境（不影响源码与数据库）
Remove-Item -Recurse -Force .venv
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3. 验证环境
.\.venv\Scripts\python.exe -c "import duckdb, sqlglot, pydantic, openai, matplotlib, pandas; print('env OK')"
.\.venv\Scripts\python.exe -m pytest -q
```

> 提示：**不要**在安装时使用 `pip install -e . --ignore-installed`。该选项会重装并可能中断其他包的安装，
> 本项目曾在验证安装时因此连锁损坏 9 个依赖（已记录在阶段 07.6.4 报告）；常规安装用
> `pip install -e ".[dev]"` 即可。

## 相关文档

- [README](../README.md)：项目全景、架构、安全与许可证；
- [docs/architecture.md](./architecture.md)：组件调用关系与数据流；
- [docs/data_dictionary.md](./data_dictionary.md)：字段与指标口径；
- [`examples/`](../examples/)：一份由合成数据库实际运行生成的报告与证据样例。

## 模式 1.5：跨年同月逐日对比（新增）

问法示例：`帮我分析2013年8月每天与2014年8月每天的资金申购对比`

- 主窗口取**较晚**的月份，对比窗口取**较早**的月份；
- 按**月内日期（1—31）**逐日对齐，输出双折线图、每日差额与同比、两月合计对比；
- 缺失日期不补零；对比窗口为 0 时同比显示 N/A；
- 没有可配对日期时返回"数据不足"，不会输出空报告。

限制：仅支持同月跨年的逐日对齐；不支持任意序列对齐或不同长度月份。
