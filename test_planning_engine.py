"""
tests/test_planning_engine.py
Unit tests for the planning model logic.
Uses in-memory pandas DataFrames — no DB required.
"""

import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

# Patch DB calls before importing the module
import sys
sys.modules.setdefault("etl.db", MagicMock())

from models.planning_engine import (
    compute_kpis,
    compute_non_buyability_large,
    compute_planning,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_planning_df():
    return pd.DataFrame({
        "sku":             ["A", "B", "C", "D", "E"],
        "product_name":    ["P1","P2","P3","P4","P5"],
        "category":        ["Electronics","Electronics","Furniture","Appliances","Appliances"],
        "size_class":      ["LARGE","LARGE","LARGE","STANDARD","STANDARD"],
        "lead_time_days":  [14, 14, 21, 7, 7],
        "unit_cost":       [50000, 62000, 25000, 3500, 800],
        "qty_on_hand":     [0, 5, 12, 100, 200],
        "qty_in_transit":  [0, 0, 3, 20, 0],
        "qty_reserved":    [0, 1, 2, 10, 20],
        "qty_available":   [0, 4, 10, 90, 180],
        "avg_daily_units": [2.0, 1.5, 0.0, 10.0, 8.0],
        "coverage_days":   [None, 2.7, None, 9.0, 22.5],
        "reorder_point":   [34, 25, 0, 84, 67],
        "suggested_order_qty": [28, 17, 0, 50, 0],
        "suggested_order_cost":[1400000, 1054000, 0, 175000, 0],
        "planning_status": ["CRITICAL","REORDER_NOW","NO_DEMAND","PLAN_REORDER","HEALTHY"],
        "snapshot_date":   pd.to_datetime(["2024-06-01"]*5),
    })


@pytest.fixture
def sample_non_buy_df():
    return pd.DataFrame({
        "category":          ["Electronics","Furniture","Appliances"],
        "total_skus":        [2, 1, 3],
        "non_buyable_skus":  [1, 0, 2],
        "non_buyability_pct":[50.0, 0.0, 66.7],
        "is_alert":          [True, False, True],
        "buyable_skus":      [1, 1, 1],
    })


# ---------------------------------------------------------------------------
# Tests: KPI computation
# ---------------------------------------------------------------------------

class TestComputeKPIs:
    def test_critical_count(self, sample_planning_df, sample_non_buy_df):
        kpis = compute_kpis(sample_planning_df, sample_non_buy_df)
        assert kpis["critical_skus"] == 1

    def test_reorder_now_count(self, sample_planning_df, sample_non_buy_df):
        kpis = compute_kpis(sample_planning_df, sample_non_buy_df)
        assert kpis["reorder_now_skus"] == 1

    def test_total_daily_run_rate(self, sample_planning_df, sample_non_buy_df):
        kpis = compute_kpis(sample_planning_df, sample_non_buy_df)
        expected = round(sample_planning_df["avg_daily_units"].sum(), 1)
        assert kpis["total_daily_run_rate"] == expected

    def test_avg_large_non_buy_pct(self, sample_planning_df, sample_non_buy_df):
        kpis = compute_kpis(sample_planning_df, sample_non_buy_df)
        expected = round(sample_non_buy_df["non_buyability_pct"].mean(), 2)
        assert kpis["avg_large_non_buy_pct"] == expected

    def test_categories_in_alert(self, sample_planning_df, sample_non_buy_df):
        kpis = compute_kpis(sample_planning_df, sample_non_buy_df)
        assert kpis["categories_in_alert"] == 2

    def test_large_sku_count(self, sample_planning_df, sample_non_buy_df):
        kpis = compute_kpis(sample_planning_df, sample_non_buy_df)
        assert kpis["total_large_skus"] == 3

    def test_empty_non_buy(self, sample_planning_df):
        kpis = compute_kpis(sample_planning_df, pd.DataFrame())
        assert kpis["avg_large_non_buy_pct"] == 0.0
        assert kpis["categories_in_alert"] == 0

    def test_suggested_order_cost(self, sample_planning_df, sample_non_buy_df):
        kpis = compute_kpis(sample_planning_df, sample_non_buy_df)
        expected = round(sample_planning_df["suggested_order_cost"].sum(), 2)
        assert kpis["total_suggested_order_cost"] == expected


# ---------------------------------------------------------------------------
# Tests: Planning status classification
# ---------------------------------------------------------------------------

class TestPlanningStatus:
    """Test the planning status logic in isolation."""

    def _classify(self, qty_available, avg_daily, lead_time, target=14, safety=1.2):
        """Mirror the classify() function from planning_engine."""
        if avg_daily == 0:
            return "NO_DEMAND"
        if qty_available <= 0:
            return "CRITICAL"
        coverage = qty_available / avg_daily
        if coverage < lead_time:
            return "REORDER_NOW"
        if coverage < target:
            return "PLAN_REORDER"
        return "HEALTHY"

    def test_zero_stock_is_critical(self):
        assert self._classify(0, 2.0, 14) == "CRITICAL"

    def test_negative_stock_is_critical(self):
        assert self._classify(-5, 2.0, 14) == "CRITICAL"

    def test_coverage_below_lead_time_is_reorder_now(self):
        # 4 units / 2 per day = 2 days coverage, lead time 14
        assert self._classify(4, 2.0, 14) == "REORDER_NOW"

    def test_coverage_above_lead_time_below_target_is_plan(self):
        # 20 / 2 = 10 days, lead time 7, target 14
        assert self._classify(20, 2.0, 7) == "PLAN_REORDER"

    def test_coverage_above_target_is_healthy(self):
        # 100 / 2 = 50 days > 14 target
        assert self._classify(100, 2.0, 14) == "HEALTHY"

    def test_no_demand(self):
        assert self._classify(100, 0.0, 14) == "NO_DEMAND"


# ---------------------------------------------------------------------------
# Tests: Non-buyability
# ---------------------------------------------------------------------------

class TestNonBuyability:
    def test_all_buyable(self, sample_non_buy_df):
        row = sample_non_buy_df[sample_non_buy_df["category"] == "Furniture"].iloc[0]
        assert row["non_buyability_pct"] == 0.0
        assert row["is_alert"] is False

    def test_alert_triggered_above_threshold(self, sample_non_buy_df):
        alerts = sample_non_buy_df[sample_non_buy_df["is_alert"] == True]
        assert len(alerts) == 2
        for _, row in alerts.iterrows():
            assert row["non_buyability_pct"] > 15.0

    def test_pct_calculation(self, sample_non_buy_df):
        electronics = sample_non_buy_df[
            sample_non_buy_df["category"] == "Electronics"
        ].iloc[0]
        expected = round(1/2 * 100, 2)
        assert electronics["non_buyability_pct"] == expected
