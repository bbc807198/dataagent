# Benchmark 与质量评估

本文说明 DataAgent 的固定测试集、标准答案的独立核算方法、评测指标定义、日志字段与复现命令。

> **关键界限**：默认离线评测使用 **Fake 客户端 + 合成数据库**，逐例注入该案例对应的计划，
> 真正经过 `AnalysisPlan → QueryBuilder → SQLGuard → QueryService → Analytics →
> ReportGenerator`。它衡量的是**确定性执行链路**（SQL 生成、安全校验、查询、统计、报告）。
> **它不代表真实模型（如 DeepSeek）的自然语言理解准确率**——那需要显式启用真实模型评测。

## 1. 文件与命令

| 文件 | 说明 |
|---|---|
| `benchmark/questions.jsonl` | 50 道固定测试问题（含 `fixture_plan`：该案例期望的模型输出计划） |
| `benchmark/golden_answers.json` | 独立核算的标准答案（含容差、核算方法） |
| `benchmark/pricing.json` | **可选**价格表（缺失时成本记 `N/A`，不猜测） |
| `scripts/evaluate_agent.py` | 评测入口 |
| `src/dataagent/evaluation/` | 评测模块（`models/metrics/runner`） |
| `artifacts/evaluation/latest/` | 最新一次评测产物（已被 `.gitignore` 排除） |

```powershell
# 离线全量评测（默认；不发起任何网络请求）
.\.venv\Scripts\python.exe scripts\evaluate_agent.py

# 只跑部分案例
.\.venv\Scripts\python.exe scripts\evaluate_agent.py --limit 10
.\.venv\Scripts\python.exe scripts\evaluate_agent.py --case T01 --case N01

# 真实模型评测（显式启用，需要 API Key，会产生费用）
.\.venv\Scripts\python.exe scripts\evaluate_agent.py --real --limit 5
```

## 2. 测试集构成（50 道）

| 分组 | 数量 | 说明 |
|---|---|---|
| 有效分析请求 | **35** | 覆盖七类分析能力与多指标/多维度/期限组合 |
| 负面与边界 | **15** | 不支持指标、缺参数、数据不足、日期超界、口径冲突、Schema 拒绝、提示词注入 |

有效请求的七类覆盖：时间趋势 12、窗口对比 5、分组排名 5、分布统计 4、MAD 异常 3、
多指标关联 3、描述性汇总 3（合计 35）。

负面与边界覆盖类别：`unsupported_metric`（3）、`clarification`（2）、`insufficient_data`（2）、
`date_out_of_range`（2）、`metric_conflict`（2）、`schema_reject`（3，含非 SHIBOR 填 `term`、
非法 SHIBOR 期限、类别字段范围筛选）、`prompt_injection`（1）。

每条案例字段：`case_id`、`question`、`category`、`expected_behavior`（success/reject/clarify）、
`expected_analysis_type`、`expected_metrics`、`expected_status`、`golden_reference`、`tags`、
`fixture_plan`（该案例注入的模型输出计划）。

数据集固定为 `tests/fixtures/synthetic/`（2024-01-02 ~ 2024-01-12，**人工构造合成样本，
不是真实业务数据**）。

## 3. 标准答案如何独立核算

`benchmark/golden_answers.json` **不是**从 Agent 输出复制而来，而是用两条独立路径生成：

1. **独立 SQL**：用 DuckDB `read_csv_auto` 直接读取合成 CSV 并聚合（不经 `QueryBuilder`、
   不经 `Analytics`），得到合计、均值、排名、窗口差、期间去重用户数；
2. **独立 Python 算术**：用纯 Python 复算合计/均值/日环比极值，并在生成期断言两条路径一致
   （不一致即中止生成）。

覆盖的数值：申购总额 2850、赎回总额 1850、净流入 1000、城市申购/净流入/赎回排名、
每日收益率均值 1.40、七日年化均值 5.00、SHIBOR O_N 首/末/均值、窗口对比主/对比值、
最大降幅（2024-01-05，-100%）、活跃用户数 3。

**容差规则**：金额 `abs ≤ 0.01` 元；比率/收益率 `abs ≤ 1e-6`；
日期、指标 ID、期限、状态**严格相等**（无容差）。缺失值不视为通过。

## 4. 五类指标定义

| 指标 | 定义 | 分母（**事前固定**） |
|---|---|---|
| **执行成功率** | SQL 全部执行成功 **且** 统计分析成功的有效案例 | 有效案例数（35） |
| 报告生成成功率（单独口径） | 报告落盘成功的有效案例 | 有效案例数（35） |
| **答案正确率** | 数值/日期/指标/口径**全部符合容差**的可验证案例 | 标准答案声明 `verifiable=true` 的有效案例数（本批次 35，**不因执行失败缩小**） |
| **行为正确率** | 成功/拒绝/澄清的行为类别符合预期 | 全部案例（50） |
| **正确拒绝率** | 以正确类别拒绝/澄清/数据不足的负面案例 | 负面案例数（15） |
| 误拒绝 | 有效问题**未产出报告**（被拒绝或失败）的案例 | 有效案例数（35，单列报告） |
| **响应时间** | 端到端 `time.perf_counter()`；avg / P50 / P95 / min / max；成功与失败分别平均 | 全部案例 |
| **Token 与成本** | 真实模式**按案例**从 SDK `usage` 增量累计；价格来自 `benchmark/pricing.json` | 仅真实模式 |

**口径纪律（阶段 11.1 强化）**：

1. `report_generated` **不等于**答案正确；执行成功、报告生成、答案正确、行为正确四者分开统计。
2. 仅有 `status_only`（执行成功）**不计入**数值正确率；无独立数值标准的案例标
   `verification_kind=execution_only`，**不进入**答案正确率分子分母。
3. 可验证分母由 `golden_answers.json` 的 `verifiable` 字段**事先**声明；执行失败、被拒绝
   或未产出有效结果的案例**按答案未正确处理**计入，**不删除分母**。
4. 离线模式 Token / 成本一律 `N/A`（不填 0、不与真实成本混算）。

P50/P95 采用线性插值（`(n-1)*p/100`），与常见统计库一致；单元测试覆盖。

## 5. 日志（`run_log.jsonl`）

每案例一行 JSON，字段：`run_id`、`case_id`、`model_mode`、`data_mode`、`analysis_type`、
`status`、`error_code`、`latency_ms`、`query_count`、`token_usage`、`timestamp`、
`exception_class`。

**不记录**：API Key、Authorization Header、用户级原始记录、原始查询参数中的敏感值、
被拒绝的 LLM 幻觉内容。日志写入 `artifacts/evaluation/`（`.gitignore` 排除）。

异常分类（`EXCEPTION_CLASSES`，不允许全部归为"未知"）：`api_error`、`plan_parse_error`、
`sql_rejected`、`db_error`、`insufficient_data`、`stats_error`、`report_error`、`timeout`、
`unknown`。单案例异常不会中断整批（有专门的回归测试）。

## 6. 已知限制

1. **离线结果不含自然语言理解**：`fixture_plan` 是人写的"正确计划"，只测执行链路；
   真实 NL 理解准确率需 `--real` 显式评测（且需 API Key，会产生费用）。
2. **测试集规模小**（50 例、合成数据 12 行）：指标波动大，不能外推为生产准确率。
3. **覆盖范围受指标白名单约束**：混合口径（金额 vs 收益率）相关性、不可加指标的窗口差
   （`WINDOW_DIFF`）等会被架构拒绝，因此未作为"有效案例"，而是以正确拒绝/澄清的形式覆盖。
4. **成本为估算**：无价格表时为 `N/A`；即使有价格表也不等于账单。
5. **合成数据不是真实业务数据**，其结果不能代表真实资金流结论。

## 7. 一次离线评测的真实结果（示例）

```
案例总数 50（有效 35 / 负面 15）
查询成功率     : 100.0% (35/35)
报告生成成功率 : 100.0% (35/35)
答案正确率     : 100.0% (35/35)
正确拒绝率     : 100.0% (15/15)
误拒绝         : 0 (0.0%)
响应时间       : avg ~90 ms | P50 ~100 ms | P95 ~200 ms
Token / 成本   : N/A / N/A
```

> 该结果说明：在固定计划 + 合成数据下，执行链路与独立核算的数值完全一致，
> 且负面案例全部被正确拒绝/澄清。**它不代表 DeepSeek 或其他真实模型的能力**。

## 8. 复现

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_agent.py      # 生成 artifacts/evaluation/latest/
.\.venv\Scripts\python.exe -m pytest tests/test_benchmark.py -q
```


## 9. 阶段 11.1 修订（口径与计量）

### 9.1 执行成功 ≠ 答案正确

- 原问题：分布（D01-D04）、MAD 异常（A01-A03）、关联（C01-C03）等案例此前仅有
  `status_only`，只证明“跑通了”，却被计入答案正确率分子。
- 修订：为全部 35 个有效案例补充**独立可复算**的数值标准——分布核验
  `total_count / sample_size / value_stats(min,max,mean,median)`；MAD 核验
  `sample_size / 稳健中心 / MAD / 异常数量 / 异常日期`；关联核验
  `指标组合 / 配对数量 / Pearson 系数`；并为**所有**有效案例增加
  `analysis_type_is` 与 `metrics_exact`（身份一致性）。
- 结果：**35/35 有效案例全部具备数值标准**，`execution_only` 案例数为 0。

### 9.2 可复算性（`scripts/verify_golden.py`）

```powershell
python scripts/verify_golden.py --json artifacts/evaluation/latest/golden_verification.json
```

只读合成 CSV，用 DuckDB `read_csv_auto` + 纯 Python 算术独立复算（**不调用**
`QueryBuilder` / `Analytics` / `ReportGenerator`），逐条报告来源与一致性。
实测：**162 项核验全部一致（其中 70 项为类型/指标身份检查、92 项为结果类检查）**（不一致 0、无法复算 0）。
`tests/test_benchmark_1_1.py` 含**篡改检测**：把 `T01.series_sum` 改成 9999、
或把 `R01.ranking_top1_value` 改成 1.0 后，独立校验必须失败。

### 9.3 负面案例的校验依据

- 离线：`negative_basis=offline_injected_plan` —— 拒绝结论来自**注入计划的 Schema 校验**，
  只证明“系统会拒绝该计划”，**不证明真实模型的抗注入能力**（`summary.md` 明示此限定）。
- 真实：`negative_basis=real_model_output` —— 拒绝结论来自真实模型输出的实际解析/校验。
  负面案例分为三类统计：拒绝类别正确 / 拒绝类别错误 / **意外成功**。

### 9.4 Token 与成本的按案例归属

- 原问题：批次结束后把**累计用量**复制给所有案例，单案例 Token 失真。
- 修订：`UsageTrackingClient` 记录 `requests`（发起）/`responses`（成功返回）/
  `usage_responses`（带 usage）；每案例用**前后快照差值**得到自身用量；
  批次总量必须等于各案例增量之和（不一致直接抛错）。
- API 未返回 usage → `null`（不伪造 0）；无价格表 → 成本 `N/A`。
- 回归测试用 Fake SDK 模拟三次不同用量（100/20、200/40、300/60），验证归属与总量。

## 10. Web 面板（阶段 12）

`app.py` 的「Benchmark」页**只读取** `artifacts/evaluation/latest/evaluation_results.json`
与 `summary.md`，不重新运行评测。展示内容：执行成功率 / 答案正确率 / 行为正确率 /
正确拒绝率、P50/P95、Token 与成本（离线为 `N/A`）、各分析类型表现、失败案例。

页面明确标注「离线评测为确定性执行链路评测，**不代表真实 DeepSeek 的自然语言理解准确率**」，
并且**不会**把 Mock 模式的费用显示为真实模型调用成本。
