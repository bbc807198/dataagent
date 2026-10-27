#!/usr/bin/env python
"""从仓库自带的 Parquet 演示数据构建本地 DuckDB（离线、只读使用）。

用法：
    python scripts/build_demo_db.py                    # 构建到 data/processed/dataagent_demo.duckdb
    python scripts/build_demo_db.py --verify           # 构建前校验 demo_data/SHA256SUMS.txt
    python scripts/build_demo_db.py --out <路径>.duckdb
    python scripts/build_demo_db.py --verify --overwrite   # 已存在时强制重建

安全策略（阶段 14 任务 D）：
- 已存在输出数据库时**默认拒绝覆盖**，需用 `--overwrite` 显式重建；
- 无论是否 `--overwrite`，都**拒绝把演示库构建到原始私人数据库文件名**
  （`dataagent.duckdb`，负责人从原始数据构建、不随仓库发布），避免误覆盖；
- 先构建到临时文件、成功后才原子替换，**构建失败不会破坏已存在的数据库**；
- 只读取仓库内 `demo_data/*.parquet`，**不需要网络、不需要原始数据**；
- 构建产物是本地生成文件，已被 `.gitignore` 排除；
- 构建完成后可运行：`streamlit run app.py`（默认使用该演示库）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEMO_DIR = ROOT / "demo_data"
DEFAULT_OUT = ROOT / "data" / "processed" / "dataagent_demo.duckdb"

# 原始私人数据库文件名（负责人本地从原始数据构建，不随仓库发布、绝不应当被演示构建覆盖）
PRIVATE_DB_NAMES = {"dataagent.duckdb"}

TABLES = {
    "user_balance_table": ["user_id", "report_date", "tBalance", "yBalance",
                           "total_purchase_amt", "direct_purchase_amt",
                           "purchase_bal_amt", "purchase_bank_amt",
                           "total_redeem_amt", "consume_amt", "transfer_amt",
                           "tftobal_amt", "tftocard_amt", "share_amt",
                           "category1", "category2", "category3", "category4"],
    "user_profile_table": ["user_id", "sex", "city", "constellation"],
    "mfd_day_share_interest": ["mfd_date", "mfd_daily_yield", "mfd_7daily_yield"],
    "mfd_bank_shibor": ["mfd_date", "Interest_O_N", "Interest_1_W",
                        "Interest_2_W", "Interest_1_M", "Interest_3_M",
                        "Interest_6_M", "Interest_9_M", "Interest_1_Y"],
}


def verify_checksums(demo_dir: Path | None = None) -> int:
    """严格校验 demo_data 下 Parquet 的 SHA-256（与 SHA256SUMS.txt 精确比对）。

    返回 0 表示通过；**任何异常都失败关闭（返回非零）**，包括：
      - ``SHA256SUMS.txt`` 缺失（不再静默跳过）；
      - 清单行格式非法 / 摘要不是合法 64 位十六进制；
      - 清单存在重复条目；
      - 清单未**恰好覆盖** 4 个预期 Parquet（缺失条目 / 意外条目）；
      - 清单所列文件在磁盘缺失；
      - 实际 SHA-256 与清单不符。
    """
    demo_dir = Path(demo_dir) if demo_dir is not None else DEMO_DIR
    sums = demo_dir / "SHA256SUMS.txt"
    expected = {f"{t}.parquet" for t in TABLES}

    if not sums.is_file():
        print(f"[错误] 未找到校验清单 {sums}：无法校验演示数据，失败关闭。")
        return 1

    seen: dict[str, str] = {}
    problems: list[str] = []
    for lineno, raw in enumerate(sums.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            problems.append(f"第 {lineno} 行格式非法：{raw!r}")
            continue
        digest, name = parts[0].lower(), parts[1]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            problems.append(f"第 {lineno} 行摘要非合法 64 位十六进制：{parts[0]!r}")
            continue
        if name in seen:
            problems.append(f"重复条目：{name}")
            continue
        seen[name] = digest

    # 覆盖性：清单必须恰好覆盖 4 个预期 Parquet
    if expected - set(seen):
        problems.append(f"清单缺少条目：{sorted(expected - set(seen))}")
    if set(seen) - expected:
        problems.append(f"清单包含意外条目：{sorted(set(seen) - expected)}")

    # 逐文件：存在性 + 哈希
    for name in sorted(expected & set(seen)):
        path = demo_dir / name
        if not path.is_file():
            problems.append(f"文件缺失：{name}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != seen[name]:
            problems.append(
                f"哈希不符：{name}（清单 {seen[name][:12]}… / 实际 {actual[:12]}…）")
        else:
            print(f"[OK] {name}")

    if problems:
        print("[错误] 演示数据校验未通过（失败关闭）：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"[OK] 演示数据校验通过：{len(expected)} 个 Parquet 与 SHA256SUMS.txt 完全一致。")
    return 0


def build(out_path: Path, overwrite: bool = False,
          demo_dir: Path | None = None) -> int:
    """构建演示库到 out_path。

    返回码：
        0  成功
        1  演示数据缺失或构建过程失败
        2  输出已存在且未指定 --overwrite（默认拒绝覆盖）
        3  目标文件名被识别为原始私人数据库（拒绝）
    """
    out_path = Path(out_path)
    demo_dir = Path(demo_dir) if demo_dir is not None else DEMO_DIR

    # 1) 拒绝误覆盖原始私人数据库（无论是否 --overwrite）
    if out_path.name in PRIVATE_DB_NAMES:
        print(f"[拒绝] 目标文件名 '{out_path.name}' 被识别为原始私人数据库"
              f"（负责人本地从原始数据构建，不随仓库发布）。")
        print("        build_demo_db.py 只应构建演示库 dataagent_demo.duckdb；")
        print("        若要维护原始库请改用 scripts/build_database.py，"
              "或把输出文件名改为 dataagent_demo.duckdb。")
        return 3

    # 2) 演示数据完整性（缺失则失败，不触碰任何已有数据库）
    missing = [t for t in TABLES
               if not (demo_dir / f"{t}.parquet").is_file()]
    if missing:
        print(f"[错误] demo_data 缺少数据文件: {missing}")
        print("       请确认仓库完整（demo_data/*.parquet 随仓库发布）。")
        return 1

    # 3) 已存在时默认拒绝覆盖（保护已有数据库不被静默改写）
    if out_path.exists() and not overwrite:
        print(f"[拒绝] 输出数据库已存在，默认不覆盖：{out_path}")
        print("       如需重建请加 --overwrite（会先校验 Parquet 再覆盖）。")
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 4) 先写入临时文件，成功后才原子替换：构建失败不会破坏已有数据库
    tmp_path = out_path.parent / (out_path.name + ".tmp_build_" + uuid.uuid4().hex)
    if tmp_path.exists():
        tmp_path.unlink()
    built_ok = False
    try:
        import duckdb
        from dataagent import shibor

        con = duckdb.connect(str(tmp_path))
        try:
            for table, cols in TABLES.items():
                col_sql = ", ".join(f'"{c}"' for c in cols)
                src = (demo_dir / f"{table}.parquet").as_posix()
                con.execute(f"CREATE TABLE {table} AS SELECT {col_sql} "
                            f"FROM read_parquet('{src}')")
            con.execute("DROP VIEW IF EXISTS v_mfd_bank_shibor_daily_filled")
            shibor.create_fill_view(con)
        finally:
            con.close()

        # 成功构建后原子替换：直接 os.replace 覆盖目标（Windows 上经 MoveFileEx
        # 原子替换，POSIX 上 rename 原子替换）。**不再先删除旧文件**——避免
        # "删除成功但替换失败" 导致原数据库丢失。替换失败时旧库保持原样。
        os.replace(tmp_path, out_path)
        built_ok = True
    except Exception as e:  # noqa: BLE001 - 构建失败：清理临时文件并报错
        if tmp_path.exists():
            tmp_path.unlink()
        print(f"[错误] 构建失败：{type(e).__name__}: {e}")
        return 1
    finally:
        if not built_ok and tmp_path.exists():
            tmp_path.unlink()

    if not built_ok:
        return 1

    # 5) 打印构建结果
    print(f"已构建：{out_path}（{out_path.stat().st_size / 1024 / 1024:.1f} MB）")
    con = duckdb.connect(str(out_path), read_only=True)
    try:
        for table in TABLES:
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:24} {n:>10,} 行")
        lo, hi = con.execute("SELECT MIN(report_date), MAX(report_date) "
                             "FROM user_balance_table").fetchone()
        print(f"  数据覆盖：{lo} ~ {hi}")
        p8 = con.execute(
            "SELECT SUM(total_purchase_amt) FROM user_balance_table "
            "WHERE report_date BETWEEN DATE '2014-08-01' AND DATE '2014-08-31'"
        ).fetchone()[0]
        print(f"  2014-08 申购合计（口径校验）：{p8:,}")
    finally:
        con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="构建本地演示 DuckDB")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 .duckdb 路径")
    ap.add_argument("--verify", action="store_true", help="构建前校验 SHA-256")
    ap.add_argument("--overwrite", action="store_true",
                    help="已存在时强制重建（默认拒绝覆盖）")
    args = ap.parse_args()
    if args.verify:
        rc = verify_checksums()
        if rc:
            return rc
    return build(args.out, overwrite=args.overwrite)


if __name__ == "__main__":
    raise SystemExit(main())
