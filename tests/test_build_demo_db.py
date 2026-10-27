"""阶段 14 任务 D：安全建库测试。

覆盖：正常构建、已存在默认拒绝、明确覆盖、校验失败、拒绝误覆盖原始私人库、
构建失败不破坏已有库。

注意：演示 Parquet 中 user_balance_table.parquet 约 30MB，直接 copytree 会触发
临时盘空间不足（WinError 112）。build() 只读取 demo_dir、不写入，因此这里用
硬链接（os.link，同卷零额外空间）把 Parquet 接入临时目录；SHA256SUMS.txt 仅
377 字节，真实拷贝以便篡改测试不污染仓库数据。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

DEMO_DIR = ROOT / "demo_data"

# 直接加载脚本（scripts/ 不是包）
_spec = importlib.util.spec_from_file_location(
    "_build_demo_db_test", ROOT / "scripts" / "build_demo_db.py")
build_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_mod)

ALL_PARQUETS = list(build_mod.TABLES.keys())


def _link_demo(dest: Path, parquets=ALL_PARQUETS, copy_sums: bool = True) -> Path:
    """在 dest 处组装一份 demo 目录：Parquet 用硬链接（零额外空间），
    SHA256SUMS 真实拷贝（便于篡改而不影响仓库）。"""
    dest.mkdir(parents=True, exist_ok=True)
    for name in parquets:
        src = DEMO_DIR / f"{name}.parquet"
        dst = dest / f"{name}.parquet"
        if dst.exists():
            dst.unlink()
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    sums_src = DEMO_DIR / "SHA256SUMS.txt"
    sums_dst = dest / "SHA256SUMS.txt"
    if sums_dst.exists():
        sums_dst.unlink()
    shutil.copy2(sums_src, sums_dst)
    return dest


@pytest.fixture
def demo_copy(tmp_path: Path) -> Path:
    """硬链接一份 demo_data 到临时目录，测试互不污染真实数据。"""
    return _link_demo(tmp_path / "demo_data")


@pytest.fixture
def out_db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "processed" / "dataagent_demo.duckdb"


def _import_duckdb():
    import duckdb
    return duckdb


def test_normal_build(demo_copy, out_db):
    """正常构建：四表、行数、覆盖、2014-08 口径均正确。"""
    rc = build_mod.build(out_db, demo_dir=demo_copy)
    assert rc == 0
    assert out_db.is_file()
    duckdb = _import_duckdb()
    con = duckdb.connect(str(out_db), read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        for t in build_mod.TABLES:
            assert t in tables, t
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in build_mod.TABLES}
        assert counts["user_balance_table"] == 2_840_421
        assert counts["user_profile_table"] == 28_041
        assert counts["mfd_day_share_interest"] == 427
        assert counts["mfd_bank_shibor"] == 294
        lo, hi = con.execute(
            "SELECT MIN(report_date), MAX(report_date) "
            "FROM user_balance_table").fetchone()
        assert str(lo) == "2013-07-01" and str(hi) == "2014-08-31"
        p8 = con.execute(
            "SELECT SUM(total_purchase_amt) FROM user_balance_table "
            "WHERE report_date BETWEEN DATE '2014-08-01' AND DATE '2014-08-31'"
        ).fetchone()[0]
        assert p8 == 7_921_353_364
    finally:
        con.close()


def test_existing_refused_by_default(demo_copy, out_db):
    """已存在时默认拒绝覆盖（退出码 2），且已有库内容不变。"""
    assert build_mod.build(out_db, demo_dir=demo_copy) == 0
    duckdb = _import_duckdb()
    con = duckdb.connect(str(out_db), read_only=True)
    before = con.execute("SELECT COUNT(*) FROM user_balance_table").fetchone()[0]
    con.close()

    rc = build_mod.build(out_db, demo_dir=demo_copy)  # 无 --overwrite
    assert rc == 2
    assert out_db.is_file()
    con = duckdb.connect(str(out_db), read_only=True)
    try:
        after = con.execute("SELECT COUNT(*) FROM user_balance_table").fetchone()[0]
        assert after == before  # 内容未变
    finally:
        con.close()


def test_explicit_overwrite(demo_copy, out_db):
    """明确 --overwrite 可重建（退出码 0），产物有效。"""
    assert build_mod.build(out_db, demo_dir=demo_copy) == 0
    rc = build_mod.build(out_db, overwrite=True, demo_dir=demo_copy)
    assert rc == 0
    assert out_db.is_file()
    duckdb = _import_duckdb()
    con = duckdb.connect(str(out_db), read_only=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM user_balance_table").fetchone()[0]
        assert n == 2_840_421
    finally:
        con.close()


def test_verify_failure_blocks_build(demo_copy, out_db, monkeypatch):
    """SHA-256 校验失败应阻断构建（main --verify 退出码 1），不生成数据库。"""
    # 篡改清单：把第一个摘要改成错误值
    sums = demo_copy / "SHA256SUMS.txt"
    lines = sums.read_text(encoding="utf-8").splitlines()
    lines[0] = "0" * 64 + " " + lines[0].split(maxsplit=1)[1]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 校验本身应失败
    assert build_mod.verify_checksums(demo_dir=demo_copy) == 1

    # 通过 main --verify 走真实入口：应先校验再构建
    monkeypatch.setattr(build_mod, "DEMO_DIR", demo_copy)
    monkeypatch.setattr(build_mod, "DEFAULT_OUT", out_db)
    monkeypatch.setattr(sys, "argv",
                        ["build_demo_db.py", "--verify", "--out", str(out_db)])
    rc = build_mod.main()
    assert rc == 1
    assert not out_db.exists()


def test_refuse_private_db_name(tmp_path):
    """拒绝把演示构建到原始私人数据库文件名（即使 --overwrite 也拒绝）。"""
    private = tmp_path / "dataagent.duckdb"
    # 先放一个内容（模拟负责人已有私人库）
    private.write_bytes(b"fake-original-private-db")
    rc = build_mod.build(private, overwrite=True, demo_dir=DEMO_DIR)
    assert rc == 3
    # 原文件内容未被覆盖
    assert private.read_bytes() == b"fake-original-private-db"


def test_build_failure_keeps_existing(tmp_path):
    """构建失败（演示数据缺失）不破坏已存在的有效数据库。"""
    out = tmp_path / "data" / "processed" / "dataagent_demo.duckdb"
    good = _link_demo(tmp_path / "demo_good")
    assert build_mod.build(out, demo_dir=good) == 0  # 先建好有效库

    # 制造缺失一张表的演示目录（硬链接其余表，不复制 30MB 大文件）
    bad = _link_demo(tmp_path / "demo_bad",
                     parquets=[p for p in ALL_PARQUETS
                               if p != "mfd_bank_shibor"])
    rc = build_mod.build(out, overwrite=True, demo_dir=bad)
    assert rc == 1  # 缺失文件 → 失败退出码（在写入前即返回，不触碰已有库）

    # 已有库仍可用、内容未变
    duckdb = _import_duckdb()
    con = duckdb.connect(str(out), read_only=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM user_balance_table").fetchone()[0]
        assert n == 2_840_421
    finally:
        con.close()


def test_atomic_replace_failure_keeps_existing_db(tmp_path, monkeypatch):
    """原子替换失败（如目标被占用）时：原库仍存在且内容不变，临时文件被清理。

    这是阶段 14.1 任务 A 的关键回归：旧实现先 unlink 旧库再 os.replace，
    一旦替换失败旧库即丢失；原子替换失败必须保持原库原样。
    """
    import errno

    out = tmp_path / "data" / "processed" / "dataagent_demo.duckdb"
    good = _link_demo(tmp_path / "demo_good")
    assert build_mod.build(out, demo_dir=good) == 0  # 先建好有效库
    before = out.read_bytes()

    def _boom(src, dst):  # 模拟 os.replace 失败（保留旧库，不删除目标）
        raise OSError(errno.EACCES, "simulated atomic replace failure")

    monkeypatch.setattr(build_mod.os, "replace", _boom)
    rc = build_mod.build(out, overwrite=True, demo_dir=good)

    assert rc == 1                      # 失败退出码
    assert out.is_file()                # 旧库仍在
    assert out.read_bytes() == before   # 内容逐字节不变
    # 无残留临时文件（tmp_build_*）
    assert list(out.parent.glob("*.tmp_build_*")) == []


def test_verify_missing_manifest_fails_closed(demo_copy):
    """SHA256SUMS.txt 缺失时校验失败关闭（返回非零，不再静默跳过）。"""
    (demo_copy / "SHA256SUMS.txt").unlink()
    assert build_mod.verify_checksums(demo_dir=demo_copy) != 0
