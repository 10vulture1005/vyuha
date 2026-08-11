# scripts/seed_universe.py
"""Bootstrap script to validate and ingest the Nifty 500 seed universe.

Usage:
    python scripts/seed_universe.py          # Validate only
    python scripts/seed_universe.py --ingest # Validate + insert into DB
"""
import csv
import sys
from pathlib import Path

from loguru import logger

BASE_DIR = Path(__file__).resolve().parent.parent
CSV_PATH = BASE_DIR / "config" / "universe_nifty500.csv"

REQUIRED_HEADERS = {"symbol", "name", "isin", "sector", "exchange"}


def validate_and_load_seed() -> list[dict[str, str]]:
    """Validate CSV format and return structured records."""
    if not CSV_PATH.exists():
        logger.error(f"Seed CSV not found at {CSV_PATH}")
        sys.exit(1)

    records = []

    with open(CSV_PATH, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not REQUIRED_HEADERS.issubset(set(reader.fieldnames or [])):
            logger.error(f"CSV missing required headers: {REQUIRED_HEADERS}")
            sys.exit(1)

        for row_idx, row in enumerate(reader, start=2):
            symbol = row.get("symbol", "").strip().upper()
            isin = row.get("isin", "").strip().upper()
            if not symbol or not isin:
                logger.warning(
                    f"Row {row_idx} invalid: symbol and isin required. Skipping."
                )
                continue
            records.append(
                {
                    "symbol": symbol,
                    "name": row.get("name", "").strip(),
                    "isin": isin,
                    "sector": row.get("sector", "Unknown").strip(),
                    "exchange": row.get("exchange", "NSE").strip().upper(),
                }
            )

    logger.info(f"Successfully validated {len(records)} symbols from {CSV_PATH}")
    return records


def ingest_to_db(records: list[dict[str, str]]) -> int:
    """Insert validated records into the universe table via SQLAlchemy.

    Uses session.merge() for idempotent upserts — safe to run multiple times.
    """
    from db.models import Universe
    from db.session import get_session

    inserted = 0
    with get_session() as session:
        for rec in records:
            universe_row = Universe(
                symbol=rec["symbol"],
                name=rec["name"],
                isin=rec["isin"],
                sector=rec["sector"],
                exchange=rec["exchange"],
            )
            session.merge(universe_row)
            inserted += 1

    logger.info(f"Ingested {inserted} symbols into 'universe' table")
    return inserted


if __name__ == "__main__":
    records = validate_and_load_seed()
    print(f"Validated {len(records)} records.")
    if records:
        print(f"Sample: {records[0]}")

    if "--ingest" in sys.argv:
        # Ensure tables exist before ingesting
        from db.models import Base
        from db.session import sync_engine

        Base.metadata.create_all(bind=sync_engine)
        ingest_to_db(records)
        print("Database ingestion complete.")
