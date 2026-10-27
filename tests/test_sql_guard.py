"""SQL 安全校验器测试（AST 级，纯静态校验，不依赖数据库）。"""

from __future__ import annotations

import pytest

from dataagent import db, sql_guard
from dataagent.sql_guard import (
    ALLOWED_TABLES,
    TABLE_COLUMNS,
    validate_sql,
)


class TestWhitelistSyncWithSchema:
    """sql_guard 白名单必须与 db.py 的 Schema 保持一致。"""

    def test_tables_match_db_business_tables(self):
        assert ALLOWED_TABLES == set(db.BUSINESS_TABLES) | {"v_mfd_bank_shibor_daily_filled"}

    def test_columns_match_db_schema(self):
        for table in db.BUSINESS_TABLES:
            ddl = next(s for s in db.SCHEMA_STATEMENTS if f"CREATE TABLE IF NOT EXISTS {table} " in s)
            ddl_cols = set()
            for line in ddl.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.startswith(("CREATE", "PRIMARY", ")")):
                    continue
                ddl_cols.add(line.split()[0])
            assert TABLE_COLUMNS[table] == ddl_cols, f"{table} 白名单列与 Schema 不一致"


class TestPositiveQueries:
    """任务要求的正向用例。"""

    def test_daily_purchase_by_date(self):
        r = validate_sql("SELECT report_date, SUM(total_purchase_amt) AS purchase "
                         "FROM user_balance_table GROUP BY report_date ORDER BY report_date")
        assert r.passed, r.reasons
        assert r.tables == ["user_balance_table"]

    def test_daily_redeem_by_date(self):
        r = validate_sql("SELECT report_date, SUM(total_redeem_amt) FROM user_balance_table "
                         "GROUP BY report_date")
        assert r.passed, r.reasons

    def test_net_inflow(self):
        r = validate_sql("SELECT report_date, SUM(total_purchase_amt) - SUM(total_redeem_amt) AS net "
                         "FROM user_balance_table GROUP BY report_date")
        assert r.passed, r.reasons

    def test_city_level_aggregation(self):
        r = validate_sql("SELECT p.city, SUM(b.total_purchase_amt) FROM user_balance_table b "
                         "JOIN user_profile_table p ON b.user_id = p.user_id GROUP BY p.city")
        assert r.passed, r.reasons

    def test_legal_join_balance_and_profile(self):
        r = validate_sql("SELECT p.sex, COUNT(DISTINCT b.user_id) FROM user_balance_table b "
                         "JOIN user_profile_table p ON b.user_id = p.user_id GROUP BY p.sex")
        assert r.passed, r.reasons

    def test_yield_and_filled_shibor_view(self):
        r = validate_sql("SELECT mfd_date, mfd_daily_yield FROM mfd_day_share_interest "
                         "ORDER BY mfd_date")
        assert r.passed, r.reasons
        r2 = validate_sql("SELECT mfd_date, Interest_O_N, is_filled, source_obs_date "
                          "FROM v_mfd_bank_shibor_daily_filled ORDER BY mfd_date")
        assert r2.passed, r2.reasons

    def test_legal_cte(self):
        r = validate_sql(
            "WITH d AS (SELECT report_date, SUM(total_purchase_amt) AS p "
            "FROM user_balance_table GROUP BY report_date) "
            "SELECT report_date, p FROM d ORDER BY report_date")
        assert r.passed, r.reasons

    def test_parameterized_condition(self):
        r = validate_sql("SELECT SUM(total_purchase_amt) FROM user_balance_table "
                         "WHERE report_date = ?")
        assert r.passed, r.reasons

    def test_aggregate_sort_group(self):
        r = validate_sql("SELECT city, AVG(total_redeem_amt) AS avg_redeem, COUNT(*) AS n "
                         "FROM user_balance_table b JOIN user_profile_table p USING (user_id) "
                         "WHERE b.report_date BETWEEN DATE '2014-06-01' AND DATE '2014-08-31' "
                         "GROUP BY city ORDER BY avg_redeem DESC")
        assert r.passed, r.reasons

    def test_count_distinct_user_id_allowed(self):
        r = validate_sql("SELECT COUNT(DISTINCT user_id) FROM user_balance_table")
        assert r.passed, r.reasons

    def test_validation_result_structure(self):
        r = validate_sql("SELECT report_date, SUM(total_purchase_amt) FROM user_balance_table "
                         "GROUP BY report_date")
        assert r.statement_type == "Select"
        assert "SUM" in r.functions
        assert "report_date" in r.columns
        assert r.normalized_sql
        assert "table_view_whitelist" in r.rules_applied


class TestNegativeStatements:
    """任务要求的负向用例（语句类）。"""

    @pytest.mark.parametrize("sql", [
        "DROP TABLE user_balance_table",
        "DELETE FROM user_balance_table",
        "UPDATE user_balance_table SET tBalance = 0",
        "INSERT INTO user_balance_table VALUES (1, '20240101')",
        "MERGE INTO user_balance_table USING user_profile_table ON FALSE "
        "WHEN MATCHED THEN UPDATE SET tBalance = 0",
        "CREATE TABLE evil (x INT)",
        "ALTER TABLE user_balance_table ADD COLUMN x INT",
        "TRUNCATE user_balance_table",
        "ATTACH 'other.db' AS other",
        "DETACH other",
        "COPY user_balance_table TO 'out.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "SET threads = 4",
        "USE mydb",
        "CALL my_func()",
        "BEGIN TRANSACTION",
        "COMMIT",
    ])
    def test_write_and_admin_statements_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert r.reasons

    def test_multiple_statements_rejected(self):
        r = validate_sql("SELECT 1; DROP TABLE user_balance_table")
        assert not r.passed
        assert any("单条" in x for x in r.reasons)

    def test_cte_with_write_rejected(self):
        r = validate_sql("WITH x AS (SELECT 1) DELETE FROM user_balance_table")
        assert not r.passed

    def test_semicolon_comment_bypass_rejected(self):
        r = validate_sql("SELECT 1 -- c\n; DROP TABLE user_balance_table")
        assert not r.passed
        assert any("单条" in x for x in r.reasons)

    def test_case_obfuscation_rejected(self):
        r = validate_sql("dRoP TaBlE user_balance_table")
        assert not r.passed


class TestNegativeExternalAccess:
    """外部文件/系统访问。"""

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('x.parquet')",
        "SELECT * FROM read_json('x.json')",
        "SELECT * FROM glob('*.csv')",
        "SELECT * FROM duckdb_tables()",
        "SELECT * FROM duckdb_settings()",
    ])
    def test_external_reads_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("read_csv" in x or "外部" in x or "未授权" in x or "表函数" in x
                   for x in r.reasons), r.reasons


class TestNegativeTables:
    """未授权表/schema。"""

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM secret_table",
        "SELECT * FROM main.secret_table",
        "SELECT * FROM other.user_balance_table",
        "SELECT COUNT(*) FROM information_schema.tables",
        "SELECT COUNT(*) FROM pg_catalog.pg_tables",
    ])
    def test_unauthorized_tables_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("未授权" in x or "禁止" in x for x in r.reasons), r.reasons

    def test_nested_subquery_table_checked(self):
        r = validate_sql("SELECT * FROM (SELECT * FROM secret_table) s")
        assert not r.passed


class TestNegativeUserLevelData:
    """用户级数据保护。"""

    def test_select_star_rejected(self):
        r = validate_sql("SELECT * FROM user_balance_table")
        assert not r.passed
        assert any("SELECT *" in x for x in r.reasons)

    def test_star_in_subquery_rejected(self):
        r = validate_sql("SELECT * FROM (SELECT user_id FROM user_balance_table) s")
        assert not r.passed

    def test_user_id_projection_rejected(self):
        r = validate_sql("SELECT user_id FROM user_balance_table")
        assert not r.passed
        assert any("user_id" in x for x in r.reasons)

    def test_user_id_group_rejected(self):
        r = validate_sql("SELECT user_id, COUNT(*) FROM user_balance_table GROUP BY user_id")
        assert not r.passed

    def test_user_level_amount_projection_rejected(self):
        for col in ("tBalance", "yBalance", "total_purchase_amt", "total_redeem_amt",
                    "consume_amt", "transfer_amt", "share_amt"):
            r = validate_sql(f"SELECT {col} FROM user_balance_table")
            assert not r.passed, col

    def test_user_level_arithmetic_projection_rejected(self):
        r = validate_sql("SELECT total_purchase_amt + 1 FROM user_balance_table")
        assert not r.passed
        r2 = validate_sql("SELECT user_id + 1 FROM user_balance_table")
        assert not r2.passed

    def test_user_level_in_group_rejected(self):
        r = validate_sql("SELECT COUNT(*) FROM user_balance_table GROUP BY tBalance")
        assert not r.passed

    def test_count_distinct_user_id_in_join_allowed(self):
        r = validate_sql("SELECT p.city, COUNT(DISTINCT b.user_id) AS users "
                         "FROM user_balance_table b JOIN user_profile_table p "
                         "ON b.user_id = p.user_id GROUP BY p.city")
        assert r.passed, r.reasons


class TestNegativeFunctionsAndColumns:
    """函数与列校验。"""

    @pytest.mark.parametrize("sql", [
        "SELECT nextval('seq') FROM user_balance_table",
        "SELECT random() FROM user_balance_table",
        "SELECT read_text('x') FROM user_balance_table",
        "SELECT * FROM range(10)",
        "SELECT setseed(0.5)",
    ])
    def test_dangerous_functions_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql

    def test_unknown_column_rejected(self):
        r = validate_sql("SELECT unknown_col FROM user_balance_table")
        assert not r.passed
        assert any("未知字段" in x for x in r.reasons)

    def test_unknown_column_in_where_rejected(self):
        r = validate_sql("SELECT COUNT(*) FROM user_balance_table WHERE bogus = 1")
        assert not r.passed


class TestInvalidInput:
    @pytest.mark.parametrize("sql", [
        "this is not sql at all",
        "",
        "   ",
        "SELECT FROM WHERE",
    ])
    def test_invalid_sql_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed

    def test_non_string_rejected(self):
        r = validate_sql(None)
        assert not r.passed
        r2 = validate_sql(123)
        assert not r2.passed


# ===========================================================================
# 任务 03.1 安全漏洞回归：user_id 聚合泄露 / 用户级列子句保护 / 列-表绑定
# ===========================================================================

class TestUserIdAggregationBypass:
    """修复前 MIN/MAX/SUM/AVG(user_id) 可绕过用户标识保护。"""

    @pytest.mark.parametrize("sql", [
        "SELECT MIN(user_id) AS x FROM user_balance_table",
        "SELECT MAX(user_id) AS x FROM user_balance_table",
        "SELECT SUM(user_id) AS x FROM user_balance_table",
        "SELECT AVG(user_id) AS x FROM user_balance_table",
        "SELECT MEDIAN(user_id) FROM user_balance_table",
        "SELECT COUNT(DISTINCT user_id, report_date) FROM user_balance_table",
        "SELECT MIN(b.user_id) FROM user_balance_table b",
    ])
    def test_aggregates_on_user_id_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("user_id" in x for x in r.reasons), r.reasons

    @pytest.mark.parametrize("sql", [
        "SELECT user_id + 0 AS uid FROM user_balance_table",
        "SELECT user_id * 2 AS uid FROM user_balance_table",
        "SELECT CAST(user_id AS VARCHAR) AS uid FROM user_balance_table",
        "SELECT ABS(user_id - 100) AS d FROM user_balance_table",
        "SELECT COUNT(user_id) + 1 AS c FROM user_balance_table",
        "SELECT CASE WHEN COUNT(user_id) > 5 THEN 1 ELSE 0 END FROM user_balance_table",
    ])
    def test_user_id_expression_aliases_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql

    @pytest.mark.parametrize("sql", [
        # CTE 后经别名输出
        "WITH u AS (SELECT user_id AS uid FROM user_balance_table) SELECT uid FROM u",
        "WITH u AS (SELECT user_id FROM user_balance_table) "
        "SELECT uid FROM (SELECT user_id AS uid FROM u) s",
        # 子查询后经别名输出
        "SELECT uid FROM (SELECT user_id AS uid FROM user_balance_table) s",
        "SELECT * FROM (SELECT MIN(user_id) AS x FROM user_balance_table) s",
    ])
    def test_user_id_via_cte_or_subquery_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("user_id" in x for x in r.reasons), r.reasons

    @pytest.mark.parametrize("sql", [
        "SELECT COUNT(*) FROM user_balance_table GROUP BY user_id",
        "SELECT COUNT(*) FROM user_balance_table GROUP BY report_date HAVING user_id > 5",
        "SELECT report_date, COUNT(*) FROM user_balance_table "
        "GROUP BY report_date ORDER BY user_id",
        "SELECT report_date, SUM(total_purchase_amt) FROM user_balance_table "
        "GROUP BY report_date ORDER BY MIN(user_id)",
        "SELECT SUM(total_purchase_amt) OVER (ORDER BY user_id) FROM user_balance_table",
        "SELECT COUNT(DISTINCT user_id) OVER () FROM user_balance_table",
        "SELECT ROW_NUMBER() OVER (PARTITION BY user_id) FROM user_balance_table",
    ])
    def test_user_id_in_clauses_and_windows_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("user_id" in x for x in r.reasons), r.reasons

    @pytest.mark.parametrize("sql", [
        "SELECT COUNT(DISTINCT user_id) FROM user_balance_table",
        "SELECT COUNT(user_id) FROM user_balance_table",
        "SELECT COUNT(DISTINCT user_id) AS users FROM user_balance_table",
        "SELECT COUNT(*) FROM user_balance_table WHERE user_id > 100",
        "SELECT p.city, COUNT(DISTINCT b.user_id) AS users FROM user_balance_table b "
        "JOIN user_profile_table p ON b.user_id = p.user_id GROUP BY p.city",
    ])
    def test_legal_user_count_still_allowed(self, sql):
        """修复不得误伤合法用户计数。"""
        r = validate_sql(sql)
        assert r.passed, (sql, r.reasons)


class TestUserLevelColumnsInClauses:
    """任务 03.1-B：ORDER BY/HAVING/QUALIFY 用户级裸列必须在 AST 阶段拒绝。"""

    @pytest.mark.parametrize("sql", [
        "SELECT report_date, SUM(total_purchase_amt) AS p FROM user_balance_table "
        "GROUP BY report_date ORDER BY tBalance",
        "SELECT report_date, SUM(total_purchase_amt) AS p FROM user_balance_table "
        "GROUP BY report_date HAVING total_purchase_amt > 1000",
        "SELECT report_date, COUNT(*) FROM user_balance_table "
        "GROUP BY report_date ORDER BY consume_amt",
        "SELECT report_date, COUNT(*) FROM user_balance_table "
        "GROUP BY report_date QUALIFY total_redeem_amt > 0",
        "SELECT report_date, COUNT(*) FROM user_balance_table GROUP BY tBalance",
        "SELECT COUNT(*) FROM user_balance_table ORDER BY share_amt",
    ])
    def test_user_level_bare_columns_in_clauses_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("用户级" in x or "user" in x for x in r.reasons), r.reasons

    @pytest.mark.parametrize("sql", [
        # 聚合包裹的排序/过滤合法
        "SELECT report_date, SUM(total_purchase_amt) FROM user_balance_table "
        "GROUP BY report_date ORDER BY SUM(total_purchase_amt) DESC",
        "SELECT report_date, SUM(total_purchase_amt) AS p FROM user_balance_table "
        "GROUP BY report_date HAVING SUM(total_purchase_amt) > 1000 ORDER BY p DESC",
    ])
    def test_aggregate_wrapped_order_having_allowed(self, sql):
        r = validate_sql(sql)
        assert r.passed, (sql, r.reasons)


class TestColumnTableBinding:
    """任务 03.1-C：列-表静态绑定（限定符/歧义/未知）。"""

    def test_qualified_column_bound_to_table(self):
        # 限定符绑定 + 聚合口径（city 出现在 GROUP BY）
        r = validate_sql("SELECT b.report_date, p.city, COUNT(*) AS n "
                         "FROM user_balance_table b "
                         "JOIN user_profile_table p ON b.user_id = p.user_id "
                         "GROUP BY b.report_date, p.city")
        assert r.passed, r.reasons

    def test_qualified_column_not_in_table_rejected(self):
        r = validate_sql("SELECT b.city FROM user_balance_table b")
        assert not r.passed
        assert any("不存在字段" in x for x in r.reasons), r.reasons

    def test_unknown_table_alias_rejected(self):
        r = validate_sql("SELECT x.report_date FROM user_balance_table b")
        assert not r.passed
        assert any("未知表别名" in x for x in r.reasons), r.reasons

    def test_ambiguous_column_rejected(self):
        # user_id 同时存在于 balance 与 profile，无限定符引用应判歧义
        r = validate_sql("SELECT COUNT(DISTINCT user_id) FROM user_balance_table b "
                         "JOIN user_profile_table p ON b.user_id = p.user_id")
        assert not r.passed
        assert any("歧义" in x for x in r.reasons), r.reasons

    def test_unqualified_unique_column_allowed(self):
        # city 仅存在于 profile；report_date 仅存在于 balance——唯一候选放行
        r = validate_sql("SELECT city, COUNT(*) FROM user_balance_table b "
                         "JOIN user_profile_table p ON b.user_id = p.user_id GROUP BY city")
        assert r.passed, r.reasons
        r2 = validate_sql("SELECT report_date, COUNT(*) FROM user_balance_table b "
                          "JOIN mfd_day_share_interest y ON b.report_date = y.mfd_date "
                          "GROUP BY report_date")
        assert r2.passed, r2.reasons

    def test_projection_alias_in_order_by_allowed(self):
        r = validate_sql("SELECT city, SUM(total_purchase_amt) AS purchase "
                         "FROM user_balance_table b JOIN user_profile_table p "
                         "ON b.user_id = p.user_id GROUP BY city ORDER BY purchase DESC")
        assert r.passed, r.reasons

    def test_cte_output_columns_bound(self):
        r = validate_sql("WITH d AS (SELECT report_date, SUM(total_purchase_amt) AS p "
                         "FROM user_balance_table GROUP BY report_date) "
                         "SELECT report_date, p FROM d ORDER BY p DESC")
        assert r.passed, r.reasons


# ===========================================================================
# 任务 03.2：用户级粒度保护与集合聚合收紧
# ===========================================================================

class TestUserLevelGrainProtection:
    """任务 03.2-A：用户级表维度列裸投影必须在聚合口径内。"""

    @pytest.mark.parametrize("sql", [
        # 用户属性表逐条导出
        "SELECT city, sex, constellation FROM user_profile_table",
        "SELECT city FROM user_profile_table",
        "SELECT city, sex FROM user_profile_table LIMIT 100",
        "SELECT sex FROM user_profile_table ORDER BY sex",
        # DISTINCT 多列组合（细粒度，拒绝）
        "SELECT DISTINCT city, sex FROM user_profile_table",
        "SELECT DISTINCT city, sex, constellation FROM user_profile_table",
        # JOIN 后逐条输出用户属性
        "SELECT b.report_date, p.city FROM user_balance_table b "
        "JOIN user_profile_table p ON b.user_id = p.user_id",
        "SELECT p.sex, b.report_date FROM user_profile_table p "
        "JOIN user_balance_table b ON b.user_id = p.user_id LIMIT 100",
        # 表达式包装绕过
        "SELECT city || '-x' AS c FROM user_profile_table",
        "SELECT CAST(city AS INTEGER) AS c FROM user_profile_table",
        # 资金表裸维度投影（无聚合口径）
        "SELECT report_date FROM user_balance_table",
        "SELECT report_date FROM user_balance_table ORDER BY report_date LIMIT 100",
        # 窗口函数不改变行粒度：无 GROUP BY 的窗口投影仍为用户级
        "SELECT report_date, SUM(total_purchase_amt) OVER (ORDER BY report_date) "
        "FROM user_balance_table",
        "SELECT city, SUM(total_purchase_amt) OVER () FROM user_profile_table",
        # CTE / 子查询包装
        "WITH u AS (SELECT city FROM user_profile_table) SELECT city FROM u",
        "WITH u AS (SELECT city AS c FROM user_profile_table) SELECT c FROM u",
        "SELECT city FROM (SELECT city FROM user_profile_table) s",
        "SELECT c FROM (SELECT city AS c FROM user_profile_table) s",
    ])
    def test_user_level_grain_violations_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("聚合口径" in x or "维度" in x or "user_id" in x for x in r.reasons), r.reasons

    @pytest.mark.parametrize("sql", [
        # 合法聚合（任务 C 必须保留清单）
        "SELECT city, COUNT(*) AS user_count FROM user_profile_table GROUP BY city",
        "SELECT sex, COUNT(*) FROM user_profile_table GROUP BY sex",
        "SELECT constellation, COUNT(*) FROM user_profile_table GROUP BY constellation",
        "SELECT p.city, SUM(b.total_purchase_amt) FROM user_balance_table b "
        "JOIN user_profile_table p ON b.user_id = p.user_id GROUP BY p.city",
        "SELECT report_date, SUM(total_purchase_amt), SUM(total_redeem_amt) "
        "FROM user_balance_table GROUP BY report_date",
        "SELECT COUNT(DISTINCT user_id) FROM user_balance_table",
        "SELECT p.city, COUNT(DISTINCT b.user_id) AS users FROM user_balance_table b "
        "JOIN user_profile_table p ON b.user_id = p.user_id GROUP BY p.city",
        "SELECT mfd_date, mfd_daily_yield FROM mfd_day_share_interest ORDER BY mfd_date",
        "SELECT mfd_date, Interest_O_N, is_filled, source_obs_date "
        "FROM v_mfd_bank_shibor_daily_filled ORDER BY mfd_date",
        # DISTINCT 单列维度：输出去重维度值，不构成用户记录
        "SELECT DISTINCT city FROM user_profile_table",
        "SELECT DISTINCT sex FROM user_profile_table",
        # 分组键表达式与位置引用
        "SELECT year(report_date) AS y, SUM(total_purchase_amt) FROM user_balance_table "
        "GROUP BY year(report_date)",
        "SELECT CASE WHEN report_date < DATE '2014-06-01' THEN 'W1' ELSE 'W2' END AS win, "
        "SUM(total_purchase_amt) FROM user_balance_table GROUP BY 1",
        # 分组后的窗口累计（合法聚合口径）
        "SELECT report_date, SUM(SUM(total_purchase_amt)) OVER (ORDER BY report_date) "
        "FROM user_balance_table GROUP BY report_date",
        # CTE 已聚合 → 外层透传合法
        "WITH d AS (SELECT report_date, SUM(total_purchase_amt) AS p "
        "FROM user_balance_table GROUP BY report_date) SELECT report_date, p FROM d",
        "WITH c AS (SELECT city, COUNT(*) AS n FROM user_profile_table GROUP BY city) "
        "SELECT city, n FROM c ORDER BY n DESC",
    ])
    def test_legal_aggregates_still_allowed(self, sql):
        r = validate_sql(sql)
        assert r.passed, (sql, r.reasons)


class TestDistinctExpressionBoundary:
    """任务 03-Final：DISTINCT 单列豁免仅适用于单个裸维度 Column。"""

    @pytest.mark.parametrize("sql", [
        # CONCAT 组合多个用户属性
        "SELECT DISTINCT concat(CAST(city AS VARCHAR), '-', CAST(sex AS VARCHAR)) AS x "
        "FROM user_profile_table",
        "SELECT DISTINCT concat(city, sex) FROM user_profile_table",
        # 算术表达式组合维度
        "SELECT DISTINCT city || '-' || sex AS x FROM user_profile_table",
        "SELECT DISTINCT CAST(city AS INTEGER) + 1 AS x FROM user_profile_table",
        # CASE 包装用户属性
        "SELECT DISTINCT CASE WHEN sex = 1 THEN city ELSE 0 END AS x FROM user_profile_table",
        # 嵌套函数（单维度包装同样拒绝：无法可靠解析为裸维度列）
        "SELECT DISTINCT upper(city) AS x FROM user_profile_table",
        "SELECT DISTINCT year(report_date) AS y FROM user_balance_table",
        # CTE / 子查询包装
        "WITH u AS (SELECT DISTINCT concat(city, sex) AS x FROM user_profile_table) "
        "SELECT x FROM u",
        "SELECT x FROM (SELECT DISTINCT CASE WHEN sex = 1 THEN city ELSE 0 END AS x "
        "FROM user_profile_table) s",
        # 别名伪装成维度名
        "SELECT DISTINCT concat(city, sex) AS city FROM user_profile_table",
        # 多列 DISTINCT（既有规则）
        "SELECT DISTINCT city, sex FROM user_profile_table",
    ])
    def test_distinct_wrapped_expressions_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("聚合口径" in x or "维度" in x or "user_id" in x for x in r.reasons), r.reasons

    @pytest.mark.parametrize("sql", [
        # 单个裸维度列的 DISTINCT：输出去重维度值，保留
        "SELECT DISTINCT city FROM user_profile_table",
        "SELECT DISTINCT sex FROM user_profile_table",
        "SELECT DISTINCT constellation FROM user_profile_table",
        "SELECT DISTINCT city FROM user_profile_table ORDER BY city",
    ])
    def test_distinct_single_bare_dimension_allowed(self, sql):
        r = validate_sql(sql)
        assert r.passed, (sql, r.reasons)


class TestCollectionAggregates:
    """任务 03.2-B：集合聚合函数一律拒绝（收集多条记录进单单元格）。"""

    @pytest.mark.parametrize("sql", [
        "SELECT list(city) AS cities FROM user_profile_table",
        "SELECT list(user_id) FROM user_balance_table",
        "SELECT listagg(city) FROM user_profile_table",
        "SELECT string_agg(CAST(city AS VARCHAR), ',') FROM user_profile_table",
        "SELECT string_agg(user_id, ',') FROM user_balance_table",
        "SELECT array_agg(city) FROM user_profile_table",
        "SELECT list_value(city, sex) FROM user_profile_table",
        # CTE / 子查询包装
        "WITH c AS (SELECT list(city) AS cities FROM user_profile_table) SELECT cities FROM c",
        "SELECT cities FROM (SELECT list(city) AS cities FROM user_profile_table) s",
        # 与聚合混合
        "SELECT COUNT(*), list(city) FROM user_profile_table",
    ])
    def test_collection_aggregates_rejected(self, sql):
        r = validate_sql(sql)
        assert not r.passed, sql
        assert any("集合" in x or "LIST" in x or "GROUP_CONCAT" in x or "ARRAY_AGG" in x
                   or "LIST_VALUE" in x or "未授权" in x for x in r.reasons), r.reasons
