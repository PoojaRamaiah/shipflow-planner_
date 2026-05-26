-- ============================================================
-- Analytical Views — Inventory Planning
-- ============================================================

-- ------------------------------------------------------------
-- V1: Daily shipment run rate (rolling 7-day window)
-- Power BI: page "Daily Run Rate"
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW vw_daily_run_rate AS
WITH config AS (
    SELECT
        (SELECT config_value::INT FROM planning_config WHERE config_key = 'run_rate_window_days') AS window_days
),
daily_units AS (
    SELECT
        s.shipment_date                          AS report_date,
        p.category,
        p.size_class,
        SUM(li.qty_shipped)                      AS units_shipped,
        COUNT(DISTINCT s.shipment_id)            AS shipment_count,
        SUM(li.qty_shipped * li.unit_price)      AS revenue
    FROM shipments s
    JOIN shipment_line_items li ON li.shipment_id = s.shipment_id
    JOIN products p ON p.sku = li.sku
    WHERE s.status NOT IN ('CANCELLED', 'RETURNED')
    GROUP BY s.shipment_date, p.category, p.size_class
)
SELECT
    d.report_date,
    d.category,
    d.size_class,
    d.units_shipped,
    d.shipment_count,
    d.revenue,
    ROUND(
        AVG(d.units_shipped) OVER (
            PARTITION BY d.category, d.size_class
            ORDER BY d.report_date
            ROWS BETWEEN (SELECT window_days - 1 FROM config) PRECEDING AND CURRENT ROW
        ), 2
    )                                            AS rolling_avg_daily_units,
    ROUND(
        AVG(d.revenue) OVER (
            PARTITION BY d.category, d.size_class
            ORDER BY d.report_date
            ROWS BETWEEN (SELECT window_days - 1 FROM config) PRECEDING AND CURRENT ROW
        ), 2
    )                                            AS rolling_avg_daily_revenue,
    -- day-over-day change %
    ROUND(
        100.0 * (d.units_shipped - LAG(d.units_shipped) OVER (
            PARTITION BY d.category, d.size_class ORDER BY d.report_date
        )) / NULLIF(LAG(d.units_shipped) OVER (
            PARTITION BY d.category, d.size_class ORDER BY d.report_date
        ), 0), 1
    )                                            AS dod_change_pct
FROM daily_units d
ORDER BY d.report_date DESC, d.category;


-- ------------------------------------------------------------
-- V2: Non-buyability % by category — LARGE items only
-- Power BI: page "Non-Buyability"
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW vw_non_buyability_large AS
WITH snapshot AS (
    -- Use today's (or latest available) inventory snapshot
    SELECT i.*, p.category, p.size_class, p.product_name
    FROM inventory i
    JOIN products p ON p.sku = i.sku
    WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
      AND p.size_class = 'LARGE'
      AND p.is_active = TRUE
),
buyability AS (
    SELECT
        s.sku,
        s.category,
        s.product_name,
        s.qty_on_hand,
        s.qty_available,
        -- A product is non-buyable if it has no available stock
        -- OR has an active buyability rule flagging it
        CASE
            WHEN s.qty_available <= 0 THEN FALSE
            ELSE TRUE
        END                                     AS is_buyable,
        CASE
            WHEN s.qty_available <= 0 THEN 'NO_STOCK'
            ELSE NULL
        END                                     AS primary_reason
    FROM snapshot s
)
SELECT
    b.category,
    COUNT(*)                                    AS total_large_skus,
    SUM(CASE WHEN b.is_buyable THEN 1 ELSE 0 END)   AS buyable_skus,
    SUM(CASE WHEN NOT b.is_buyable THEN 1 ELSE 0 END) AS non_buyable_skus,
    ROUND(
        100.0 * SUM(CASE WHEN NOT b.is_buyable THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 2
    )                                           AS non_buyability_pct,
    -- Alert flag (threshold from config)
    CASE
        WHEN ROUND(
            100.0 * SUM(CASE WHEN NOT b.is_buyable THEN 1 ELSE 0 END)
            / NULLIF(COUNT(*), 0), 2
        ) > (SELECT config_value::NUMERIC * 100 FROM planning_config WHERE config_key = 'non_buy_alert_threshold')
        THEN TRUE ELSE FALSE
    END                                         AS is_alert,
    -- Most common non-buy reason in this category
    MODE() WITHIN GROUP (ORDER BY b.primary_reason) AS top_non_buy_reason,
    (SELECT snapshot_date FROM inventory ORDER BY snapshot_date DESC LIMIT 1) AS as_of_date
FROM buyability b
GROUP BY b.category
ORDER BY non_buyability_pct DESC;


-- ------------------------------------------------------------
-- V3: SKU-level non-buyability detail (drillthrough in Power BI)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW vw_non_buyability_large_detail AS
SELECT
    p.sku,
    p.product_name,
    p.category,
    p.sub_category,
    p.weight_kg,
    p.volume_cm3,
    i.qty_on_hand,
    i.qty_in_transit,
    i.qty_reserved,
    i.qty_available,
    CASE WHEN i.qty_available <= 0 THEN FALSE ELSE TRUE END AS is_buyable,
    CASE WHEN i.qty_available <= 0 THEN 'NO_STOCK'
         ELSE 'BUYABLE'
    END                                                     AS status,
    i.snapshot_date                                         AS as_of_date
FROM products p
JOIN inventory i ON i.sku = p.sku
WHERE p.size_class = 'LARGE'
  AND p.is_active = TRUE
  AND i.snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
ORDER BY is_buyable ASC, i.qty_available ASC;


-- ------------------------------------------------------------
-- V4: Inventory coverage & reorder planning
-- Power BI: page "Planning"
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW vw_inventory_planning AS
WITH config AS (
    SELECT
        (SELECT config_value::INT     FROM planning_config WHERE config_key = 'coverage_target_days')   AS target_days,
        (SELECT config_value::NUMERIC FROM planning_config WHERE config_key = 'reorder_safety_factor')  AS safety_factor
),
run_rate AS (
    -- Latest 7-day avg run rate per SKU
    SELECT
        li.sku,
        ROUND(
            AVG(li.qty_shipped) FILTER (WHERE s.shipment_date >= CURRENT_DATE - INTERVAL '7 days'), 2
        ) AS avg_daily_units
    FROM shipment_line_items li
    JOIN shipments s ON s.shipment_id = li.shipment_id
    WHERE s.status NOT IN ('CANCELLED', 'RETURNED')
    GROUP BY li.sku
),
latest_inv AS (
    SELECT *
    FROM inventory
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM inventory)
)
SELECT
    p.sku,
    p.product_name,
    p.category,
    p.size_class,
    p.lead_time_days,
    i.qty_on_hand,
    i.qty_in_transit,
    i.qty_available,
    COALESCE(r.avg_daily_units, 0)              AS avg_daily_units,
    -- Coverage days = available stock / daily run rate
    CASE
        WHEN COALESCE(r.avg_daily_units, 0) = 0 THEN NULL
        ELSE ROUND(i.qty_available / r.avg_daily_units, 1)
    END                                         AS coverage_days,
    -- Reorder point = lead time demand × safety factor
    ROUND(
        COALESCE(r.avg_daily_units, 0) * p.lead_time_days
        * (SELECT safety_factor FROM config), 0
    )                                           AS reorder_point,
    -- Quantity to order to reach target coverage
    GREATEST(0, ROUND(
        (SELECT target_days FROM config) * COALESCE(r.avg_daily_units, 0)
        - i.qty_available - i.qty_in_transit, 0
    ))                                          AS suggested_order_qty,
    -- Planning status
    CASE
        WHEN COALESCE(r.avg_daily_units, 0) = 0 THEN 'NO_DEMAND'
        WHEN i.qty_available <= 0 THEN 'CRITICAL'
        WHEN i.qty_available / NULLIF(r.avg_daily_units, 0)
             < p.lead_time_days THEN 'REORDER_NOW'
        WHEN i.qty_available / NULLIF(r.avg_daily_units, 0)
             < (SELECT target_days FROM config) THEN 'PLAN_REORDER'
        ELSE 'HEALTHY'
    END                                         AS planning_status,
    i.snapshot_date                             AS as_of_date
FROM products p
JOIN latest_inv i ON i.sku = p.sku
LEFT JOIN run_rate r ON r.sku = p.sku
WHERE p.is_active = TRUE
ORDER BY
    CASE WHEN COALESCE(r.avg_daily_units, 0) = 0 THEN 0
         WHEN i.qty_available <= 0 THEN 5
         WHEN i.qty_available / NULLIF(r.avg_daily_units, 0) < p.lead_time_days THEN 4
         WHEN i.qty_available / NULLIF(r.avg_daily_units, 0) < (SELECT target_days FROM config) THEN 3
         ELSE 1
    END DESC,
    p.category, p.sku;


-- ------------------------------------------------------------
-- V5: Summary KPIs (Power BI card visuals)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW vw_kpi_summary AS
SELECT
    (SELECT COUNT(*) FROM products WHERE is_active = TRUE)                     AS total_active_skus,
    (SELECT COUNT(*) FROM products WHERE is_active = TRUE AND size_class = 'LARGE') AS total_large_skus,
    (SELECT ROUND(AVG(non_buyability_pct),1) FROM vw_non_buyability_large)    AS avg_large_non_buy_pct,
    (SELECT COUNT(*) FROM vw_non_buyability_large WHERE is_alert = TRUE)       AS categories_in_alert,
    (SELECT COUNT(*) FROM vw_inventory_planning WHERE planning_status = 'CRITICAL') AS critical_sku_count,
    (SELECT COUNT(*) FROM vw_inventory_planning WHERE planning_status = 'REORDER_NOW') AS reorder_now_count,
    (SELECT ROUND(SUM(avg_daily_units),0) FROM vw_inventory_planning)          AS total_daily_run_rate,
    NOW()                                                                       AS refreshed_at;
