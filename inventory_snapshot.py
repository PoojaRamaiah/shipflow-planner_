"""
etl/inventory_snapshot.py
Pulls today's inventory levels and writes a snapshot row per SKU.
Designed to run daily at 06:00 (before planning refresh).

Usage:
    python -m etl.inventory_snapshot
    python -m etl.inventory_snapshot --date 2024-06-01
"""

import argparse
import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd
from sqlalchemy import text

from etl.db import get_engine, load_config

logger = logging.getLogger(__name__)


def extract_current_inventory(engine, as_of: Optional[date] = None) -> pd.DataFrame:
    """
    Extract current inventory from source system.
    In production: replace the SELECT with a query against your
    WMS / ERP integration table or API call result staging table.
    """
    snapshot_date = as_of or date.today()
    sql = text("""
        SELECT
            p.sku,
            COALESCE(i.qty_on_hand,    0)  AS qty_on_hand,
            COALESCE(i.qty_in_transit, 0)  AS qty_in_transit,
            COALESCE(i.qty_reserved,   0)  AS qty_reserved,
            i.warehouse_location,
            :snapshot_date                  AS snapshot_date
        FROM products p
        LEFT JOIN inventory i
            ON i.sku = p.sku
           AND i.snapshot_date = :snapshot_date
        WHERE p.is_active = TRUE
        ORDER BY p.sku
    """)
    with engine.connect() as conn:
        df = pd.read_sql(sql, conn, params={"snapshot_date": str(snapshot_date)})
    logger.info("Extracted %d SKU rows for %s", len(df), snapshot_date)
    return df


def classify_large_items(df: pd.DataFrame, engine) -> pd.DataFrame:
    """Tag rows with size_class from products table."""
    size_sql = text("SELECT sku, size_class, weight_kg, volume_cm3 FROM products WHERE is_active = TRUE")
    with engine.connect() as conn:
        sizes = pd.read_sql(size_sql, conn)
    df = df.merge(sizes, on="sku", how="left")
    return df


def upsert_inventory_snapshot(df: pd.DataFrame, engine, snapshot_date: date) -> int:
    """
    Upsert inventory rows.
    Uses INSERT ... ON CONFLICT DO UPDATE so re-runs are idempotent.
    """
    if df.empty:
        logger.warning("No rows to upsert")
        return 0

    rows_affected = 0
    upsert_sql = text("""
        INSERT INTO inventory
            (sku, snapshot_date, qty_on_hand, qty_in_transit, qty_reserved, warehouse_location, last_updated)
        VALUES
            (:sku, :snapshot_date, :qty_on_hand, :qty_in_transit, :qty_reserved, :warehouse_location, NOW())
        ON CONFLICT (sku, snapshot_date) DO UPDATE SET
            qty_on_hand       = EXCLUDED.qty_on_hand,
            qty_in_transit    = EXCLUDED.qty_in_transit,
            qty_reserved      = EXCLUDED.qty_reserved,
            warehouse_location = EXCLUDED.warehouse_location,
            last_updated       = NOW()
    """)

    cfg = load_config()
    batch_size = int(cfg["pipeline"]["inventory_batch_size"])

    with engine.begin() as conn:
        for start in range(0, len(df), batch_size):
            batch = df.iloc[start : start + batch_size]
            records = batch[
                ["sku", "snapshot_date", "qty_on_hand", "qty_in_transit",
                 "qty_reserved", "warehouse_location"]
            ].to_dict(orient="records")
            conn.execute(upsert_sql, records)
            rows_affected += len(records)
            logger.debug("Upserted batch %d rows", len(records))

    logger.info("Inventory snapshot complete: %d rows upserted for %s", rows_affected, snapshot_date)
    return rows_affected


def run(as_of: Optional[date] = None) -> dict:
    """Main entry point. Returns a summary dict for logging/monitoring."""
    start_ts = datetime.now()
    snapshot_date = as_of or date.today()
    engine = get_engine()

    try:
        df = extract_current_inventory(engine, snapshot_date)
        df = classify_large_items(df, engine)
        rows = upsert_inventory_snapshot(df, engine, snapshot_date)

        large_skus = len(df[df["size_class"] == "LARGE"]) if "size_class" in df.columns else 0
        summary = {
            "status": "success",
            "snapshot_date": str(snapshot_date),
            "total_skus": len(df),
            "large_skus": large_skus,
            "rows_upserted": rows,
            "duration_seconds": round((datetime.now() - start_ts).total_seconds(), 2),
        }
        logger.info("Snapshot summary: %s", summary)
        return summary

    except Exception as e:
        logger.exception("Inventory snapshot failed: %s", e)
        return {"status": "error", "error": str(e), "snapshot_date": str(snapshot_date)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run inventory snapshot ETL")
    parser.add_argument("--date", help="Snapshot date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.date) if args.date else None
    result = run(as_of)
    print(result)
