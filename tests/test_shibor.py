"""SHIBOR 前向填充视图测试。

覆盖：原始表不动、日历脊柱范围、LOCF 来源与值、非 cummax、不用未来值、
截止日后特殊日期；真实数据下与 pandas LOCF 交叉验证。
"""

from __future__ import annotations

import datetime as dt

import pytest

from dataagent import shibor


def _view_row(con, d: str):
    return con.execute(
        f"SELECT mfd_date, source_obs_date, is_filled, Interest_O_N "
        f"FROM {shibor.FILL_VIEW_NAME} WHERE mfd_date = DATE '{d}'"
    ).fetchone()


class TestFillViewSynthetic:
    def test_view_covers_balance_spine(self, synth_db):
        lo, hi = synth_db.execute(
            "SELECT min(report_date), max(report_date) FROM user_balance_table"
        ).fetchone()
        n = synth_db.execute(
            f"SELECT count(*) FROM {shibor.FILL_VIEW_NAME}"
        ).fetchone()[0]
        assert n == (hi - lo).days + 1 == 11

    def test_raw_table_untouched(self, synth_db):
        assert synth_db.execute("SELECT count(*) FROM mfd_bank_shibor").fetchone()[0] == 8

    def test_weekend_filled_from_prior_observation(self, synth_db):
        # 2024-01-06/07 是周末：来源应为 01-05，值 3.2（不是 cummax 的 4.0）
        for d in ("2024-01-06", "2024-01-07"):
            row = _view_row(synth_db, d)
            assert str(row[0]) == d
            assert str(row[1]) == "2024-01-05", f"{d} 来源应为 01-05"
            assert bool(row[2]) is True
            assert abs(row[3] - 3.2) < 1e-9, f"{d} 应填 LOCF 值 3.2"

    def test_original_observation_not_filled(self, synth_db):
        row = _view_row(synth_db, "2024-01-09")
        assert str(row[1]) == "2024-01-09"
        assert bool(row[2]) is False

    def test_special_dates_after_last_observation(self, synth_db):
        # 复刻真实场景：最后观测 01-10，脊柱末两日 01-11/12 由其承接
        for d in ("2024-01-11", "2024-01-12"):
            row = _view_row(synth_db, d)
            assert str(row[1]) == "2024-01-10"
            assert bool(row[2]) is True
            assert abs(row[3] - 2.6) < 1e-9

    def test_view_matches_independent_locf(self, synth_db):
        fv = shibor.verify_fill_view(synth_db)
        assert fv.ok, f"填充校验失败: {fv.as_dict()}"
        assert fv.filled_rows == 4 and fv.original_rows == 7
        assert fv.special_dates.get("2024-01-11", {}).get("source_obs_date") == "2024-01-10"
        assert fv.last_obs_date == "2024-01-10"

    def test_no_future_observation_used(self, synth_db):
        # 所有 source_obs_date 必须 <= mfd_date（结构性保证，逐行断言）
        bad = synth_db.execute(
            f"SELECT count(*) FROM {shibor.FILL_VIEW_NAME} WHERE source_obs_date > mfd_date"
        ).fetchone()[0]
        assert bad == 0

    def test_not_cummax(self, synth_db):
        # 递减序列中 cummax 会把 01-01 的 4.0 带到所有填充日；视图必须不是 4.0
        row = _view_row(synth_db, "2024-01-06")
        assert abs(row[3] - 4.0) > 1e-9


@pytest.mark.integration
class TestFillViewRealData:
    def test_view_matches_independent_locf(self, real_db):
        fv = shibor.verify_fill_view(real_db)
        assert fv.ok, f"真实数据填充校验失败: {fv.as_dict()}"
        assert fv.spine_days == 427
        assert fv.filled_rows == 133 and fv.original_rows == 294
        assert fv.last_obs_date == "2014-08-29"

    def test_special_dates_2014_08_30_31(self, real_db):
        fv = shibor.verify_fill_view(real_db)
        assert fv.special_dates["2014-08-30"]["source_obs_date"] == "2014-08-29"
        assert fv.special_dates["2014-08-31"]["source_obs_date"] == "2014-08-29"

    def test_pandas_cross_check(self, real_db):
        """与 pandas 的正确 LOCF 结果逐格交叉验证。"""
        pd = pytest.importorskip("pandas")
        raw = real_db.execute(
            "SELECT mfd_date, Interest_O_N, Interest_1_W, Interest_2_W, Interest_1_M, "
            "Interest_3_M, Interest_6_M, Interest_9_M, Interest_1_Y "
            "FROM mfd_bank_shibor ORDER BY mfd_date"
        ).fetchdf()
        raw["mfd_date"] = pd.to_datetime(raw["mfd_date"])
        raw = raw.set_index("mfd_date").sort_index()
        d0, d1 = real_db.execute(
            "SELECT min(report_date), max(report_date) FROM user_balance_table"
        ).fetchone()
        spine = pd.date_range(d0, d1, freq="D")
        expected = raw.reindex(spine).ffill()
        view = real_db.execute(
            f"SELECT mfd_date, Interest_O_N, Interest_1_W, Interest_2_W, Interest_1_M, "
            f"Interest_3_M, Interest_6_M, Interest_9_M, Interest_1_Y "
            f"FROM {shibor.FILL_VIEW_NAME} ORDER BY mfd_date"
        ).fetchdf().set_index("mfd_date")
        view.index = pd.to_datetime(view.index)
        assert len(view) == len(expected) == 427
        for col in expected.columns:
            diff = (view[col].astype(float) - expected[col].astype(float)).abs().max()
            assert diff < 1e-9, f"{col} 与 pandas LOCF 不一致，最大差 {diff}"

    def test_raw_table_still_294(self, real_db):
        assert real_db.execute("SELECT count(*) FROM mfd_bank_shibor").fetchone()[0] == 294
