"""
models/planning_engine.py

Core inventory planning logic:
  1. Compute daily run rate per SKU (rolling window)
  2. Calculate non-buyability % for LARGE items by category
  3. Generate reorder recommendations
  4. Emit planning actions (CRITICAL / REORDER_NOW / PLAN_REORDER / HEALTHY)

Output is written to the DB for Power BI to consume via SQL views,
and returned as DataFrames for the alerting module.
"""

import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd
import numpy as np
from sqlalchemy import text

from etl.db import get_engine, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Daily run rate
# ---------------------------------------------------------------------------

def compute_run_rate(engine, window_days: int = 7) -> pd.DataFrame:
    """
    Rolling N-day average daily shipment units per SKU.
    Returns columns: sku, avg_daily_units, total_units_window,
                     shipment_days, category, size_class
    """
    sql = text("""
        SELECT
            li.sku,
            p.category,
            p.size_class,
            s.shipment_date,
            SUM(li.qty_shipped) AS daily_units
        FROM shipment_line_items li
        JOIN shipments s ON s.shipment_id = li.shipment_id
        JOIN products  p ON p.sku = li.sku
        WHERE s.status NOT IN ('CANCELLED', 'RETURNED')
          AND s.shipment_date >= CURRENT_DATE - :window_days
        GROUP BY li.sku, p.category, p.size_class, s.shipment_date
        ORDER BY li.sku, s.shipment_date
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"window_days": window_days})

    if df.empty:
        logger.warning("No shipment data in last %d days", window_days)
        return pd.DataFrame(columns=["sku","avg_daily_units","total_units_window",
                                     "shipment_days","category","size_class"])

    result = (
        df.groupby(["sku","category","size_class"])
          .agg(
              total_units_window=("daily_units","sum"),
              shipment_days=("shipment_date","nunique"),
              avg_daily_units=("daily_units","mean"),
          )
          .reset_index()
    )
    result["avg_daily_units"] = result["avg_daily_units"].round(2)
    logger.info("Run rate computed for %d SKUs (window=%d days)", len(result), window_days)
    return result


# ---------------------------------------------------------------------------
# 2. Non-buyability for LARGE items
# ---------------------------------------------------------------------------

def compute_non_buyability_large(engine) -> pd.DataFrame:
    """
    Non-buyability % for LARGE SKUs, grouped by category.
    A SKU is non-buyable if qty_available <= 0.
    Returns: category, total_skus, non_buyable_skus, non_buyability_pct,
             is_alert, as_of_date
    """
    sql = text("""
        SELECT
            p.category,
            COUNT(*)                                               AS total_skus,
            SUM(CASE WHEN i.qty_available <= 0 THEN 1 ELSE 0 END) AS non_buyable_skus
        FROM products p
        JOIN inventory i ON i.sku = p.sku
        WHERE p.size_class = 'LARGE'
          AND p.is_active   = TRUE
          AND i.snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
        GROUP BY p.category
        ORDER BY p.category
    """)
    cfg = load_config()
    alert_threshold = float(cfg["pipeline"]["non_buy_alert_threshold"])

    with engine.connect() as conn:
        df = pd.read_sql(sql, conn)

    if df.empty:
        return df

    df["non_buyability_pct"] = (
        (df["non_buyable_skus"] / df["total_skus"].replace(0, np.nan)) * 100
    ).round(2)
    df["is_alert"] = df["non_buyability_pct"] > (alert_threshold * 100)
    df["buyable_skus"] = df["total_skus"] - df["non_buyable_skus"]
    df["as_of_date"] = date.today()

    logger.info(
        "Non-buyability computed: %d categories, %d in alert",
        len(df), df["is_alert"].sum()
    )
    return df


# ---------------------------------------------------------------------------
# 3. Planning recommendations
# ---------------------------------------------------------------------------

def compute_planning(engine, run_rate_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    Generate planning actions for all active SKUs.
    Joins inventory + run rate to determine:
      - coverage_days
      - reorder_point
      - suggested_order_qty
      - planning_status
    """
    cfg = load_config()["pipeline"]
    target_days    = int(cfg["coverage_target_days"])
    safety_factor  = float(cfg["safety_factor"])

    inv_sql = text("""
        SELECT
            p.sku, p.product_name, p.category, p.size_class,
            p.lead_time_days, p.unit_cost,
            i.qty_on_hand, i.qty_in_transit, i.qty_reserved, i.qty_available,
            i.snapshot_date
        FROM products p
        JOIN inventory i ON i.sku = p.sku
        WHERE p.is_active = TRUE
          AND i.snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
    """)
    with engine.connect() as conn:
        inv_df = pd.read_sql(inv_sql, conn)

    if run_rate_df is None:
        run_rate_df = compute_run_rate(engine)

    df = inv_df.merge(
        run_rate_df[["sku","avg_daily_units"]],
        on="sku", how="left"
    )
    df["avg_daily_units"] = df["avg_daily_units"].fillna(0)

    # Coverage days
    df["coverage_days"] = np.where(
        df["avg_daily_units"] > 0,
        (df["qty_available"] / df["avg_daily_units"]).round(1),
        None
    )

    # Reorder point = lead_time * avg_daily * safety_factor
    df["reorder_point"] = (
        df["lead_time_days"] * df["avg_daily_units"] * safety_factor
    ).round(0).astype(int)

    # Suggested order qty
    df["suggested_order_qty"] = np.maximum(
        0,
        (target_days * df["avg_daily_units"]
         - df["qty_available"].clip(lower=0)
         - df["qty_in_transit"].clip(lower=0)
        ).round(0)
    ).astype(int)

    # Estimated reorder cost
    df["suggested_order_cost"] = (
        df["suggested_order_qty"] * df["unit_cost"].fillna(0)
    ).round(2)

    # Planning status
    def classify(row):
        if row["avg_daily_units"] == 0:
            return "NO_DEMAND"
        if row["qty_available"] <= 0:
            return "CRITICAL"
        if row["coverage_days"] is not None and row["coverage_days"] < row["lead_time_days"]:
            return "REORDER_NOW"
        if row["coverage_days"] is not None and row["coverage_days"] < target_days:
            return "PLAN_REORDER"
        return "HEALTHY"

    df["planning_status"] = df.apply(classify, axis=1)

    status_order = {"CRITICAL": 4, "REORDER_NOW": 3, "PLAN_REORDER": 2,
                    "HEALTHY": 1, "NO_DEMAND": 0}
    df["_sort"] = df["planning_status"].map(status_order)
    df = df.sort_values(["_sort","category","sku"], ascending=[False,True,True])
    df = df.drop(columns=["_sort"])

    logger.info(
        "Planning computed: %d SKUs | CRITICAL=%d | REORDER_NOW=%d | PLAN=%d",
        len(df),
        (df["planning_status"]=="CRITICAL").sum(),
        (df["planning_status"]=="REORDER_NOW").sum(),
        (df["planning_status"]=="PLAN_REORDER").sum(),
    )
    return df


# ---------------------------------------------------------------------------
# 4. Summary KPIs
# ---------------------------------------------------------------------------

def compute_kpis(planning_df: pd.DataFrame, non_buy_df: pd.DataFrame) -> dict:
    """Roll up top-level KPIs for alerting and Power BI cards."""
    return {
        "as_of": date.today().isoformat(),
        "total_active_skus":        len(planning_df),
        "total_large_skus":         int((planning_df["size_class"] == "LARGE").sum()),
        "critical_skus":            int((planning_df["planning_status"] == "CRITICAL").sum()),
        "reorder_now_skus":         int((planning_df["planning_status"] == "REORDER_NOW").sum()),
        "total_daily_run_rate":     float(planning_df["avg_daily_units"].sum().round(1)),
        "avg_large_non_buy_pct":    float(non_buy_df["non_buyability_pct"].mean().round(2)) if not non_buy_df.empty else 0.0,
        "categories_in_alert":      int(non_buy_df["is_alert"].sum()) if not non_buy_df.empty else 0,
        "total_suggested_order_cost": float(planning_df["suggested_order_cost"].sum().round(2)),
    }


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run() -> dict:
    """Compute all planning outputs. Called by scheduler."""
    start_ts = datetime.now()
    engine = get_engine()
    cfg = load_config()

    try:
        window = int(cfg["pipeline"]["run_rate_window_days"])
        run_rate_df  = compute_run_rate(engine, window_days=window)
        non_buy_df   = compute_non_buyability_large(engine)
        planning_df  = compute_planning(engine, run_rate_df)
        kpis         = compute_kpis(planning_df, non_buy_df)

        return {
            "status": "success",
            "kpis": kpis,
            "run_rate_rows":  len(run_rate_df),
            "non_buy_rows":   len(non_buy_df),
            "planning_rows":  len(planning_df),
            "duration_seconds": round((datetime.now() - start_ts).total_seconds(), 2),
            # DataFrames available for callers (alerting, Power BI refresh)
            "_run_rate_df":  run_rate_df,
            "_non_buy_df":   non_buy_df,
            "_planning_df":  planning_df,
        }
    except Exception as e:
        logger.exception("Planning engine failed: %s", e)
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run()
    kpis = result.get("kpis", {})
    print("\n=== PLANNING KPIs ===")
    for k, v in kpis.items():
        print(f"  {k:<35} {v}")
