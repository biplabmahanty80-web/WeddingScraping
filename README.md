# Wedding Scraper — Google Maps → PostgreSQL

Low-memory batch scraper for Google Maps business data.
Designed for a ~512 MB Render worker instance.

---

## Architecture

```
PostgreSQL
    │
    ├── scrape_locations   (imported from Excel once)
    ├── scrape_jobs        (one row per category+state run)
    ├── scrape_location_jobs
    ├── businesses         (UPSERT on gmaps_url)
    └── business_discoveries
         │
    Python worker  →  one Chromium  →  Google Maps
```

Each `python -m app.worker` run:
1. Claims `SCRAPER_BATCH_SIZE` pending locations (default 5).
2. Scrapes them sequentially with one Chromium instance.
3. UPSERTs every business directly to PostgreSQL.
4. Exits cleanly.

---

## Local setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

Copy and fill in environment variables:

```bash
cp .env.example .env
# edit .env — set DATABASE_URL at minimum
```

---

## Database setup

```bash
psql "$DATABASE_URL" -f migrations/001_initial.sql
```

---

## Import locations (one-time)

```bash
python scripts/import_locations.py Jharkhand_Localities_2000.xlsx
```

Safe to re-run — duplicates are skipped.

---

## Run the worker

```bash
python -m app.worker
```

Expected output:

```
Worker started.
Category: mehendi_artist | State: Jharkhand
Batch size: 5 | Max attempts: 3
Connecting to PostgreSQL...
Connected.
Job id: 1
Claimed 5 location(s).
Starting Chromium...
Browser ready.
[1/5] Ranchi Jharkhand
  Location: Ranchi Jharkhand (23.34,85.31) zoom=13
  Found 28 candidate URLs
  Saved 28 businesses
[1/5] Completed.
...
[5/5] Completed.
Closing browser...
Browser closed.
Job counters updated.
Worker finished.
```

Run again to process the next batch:

```bash
python -m app.worker   # → locations 6-10
python -m app.worker   # → locations 11-15
```

---

## Changing category

Set `SCRAPER_CATEGORY` to any key that has a matching `matchers/<category>.yaml`:

```bash
SCRAPER_CATEGORY=photographer python -m app.worker
```

Available matchers: `mehendi_artist`, `photographer`, `caterer`, `decorator`,
`venue`, `bridal_makeup`, `cake_shop`, `event_planner`.

---

## Render deployment

### Environment variables (set in Render dashboard)

| Variable | Example value |
|---|---|
| `DATABASE_URL` | `postgresql://...` (from Render Postgres) |
| `SCRAPER_CATEGORY` | `mehendi_artist` |
| `SCRAPER_STATE` | `Jharkhand` |
| `SCRAPER_BATCH_SIZE` | `5` |
| `MAX_LOCATION_ATTEMPTS` | `3` |
| `MAX_RESULTS` | `30` |
| `STALE_RUNNING_MINUTES` | `30` |

### Start command

```
python -m app.worker
```

### Service type

Use a **Background Worker** (not a Web Service).  
For scheduled execution use a **Cron Job** service on Render with the same start command.

### Limitation — Render Free tier

Render Free workers spin down after inactivity and have no built-in cron.
Options:
- Upgrade to a paid Render plan and use a Cron Job service.
- Use an external scheduler (GitHub Actions scheduled workflow, cron-job.org) to trigger the worker via a webhook or by restarting the service.
- Run the worker locally on a schedule.

---

## Crash recovery

If the worker crashes mid-batch, locations left in `status='running'` are
automatically reset to `pending` on the next startup (after `STALE_RUNNING_MINUTES`).
No data is lost — PostgreSQL is the only source of truth.

---

## Retry logic

- Each failed location increments `scrape_locations.attempts`.
- After `MAX_LOCATION_ATTEMPTS` failures the status becomes `permanently_failed`.
- `permanently_failed` locations are not re-queued automatically.
  To retry them manually:
  ```sql
  UPDATE scrape_locations SET status='pending', attempts=0 WHERE status='permanently_failed';
  ```

---

## Duplicate prevention

`businesses.gmaps_url` has a `UNIQUE` constraint.  
Every insert uses `ON CONFLICT (gmaps_url) DO UPDATE` — the same business
discovered from multiple locations is stored once and updated in place.
