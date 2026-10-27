#!/usr/bin/env python
"""DataAgent Benchmark 评测入口（阶段 11）。

默认**离线评测**（Mock/Fake 客户端 + 合成数据库，不发起任何网络请求）：
对 50 道固定案例逐例注入其对应的 Fake 模型输出，真实执行
`AnalysisPlan → QueryBuilder → SQLGuard → QueryService → Analytics → ReportGenerator`。

真实模型评测需显式 `--real`（需要 `DATAAGENT_LLM_API_KEY`），默认不调用、不重试。

用法：
    python scripts/evaluate_agent.py                       # 离线全量
    python scripts/evaluate_agent.py --limit 10             # 只跑前 10 个案例
    python scripts/evaluate_agent.py --case T01 --case R01  # 指定案例
    python scripts/evaluate_agent.py --real --limit 5       # 真实模型（显式）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent.evaluation import EvaluationRunner, load_benchmark  # noqa: E402
from dataagent.evaluation.runner import UsageTrackingClient  # noqa: E402


def _build_real_client():
    """构建真实客户端（仅 --real 时）。不配置 Key 直接报错退出，不静默回退。"""
    key = os.environ.get("DATAAGENT_LLM_API_KEY")
    if not key:
        print("[错误] --real 需要配置 DATAAGENT_LLM_API_KEY（当前未设置）。")
        print("       默认离线评测不需要 Key；如需真实评测请先设置环境变量。")
        return None
    try:
        import openai
    except Exception as e:      # noqa: BLE001
        print(f"[错误] 无法导入 openai SDK：{type(e).__name__}（建议重建虚拟环境）")
        return None
    model = os.environ.get("DATAAGENT_LLM_MODEL") or "gpt-4o-mini"
    base_url = os.environ.get("DATAAGENT_LLM_BASE_URL")
    kwargs = {"api_key": key}
    if base_url:
        kwargs["base_url"] = base_url
    # A1-1：显式禁用 SDK 内部重试（否则一次失败会隐式再调用并产生费用）
    client = openai.OpenAI(max_retries=0, **kwargs)
    client.model = model
    return UsageTrackingClient(client)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DataAgent Benchmark 评测")
    ap.add_argument("--real", action="store_true",
                    help="真实模型评测（显式启用；需要 API Key；会产生费用）")
    ap.add_argument("--limit", type=int, default=None,
                    help="只评测前 N 个案例（真实模式下强烈建议设置上限）")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="模型调用总预算（真实模式默认 = 案例数 × 2；达到即停止，不追加请求）")
    ap.add_argument("--case", action="append", default=None,
                    help="只评测指定 case_id（可重复）")
    ap.add_argument("--out", type=Path, default=None,
                    help="输出根目录（默认 artifacts/evaluation）")
    ap.add_argument("--quiet", action="store_true", help="减少进度输出")
    args = ap.parse_args(argv)

    cases, answers = load_benchmark()
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c["case_id"] in wanted]
        if not cases:
            print(f"[错误] 未找到指定案例：{sorted(wanted)}")
            return 2

    mode = "real" if args.real else "mock"
    # A2-3：真实模式必须有有限预算（默认 案例数 × 2；可用 --max-requests 覆盖）
    n_cases = len(cases) if not args.limit else min(args.limit, len(cases))
    max_requests = args.max_requests
    if mode == "real" and max_requests is None:
        max_requests = max(1, n_cases * 2)
    client = None
    if args.real:
        client = _build_real_client()
        if client is None:
            return 2
        n = len(cases) if not args.limit else min(args.limit, len(cases))
        print("=" * 74)
        print(f"真实模型评测（显式启用）：将发送 **{n}** 个案例到模型 API")
        print(f"模型：{client.model}　|　Base URL：{os.environ.get('DATAAGENT_LLM_BASE_URL') or '默认'}")
        print(f"调用预算：最多 **{max_requests}** 次模型请求（达到即停止，不追加）")
        print("说明：评测已关闭 SDK 重试（max_retries=0）与 Schema 修复重试，"
              "一个案例最多 1 次请求。")
        try:
            from dataagent.evaluation.runner import load_pricing
            pricing = load_pricing()
        except Exception:       # noqa: BLE001
            pricing = None
        if pricing:
            print(f"价格表：{pricing.get('source')}（{pricing.get('date')}，"
                  f"{pricing.get('currency')}/{pricing.get('unit')}）—— 结束后按实际 token 估算")
        else:
            print("预计费用：**未知**（未配置 benchmark/pricing.json 价格表）")
        print("说明：不自动重试；每个案例最多一次解析调用（叙述层已关闭）")
        print("=" * 74)
    else:
        print("离线评测：Mock/Fake 客户端 + 合成数据库（不发起任何网络请求）")
        print("注意：结果衡量**确定性执行链路**，不代表真实模型的自然语言理解准确率。")

    runner = EvaluationRunner(cases, answers, model_mode=mode,
                             output_root=args.out, real_client=client,
                             quiet=args.quiet, max_requests=max_requests)
    try:
        results, summary = runner.run(limit=args.limit)
    finally:
        runner.cleanup()

    def pct(v):
        return "N/A" if v is None else f"{v * 100:.1f}%"

    print()
    print("=" * 74)
    print(f"案例总数 {summary.total_cases}（有效 {summary.valid_cases} / "
          f"负面 {summary.negative_cases}）")
    print(f"查询成功率     : {pct(summary.query_success_rate)} "
          f"({summary.query_success}/{summary.valid_cases})")
    print(f"报告生成成功率 : {pct(summary.report_success_rate)} "
          f"({summary.report_generated}/{summary.valid_cases})")
    print(f"答案正确率     : {pct(summary.answer_accuracy)} "
          f"({summary.answers_correct}/{summary.verifiable_cases})")
    print(f"正确拒绝率     : {pct(summary.negative_accuracy)} "
          f"({summary.negative_correct}/{summary.negative_cases})")
    print(f"误拒绝         : {summary.false_rejections} "
          f"({pct(summary.false_rejection_rate)})")
    print(f"响应时间       : avg {summary.latency_avg_ms} ms | "
          f"P50 {summary.latency_p50_ms} ms | P95 {summary.latency_p95_ms} ms")
    print(f"Token / 成本   : {summary.token_total or 'N/A'} / "
          f"{summary.cost_total if summary.cost_total is not None else 'N/A'}")
    total_req = sum(getattr(r, "request_count", 0) for r in results)
    total_resp = sum(getattr(r, "response_count", 0) for r in results)
    skipped = sum(1 for r in results if r.status == "skipped_budget")
    print(f"请求/响应/跳过 : {total_req} / {total_resp} / {skipped}"
          f"（预算 {max_requests if max_requests is not None else '不限'}）")
    print(f"输出目录       : {runner.output_root / 'latest'}")
    if summary.failures:
        print()
        print("失败案例：")
        for f in summary.failures:
            print(f"  - {f['case_id']} [{f['category']}] expected={f['expected_behavior']} "
                  f"actual={f['status']} :: {(f['failure_reason'] or '')[:70]}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
