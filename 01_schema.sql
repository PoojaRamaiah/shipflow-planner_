-- ============================================================
-- Inventory Planning Schema
-- Creates all base tables for the pipeline
-- ============================================================

-- Drop and recreate in safe order
DROP TABLE IF EXISTS shipment_line_items CASCADE;
DROP TABLE IF EXISTS shipments CASCADE;
DROP TABLE IF EXISTS inventory CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS buyability_rules CASCADE;
DROP TABLE IF EXISTS planning_config CASCADE;

-- ------------------------------------------------------------
-- Products master
-- ------------------------------------------------------------
CREATE TABLE products (
    product_id      SERIAL PRIMARY KEY,
    sku             VARCHAR(64)  NOT NULL UNIQUE,
    product_name    VARCHAR(256) NOT NULL,
    category        VARCHAR(128) NOT NULL,          -- e.g. Electronics, Apparel
    sub_category    VARCHAR(128),
    size_class      VARCHAR(16)  NOT NULL DEFAULT 'STANDARD',
    -- size_class values: SMALL, STANDARD, LARGE, EXTRA_LARGE
    weight_kg       NUMERIC(10,3),
    volume_cm3      NUMERIC(12,2),
    unit_cost       NUMERIC(12,4),
    lead_time_days  INT          NOT NULL DEFAULT 7,
    reorder_point   INT          NOT NULL DEFAULT 0,  -- overridden by planning model
    safety_stock    INT          NOT NULL DEFAULT 0,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Inventory snapshot (updated daily by ETL)
-- ------------------------------------------------------------
CREATE TABLE inventory (
    inventory_id        SERIAL PRIMARY KEY,
    sku                 VARCHAR(64)  NOT NULL REFERENCES products(sku),
    snapshot_date       DATE         NOT NULL DEFAULT CURRENT_DATE,
    qty_on_hand         INT          NOT NULL DEFAULT 0,
    qty_in_transit      INT          NOT NULL DEFAULT 0,
    qty_reserved        INT          NOT NULL DEFAULT 0,
    qty_available       INT GENERATED ALWAYS AS (qty_on_hand - qty_reserved) STORED,
    warehouse_location  VARCHAR(64),
    last_updated        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (sku, snapshot_date)
);

-- ------------------------------------------------------------
-- Shipments header
-- ------------------------------------------------------------
CREATE TABLE shipments (
    shipment_id         SERIAL PRIMARY KEY,
    order_reference     VARCHAR(128),
    shipment_date       DATE         NOT NULL,
    promised_date       DATE,
    actual_delivery     DATE,
    channel             VARCHAR(64)  NOT NULL DEFAULT 'DIRECT', -- B2B, D2C, MARKETPLACE
    origin_warehouse    VARCHAR(64),
    destination_region  VARCHAR(64),
    carrier             VARCHAR(64),
    status              VARCHAR(32)  NOT NULL DEFAULT 'PENDING',
    -- PENDING, DISPATCHED, IN_TRANSIT, DELIVERED, CANCELLED, RETURNED
    total_units         INT          NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------
-- Shipment line items
-- ------------------------------------------------------------
CREATE TABLE shipment_line_items (
    line_id         SERIAL PRIMARY KEY,
    shipment_id     INT          NOT NULL REFERENCES shipments(shipment_id) ON DELETE CASCADE,
    sku             VARCHAR(64)  NOT NULL REFERENCES products(sku),
    qty_ordered     INT          NOT NULL,
    qty_shipped     INT          NOT NULL DEFAULT 0,
    qty_cancelled   INT          NOT NULL DEFAULT 0,
    unit_price      NUMERIC(12,4),
    is_large        BOOLEAN      NOT NULL DEFAULT FALSE,  -- flagged from products.size_class
    is_buyable      BOOLEAN      NOT NULL DEFAULT TRUE,
    non_buy_reason  VARCHAR(256)  -- e.g. OUT_OF_STOCK, RESTRICTED, DAMAGED
);

-- ------------------------------------------------------------
-- Buyability rules (drives non-buyability classification)
-- ------------------------------------------------------------
CREATE TABLE buyability_rules (
    rule_id         SERIAL PRIMARY KEY,
    rule_code       VARCHAR(64)  NOT NULL UNIQUE,
    rule_name       VARCHAR(128) NOT NULL,
    applies_to_size VARCHAR(16), -- NULL = all sizes, 'LARGE' = large only
    category        VARCHAR(128), -- NULL = all categories
    description     TEXT,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Seed default rules
INSERT INTO buyability_rules (rule_code, rule_name, applies_to_size, description) VALUES
('NO_STOCK',     'Out of stock',              NULL,    'qty_available <= 0'),
('RESTRICTED',   'Regulatory restriction',    NULL,    'Product flagged as restricted for destination'),
('DAMAGED',      'Damaged / QC hold',         NULL,    'Product under quality hold'),
('OVERSIZE_CAP', 'Oversize carrier cap',      'LARGE', 'Large item exceeds carrier dimension limits'),
('OVERSIZE_WGT', 'Oversize weight limit',     'LARGE', 'Large item exceeds carrier weight threshold'),
('LARGE_HAZMAT', 'Large hazmat restriction',  'LARGE', 'Large item classified as hazardous material');

-- ------------------------------------------------------------
-- Planning configuration (editable by planners)
-- ------------------------------------------------------------
CREATE TABLE planning_config (
    config_key      VARCHAR(64) PRIMARY KEY,
    config_value    VARCHAR(256) NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO planning_config VALUES
('run_rate_window_days',    '7',    'Rolling window (days) for daily run rate calculation', NOW()),
('coverage_target_days',   '14',   'Target inventory coverage in days', NOW()),
('large_size_weight_kg',   '10',   'Weight threshold (kg) to classify LARGE', NOW()),
('large_size_volume_cm3',  '30000','Volume threshold (cm3) to classify LARGE', NOW()),
('reorder_safety_factor',  '1.2',  'Multiply reorder point by this safety factor', NOW()),
('non_buy_alert_threshold','0.15', 'Alert if non-buyability % exceeds this (0.15 = 15%)', NOW());

-- ------------------------------------------------------------
-- Indexes
-- ------------------------------------------------------------
CREATE INDEX idx_inventory_sku_date      ON inventory(sku, snapshot_date DESC);
CREATE INDEX idx_shipments_date          ON shipments(shipment_date DESC);
CREATE INDEX idx_shipments_status        ON shipments(status);
CREATE INDEX idx_lines_sku               ON shipment_line_items(sku);
CREATE INDEX idx_lines_shipment          ON shipment_line_items(shipment_id);
CREATE INDEX idx_products_size_class     ON products(size_class);
CREATE INDEX idx_products_category       ON products(category);
