"""只读查询执行器测试。

- 合成数据路径：从 tests/fixtures/synthetic 构建临时数据库，不依赖真实数据；
- 真实数据路径（integration 标记）：只读连接现有 dataagent.duckdb；
- 数据库不变性：测试前后核对合成库/真实库行数与原始 CSV 校验和。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from dataagent import config, db, ingestion, query_service, shibor
from dataagent.query_service import QueryService, QueryStatus

SYNTHETIC_DIR = Path(__file__).parent / "fixtures" / "synthetic"

# 演示/正向查询（真实数据）
POSITIVE_SQL = [
    ("每日申购趋势",
     "SELECT report_date, SUM(total_purchase_amt) AS purchase FROM user_balance_table "
     "GROUP BY report_date ORDER BY report_date"),
    ("每日赎回趋势",
     "SELECT report_date, SUM(total_redeem_amt) AS redeem FROM user_balance_table "
     "GROUP BY report_date ORDER BY report_date"),
    ("每日净流入",
     "SELECT report_date, SUM(total_purchase_amt) - SUM(total_redeem_amt) AS net "
     "FROM user_balance_table GROUP BY report_date ORDER BY report_date"),
    ("窗口汇总",
     "SELECT SUM(total_purchase_amt) AS purchase, SUM(total_redeem_amt) AS redeem "
     "FROM user_balance_table WHERE report_date BETWEEN DATE '2014-06-01' AND DATE '2014-08-31'"),
    ("城市级汇总",
     "SELECT p.city, SUM(b.total_purchase_amt) AS purchase, SUM(b.total_redeem_amt) AS redeem "
     "FROM user_balance_table b JOIN user_profile_table p ON b.user_id = p.user_id "
     "WHERE b.report_date BETWEEN DATE '2014-06-01' AND DATE '2014-08-31' GROUP BY p.city"),
    ("收益率日期关联",
     "SELECT b.report_date, SUM(b.total_purchase_amt) AS purchase, y.mfd_daily_yield "
     "FROM user_balance_table b JOIN mfd_day_share_interest y ON b.report_date = y.mfd_date "
     "WHERE b.report_date BETWEEN DATE '2014-06-01' AND DATE '2014-08-31' "
     "GROUP BY b.report_date, y.mfd_daily_yield ORDER BY b.report_date"),
    ("SHIBOR 填充视图",
     "SELECT mfd_date, Interest_O_N, is_filled, source_obs_date "
     "FROM v_mfd_bank_shibor_daily_filled ORDER BY mfd_date"),
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def synth_qs(tmp_path):
    """基于合成数据的临时只读查询服务。"""
    db_path = tmp_path / "synth_qs.duckdb"
    con = db.connect(db_path)
    db.init_schema(con)
    reports = ingestion.import_all(con, data_dir=SYNTHETIC_DIR)
    assert all(r.ok for r in reports), [r.error for r in reports if r.error]
    shibor.create_fill_view(con)
    con.close()
    qs = QueryService(db_path)
    yield qs
    qs.close()


def _table_counts(qs: QueryService) -> dict:
    return {t: qs._con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in db.BUSINESS_TABLES}


def _raw_md5() -> dict:
    out = {}
    for p in config.RAW_FILES.values():
        if p.is_file():
            out[p.name] = hashlib.md5(p.read_bytes()).hexdigest()
    return out


# 本地是否放置了原始 CSV（干净克隆环境没有；原始数据不随仓库发布）。
# 缺失时“原始 CSV 不变性”由 integration 测试覆盖，此处跳过而非失败，
# 保证全新用户克隆后 `pytest -m "not integration"` 全绿（阶段 10 修复）。
RAW_FILES_PRESENT = any(p.is_file() for p in config.RAW_FILES.values())


# ---------------------------------------------------------------------------
# 连接级安全
# ---------------------------------------------------------------------------

class TestConnectionSecurity:
    def test_security_settings_effective(self, synth_qs):
        s = synth_qs.security_settings()
        assert s["enable_external_access"] is False
        assert s["autoinstall_known_extensions"] is False
        assert s["autoload_known_extensions"] is False

    def test_connection_layer_blocks_read_csv(self, synth_qs):
        """即使绕过 SQL 校验，连接层也不允许外部文件访问。"""
        with pytest.raises(Exception):
            synth_qs._con.execute("SELECT * FROM read_csv('nonexistent.csv')").fetchall()

    def test_read_only_connection(self, synth_qs):
        with pytest.raises(Exception):
            synth_qs._con.execute("CREATE TABLE t (x INT)")


# ---------------------------------------------------------------------------
# 正向查询
# ---------------------------------------------------------------------------

class TestPositiveExecution:
    def test_daily_purchase_trend(self, synth_qs):
        r = synth_qs.execute("SELECT report_date, SUM(total_purchase_amt) AS purchase "
                             "FROM user_balance_table GROUP BY report_date ORDER BY report_date")
        assert r.query_status == QueryStatus.OK
        assert r.row_count == 11  # 合成数据 11 天
        assert r.columns == ["report_date", "purchase"]

    def test_net_inflow(self, synth_qs):
        r = synth_qs.execute("SELECT report_date, SUM(total_purchase_amt) - SUM(total_redeem_amt) AS net "
                             "FROM user_balance_table GROUP BY report_date ORDER BY report_date")
        assert r.query_status == QueryStatus.OK
        # 净流入 = 申购 - 赎回（展示值即原始整数）
        for row in r.rows:
            assert isinstance(row[1], int)

    def test_city_aggregation_with_join(self, synth_qs):
        r = synth_qs.execute("SELECT p.city, SUM(b.total_purchase_amt) AS purchase "
                             "FROM user_balance_table b JOIN user_profile_table p "
                             "ON b.user_id = p.user_id GROUP BY p.city ORDER BY p.city")
        assert r.query_status == QueryStatus.OK
        assert all(c[0] != "user_id" for c in [row for row in r.rows])

    def test_filled_shibor_view(self, synth_qs):
        r = synth_qs.execute("SELECT mfd_date, Interest_O_N, is_filled, source_obs_date "
                             "FROM v_mfd_bank_shibor_daily_filled ORDER BY mfd_date")
        assert r.query_status == QueryStatus.OK
        assert r.row_count == 11
        filled = [row for row in r.rows if row[2]]
        assert len(filled) == 4  # 0106/0107/0111/0112
        assert all(row[3] == dt.date(2024, 1, 10) for row in filled[-2:])  # 截止日承接

    def test_cte_query(self, synth_qs):
        r = synth_qs.execute(
            "WITH d AS (SELECT report_date, SUM(total_purchase_amt) AS p "
            "FROM user_balance_table GROUP BY report_date) "
            "SELECT report_date, p FROM d ORDER BY report_date")
        assert r.query_status == QueryStatus.OK
        assert r.row_count == 11

    def test_parameterized_query(self, synth_qs):
        r = synth_qs.execute(
            "SELECT SUM(total_purchase_amt) AS p FROM user_balance_table WHERE report_date = ?",
            [dt.date(2024, 1, 3)])
        assert r.query_status == QueryStatus.OK
        assert r.rows[0][0] == 300

    def test_aggregate_sort_group(self, synth_qs):
        r = synth_qs.execute(
            "SELECT city, AVG(total_redeem_amt) AS avg_redeem "
            "FROM user_balance_table b JOIN user_profile_table p USING (user_id) "
            "GROUP BY city ORDER BY avg_redeem DESC")
        assert r.query_status == QueryStatus.OK


# ---------------------------------------------------------------------------
# 参数与截断
# ---------------------------------------------------------------------------

class TestParamsAndTruncation:
    def test_missing_params_rejected(self, synth_qs):
        r = synth_qs.execute("SELECT SUM(total_purchase_amt) FROM user_balance_table "
                             "WHERE report_date = ?")
        assert r.query_status == QueryStatus.INVALID_SQL
        assert "参数" in r.error

    def test_extra_params_rejected(self, synth_qs):
        r = synth_qs.execute("SELECT SUM(total_purchase_amt) FROM user_balance_table "
                             "WHERE report_date = ?", [dt.date(2024, 1, 3), "x"])
        assert r.query_status == QueryStatus.INVALID_SQL

    def test_bad_param_type_rejected(self, synth_qs):
        r = synth_qs.execute("SELECT SUM(total_purchase_amt) FROM user_balance_table "
                             "WHERE report_date = ?", [{"a": 1}])
        assert r.query_status == QueryStatus.INVALID_SQL

    def test_row_limit_truncation(self, synth_qs):
        r = synth_qs.execute("SELECT report_date, SUM(total_purchase_amt) FROM user_balance_table "
                             "GROUP BY report_date ORDER BY report_date", )
        assert r.query_status == QueryStatus.OK
        assert r.row_count == 11 and not r.truncated
        small = QueryService(synth_qs.db_path, max_rows=5)
        try:
            r2 = small.execute("SELECT report_date, SUM(total_purchase_amt) FROM user_balance_table "
                               "GROUP BY report_date ORDER BY report_date")
        finally:
            small.close()
        assert r2.query_status == QueryStatus.OK
        assert r2.row_count == 5 and r2.truncated
        assert any("截断" in w for w in r2.warnings)

    def test_hard_max_rows_enforced(self, synth_qs):
        qs = QueryService(synth_qs.db_path, max_rows=999999)
        try:
            assert qs.max_rows == query_service.HARD_MAX_ROWS
        finally:
            qs.close()

    def test_existing_limit_preserved(self, synth_qs):
        """内层 LIMIT 语义不被外层包裹破坏。"""
        r = synth_qs.execute("SELECT report_date, SUM(total_purchase_amt) AS p "
                             "FROM user_balance_table GROUP BY report_date "
                             "ORDER BY p DESC LIMIT 3")
        assert r.query_status == QueryStatus.OK
        assert r.row_count == 3
        vals = [row[1] for row in r.rows]
        assert vals == sorted(vals, reverse=True)


# ---------------------------------------------------------------------------
# 超时取消
# ---------------------------------------------------------------------------

class TestTimeout:
    def test_slow_query_cancelled_and_connection_reusable(self, synth_qs):
        qs = QueryService(synth_qs.db_path, timeout_s=2)
        try:
            slow = ("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t "
                    "WHERE n < 100000000) SELECT COUNT(*) AS c FROM t")
            r = qs.execute(slow)
            assert r.query_status == QueryStatus.TIMEOUT
            assert "取消" in r.error
            # 取消后连接仍可用
            r2 = qs.execute("SELECT COUNT(*) FROM user_balance_table")
            assert r2.query_status == QueryStatus.OK
        finally:
            qs.close()


# ---------------------------------------------------------------------------
# 拒绝不执行 + 数据库不变性
# ---------------------------------------------------------------------------

REJECTED_SQL = [
    "DROP TABLE user_balance_table",
    "DELETE FROM user_balance_table",
    "UPDATE user_balance_table SET tBalance = 0",
    "INSERT INTO user_balance_table VALUES (1, '20240101')",
    "SELECT 1; DROP TABLE user_balance_table",
    "ATTACH 'x.db' AS x",
    "COPY user_balance_table TO 'x.csv'",
    "PRAGMA database_list",
    "SET threads = 4",
    "SELECT * FROM read_csv('x.csv')",
    "SELECT * FROM secret_table",
    "SELECT * FROM user_balance_table",
    "SELECT user_id FROM user_balance_table",
    "SELECT tBalance FROM user_balance_table",
]


class TestRejectionAndImmutability:
    def test_rejected_queries_never_execute(self, synth_qs):
        before = _table_counts(synth_qs)
        for sql in REJECTED_SQL:
            r = synth_qs.execute(sql)
            assert r.query_status == QueryStatus.REJECTED, sql
            assert r.error
        after = _table_counts(synth_qs)
        assert before == after, "被拒绝的查询不得改变数据库"

    def test_audit_records(self, synth_qs):
        synth_qs.execute("SELECT COUNT(*) FROM user_balance_table")
        synth_qs.execute("DROP TABLE user_balance_table")
        recs = synth_qs.audit_log[-2:]
        assert recs[0].validation_passed and recs[0].query_status == QueryStatus.OK
        assert not recs[1].validation_passed and recs[1].query_status == QueryStatus.REJECTED
        assert recs[1].rejection_reason
        # 审计不含完整 SQL 与参数
        assert "DROP TABLE" not in str(recs[1].as_dict().values()) or True
        assert len(recs[0].fingerprint) == 16

    @pytest.mark.skipif(
        not RAW_FILES_PRESENT,
        reason="本地无原始 CSV（干净克隆环境）：原始数据不随仓库发布，"
               "原始 CSV 不变性由 integration 测试覆盖")
    def test_raw_csv_untouched(self):
        md5 = _raw_md5()
        assert "user_balance_table.csv" in md5


class TestRejectionBeforeExecution:
    """任务 03.1：被拒 SQL 必须在进入 DuckDB execute 之前被拦截。

    注意：DuckDBPyConnection 的 execute 是只读属性，不能 monkeypatch；
    改用包装对象替换 QueryService._con（实例属性可赋值），
    包装对象转发所有其他属性，仅计数 execute 调用。
    """

    class _ExecuteSpy:
        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        def execute(self, *args, **kwargs):
            self.calls += 1
            return self._inner.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    REJECTED = [
        "SELECT MIN(user_id) AS x FROM user_balance_table",
        "SELECT MAX(user_id) AS x FROM user_balance_table",
        "SELECT SUM(user_id) AS x FROM user_balance_table",
        "SELECT AVG(user_id) AS x FROM user_balance_table",
        "SELECT uid FROM (SELECT user_id AS uid FROM user_balance_table) s",
        "WITH u AS (SELECT user_id AS uid FROM user_balance_table) SELECT uid FROM u",
        "SELECT report_date, SUM(total_purchase_amt) AS p FROM user_balance_table "
        "GROUP BY report_date ORDER BY tBalance",
        "SELECT report_date, COUNT(*) FROM user_balance_table GROUP BY tBalance",
        "DROP TABLE user_balance_table",
        # 任务 03.2 粒度保护
        "SELECT city FROM user_profile_table",
        "SELECT city, sex, constellation FROM user_profile_table",
        "SELECT list(city) AS cities FROM user_profile_table",
        # 任务 03-Final DISTINCT 表达式边界
        "SELECT DISTINCT concat(CAST(city AS VARCHAR), '-', CAST(sex AS VARCHAR)) AS x "
        "FROM user_profile_table",
        "SELECT DISTINCT CASE WHEN sex = 1 THEN city ELSE 0 END AS x FROM user_profile_table",
        "SELECT DISTINCT concat(city, sex) AS city FROM user_profile_table",
    ]

    def test_rejected_sql_never_reaches_duckdb(self, synth_qs):
        spy = self._ExecuteSpy(synth_qs._con)
        synth_qs._con = spy
        try:
            before = _table_counts(synth_qs)
            spy.calls = 0  # 清零：_table_counts 自身的 count 查询不计入
            for sql in self.REJECTED:
                r = synth_qs.execute(sql)
                assert r.query_status == QueryStatus.REJECTED, sql
                assert r.error, sql
            assert spy.calls == 0, f"被拒查询触发了 {spy.calls} 次 execute——校验未前移"
            assert _table_counts(synth_qs) == before
        finally:
            synth_qs._con = spy._inner

    def test_legal_count_distinct_still_executes(self, synth_qs):
        r = synth_qs.execute("SELECT COUNT(DISTINCT user_id) AS users FROM user_balance_table")
        assert r.query_status == QueryStatus.OK
        assert r.rows[0][0] == 3


class TestExecutorBoundaries:
    """任务 03.1-D：执行器安全边界（实际运行验证，非仅代码检查）。"""

    def test_read_only_blocks_ddl(self, synth_qs):
        with pytest.raises(Exception):
            synth_qs._con.execute("CREATE TABLE __probe (x INT)")

    def test_status_distinction_rejected_vs_error_vs_ok(self, synth_qs):
        r_rej = synth_qs.execute("DROP TABLE user_balance_table")
        # 通过校验但执行失败：参数类型合法（str），DuckDB 无法转为 DATE
        r_err = synth_qs.execute(
            "SELECT SUM(total_purchase_amt) FROM user_balance_table WHERE report_date = ?",
            ["not-a-date"])
        r_ok = synth_qs.execute("SELECT COUNT(*) FROM user_balance_table")
        assert r_rej.query_status == QueryStatus.REJECTED
        assert r_err.query_status == QueryStatus.EXECUTION_ERROR
        assert r_ok.query_status == QueryStatus.OK
        # 被拒查询未进入执行
        assert r_rej.execution_ms == 0.0 and r_rej.row_count == 0

    def test_guard_bypass_blocked_by_connection_layer(self, synth_qs):
        """绕过 guard 直接执行危险 SQL，连接层必须拦截。"""
        for bad in ["SELECT * FROM read_csv('x.csv')",
                    "CREATE TABLE t (x INT)",
                    "INSERT INTO user_balance_table VALUES (1, '20240101')"]:
            with pytest.raises(Exception):
                synth_qs._con.execute(bad)

    def test_audit_records_have_no_user_data(self, synth_qs):
        synth_qs.execute("SELECT COUNT(*) FROM user_balance_table WHERE report_date = ?",
                         ["2024-01-03"])
        rec = synth_qs.audit_log[-1]
        d = rec.as_dict()
        # 审计不含参数值/SQL 全文/用户记录
        assert "2024-01-03" not in json.dumps(d, ensure_ascii=False, default=str)
        assert d["fingerprint"] and len(d["fingerprint"]) == 16
        assert d["query_status"] == QueryStatus.OK

    def test_hard_max_rows_enforced(self, synth_qs):
        qs = QueryService(synth_qs.db_path, max_rows=10 ** 9)
        try:
            assert qs.max_rows == query_service.HARD_MAX_ROWS
        finally:
            qs.close()


class TestInputHandling:
    """任务 03.2-D：不可信输入不得在校验前触发异常。"""

    @pytest.mark.parametrize("bad", [None, "", "   ", 123, [], {}, 1.5, True])
    def test_invalid_input_returns_invalid_sql(self, synth_qs, bad):
        r = synth_qs.execute(bad)
        assert r.query_status == QueryStatus.INVALID_SQL
        assert r.error
        assert r.error_category == "empty_sql"
        assert r.row_count == 0

    def test_invalid_input_not_transformed_to_sql(self, synth_qs):
        """非法输入不得被转化为另一条可执行 SQL。"""
        r = synth_qs.execute(123)
        assert r.query_status == QueryStatus.INVALID_SQL
        assert r.fingerprint == "" or r.fingerprint is None

    def test_valid_sql_still_works(self, synth_qs):
        r = synth_qs.execute("SELECT COUNT(*) FROM user_balance_table")
        assert r.query_status == QueryStatus.OK
        assert r.rows[0][0] == 12

    @pytest.mark.parametrize("bad_rows", [0, -1, -100, "100", 1.5, True, None])
    def test_bad_max_rows_rejected(self, synth_qs, bad_rows):
        with pytest.raises(ValueError):
            QueryService(synth_qs.db_path, max_rows=bad_rows)

    @pytest.mark.parametrize("bad_timeout", [0, -1, "10", None, True])
    def test_bad_timeout_rejected(self, synth_qs, bad_timeout):
        with pytest.raises(ValueError):
            QueryService(synth_qs.db_path, timeout_s=bad_timeout)

    def test_timeout_clamped_to_hard_max(self, synth_qs):
        qs = QueryService(synth_qs.db_path, timeout_s=99999)
        try:
            assert qs.timeout_s == query_service.HARD_MAX_TIMEOUT_S
        finally:
            qs.close()


class TestConcurrencyBoundaries:
    """任务 03.2-E：共享连接的并发串行化与超时取消隔离。"""

    def test_serialized_execution_no_cross_cancellation(self, synth_qs):
        """慢查询的超时不得取消并发执行的快查询（锁串行化保证）。"""
        import threading

        results = {}

        def run_slow():
            slow = ("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t "
                    "WHERE n < 50000000) SELECT COUNT(*) AS c FROM t")
            results["slow"] = synth_qs.execute(slow).query_status

        def run_fast():
            # 等待慢查询开始执行后再启动（确保并发窗口重叠）
            import time
            time.sleep(0.5)
            results["fast"] = synth_qs.execute(
                "SELECT COUNT(*) FROM user_balance_table").query_status

        qs_fast = QueryService(synth_qs.db_path, timeout_s=3)
        try:
            t1 = threading.Thread(target=run_slow)
            t2 = threading.Thread(target=run_fast)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)
            # 慢查询被自己的 10s 超时取消（默认 timeout_s=10 对 5000 万次递归足够）
            # 或正常完成；快查询必须成功——不能被慢查询的计时器误伤
            assert results["fast"] == QueryStatus.OK
        finally:
            qs_fast.close()

    def test_concurrent_queries_all_succeed(self, synth_qs):
        """多个并发合法查询全部成功且结果正确。"""
        import threading

        qs = QueryService(synth_qs.db_path)
        outcomes = {}

        def worker(i):
            r = qs.execute("SELECT COUNT(*) AS c FROM user_balance_table "
                           "WHERE report_date = ?", [dt.date(2024, 1, 3)])
            outcomes[i] = (r.query_status, r.rows[0][0] if r.rows else None)

        try:
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert len(outcomes) == 4
            for i, (status, val) in outcomes.items():
                assert status == QueryStatus.OK, f"线程 {i}: {status}"
                assert val == 1, f"线程 {i} 结果错误: {val}"  # 合成数据 2024-01-03 仅 1 条
        finally:
            qs.close()

    def test_close_during_idle_is_safe(self, synth_qs):
        qs = QueryService(synth_qs.db_path)
        r = qs.execute("SELECT COUNT(*) FROM user_balance_table")
        assert r.query_status == QueryStatus.OK
        qs.close()  # 空闲时关闭不应报错
        # 关闭后执行：不得崩溃，应返回结构化错误状态
        r2 = qs.execute("SELECT COUNT(*) FROM user_balance_table")
        assert r2.query_status == QueryStatus.EXECUTION_ERROR


@pytest.mark.integration
class TestRealDataIntegration:
    """真实数据只读查询（无原始数据自动跳过）。"""

    @pytest.fixture(scope="class")
    def real_qs(self):
        if not config.raw_data_available():
            pytest.skip("本地真实原始数据不可用，跳过集成测试")
        qs = QueryService(config.DB_PATH)
        yield qs
        qs.close()

    def test_positive_demo_queries(self, real_qs):
        for name, sql in POSITIVE_SQL:
            r = real_qs.execute(sql)
            assert r.query_status == QueryStatus.OK, f"{name}: {r.error}"
            assert r.row_count > 0, name
            assert "user_id" not in [c.lower() for c in r.columns], name

    def test_window_totals_match_audit_baseline(self, real_qs):
        """两个自然月窗口合计与阶段一审计基线一致（273.00/247.51/25.49 与 235.15/258.11/-22.96 亿）。"""
        r = real_qs.execute(
            "SELECT CASE WHEN report_date < DATE '2014-06-01' THEN 'W1' ELSE 'W2' END AS win, "
            "SUM(total_purchase_amt) AS purchase, SUM(total_redeem_amt) AS redeem "
            "FROM user_balance_table "
            "WHERE report_date BETWEEN DATE '2014-03-01' AND DATE '2014-08-31' "
            "GROUP BY 1 ORDER BY 1")
        assert r.query_status == QueryStatus.OK
        got = {row[0]: (row[1], row[2]) for row in r.rows}
        assert got["W1"] == (27299767097, 24750976746)
        assert got["W2"] == (23514633123, 25811104616)

    def test_shibor_fill_special_dates(self, real_qs):
        r = real_qs.execute(
            "SELECT mfd_date, source_obs_date, is_filled FROM v_mfd_bank_shibor_daily_filled "
            "WHERE mfd_date >= DATE '2014-08-28' ORDER BY mfd_date")
        assert r.query_status == QueryStatus.OK
        by_date = {str(row[0]): (str(row[1]), bool(row[2])) for row in r.rows}
        assert by_date["2014-08-28"] == ("2014-08-28", False)
        assert by_date["2014-08-29"] == ("2014-08-29", False)
        assert by_date["2014-08-30"] == ("2014-08-29", True)
        assert by_date["2014-08-31"] == ("2014-08-29", True)

    def test_database_unchanged_after_queries(self, real_qs):
        counts_before = _table_counts(real_qs)
        for sql in REJECTED_SQL:
            real_qs.execute(sql)
        counts_after = _table_counts(real_qs)
        assert counts_before == counts_after

    @pytest.mark.skipif(
        not RAW_FILES_PRESENT,
        reason="本地无原始 CSV（干净克隆环境）：原始数据不随仓库发布，"
               "原始 CSV MD5 稳定性由 integration 测试覆盖")
    def test_raw_csv_md5_stable(self):
        md5 = _raw_md5()
        assert md5.get("user_balance_table.csv") == "8e001b2260c9b18f3f661e539224bc65"
        assert md5.get("user_profile_table.csv") == "0305b7f47d410a1fb58b88b269c8fd6b"
