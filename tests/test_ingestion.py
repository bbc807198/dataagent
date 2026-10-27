"""导入模块测试：校验逻辑、日期规范化、空值语义、重复导入保护。"""

from __future__ import annotations

import duckdb
import pytest

from dataagent import config, db, ingestion


def _write_csv(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestRawCsvValidation:
    def test_synthetic_header_matches(self, synthetic_dir):
        for table, cols in ingestion.EXPECTED_COLUMNS.items():
            with open(synthetic_dir / f"{table}.csv", encoding="utf-8-sig") as f:
                header = f.readline().strip().split(",")
            assert header == cols, f"{table} 合成 fixture 表头与 EXPECTED_COLUMNS 不一致"

    def test_detects_bad_header(self, tmp_path):
        p = _write_csv(tmp_path / "bad.csv",
                       "user_id,wrong_col\n1,2\n")
        res = ingestion.validate_raw_csv("user_balance_table", p)
        assert not res.ok and not res.header_ok

    def test_detects_bad_date(self, tmp_path):
        p = _write_csv(tmp_path / "bad_date.csv",
                       "user_id,report_date,tBalance,yBalance,total_purchase_amt,direct_purchase_amt,"
                       "purchase_bal_amt,purchase_bank_amt,total_redeem_amt,consume_amt,transfer_amt,"
                       "tftobal_amt,tftocard_amt,share_amt,category1,category2,category3,category4\n"
                       "1,2024-1-2,0,0,0,0,0,0,0,0,0,0,0,0,,,,\n")
        res = ingestion.validate_raw_csv("user_balance_table", p)
        assert res.date_format_errors == 1

    def test_detects_bad_amount(self, tmp_path):
        p = _write_csv(tmp_path / "bad_amt.csv",
                       "user_id,report_date,tBalance,yBalance,total_purchase_amt,direct_purchase_amt,"
                       "purchase_bal_amt,purchase_bank_amt,total_redeem_amt,consume_amt,transfer_amt,"
                       "tftobal_amt,tftocard_amt,share_amt,category1,category2,category3,category4\n"
                       "1,20240102,0,0,12.5,0,0,0,0,0,0,0,0,0,,,,\n")
        res = ingestion.validate_raw_csv("user_balance_table", p)
        assert res.amount_parse_errors == 1

    def test_detects_field_count(self, tmp_path):
        p = _write_csv(tmp_path / "bad_cols.csv",
                       "user_id,report_date,tBalance,yBalance,total_purchase_amt,direct_purchase_amt,"
                       "purchase_bal_amt,purchase_bank_amt,total_redeem_amt,consume_amt,transfer_amt,"
                       "tftobal_amt,tftocard_amt,share_amt,category1,category2,category3,category4\n"
                       "1,20240102,0,0,0,0,0,0,0,0,0,0,0,0,,,,\n"
                       "2,20240103\n")
        res = ingestion.validate_raw_csv("user_balance_table", p)
        assert res.field_count_errors == 1


class TestSyntheticImport:
    def test_row_counts(self, synth_db):
        counts = {t: synth_db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in config.RAW_FILES}
        assert counts == {
            "user_balance_table": 12,
            "user_profile_table": 4,
            "mfd_day_share_interest": 11,
            "mfd_bank_shibor": 8,
        }

    def test_dates_normalized_to_date_type(self, synth_db):
        col_type = synth_db.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='user_balance_table' AND column_name='report_date'"
        ).fetchone()[0]
        assert col_type.upper() == "DATE"
        got = synth_db.execute(
            "SELECT min(report_date), max(report_date) FROM user_balance_table"
        ).fetchone()
        assert str(got[0]) == "2024-01-02" and str(got[1]) == "2024-01-12"

    def test_category_empty_is_null_by_design(self, synth_db):
        # consume=0 的行 category 全部为 NULL（已审计的设计性空值）
        nulls = synth_db.execute(
            "SELECT count(*) FROM user_balance_table "
            "WHERE consume_amt = 0 AND (category1 IS NOT NULL OR category2 IS NOT NULL "
            "OR category3 IS NOT NULL OR category4 IS NOT NULL)"
        ).fetchone()[0]
        assert nulls == 0
        # consume>0 的行 category 全部非空
        filled = synth_db.execute(
            "SELECT count(*) FROM user_balance_table "
            "WHERE consume_amt > 0 AND (category1 IS NULL AND category2 IS NULL "
            "AND category3 IS NULL AND category4 IS NULL)"
        ).fetchone()[0]
        assert filled == 0

    def test_amounts_are_bigint_integers(self, synth_db):
        col_type = synth_db.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='user_balance_table' AND column_name='total_purchase_amt'"
        ).fetchone()[0]
        assert col_type.upper() == "BIGINT"

    def test_city_kept_as_varchar_code(self, synth_db):
        col_type = synth_db.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='user_profile_table' AND column_name='city'"
        ).fetchone()[0]
        assert col_type.upper().startswith("VARCHAR")

    def test_reimport_refused_without_explicit_rebuild(self, synth_db, synthetic_dir):
        with pytest.raises(RuntimeError, match="拒绝重复导入"):
            ingestion.import_table(synth_db, "user_profile_table",
                                   synthetic_dir / "user_profile_table.csv")


class TestRebuildPolicy:
    def test_rebuild_then_reimport_ok(self, tmp_path, synthetic_dir):
        con = db.connect(tmp_path / "rb.duckdb")
        try:
            db.init_schema(con)
            r1 = ingestion.import_all(con, data_dir=synthetic_dir)
            assert all(r.ok for r in r1)
            db.rebuild_tables(con)
            r2 = ingestion.import_all(con, data_dir=synthetic_dir)
            assert all(r.ok for r in r2)
            assert con.execute("SELECT count(*) FROM user_balance_table").fetchone()[0] == 12
        finally:
            con.close()

    def test_comp_predict_table_excluded(self):
        assert "comp_predict_table" not in config.RAW_FILES
        assert set(config.RAW_FILES) == set(db.BUSINESS_TABLES)

    def test_post_import_checks_clean(self, synth_db):
        checks = ingestion.post_import_checks(synth_db)
        assert checks["user_balance_table"]["negative_amount_rows"] == 0
        assert checks["user_balance_table"]["null_key_or_amount"] == 0
        assert checks["mfd_day_share_interest"]["null_dates"] == 0
