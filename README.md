# 📦 Inventory Planning Pipeline

End-to-end inventory planning system for **daily shipment run rate**, **large-item non-buyability tracking**, and **reorder planning** — built with Python, PostgreSQL, and Power BI.

---

## Architecture

```
Data Sources
  └── shipments DB / WMS / ERP
        │
        ▼
Python ETL (runs on schedule)
  ├── etl/inventory_snapshot.py   Daily 06:00  → inventory table
  ├── etl/shipment_sync.py        Every 30 min → shipments + line_items
        │
        ▼
SQL Views (analytical layer)
  ├── vw_daily_run_rate            Rolling 7-day units/day by category
  ├── vw_non_buyability_large      Non-buyability % for LARGE SKUs
  ├── vw_inventory_planning        Coverage days, reorder recommendations
  └── vw_kpi_summary               Dashboard cards
        │
        ▼
Planning Model  (daily 07:00)
  └── models/planning_engine.py   Run rate × coverage → planning actions
        │
        ├──▶ models/alerts.py      Email alerts (critical / threshold breach)
        └──▶ Power BI              Scheduled dataset refresh (08:00)
```

---

## Quick Start

### 1. Clone and install
```bash
git clone https://github.com/your-org/inventory-planning.git
cd inventory-planning

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure
```bash
cp config/.env.example .env
# Edit .env with your DB credentials and SMTP settings
```

### 3. Initialise the database
```bash
psql -h localhost -U postgres -d inventory_db \
  -f sql/01_schema.sql \
  -f sql/02_views.sql \
  -f sql/03_seed_data.sql   # optional — sample data for dev
```

### 4. Run the pipeline once (verify setup)
```bash
python -m scheduler.runner --once
```

### 5. Start the scheduler
```bash
python -m scheduler.runner
```

---

## Power BI

See **`reports/POWERBI_SETUP.md`** for:
- How to connect Power BI Desktop to PostgreSQL
- Which views to import
- DAX measures to copy from `reports/dax_measures.dax`
- Page-by-page visual layout guide

---

## Project Structure

```
inventory-planning/
├── sql/
│   ├── 01_schema.sql         Tables: products, inventory, shipments, line_items
│   ├── 02_views.sql          Analytical views consumed by Power BI
│   └── 03_seed_data.sql      Sample data (dev/test only)
│
├── etl/
│   ├── db.py                 DB engine, config loader
│   ├── inventory_snapshot.py Daily inventory upsert
│   └── shipment_sync.py      Shipment & line-item sync
│
├── models/
│   ├── planning_engine.py    Run rate, non-buyability, reorder logic
│   └── alerts.py             Email alerts (CRITICAL, threshold breach, daily summary)
│
├── scheduler/
│   └── runner.py             APScheduler — all cron jobs in one process
│
├── reports/
│   ├── dax_measures.dax      Power BI DAX measures (paste into Desktop)
│   └── POWERBI_SETUP.md      Step-by-step Power BI guide
│
├── tests/
│   └── test_planning_engine.py   Unit tests (no DB required)
│
├── config/
│   ├── settings.yaml         All config (env vars override)
│   └── .env.example          Environment variable template
│
├── .github/workflows/ci.yml  GitHub Actions: test + smoke test
├── requirements.txt
└── README.md
```

---

## Key Metrics Explained

### Daily Run Rate
Rolling N-day (default 7) average of units shipped per day, broken down by category and size class. Used as the denominator for all coverage calculations.

### Non-Buyability % (LARGE items)
Percentage of active LARGE SKUs with `qty_available ≤ 0`, grouped by category.
- **Alert threshold**: 15% (configurable in `planning_config` table)
- **LARGE definition**: weight ≥ 10 kg **or** volume ≥ 30,000 cm³

### Planning Status
| Status | Meaning |
|--------|---------|
| 🔴 CRITICAL | Zero available stock with active demand |
| 🟠 REORDER_NOW | Coverage < lead time — stock runs out before replenishment arrives |
| 🔵 PLAN_REORDER | Coverage < target days — order soon |
| 🟢 HEALTHY | Coverage ≥ target days |
| ⚪ NO_DEMAND | No shipments in the run-rate window |

---

## Configuration Reference

All settings live in `config/settings.yaml`. Key values:

| Key | Default | Description |
|-----|---------|-------------|
| `pipeline.run_rate_window_days` | 7 | Rolling window for run rate |
| `pipeline.coverage_target_days` | 14 | Target coverage to trigger PLAN_REORDER |
| `pipeline.safety_factor` | 1.2 | Reorder point multiplier |
| `pipeline.non_buy_alert_threshold` | 0.15 | 15% non-buyability triggers alert |
| `pipeline.large_weight_kg` | 10.0 | LARGE classification weight threshold |
| `pipeline.large_volume_cm3` | 30000 | LARGE classification volume threshold |

---

## Scheduler Jobs

| Job | Schedule (IST) | Description |
|-----|---------------|-------------|
| `inventory_snapshot` | Daily 06:00 | Snapshot today's inventory levels |
| `shipment_sync` | Every 30 min | Sync new/updated shipments |
| `planning_refresh` | Daily 07:00 | Recompute run rate + planning + fire alerts |
| `daily_report` | Weekdays 08:00 | Email summary to planners |

Run a single job manually:
```bash
python -m scheduler.runner --job inv     # inventory snapshot
python -m scheduler.runner --job ships   # shipment sync
python -m scheduler.runner --job plan    # planning + alerts
python -m scheduler.runner --job report  # daily email
```

---

## Extending the Pipeline

**Connect to your WMS/ERP**: edit `extract_shipments_from_source()` in `etl/shipment_sync.py` and `extract_current_inventory()` in `etl/inventory_snapshot.py`. Both functions have comments marking where to swap in your API call or staging table query.

**Add more size categories**: update `products.size_class` values and add new rules to `buyability_rules`. The views parameterise on `size_class = 'LARGE'` — clone `vw_non_buyability_large` with a different filter for other sizes.

**Change planning parameters**: update `planning_config` table rows directly in the DB — no code change needed.

---

## Running Tests

```bash
pytest tests/ -v --cov=etl --cov=models
```

Tests use in-memory DataFrames — no live DB needed.

---

## License
MIT
