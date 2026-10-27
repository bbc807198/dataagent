"""数据契约测试：恒等式检出能力、规模断言、关联断言。

合成数据上验证契约逻辑本身有效（能检出违例、不会假阳性）；
真实数据上验证审计基线全部成立。
"""

from __future__ import annotations

import pytest

from dataagent import data_contracts


def _by_name(results):
    return {r.name: r for r in results}


class TestIdentitiesOnCleanSynthetic:
    def test_all_identities_pass(self, synth_db):
        results = data_contracts.check_identities(synth_db)
        assert results, "未产生任何恒等式检查"
        for r in results:
            assert r.passed, f"{r.name} 未通过: {r.actual}"
        assert len(results) == 5


class TestIdentityViolationDetection:
    """在临时测试库中制造违例，验证契约能检出（防止假阳性）。"""

    def test_detects_total_redeem_violation(self, synth_db):
        synth_db.execute(
            "UPDATE user_balance_table SET total_redeem_amt = total_redeem_amt + 1 "
            "WHERE user_id = 101 AND report_date = DATE '2024-01-03'"
        )
        r = _by_name(data_contracts.check_identities(synth_db))["identity_total_redeem"]
        assert not r.passed
        assert r.actual == "1 违例"

    def test_detects_direct_purchase_violation(self, synth_db):
        synth_db.execute(
            "UPDATE user_balance_table SET purchase_bal_amt = purchase_bal_amt + 7 "
            "WHERE user_id = 102 AND report_date = DATE '2024-01-09'"
        )
        r = _by_name(data_contracts.check_identities(synth_db))["identity_direct_purchase"]
        assert not r.passed
        assert r.actual == "1 违例"

    def test_detects_consume_category_violation(self, synth_db):
        # 注意：必须选 category1 已非空的行（2024-01-09 的 category1=100）；
        # 对 NULL 做 +5 会传播 NULL，无法制造违例。
        synth_db.execute(
            "UPDATE user_balance_table SET category1 = category1 + 5 "
            "WHERE user_id = 102 AND report_date = DATE '2024-01-09'"
        )
        r = _by_name(data_contracts.check_identities(synth_db))["identity_consume_categories"]
        assert not r.passed
        assert r.actual == "1 违例"

    def test_detects_total_purchase_violation(self, synth_db):
        synth_db.execute(
            "UPDATE user_balance_table SET share_amt = share_amt + 3 "
            "WHERE user_id = 101 AND report_date = DATE '2024-01-02'"
        )
        r = _by_name(data_contracts.check_identities(synth_db))["identity_total_purchase"]
        assert not r.passed
        assert r.actual == "1 违例"


class TestScaleAndJoinChecks:
    def test_synthetic_expectations_all_pass(self, synth_db):
        from conftest import SYNTHETIC_EXPECTATIONS
        report = data_contracts.run_contracts(synth_db, expectations=SYNTHETIC_EXPECTATIONS,
                                              include_scale=True)
        assert report.ok, f"失败项: {[r.name for r in report.fatal_failures]}"

    def test_orphan_profile_user_detected(self, synth_db):
        from conftest import SYNTHETIC_EXPECTATIONS
        r = _by_name(data_contracts.check_joins(synth_db, SYNTHETIC_EXPECTATIONS))[
            "join_profile_users_not_in_balance"]
        assert r.passed and r.actual == "1"  # 合成数据含 1 个孤儿画像用户

    def test_wrong_expectation_fails(self, synth_db):
        """契约必须真的在做比较：给错误期望必须失败。"""
        from conftest import SYNTHETIC_EXPECTATIONS
        bad = {k: (dict(v) if isinstance(v, dict) else v)
               for k, v in SYNTHETIC_EXPECTATIONS.items()}
        bad["user_balance_table"] = dict(bad["user_balance_table"])
        bad["user_balance_table"]["rows"] = 999
        report = data_contracts.run_contracts(synth_db, expectations=bad, include_scale=True)
        assert not report.ok
        names = [r.name for r in report.fatal_failures]
        assert "balance_row_count" in names

    def test_balance_flow_diagnostic_is_non_fatal(self, synth_db):
        from conftest import SYNTHETIC_EXPECTATIONS
        r = data_contracts.check_balance_flow_diagnostic(
            synth_db, expected_violations=SYNTHETIC_EXPECTATIONS["balance_flow_diagnostic"]["expected_violations"])
        assert r.passed and not r.fatal


@pytest.mark.integration
class TestRealDataContracts:
    def test_audit_baseline_all_pass(self, real_db):
        report = data_contracts.run_contracts(real_db)
        assert report.ok, "真实数据契约失败: " + "; ".join(
            f"{r.name}(期望{r.expected}/实际{r.actual})" for r in report.fatal_failures)

    def test_balance_row_count_baseline(self, real_db):
        r = _by_name(data_contracts.check_scale(real_db))["balance_row_count"]
        assert r.passed and r.actual == "2840421"

    def test_join_match_100pct(self, real_db):
        results = data_contracts.check_joins(real_db)
        for r in results:
            assert r.passed, f"{r.name}: {r.actual}"

    def test_balance_flow_diagnostic_matches_baseline(self, real_db):
        r = data_contracts.check_balance_flow_diagnostic(real_db)
        assert r.passed, f"余额流诊断与审计基线不符: {r.actual}"
        assert not r.fatal
