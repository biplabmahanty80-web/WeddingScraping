-- migrations/001_initial.sql
-- Run once: psql "$DATABASE_URL" -f migrations/001_initial.sql

-- ── scrape_jobs ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    category            TEXT        NOT NULL,
    state               TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'running',
    total_locations     INTEGER     NOT NULL DEFAULT 0,
    completed_locations INTEGER     NOT NULL DEFAULT 0,
    failed_locations    INTEGER     NOT NULL DEFAULT 0,
    started_at          TIMESTAMP   DEFAULT NOW(),
    finished_at         TIMESTAMP,
    created_at          TIMESTAMP   DEFAULT NOW()
);

-- ── scrape_locations ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_locations (
    id              BIGSERIAL PRIMARY KEY,
    area            TEXT,
    city            TEXT,
    district        TEXT,
    state           TEXT,
    country         TEXT,
    postal_code     TEXT,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending',
    attempts        INTEGER     NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_scraped_at TIMESTAMP,
    created_at      TIMESTAMP   DEFAULT NOW(),
    updated_at      TIMESTAMP   DEFAULT NOW(),
    UNIQUE (area, city, district, postal_code, latitude, longitude)
);

-- ── scrape_location_jobs ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_location_jobs (
    id           BIGSERIAL PRIMARY KEY,
    job_id       BIGINT      NOT NULL REFERENCES scrape_jobs(id),
    location_id  BIGINT      NOT NULL REFERENCES scrape_locations(id),
    status       TEXT        NOT NULL DEFAULT 'pending',
    attempts     INTEGER     NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at   TIMESTAMP,
    completed_at TIMESTAMP,
    UNIQUE (job_id, location_id)
);

-- ── businesses ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS businesses (
    id                    BIGSERIAL PRIMARY KEY,
    scraper_category      TEXT,
    name                  TEXT,
    address               TEXT,
    phone                 TEXT,
    rating                TEXT,
    review_count          TEXT,
    opening_hours         TEXT,
    website               TEXT,
    gmaps_url             TEXT        UNIQUE NOT NULL,
    gmaps_name            TEXT,
    gmaps_address         TEXT,
    gmaps_phone           TEXT,
    gmaps_rating          TEXT,
    gmaps_reviews_count   TEXT,
    gmaps_opening_hours   TEXT,
    gmaps_category        TEXT,
    gmaps_website         TEXT,
    gmaps_about           TEXT,
    location_lat          DOUBLE PRECISION,
    location_lng          DOUBLE PRECISION,
    search_term           TEXT,
    scraped_at            TIMESTAMP,
    created_at            TIMESTAMP   DEFAULT NOW(),
    updated_at            TIMESTAMP   DEFAULT NOW()
);

-- ── business_discoveries ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS business_discoveries (
    id            BIGSERIAL PRIMARY KEY,
    business_id   BIGINT  NOT NULL REFERENCES businesses(id),
    location_id   BIGINT  REFERENCES scrape_locations(id),
    job_id        BIGINT  REFERENCES scrape_jobs(id),
    search_term   TEXT,
    discovered_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (business_id, location_id, job_id, search_term)
);

-- ── indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_scrape_locations_status  ON scrape_locations(status);
CREATE INDEX IF NOT EXISTS idx_scrape_locations_state   ON scrape_locations(state);
CREATE INDEX IF NOT EXISTS idx_scrape_location_jobs_status ON scrape_location_jobs(status);
CREATE INDEX IF NOT EXISTS idx_businesses_category      ON businesses(scraper_category);
CREATE INDEX IF NOT EXISTS idx_businesses_gmaps_url     ON businesses(gmaps_url);
