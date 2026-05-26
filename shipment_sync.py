"""
etl/shipment_sync.py
Syncs new/updated shipment records from source system into the DB.
Runs every 30 minutes via scheduler.

Extend `extract_shipments_from_source()` to connect to your
actual order management system (OMS), WMS API, or flat file.
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import text

from etl.db import get_engine, load_config

logger = logging.getLogger(__name__)


def extract_shipments_from_source(
    engine,
    since: Optional[datetime] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Pull shipments and line items created/updated since `since`.

    PRODUCTION: Replace the demo SELECT below with:
      - An API call to your OMS/WMS
      - A query against a staging/integration table
      - A read from an S3/blob file dropped by your ERP

    Returns: (shipments_df, lines_df)
    """
    since = since or (datetime.now() - timedelta(hours=1))

    # Demo: read back what's already in the DB (no-op in practice)
    ships_sql = text("""
        SELECT shipment_id, order_reference, shipment_date, promised_date,
               actual_delivery, channel, origin_warehouse, destination_region,
               carrier, status, total_units, created_at
        FROM shipments
        WHERE created_at >= :since
        ORDER BY shipment_id
    """)
    lines_sql = text("""
        SELECT li.line_id, li.shipment_id, li.sku, li.qty_ordered,
               li.qty_shipped, li.qty_cancelled, li.unit_price,
               li.is_buyable, li.non_buy_reason
        FROM shipment_line_items li
        JOIN shipments s ON s.shipment_id = li.shipment_id
        WHERE s.created_at >= :since
    """)
    with engine.connect() as conn:
        ships_df = pd.read_sql(ships_sql, conn, params={"since": since})
        lines_df = pd.read_sql(lines_sql, conn, params={"since": since})

    logger.info("Extracted %d shipments, %d lines since %s", len(ships_df), len(lines_df), since)
    return ships_df, lines_df


def tag_large_items(lines_df: pd.DataFrame, engine) -> pd.DataFrame:
    """Set is_large flag from products.size_class."""
    if lines_df.empty:
        return lines_df
    size_sql = text("SELECT sku, size_class FROM products WHERE is_active = TRUE")
    with engine.connect() as conn:
        sizes = pd.read_sql(size_sql, conn)
    lines_df = lines_df.merge(sizes, on="sku", how="left")
    lines_df["is_large"] = lines_df["size_class"] == "LARGE"
    return lines_df.drop(columns=["size_class"])


def upsert_shipments(ships_df: pd.DataFrame, lines_df: pd.DataFrame, engine) -> dict:
    """Upsert shipments and line items. Returns counts."""
    if ships_df.empty:
        logger.info("No shipments to sync")
        return {"shipments": 0, "lines": 0}

    cfg = load_config()
    batch_size = int(cfg["pipeline"]["shipment_batch_size"])

    ship_sql = text("""
        INSERT INTO shipments
            (shipment_id, order_reference, shipment_date, promised_date,
             actual_delivery, channel, origin_warehouse, destination_region,
             carrier, status, total_units)
        VALUES
            (:shipment_id, :order_reference, :shipment_date, :promised_date,
             :actual_delivery, :channel, :origin_warehouse, :destination_region,
             :carrier, :status, :total_units)
        ON CONFLICT (shipment_id) DO UPDATE SET
            status           = EXCLUDED.status,
            actual_delivery  = EXCLUDED.actual_delivery,
            total_units      = EXCLUDED.total_units
    """)
    line_sql = text("""
        INSERT INTO shipment_line_items
            (line_id, shipment_id, sku, qty_ordered, qty_shipped,
             qty_cancelled, unit_price, is_large, is_buyable, non_buy_reason)
        VALUES
            (:line_id, :shipment_id, :sku, :qty_ordered, :qty_shipped,
             :qty_cancelled, :unit_price, :is_large, :is_buyable, :non_buy_reason)
        ON CONFLICT (line_id) DO UPDATE SET
            qty_shipped    = EXCLUDED.qty_shipped,
            qty_cancelled  = EXCLUDED.qty_cancelled,
            is_buyable     = EXCLUDED.is_buyable,
            non_buy_reason = EXCLUDED.non_buy_reason
    """)

    ships_count = lines_count = 0
    with engine.begin() as conn:
        for start in range(0, len(ships_df), batch_size):
            batch = ships_df.iloc[start : start + batch_size].to_dict(orient="records")
            conn.execute(ship_sql, batch)
            ships_count += len(batch)

        for start in range(0, len(lines_df), batch_size):
            batch = lines_df.iloc[start : start + batch_size].to_dict(orient="records")
            conn.execute(line_sql, batch)
            lines_count += len(batch)

    logger.info("Synced %d shipments, %d lines", ships_count, lines_count)
    return {"shipments": ships_count, "lines": lines_count}


def run(since: Optional[datetime] = None) -> dict:
    """Main entry point."""
    start_ts = datetime.now()
    engine = get_engine()
    try:
        ships_df, lines_df = extract_shipments_from_source(engine, since)
        lines_df = tag_large_items(lines_df, engine)
        counts = upsert_shipments(ships_df, lines_df, engine)
        return {
            "status": "success",
            "duration_seconds": round((datetime.now() - start_ts).total_seconds(), 2),
            **counts,
        }
    except Exception as e:
        logger.exception("Shipment sync failed: %s", e)
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(run())
