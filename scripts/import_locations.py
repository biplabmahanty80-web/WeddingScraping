"""
scripts/import_locations.py — one-time import of Jharkhand Excel → PostgreSQL.

Usage:
    python scripts/import_locations.py Jharkhand_Localities_2000.xlsx

Idempotent: re-running will not create duplicates (uses ON CONFLICT DO NOTHING).
"""
import asyncio
import os
import sys

import asyncpg
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


def load_excel(path: str) -> pd.DataFrame:
    df = pd.read_excel(path)

    # Detect coordinate columns (handles "location.coordinates[0]" etc.)
    coord0 = coord1 = None
    for col in df.columns:
        if "coordinates[0]" in col or "coord[0]" in col:
            coord0 = col
        if "coordinates[1]" in col or "coord[1]" in col:
            coord1 = col

    if coord0 and coord1:
        df = df.rename(columns={coord0: "lng", coord1: "lat"})
    elif "lat" not in df.columns or "lng" not in df.columns:
        raise ValueError(
            f"Cannot find coordinate columns in {path}.\nColumns: {list(df.columns)}"
        )

    df = df.dropna(subset=["lat", "lng"])
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lng"] = pd.to_numeric(df["lng"], errors="coerce")
    df = df.dropna(subset=["lat", "lng"])

    # Normalise column names
    rename = {
        "postalCode": "postal_code",
        "PostalCode": "postal_code",
        "postal_code": "postal_code",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ("area", "city", "district", "state", "country", "postal_code"):
        if col not in df.columns:
            df[col] = None

    return df


async def import_locations(path: str):
    df = load_excel(path)
    print(f"Loaded {len(df)} rows from {path}")

    conn = await asyncpg.connect(DATABASE_URL)
    inserted = skipped = 0
    try:
        for _, row in df.iterrows():
            result = await conn.execute(
                """INSERT INTO scrape_locations
                       (area, city, district, state, country, postal_code, latitude, longitude)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (area, city, district, postal_code, latitude, longitude)
                   DO NOTHING""",
                row.get("area") or None,
                row.get("city") or None,
                row.get("district") or None,
                row.get("state") or None,
                row.get("country") or None,
                str(row.get("postal_code") or "") or None,
                float(row["lat"]),
                float(row["lng"]),
            )
            if result == "INSERT 0 1":
                inserted += 1
            else:
                skipped += 1
    finally:
        await conn.close()

    print(f"Done. Inserted: {inserted}  Skipped (duplicates): {skipped}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_locations.py <path_to_excel>")
        sys.exit(1)
    asyncio.run(import_locations(sys.argv[1]))
