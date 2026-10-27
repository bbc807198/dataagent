#!/usr/bin/env python
"""审计已构建的数据库：数据契约 + 业务恒等式 + SHIBOR 填充验证。

用法：
    python scripts/audit_database.py                # 审计默认数据库
    python scripts/audit_database.py --db PATH      # 指定数据库
    python scripts/audit_database.py --json OUT     # 结果另存 JSON

退出码：0 = 全部契约通过；1 = 存在失败的契约或验证不一致。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent import config, data_contracts, db, shibor  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计 dataagent DuckDB 数据底座")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None, help="将审计结果写入 JSON 文件")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve() if args.db else config.DB_PATH
    if not db_path.exists():
        print(f"[错误] 数据库不存在: {db_path}")
        print("请先运行: python scripts/build_database.py")
        return 1

    print("=" * 72)
    print(f"dataagent 数据库审计  ({datetime.now():%Y-%m-%d %H:%M:%S})")
    print(f"数据库: {db_path}")
    print("=" * 72)

    con = db.connect(db_path, read_only=True)
    payload: dict = {"db": str(db_path), "generated_at": datetime.now().isoformat(timespec="seconds")}
    ok = True
    try:
        report = data_contracts.run_contracts(con)
        print("\n[数据契约]")
        for r in report.results:
            mark = "PASS" if r.passed else ("FAIL" if r.fatal else "WARN")
            print(f"  [{mark}] {r.name}: 期望 {r.expected} / 实际 {r.actual}")
            if r.detail:
                print(f"         {r.detail}")
        payload["contracts"] = [r.as_dict() for r in report.results]
        if not report.ok:
            ok = False
            print("\n[失败] 存在未通过的核心契约：")
            for r in report.fatal_failures:
                print(f"  - {r.name}: 期望 {r.expected}, 实际 {r.actual}")

        print("\n[SHIBOR 前向填充验证]（视图 vs 独立 LOCF 重算）")
        fv = shibor.verify_fill_view(con)
        for k, v in fv.as_dict().items():
            print(f"  {k}: {v}")
        payload["shibor_fill"] = fv.as_dict()
        if not fv.ok:
            ok = False
            print("  [失败] 填充结果与 LOCF 不一致！")
    finally:
        con.close()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n[输出] 审计结果已写入 {args.json}")

    print("\n" + ("[结论] 审计通过。" if ok else "[结论] 审计未通过，请检查上方 FAIL 项。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
