-- ============================================================
-- Seed / sample data for development & testing
-- Run AFTER 01_schema.sql
-- ============================================================

-- Products (mix of LARGE and STANDARD)
INSERT INTO products (sku, product_name, category, sub_category, size_class, weight_kg, volume_cm3, unit_cost, lead_time_days, reorder_point, safety_stock)
VALUES
-- LARGE items
('SKU-LG-001', '65" OLED TV',             'Electronics',  'Televisions',     'LARGE', 28.5,  180000, 45000.00, 14, 10, 5),
('SKU-LG-002', 'King Size Mattress',       'Furniture',    'Bedding',         'LARGE', 35.0,  360000, 18000.00, 21, 5,  3),
('SKU-LG-003', 'Industrial Air Purifier',  'Appliances',   'Air Quality',     'LARGE', 18.2,  95000,  12000.00, 10, 8,  4),
('SKU-LG-004', '75" Smart TV',             'Electronics',  'Televisions',     'LARGE', 34.0,  220000, 62000.00, 14, 6,  3),
('SKU-LG-005', 'Double Door Refrigerator', 'Appliances',   'Refrigeration',   'LARGE', 82.0,  480000, 55000.00, 21, 4,  2),
('SKU-LG-006', 'Front Load Washing Machine','Appliances',  'Laundry',         'LARGE', 68.0,  400000, 32000.00, 21, 5,  2),
('SKU-LG-007', 'L-Shaped Sofa',            'Furniture',    'Seating',         'LARGE', 95.0,  750000, 25000.00, 28, 3,  2),
('SKU-LG-008', 'Treadmill Pro',            'Sports',       'Fitness',         'LARGE', 75.0,  320000, 38000.00, 14, 4,  2),
-- STANDARD items
('SKU-SM-001', 'Bluetooth Headphones',     'Electronics',  'Audio',           'STANDARD', 0.35, 2500,  2500.00, 7,  50, 20),
('SKU-SM-002', 'Yoga Mat',                 'Sports',       'Yoga',            'STANDARD', 1.2,  8000,   800.00, 5,  80, 30),
('SKU-SM-003', 'Coffee Maker',             'Appliances',   'Kitchen',         'STANDARD', 3.5,  15000,  3500.00, 7,  30, 10),
('SKU-SM-004', 'Running Shoes (Size 10)',  'Fashion',      'Footwear',        'STANDARD', 0.8,  4000,   4500.00, 7,  40, 15);

-- Inventory snapshot (today)
INSERT INTO inventory (sku, snapshot_date, qty_on_hand, qty_in_transit, qty_reserved, warehouse_location)
VALUES
-- LARGE items - varying stock levels
('SKU-LG-001', CURRENT_DATE, 12,  5,  2,  'WH-SOUTH-01'),
('SKU-LG-002', CURRENT_DATE, 0,   3,  0,  'WH-SOUTH-01'),  -- OUT OF STOCK
('SKU-LG-003', CURRENT_DATE, 25,  0,  3,  'WH-NORTH-01'),
('SKU-LG-004', CURRENT_DATE, 3,   0,  1,  'WH-SOUTH-01'),  -- LOW STOCK
('SKU-LG-005', CURRENT_DATE, 0,   0,  0,  'WH-NORTH-01'),  -- OUT OF STOCK
('SKU-LG-006', CURRENT_DATE, 8,   4,  2,  'WH-SOUTH-01'),
('SKU-LG-007', CURRENT_DATE, 0,   2,  0,  'WH-NORTH-01'),  -- OUT OF STOCK
('SKU-LG-008', CURRENT_DATE, 6,   0,  0,  'WH-SOUTH-01'),
-- STANDARD items
('SKU-SM-001', CURRENT_DATE, 250, 100, 30, 'WH-SOUTH-01'),
('SKU-SM-002', CURRENT_DATE, 180, 0,  20, 'WH-NORTH-01'),
('SKU-SM-003', CURRENT_DATE, 45,  20, 5,  'WH-SOUTH-01'),
('SKU-SM-004', CURRENT_DATE, 90,  0,  10, 'WH-NORTH-01');

-- Shipments — last 30 days
DO $$
DECLARE
    d DATE;
    ship_id INT;
    i INT;
BEGIN
    FOR i IN 0..29 LOOP
        d := CURRENT_DATE - i;

        -- Skip some days to simulate weekends/gaps
        IF EXTRACT(DOW FROM d) NOT IN (0) THEN
            INSERT INTO shipments (order_reference, shipment_date, promised_date, actual_delivery, channel, origin_warehouse, destination_region, carrier, status, total_units)
            VALUES (
                'ORD-' || TO_CHAR(d, 'YYYYMMDD') || '-' || LPAD(i::TEXT, 3, '0'),
                d, d + 3, CASE WHEN i > 3 THEN d + 3 ELSE NULL END,
                (ARRAY['D2C','B2B','MARKETPLACE'])[1 + (i % 3)],
                (ARRAY['WH-SOUTH-01','WH-NORTH-01'])[1 + (i % 2)],
                (ARRAY['SOUTH','NORTH','EAST','WEST'])[1 + (i % 4)],
                (ARRAY['BlueDart','DTDC','Delhivery','Ekart'])[1 + (i % 4)],
                CASE WHEN i > 3 THEN 'DELIVERED' WHEN i > 0 THEN 'IN_TRANSIT' ELSE 'DISPATCHED' END,
                FLOOR(5 + RANDOM() * 20)
            ) RETURNING shipment_id INTO ship_id;

            -- Line items — mix of LARGE and STANDARD
            INSERT INTO shipment_line_items (shipment_id, sku, qty_ordered, qty_shipped, unit_price, is_large, is_buyable)
            VALUES
            (ship_id, 'SKU-LG-001', 2 + (i % 3), 2 + (i % 3), 45000, TRUE, TRUE),
            (ship_id, 'SKU-LG-003', 3 + (i % 2), 3 + (i % 2), 12000, TRUE, TRUE),
            (ship_id, 'SKU-SM-001', 10 + (i % 5), 10 + (i % 5), 2500, FALSE, TRUE),
            (ship_id, 'SKU-SM-002', 8 + (i % 4), 8 + (i % 4), 800, FALSE, TRUE);

            -- Occasional large item non-buyable lines
            IF i % 5 = 0 THEN
                INSERT INTO shipment_line_items (shipment_id, sku, qty_ordered, qty_shipped, unit_price, is_large, is_buyable, non_buy_reason)
                VALUES (ship_id, 'SKU-LG-004', 1, 0, 62000, TRUE, FALSE, 'NO_STOCK');
            END IF;
        END IF;
    END LOOP;
END $$;
