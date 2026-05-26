# Power BI Setup Guide
## Inventory Planning Dashboard

---

### Step 1 — Connect Power BI to PostgreSQL

1. Open Power BI Desktop → **Get Data** → **PostgreSQL database**
2. Enter:
   - Server: `your-db-host`
   - Database: `inventory_db`
3. Choose **Import** mode (recommended for scheduled refresh)
4. Select these views from the `public` schema:

| View | Power BI Table Name |
|------|---------------------|
| `vw_daily_run_rate` | DailyRunRate |
| `vw_non_buyability_large` | NonBuyabilityLarge |
| `vw_inventory_planning` | InventoryPlanning |
| `vw_kpi_summary` | KPISummary |

---

### Step 2 — Data Model Relationships

In the **Model** view, verify or create these relationships:

```
DailyRunRate[category]  ──── NonBuyabilityLarge[category]
InventoryPlanning[sku]  ──── DailyRunRate[sku]   (via category)
```

Add a disconnected **Date Table** for time intelligence:
```
DateTable = CALENDAR(DATE(2024,1,1), DATE(2026,12,31))
```
Mark it as the Date Table. Link `DailyRunRate[report_date]` → `DateTable[Date]`.

---

### Step 3 — Import DAX Measures

1. Open **reports/dax_measures.dax** in a text editor
2. In Power BI, select a table (e.g. InventoryPlanning)
3. **New Measure** → paste each measure block
4. Name and save

---

### Step 4 — Build the 3 Report Pages

#### Page 1: Daily Run Rate
| Visual | Fields |
|--------|--------|
| Line chart | X: report_date, Y: [Daily Run Rate (Avg)], Legend: category |
| Column chart | X: report_date, Y: units_shipped, Legend: size_class |
| KPI card | [Rolling 7D Run Rate] vs [Daily Run Rate (Avg)] |
| KPI card | [DoD Change %] |
| Table | report_date, category, units_shipped, rolling_avg_daily_units |

Slicers: `category`, `size_class`, date range

---

#### Page 2: Non-Buyability (LARGE)
| Visual | Fields |
|--------|--------|
| Donut chart | Values: [Non-Buyable LARGE SKUs] vs [Total LARGE SKUs] |
| Bar chart | Y: category, X: [Non-Buyability %] — color by [Non-Buyability KPI Color] |
| KPI card | [Non-Buyability %] — target: 15% |
| KPI card | [Categories in Alert] |
| Table | category, total_large_skus, buyable_skus, non_buyable_skus, non_buyability_pct, is_alert |

Conditional formatting on `non_buyability_pct`:
- ≥ 15% → Red background
- ≥ 10% → Amber background
- < 10% → Green background

Slicer: `category`

---

#### Page 3: Inventory Planning
| Visual | Fields |
|--------|--------|
| Multi-row card | [CRITICAL SKUs], [Reorder Now SKUs], [Avg Coverage Days], [Total Suggested Order Qty] |
| Treemap | Group: category, Size: suggested_order_qty |
| Scatter | X: avg_daily_units, Y: coverage_days, Size: qty_available, Legend: size_class |
| Table | sku, product_name, category, size_class, planning_status, qty_available, avg_daily_units, coverage_days, suggested_order_qty |

Drillthrough from Page 2 (Non-Buyability) → Page 3 (SKU detail): set `sku` as drillthrough field.

Conditional formatting on `planning_status` column:
- CRITICAL → Red
- REORDER_NOW → Orange
- PLAN_REORDER → Blue
- HEALTHY → Green

Slicers: `planning_status`, `category`, `size_class`

---

### Step 5 — Schedule Refresh (Power BI Service)

1. Publish the `.pbix` to your Power BI workspace
2. **Dataset Settings** → **Gateway connection**: configure your on-premises data gateway (if DB is on-prem)
3. **Scheduled refresh** → set to run **daily at 08:00 IST** (after the pipeline's 07:00 planning refresh)
4. Optionally use `etl/powerbi_refresh.py` to trigger refresh via API immediately after the pipeline run

---

### Bookmarks (optional)

| Bookmark | Purpose |
|----------|---------|
| `All Categories` | Default view, all slicers cleared |
| `Large Items Only` | size_class = LARGE filter applied |
| `Critical Only` | planning_status = CRITICAL |
| `Alert Categories` | is_alert = True on Page 2 |
