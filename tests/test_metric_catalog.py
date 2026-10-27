"""指标目录测试（第四阶段）：与 Schema 一致性、禁用指标、维度白名单。"""

from __future__ import annotations

import pytest

from dataagent import metric_catalog as mc
from dataagent.sql_guard import TABLE_COLUMNS


class TestCatalogSchemaConsistency:
    def test_catalog_matches_schema(self):
        problems = mc.validate_catalog_against_schema()
        assert problems == [], f"指标目录与 Schema 不一致: {problems}"

    def test_all_metric_fields_exist_in_schema(self):
        for m in mc.METRIC_CATALOG.values():
            for f in m.fields:
                assert f in TABLE_COLUMNS[m.table], f"{m.metric_id}.{f}"

    def test_metric_tables_are_whitelisted(self):
        for m in mc.METRIC_CATALOG.values():
            assert m.table in TABLE_COLUMNS


class TestDisabledMetrics:
    def test_balance_metric_disabled(self):
        """tBalance/yBalance 语义未确认 → 不启用。"""
        m = mc.METRIC_CATALOG["balance"]
        assert not m.enabled
        assert not m.semantics_confirmed

    def test_get_disabled_metric_raises(self):
        with pytest.raises(ValueError, match="未启用"):
            mc.get_metric("balance")

    def test_get_unknown_metric_raises(self):
        with pytest.raises(KeyError):
            mc.get_metric("sales_amount")

    def test_available_metrics_exclude_disabled(self):
        ids = {m.metric_id for m in mc.list_available_metrics()}
        assert "balance" not in ids
        assert {"purchase_amount", "redeem_amount", "net_inflow",
                "daily_yield", "seven_day_yield", "shibor_rate"} <= ids


class TestMetricDefinitions:
    def test_purchase_not_called_sales(self):
        """申购金额的业务口径必须声明为资金流，不得称为销售额。"""
        m = mc.get_metric("purchase_amount")
        assert "销售额" in m.formula and "不是商品销售额" in m.formula

    def test_net_inflow_formula(self):
        m = mc.get_metric("net_inflow")
        assert "total_purchase_amt" in m.formula and "total_redeem_amt" in m.formula

    def test_city_has_no_name_mapping(self):
        """维度标签必须是编码，不得虚构城市名称。"""
        assert mc.DIMENSION_LABELS["city"] == "城市编码"

    def test_shibor_requires_term_and_view(self):
        m = mc.get_metric("shibor_rate")
        assert m.table == "v_mfd_bank_shibor_daily_filled"
        assert any("term" in r for r in m.restrictions)
        assert any("2014-08-29" in r for r in m.restrictions)

    def test_shibor_columns(self):
        assert mc.shibor_column("1M") == "Interest_1_M"
        with pytest.raises(KeyError):
            mc.shibor_column("2Y")

    def test_data_date_range_constants(self):
        assert mc.DATA_DATE_MIN == "2013-07-01"
        assert mc.DATA_DATE_MAX == "2014-08-31"
        assert mc.SHIBOR_LAST_OBS_DATE == "2014-08-29"


class TestDimensionWhitelist:
    def test_user_id_not_a_dimension(self):
        assert not mc.is_known_dimension("user_id")

    @pytest.mark.parametrize("d", ["report_date", "mfd_date", "city", "sex", "constellation"])
    def test_allowed_dimensions(self, d):
        assert mc.is_known_dimension(d)

    def test_amount_columns_not_dimensions(self):
        assert not mc.is_known_dimension("total_purchase_amt")
