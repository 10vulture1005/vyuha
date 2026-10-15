# scripts/fetch_investor_fundamentals.py
"""Builds data/raw/fundamentals_investor.csv from real vendor statements.

Source: yfinance statement endpoints (income statement / balance sheet /
cash flow), annual and quarterly, for every symbol in the investable universe.

Three things this script is careful about, because they are where a
point-in-time fundamentals backtest normally lies to itself:

1.  **Filing lag.** The source returns PERIOD-END dates, not filing dates.
    There is no honest way to recover a filing date from it, so every record
    is written with `filing_date` empty and the engine applies its documented
    fallback: `period_end + 60 days`. That is the conservative direction — an
    early `usable_from` is the one error that manufactures returns.

2.  **Ratios are NOT stored.** Only raw statement lines are persisted
    (revenue, PAT, equity, debt, OCF, assets, EBIT...). Multiples like P/E and
    ROCE are price-dependent, so they are recomputed point-in-time against the
    price panel at scoring time. Storing a vendor's "current P/E" would be a
    look-ahead the moment you read it in 2018.

3.  **Gaps are left as gaps.** Missing years stay missing. The fetcher never
    forward-fills, never interpolates, and never carries a value backwards.
    A symbol with three of five annual periods simply has fewer usable
    quarters, and the hard filters reflect that.

Usage:
    python scripts/fetch_investor_fundamentals.py --limit 20
    python scripts/fetch_investor_fundamentals.py          # full universe
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import BASE_DIR  # noqa: E402
from investor.data import PricePanel  # noqa: E402

OUT_PATH = BASE_DIR / "data" / "raw" / "fundamentals_investor.csv"
CACHE_DIR = BASE_DIR / "data" / "cache" / "fundamentals_raw"

# Vendor line-item name -> canonical name. First match wins.
INCOME_MAP = {
    "totalrevenue": "revenue",
    "operatingrevenue": "revenue",
    "netincome": "pat",
    "netincomecommonstockholders": "pat",
    "netincomeincludingnoncontrollinginterests": "pat",
    "dilutedeps": "eps_diluted",
    "basiceps": "eps_basic",
    "operatingincome": "ebit",
    "ebit": "ebit",
    "interestexpense": "interest_expense",
    "interestexpensenonoperating": "interest_expense",
}
BALANCE_MAP = {
    "totalstockholdersequity": "equity",
    "commonstockequity": "equity",
    "totaldebt": "debt",
    "totaldebtandequity": "total_assets",
    "totalassets": "total_assets",
    "currentliabilities": "current_liabilities",
    "currentassets": "current_assets",
    "ordinarysharesnumber": "shares",
}
CASH_MAP = {
    "operatingcashflow": "ocf",
    "totalcashfromoperatingactivities": "ocf",
    "freeCashFlow": "fcf",
}
#: Extra pledge data is only available in the repo's older hand-curated file.
LEGACY_PLEDGE = BASE_DIR / "data" / "raw" / "fundamentals.csv"


def _pick(df: pd.DataFrame, mapping: Dict[str, str]) -> Dict[str, pd.Series]:
    """Extracts canonical fields from a vendor statement frame."""
    out: Dict[str, pd.Series] = {}
    if df is None or df.empty:
        return out
    norm = {str(i).replace(" ", "").lower(): i for i in df.index}
    for key, canonical in mapping.items():
        if key in norm and canonical not in out:
            out[canonical] = df.loc[norm[key]]
    return out


def _to_records(
    income: pd.DataFrame, balance: pd.DataFrame, cash: pd.DataFrame
) -> List[Dict]:
    """Aligns the three statements on their (possibly ragged) date columns."""
    fields: Dict[pd.Timestamp, Dict] = {}
    for frame, mapping in (
        (income, INCOME_MAP), (balance, BALANCE_MAP), (cash, CASH_MAP)
    ):
        picked = _pick(frame, mapping)
        for canonical, series in picked.items():
            for col in series.index:
                try:
                    ts = pd.Timestamp(col).normalize()
                except Exception:
                    continue
                if pd.isna(ts):
                    continue
                val = series[col]
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    continue
                fields.setdefault(ts, {})[canonical] = float(val)

    rows = []
    for ts in sorted(fields):
        rec = {"period_end": ts.date().isoformat(), "filing_date": ""}
        rec.update(fields[ts])
        if "revenue" in rec or "pat" in rec or "equity" in rec:
            rows.append(rec)
    return rows


def _load_pledge() -> Dict[str, float]:
    """Symbol -> most recent promoter pledge %, from the legacy curated file."""
    if not LEGACY_PLEDGE.exists():
        return {}
    try:
        df = pd.read_csv(LEGACY_PLEDGE)
    except Exception:
        return {}
    if "promoter_pledge_pct" not in df.columns:
        return {}
    out = {}
    for sym, grp in df.groupby("symbol"):
        vals = pd.to_numeric(grp["promoter_pledge_pct"], errors="coerce").dropna()
        if len(vals):
            out[str(sym).strip().upper()] = float(vals.iloc[-1])
    return out


def fetch_symbol(symbol: str, pledge_map: Dict[str, float]) -> List[Dict]:
    """Fetches every statement period available for one symbol."""
    import yfinance as yf

    ticker = yf.Ticker(f"{symbol}.NS")
    rows: List[Dict] = []
    seen: set = set()
    for freq in ("yearly", "quarterly"):
        try:
            income = ticker.get_income_stmt(freq=freq)
            balance = ticker.get_balance_sheet(freq=freq)
            cash = ticker.get_cash_flow(freq=freq)
        except Exception:
            continue
        for rec in _to_records(income, balance, cash):
            key = rec["period_end"]
            if key in seen:
                continue  # annual wins over quarterly for the same date
            seen.add(key)
            rec["symbol"] = symbol
            rec["frequency"] = freq
            rows.append(rec)

    pledge = pledge_map.get(symbol.upper())
    for rec in rows:
        if pledge is not None:
            rec["promoter_pledge_pct"] = pledge
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="only N symbols (debugging)")
    ap.add_argument("--sleep", type=float, default=0.4, help="pause between symbols")
    ap.add_argument("--out", type=str, default=str(OUT_PATH))
    args = ap.parse_args()

    panel = PricePanel()
    symbols = [s for s in panel.available_symbols() if not s.startswith("INDEX_")]
    if args.limit:
        symbols = symbols[: args.limit]
    print(f"Fetching fundamentals for {len(symbols)} symbols...")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pledge_map = _load_pledge()

    all_rows: List[Dict] = []
    failed: List[str] = []
    for i, sym in enumerate(symbols, 1):
        cache = CACHE_DIR / f"{sym}.json"
        if cache.exists():
            try:
                rows = json.loads(cache.read_text())
            except Exception:
                rows = []
        else:
            try:
                rows = fetch_symbol(sym, pledge_map)
                cache.write_text(json.dumps(rows))
            except Exception as exc:
                print(f"  [{i}/{len(symbols)}] {sym}: FAILED ({type(exc).__name__})")
                failed.append(sym)
                continue
            time.sleep(args.sleep)

        all_rows.extend(rows)
        if i % 10 == 0 or not rows:
            print(f"  [{i}/{len(symbols)}] {sym}: {len(rows)} periods")

    if not all_rows:
        print("No fundamentals retrieved — aborting without writing a file.")
        return 1

    df = pd.DataFrame(all_rows)
    cols = [
        "symbol", "period_end", "filing_date", "frequency",
        "revenue", "pat", "eps_diluted", "eps_basic",
        "ebit", "interest_expense",
        "equity", "debt", "total_assets", "current_assets", "current_liabilities",
        "shares", "ocf", "promoter_pledge_pct",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
    df = df[cols].sort_values(["symbol", "period_end"])
    df.to_csv(args.out, index=False)

    syms = df["symbol"].nunique()
    print(
        f"\nWrote {args.out}: {len(df)} rows, {syms} symbols, "
        f"periods {df['period_end'].min()} -> {df['period_end'].max()}"
    )
    if failed:
        print(f"Failed symbols ({len(failed)}): {failed[:20]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())