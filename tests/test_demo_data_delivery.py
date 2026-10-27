"""阶段 14：演示数据交付与图表缺失日期的回归测试（任务 E/F）。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataagent import config  # noqa: E402

DEMO_DIR = ROOT / "demo_data"
TABLES = ("user_balance_table", "user_profile_table",
          "mfd_day_share_interest", "mfd_bank_shibor")

# 加载建库脚本（scripts/ 不是包），用于校验清单严格性测试
_spec = importlib.util.spec_from_file_location(
    "_build_demo_db_delivery", ROOT / "scripts" / "build_demo_db.py")
build_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_mod)


def _tiny_demo(tmp: Path, names=None) -> Path:
    """构造只含小文件的 demo 目录，SHA256SUMS.txt 与文件内容精确一致。

    用于校验清单严格性测试，避免读 30MB 真实 Parquet。
    """
    dest = tmp / "demo_tiny"
    dest.mkdir(parents=True, exist_ok=True)
    names = list(build_mod.TABLES) if names is None else names
    lines = []
    for t in names:
        fname = f"{t}.parquet"
        p = dest / fname
        p.write_bytes(b"dummy-parquet-" + t.encode())
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {fname}")
    (dest / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


# ---------------------------------------------------------------- 任务 F：数据交付
class TestDemoDataDelivery:
    def test_demo_data_files_present(self):
        """仓库自带演示数据文件齐全（不依赖任何外部下载）。"""
        for t in TABLES:
            p = DEMO_DIR / f"{t}.parquet"
            assert p.is_file(), f"缺少 {p.name}"
            assert p.stat().st_size > 0

    def test_checksums_manifest_matches(self):
        """SHA-256 清单与实际文件一致（发布可校验）。"""
        sums = DEMO_DIR / "SHA256SUMS.txt"
        assert sums.is_file()
        for line in sums.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, name = line.split(maxsplit=1)
            path = DEMO_DIR / name.strip()
            assert path.is_file(), name
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name

    def test_demo_db_path_is_project_relative(self):
        """默认数据库路径从项目相对路径解析（不含个人绝对路径）。"""
        assert str(config.DB_PATH).startswith(str(ROOT))
        assert config.DB_PATH.name == "dataagent_demo.duckdb"
        assert "Users" not in str(config.DB_PATH).split(str(ROOT))[-1]

    def test_build_script_exists_and_offline(self):
        """一键建库脚本存在，且不依赖网络。"""
        script = ROOT / "scripts" / "build_demo_db.py"
        assert script.is_file()
        text = script.read_text(encoding="utf-8")
        assert "requests" not in text and "urllib" not in text
        assert "http" not in text.lower()

    @pytest.mark.skipif(not config.demo_db_available(),
                        reason="演示数据库尚未构建（先运行 scripts/build_demo_db.py）")
    def test_demo_db_has_four_tables_and_required_columns(self):
        """演示库包含四张业务表与关键指标字段。"""
        import duckdb
        con = duckdb.connect(str(config.DB_PATH), read_only=True)
        try:
            tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            for t in TABLES:
                assert t in tables, t
            cols = {r[0] for r in con.execute(
                "DESCRIBE user_balance_table").fetchall()}
            for c in ("user_id", "report_date", "total_purchase_amt",
                      "total_redeem_amt", "consume_amt", "transfer_amt",
                      "direct_purchase_amt", "share_amt"):
                assert c in cols, c
            pcols = {r[0] for r in con.execute(
                "DESCRIBE user_profile_table").fetchall()}
            assert {"user_id", "sex", "city", "constellation"} <= pcols
        finally:
            con.close()

    @pytest.mark.skipif(not config.demo_db_available(),
                        reason="演示数据库尚未构建")
    def test_demo_db_coverage_and_readonly(self):
        """覆盖 2013—2014 且连接只读。"""
        import duckdb
        con = duckdb.connect(str(config.DB_PATH), read_only=True)
        try:
            lo, hi = con.execute("SELECT MIN(report_date), MAX(report_date) "
                                 "FROM user_balance_table").fetchone()
            assert str(lo) == "2013-07-01"
            assert str(hi) == "2014-08-31"
            with pytest.raises(Exception):
                con.execute("CREATE TABLE probe(x INTEGER)")
        finally:
            con.close()

    @pytest.mark.skipif(not config.demo_db_available(),
                        reason="演示数据库尚未构建")
    def test_demo_db_metric_value_matches_documented_baseline(self):
        """2014-08 申购合计与文档基准一致（脱敏未改变金额口径）。"""
        import duckdb
        con = duckdb.connect(str(config.DB_PATH), read_only=True)
        try:
            total = con.execute(
                "SELECT SUM(total_purchase_amt) FROM user_balance_table "
                "WHERE report_date BETWEEN DATE '2014-08-01' AND DATE '2014-08-31'"
            ).fetchone()[0]
            assert total == 7_921_353_364
        finally:
            con.close()

    def test_no_plaintext_secrets_in_repo_text(self):
        """仓库文本文件中不得出现明文 Key / Token。"""
        import re
        pat = re.compile(r"sk-[A-Za-z0-9]{20,}")
        for p in list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("*.md")) \
                + list((ROOT / "scripts").glob("*.py")):
            text = p.read_text(encoding="utf-8", errors="ignore")
            assert not pat.search(text), p.name

    def test_gitignore_blocks_private_db_but_allows_demo_data(self):
        """私人数据库仍被忽略；演示数据（Parquet）可入库，构建产物仍被忽略。"""
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "*.duckdb" in text              # 任何 .duckdb（含构建产物）都不入库
        assert "/data/processed/" in text     # 私人/本地库目录不入库
        assert "/Purchase Redemption Data/" in text
        assert "!/demo_data/" in text         # 显式允许演示数据目录
        assert "!/demo_data/*.parquet" in text
        # 行为校验（仅在 Git 仓库内可做；干净目录可能未初始化 Git）
        import subprocess
        if not (ROOT / ".git").exists():
            pytest.skip("当前目录不是 Git 仓库，跳过 check-ignore 行为校验")
        r = subprocess.run(["git", "check-ignore", "-v",
                            "demo_data/user_balance_table.parquet"],
                           cwd=str(ROOT), capture_output=True, text=True)
        assert r.returncode != 0, "演示数据不应被忽略"
        r2 = subprocess.run(["git", "check-ignore", "-v",
                             "data/processed/dataagent_demo.duckdb"],
                            cwd=str(ROOT), capture_output=True, text=True)
        assert r2.returncode == 0, "构建出的演示库应被忽略"


# ---------------------------------------------------------------- 任务 E：图表缺失日期
class TestCrossYearChartGaps:
    def _stats(self, pairs_spec):
        from dataagent.analytics_models import WindowComparisonStats, WindowPair
        pairs = []
        for align_key, main_v, comp_v in pairs_spec:
            day = int(align_key.split("-")[1])
            pairs.append(WindowPair(
                group={"report_date": f"2014-08-{day:02d}"},
                main_value=main_v, comparison_value=comp_v,
                abs_diff=main_v - comp_v, change_rate=None, direction="up",
                align_key=align_key,
                main_date=f"2014-08-{day:02d}",
                comparison_date=f"2013-08-{day:02d}"))
        return WindowComparisonStats(
            pairs=pairs, main_window=("2014-08-01", "2014-08-31"),
            comparison_window=("2013-08-01", "2013-08-31"),
            alignment="day_of_month", main_total=1.0, comparison_total=1.0)

    def test_axis_is_full_month_range(self):
        """横轴为完整月份日历天数（缺失日与缺失 31 日也占位到月末）。"""
        from dataagent.visualization import cross_year_series
        st = self._stats([("08-01", 10.0, 5.0), ("08-03", 12.0, 6.0)])
        days, main_v, comp_v = cross_year_series(st)
        assert days == list(range(1, 32))            # 完整 8 月，即使 31 日无观测
        assert len(main_v) == len(comp_v) == 31
        assert math.isnan(main_v[1]) and math.isnan(comp_v[1])   # 8-02 缺失
        assert main_v[0] == 10.0 and comp_v[0] == 5.0
        assert main_v[2] == 12.0 and comp_v[2] == 6.0

    def test_missing_day_is_nan_not_zero(self):
        """缺失观测用 NaN（不得用 0 替代）。"""
        from dataagent.visualization import cross_year_series
        st = self._stats([("08-01", 10.0, 5.0), ("08-03", 12.0, 6.0)])
        days, main_v, comp_v = cross_year_series(st)
        idx2 = days.index(2)
        assert math.isnan(main_v[idx2])
        assert math.isnan(comp_v[idx2])
        assert main_v[idx2] != 0.0

    def test_two_years_are_separate_series(self):
        """两个年份不混在同一条序列里。"""
        from dataagent.visualization import cross_year_series
        st = self._stats([("08-01", 10.0, 5.0), ("08-02", 11.0, 7.0)])
        days, main_v, comp_v = cross_year_series(st)
        assert main_v != comp_v
        assert main_v[0] == 10.0 and comp_v[0] == 5.0

    def test_single_side_observation_kept(self):
        """只在一年出现的合法观测必须保留（不丢弃）。"""
        from dataagent.visualization import cross_year_series
        st = self._stats([("08-01", 10.0, 5.0), ("08-02", 11.0, 7.0)])
        days, main_v, comp_v = cross_year_series(st)
        assert days == list(range(1, 32))            # 完整 8 月
        assert main_v[0] == 10.0 and comp_v[0] == 5.0
        assert main_v[1] == 11.0 and comp_v[1] == 7.0
        assert math.isnan(main_v[2]) and math.isnan(comp_v[2])

    def test_one_sided_missing_shows_observed(self):
        """单边缺失（仅主窗口有该日）时，主窗口观测值仍显示、对比侧为 NaN；
        且不应产生虚假配对。使用 compare_windows 直接构造（非仅完整 WindowPair）。"""
        from dataagent.analytics import compare_windows
        from dataagent.visualization import cross_year_series
        main_rows = [("2014-08-01", 100.0), ("2014-08-20", 250.0),
                     ("2014-08-31", 300.0)]
        comp_rows = [("2013-08-01", 80.0), ("2013-08-31", 260.0)]  # 8-20 缺失
        st = compare_windows(
            main_rows, comp_rows,
            ["report_date", "purchase_amount"],
            ["report_date", "purchase_amount"], ["report_date"],
            "purchase_amount", ("2014-08-01", "2014-08-31"),
            ("2013-08-01", "2013-08-31"), "元", alignment="day_of_month")
        days, main_v, comp_v = cross_year_series(st)
        assert days == list(range(1, 32))
        assert main_v[19] == 250.0          # 第 20 天（索引 19）主窗口有值
        assert math.isnan(comp_v[19])        # 对比窗口 8-20 缺失 -> NaN
        assert main_v[0] == 100.0 and comp_v[0] == 80.0     # 两侧都有的日期
        assert main_v[30] == 300.0 and comp_v[30] == 260.0
        assert not any(p.align_key == "08-20" for p in st.pairs)  # 不伪造配对
        # 单边缺失日应保留在各自每日金额字典中
        assert st.main_daily_amounts.get(20) == 250.0
        assert 20 not in st.comparison_daily_amounts

    def test_chart_file_generated_with_gaps(self):
        """最终可视化可生成（含 NaN 断点，不抛异常）。"""
        import tempfile
        from dataagent.analytics_models import (AnalysisResult, AnalysisStatus,
                                                MetricInfo)
        from dataagent.visualization import plot_cross_year_daily
        st = self._stats([("08-01", 10.0, 5.0), ("08-03", 12.0, 6.0)])
        res = AnalysisResult(
            question="q", analysis_type="window_comparison",
            status=AnalysisStatus.OK, statistics=st,
            metrics=[MetricInfo(metric_id="purchase_amount", name_zh="申购金额",
                                unit="元", formula="SUM(...)")])
        out = Path(tempfile.mkdtemp())
        cr = plot_cross_year_daily(res, out)
        assert cr.status == "ok" and cr.path
        assert Path(cr.path).stat().st_size > 0


# ---------------------------------------------------------------- 任务 F：无 Key / Fake SDK
class TestPortability:
    def test_no_key_mock_demo_runs(self):
        """无 Key 的 Mock 演示在演示数据上可用。"""
        from web import service
        out = service.run_question("帮我分析申购和赎回金额最近的趋势。",
                                   "mock", "synthetic")
        assert out.ok

    @pytest.mark.skipif(not config.demo_db_available(),
                        reason="演示数据库尚未构建")
    def test_fake_sdk_can_analyze_demo_database(self):
        """Fake SDK 下可对演示数据库做真实查询与统计。"""
        import tempfile
        from datetime import date
        from types import SimpleNamespace
        from dataagent.agent import AgentConfig, run_agent
        from dataagent.query_service import QueryService

        plan = {
            "question": "分析 2014 年 8 月每天的申购和赎回趋势",
            "intent": "趋势", "analysis_type": "time_trend",
            "metrics": [{"metric_id": "purchase_amount"},
                        {"metric_id": "redeem_amount"}],
            "time_range": {"type": "explicit_dates",
                           "resolved_start_date": "2014-08-01",
                           "resolved_end_date": "2014-08-31",
                           "resolution_status": "resolved"},
            "group_by": [], "filters": [], "statistics": ["SUM"],
            "outputs": {"summary_table": True, "trend_chart": True,
                        "chinese_report": True},
            "unresolved": [], "notes": [],
        }

        class _LLM:
            def __init__(self):
                self.model = "fake"
                self.chat = SimpleNamespace(completions=self)

            def create(self, **kw):
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(plan)),
                    finish_reason="stop")])

        cfg = AgentConfig(model_mode="real", data_mode="project_db",
                          db_path=config.DB_PATH,
                          data_range=(date(2013, 7, 1), date(2014, 8, 31)),
                          output_dir=Path(tempfile.mkdtemp()),
                          llm_client=_LLM())
        with QueryService(config.DB_PATH) as qs:
            res = run_agent(plan["question"], cfg, query_service=qs)
        assert res.status.value in ("report_generated",
                                    "report_generated_with_warnings")
        assert res.chart_paths
        text = Path(res.report_path).read_text(encoding="utf-8")
        assert "2014-08" in text


# ------------------------------------------------ 阶段 14.1 任务 B：校验清单严格性
class TestChecksumManifestStrictness:
    """SHA256SUMS.txt 必须严格、恰好覆盖 4 个预期 Parquet，异常一律失败关闭。"""

    def test_manifest_ok_passes(self, tmp_path):
        d = _tiny_demo(tmp_path)
        assert build_mod.verify_checksums(demo_dir=d) == 0

    def test_missing_manifest_fails_closed(self, tmp_path):
        """清单缺失 → 非零（不静默跳过）。"""
        d = _tiny_demo(tmp_path)
        (d / "SHA256SUMS.txt").unlink()
        assert build_mod.verify_checksums(demo_dir=d) != 0

    def test_duplicate_entry_fails(self, tmp_path):
        d = _tiny_demo(tmp_path)
        sums = d / "SHA256SUMS.txt"
        lines = sums.read_text(encoding="utf-8").splitlines()
        sums.write_text("\n".join(lines + [lines[0]]) + "\n", encoding="utf-8")
        assert build_mod.verify_checksums(demo_dir=d) != 0

    def test_missing_entry_fails(self, tmp_path):
        """清单只覆盖 3 个（缺 1 个预期文件）→ 失败。"""
        d = _tiny_demo(tmp_path)
        sums = d / "SHA256SUMS.txt"
        lines = sums.read_text(encoding="utf-8").splitlines()
        sums.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        assert build_mod.verify_checksums(demo_dir=d) != 0

    def test_unexpected_entry_fails(self, tmp_path):
        """清单多列一个非预期文件 → 失败。"""
        d = _tiny_demo(tmp_path)
        extra = d / "extra.parquet"
        extra.write_bytes(b"x")
        h = hashlib.sha256(extra.read_bytes()).hexdigest()
        with open(d / "SHA256SUMS.txt", "a", encoding="utf-8") as f:
            f.write(f"{h}  extra.parquet\n")
        assert build_mod.verify_checksums(demo_dir=d) != 0

    def test_hash_mismatch_fails(self, tmp_path):
        d = _tiny_demo(tmp_path)
        sums = d / "SHA256SUMS.txt"
        lines = sums.read_text(encoding="utf-8").splitlines()
        lines[0] = "0" * 64 + "  " + lines[0].split(None, 1)[1]
        sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert build_mod.verify_checksums(demo_dir=d) != 0

    def test_missing_file_fails(self, tmp_path):
        d = _tiny_demo(tmp_path)
        (d / "user_balance_table.parquet").unlink()
        assert build_mod.verify_checksums(demo_dir=d) != 0

    def test_malformed_line_fails(self, tmp_path):
        d = _tiny_demo(tmp_path)
        with open(d / "SHA256SUMS.txt", "a", encoding="utf-8") as f:
            f.write("not-a-valid-manifest-line\n")
        assert build_mod.verify_checksums(demo_dir=d) != 0

    def test_verify_failure_does_not_touch_existing_db(self, tmp_path, monkeypatch):
        """校验失败（清单哈希不符）时：已有数据库不被删除或覆盖。"""
        d = _tiny_demo(tmp_path)
        sums = d / "SHA256SUMS.txt"
        lines = sums.read_text(encoding="utf-8").splitlines()
        lines[0] = "0" * 64 + "  " + lines[0].split(None, 1)[1]
        sums.write_text("\n".join(lines) + "\n", encoding="utf-8")

        out = tmp_path / "data" / "processed" / "dataagent_demo.duckdb"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"EXISTING-DB-CONTENT")

        monkeypatch.setattr(build_mod, "DEMO_DIR", d)
        monkeypatch.setattr(build_mod, "DEFAULT_OUT", out)
        monkeypatch.setattr(sys, "argv",
                            ["build_demo_db.py", "--verify", "--out", str(out)])
        rc = build_mod.main()
        assert rc != 0
        assert out.read_bytes() == b"EXISTING-DB-CONTENT"
