#!/usr/bin/env python
"""构建 DuckDB 数据底座：原始 CSV → data/processed/dataagent.duckdb。

用法：
    python scripts/build_database.py                  # 构建（数据库不存在时）
    python scripts/build_database.py --rebuild        # 显式重建四张业务表（DROP + CREATE + 导入）
    python scripts/build_database.py --force          # 删除旧数据库文件后重建（显式覆盖）
    python scripts/build_database.py --db PATH        # 指定数据库文件路径
    python scripts/build_database.py --data-dir PATH  # 指定原始数据目录

安全策略：数据库已存在且包含业务表时，默认拒绝执行（退出码 2），防止静默覆盖；
--rebuild 只重建表结构不清空文件其他对象；--force 删除整个数据库文件。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 允许直接从项目根运行（未安装包时）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataagent import config, db, ingestion, shibor  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("build_database")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 dataagent DuckDB 数据底座")
    parser.add_argument("--db", type=Path, default=None, help="数据库文件路径（默认 config.DB_PATH）")
    parser.add_argument("--data-dir", type=Path, default=None, help="原始数据目录（默认 config.DATA_DIR）")
    parser.add_argument("--rebuild", action="store_true",
                        help="显式重建四张业务表（DROP 后重新导入）")
    parser.add_argument("--force", action="store_true",
                        help="删除已存在的数据库文件后重建（显式覆盖整个文件）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve() if args.db else config.DB_PATH
    data_dir = Path(args.data_dir).resolve() if args.data_dir else config.DATA_DIR

    print("=" * 72)
    print("dataagent 数据库构建")
    print(f"项目根目录 : {config.PROJECT_ROOT}")
    print(f"原始数据   : {data_dir}")
    print(f"数据库文件 : {db_path}")
    print("=" * 72)

    missing = [str(p) for p in config.RAW_FILES.values() if not p.is_file()]
    if missing and data_dir == config.DATA_DIR:
        print("[错误] 未找到全部原始数据文件：")
        for m in missing:
            print(f"  - {m}")
        print("请将四张业务表放入 Purchase Redemption Data/，或用 --data-dir 指定目录。")
        return 1

    if db_path.exists():
        if args.force:
            print(f"[--force] 删除现有数据库文件: {db_path}")
            db_path.unlink()
        elif args.rebuild:
            print("[--rebuild] 将显式重建四张业务表")
        else:
            # 默认拒绝：检查是否包含业务表
            import duckdb
            probe = duckdb.connect(str(db_path), read_only=True)
            existing = db.existing_business_tables(probe)
            probe.close()
            if existing:
                print("[拒绝] 数据库已存在且包含业务表，默认不覆盖：")
                print(f"  文件: {db_path}")
                print(f"  已有表: {', '.join(existing)}")
                print("如需重建请使用 --rebuild（保留文件、重建表）或 --force（删除文件重建）。")
                return 2

    con = db.connect(db_path)
    try:
        if args.rebuild or args.force:
            dropped = db.rebuild_tables(con)
            print(f"[重建] 已删除并重建表: {dropped or '无既有表'}")
        else:
            created = db.init_schema(con)
            print(f"[Schema] 新建表: {created or '无（已存在）'}")

        print("\n[导入] 开始 ...")
        reports = ingestion.import_all(con, data_dir=data_dir)
        failed = False
        for r in reports:
            status = "OK" if r.ok else "FAILED"
            print(f"  [{status}] {r.table}: {r.rows_inserted:,} 行, {r.duration_s}s")
            if r.error:
                print(f"        错误: {r.error}")
                failed = True
        if failed:
            print("\n[结果] 导入失败，请检查上方错误。数据库未通过质量检查。")
            return 1

        print("\n[导入后检查]")
        checks = ingestion.post_import_checks(con)
        for t, info in checks.items():
            print(f"  {t}: {info}")

        print("\n[SHIBOR 视图] 创建前向填充视图 ...")
        name = shibor.create_fill_view(con)
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"  视图 {name}: {n} 行")

        print("\n[完成] 数据库构建成功。")
        print(f"  路径: {db_path}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
