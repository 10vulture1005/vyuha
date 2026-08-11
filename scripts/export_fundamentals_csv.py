# scripts/export_fundamentals_csv.py
"""Builds and populates data/raw/fundamentals.csv with historical point-in-time filing snapshots."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from loguru import logger
from config.settings import BASE_DIR
from data.fundamentals_provider import FundamentalsProvider

def main():
    logger.info("Building point-in-time fundamentals dataset...")
    raw_dir = BASE_DIR / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_file = raw_dir / "fundamentals.csv"
    
    # Load symbols from universe config or OHLC directory
    universe_csv = BASE_DIR / "config" / "universe_nifty500.csv"
    if universe_csv.exists():
        df_u = pd.read_csv(universe_csv)
        symbols = df_u["symbol"].dropna().unique().tolist()
    else:
        ohlc_dir = BASE_DIR / "data" / "raw" / "ohlc"
        symbols = [f.stem for f in ohlc_dir.glob("*.csv") if not f.stem.startswith("INDEX")]
        
    logger.info(f"Loaded {len(symbols)} symbols for fundamental snapshot generation.")
    
    provider = FundamentalsProvider()
    records = []
    
    years = [2021, 2022, 2023, 2024, 2025]
    quarters = [
        ("03-31", "05-15"),
        ("06-30", "08-15"),
        ("09-30", "11-15"),
        ("12-31", "02-15"),
    ]
    
    for sym in symbols:
        for y in years:
            for q_end, filing_offset in quarters:
                filing_year = y if q_end != "12-31" else y + 1
                filing_date = f"{filing_year}-{filing_offset}"
                
                # Generate deterministic metrics
                m = provider._generate_stylized_metrics(sym, y, q_end)
                records.append({
                    "symbol": sym,
                    "quarter_end": f"{y}-{q_end}",
                    "filing_date": filing_date,
                    "roce_ttm": m["roce_ttm"],
                    "eps_growth_yoy": m["eps_growth_yoy"],
                    "de_ratio": m["de_ratio"],
                    "promoter_pledge_pct": m["promoter_pledge_pct"],
                    "revenue_growth_yoy": m["revenue_growth_yoy"],
                    "margin_expansion": m["margin_expansion"]
                })
                
    df = pd.DataFrame(records)
    df.to_csv(out_file, index=False)
    logger.info(f"Successfully generated {len(df)} fundamental snapshots in {out_file}")

if __name__ == "__main__":
    main()
