# investor/data.py
"""Point-in-time data access for Investor Mode.

Three jobs:

1. **Prices** — adjusted OHLCV panels keyed by symbol, sliced strictly
   ``loc[:as_of]`` so no caller can accidentally read tomorrow's bar.
2. **Universe + sectors** — reuses `universe.computed_point_in_time.PointInTimeUniverse`
   (the same PIT resolver the swing engine uses) and layers on a real sector
   map read from `config/universe_*.csv`.
3. **Fundamentals** — a filing-lag-aware store. A quarter's numbers become
   usable only from ``filing_date + 1`` day (fallback ``period_end + 60``).
   This module is the *only* place that decision is made, so the look-ahead
   guarantee is testable in one place.

Price history in this repo comes from `scripts/ingest_ohlc.py`, which uses
`yfinance(auto_adjust=True)` — i.e. the series is split/bonus/dividend
adjusted. That is what makes benchmark CAGRs comparable to a TRI series.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import BASE_DIR
from investor.config import investor_config

OHLC_DIR = BASE_DIR / "data" / "raw" / "ohlc"
PRICE_COLS = ["Open", "High", "Low", "Close", "Volume"]

#: Raw statement lines persisted by scripts/fetch_investor_fundamentals.py.
#: RATIOS ARE DELIBERATELY NOT STORED: P/E, ROCE and friends are price- or
#: period-dependent, so a stored multiple read in 2018 would be a look-ahead.
#: They are recomputed point-in-time in investor/fundamentals.py.
FUNDAMENTAL_FIELDS: List[str] = [
    # income statement
    "revenue", "pat", "eps_diluted", "eps_basic", "ebit", "interest_expense",
    # balance sheet
    "equity", "debt", "total_assets", "current_assets", "current_liabilities",
    "shares",
    # cash flow
    "ocf",
    # governance
    "promoter_pledge_pct",
]

#: Alternative column spellings seen in vendor exports -> canonical name.
_FIELD_ALIASES: Dict[str, str] = {
    "totalrevenue": "revenue", "operatingrevenue": "revenue", "sales": "revenue",
    "netincome": "pat", "netincomecommonstockholders": "pat",
    "dilutedeps": "eps_diluted", "basiceps": "eps_basic",
    "operatingincome": "ebit", "ebit": "ebit",
    "interestexpense": "interest_expense",
    "totalstockholdersequity": "equity", "commonstockequity": "equity",
    "totaldebt": "debt", "totalassets": "total_assets",
    "currentliabilities": "current_liabilities", "currentassets": "current_assets",
    "ordinarysharesnumber": "shares",
    "operatingcashflow": "ocf", "totalcashfromoperatingactivities": "ocf",
    "pledge_pct": "promoter_pledge_pct", "promoter_pledge": "promoter_pledge_pct",
    # legacy curated file spellings
    "roce_ttm": "roce", "de_ratio": "debt_to_equity",
}


# ─────────────────────────────────────────────────────────────────────────────
# Prices
# ─────────────────────────────────────────────────────────────────────────────

class PricePanel:
    """In-memory OHLCV store with point-in-time slicing.

    A panel is built once per run and handed to every layer, so indicator
    computation is consistent between the live engine and the backtest.
    """

    def __init__(self, ohlc_dir: Optional[Path] = None):
        self.dir = Path(ohlc_dir) if ohlc_dir else OHLC_DIR
        self._frames: Dict[str, pd.DataFrame] = {}
        self._load_failures: Set[str] = set()

    # ── loading ─────────────────────────────────────────────────────────────
    def available_symbols(self) -> List[str]:
        if not self.dir.exists():
            return []
        return sorted(
            p.stem for p in self.dir.glob("*.csv")
            if p.stem and not p.name.startswith(".")
        )

    def load(self, symbol: str) -> pd.DataFrame:
        """Returns the full adjusted OHLCV frame for a symbol (empty on failure)."""
        if symbol in self._frames:
            return self._frames[symbol]
        if symbol in self._load_failures:
            return pd.DataFrame()

        path = self.dir / f"{symbol}.csv"
        if not path.exists():
            self._load_failures.add(symbol)
            return pd.DataFrame()

        try:
            df = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
            df = df.rename(columns={c: c.capitalize() for c in df.columns})
            df = df[[c for c in PRICE_COLS if c in df.columns]].copy()
            df.index = pd.to_datetime(df.index, errors="coerce")
            # The OHLC archive is mixed: 150 of the CSVs carry a "+05:30"
            # offset in the Date column, the rest (and every INDEX_* file)
            # do not. Left alone, those symbols get tz-aware indexes while the
            # benchmark stays naive, and any cross-symbol alignment in
            # investor/indicators.py raises "Cannot join tz-naive with
            # tz-aware DatetimeIndex" — or, worse, silently intersects to
            # empty and turns relative strength into all-NaN. Daily bars
            # carry no meaningful time-of-day and every consumer already
            # passes naive timestamps, so normalise to naive at the source.
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df = df[~df.index.isna()].sort_index()
            df = df.dropna(subset=["Close"])
            # A single bad print (Volume 0, Close 0) poisons every downstream
            # return and ATR; drop those rows rather than the symbol.
            df = df[(df["Close"] > 0) & (df["High"] >= df["Low"])]
            self._frames[symbol] = df
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Failed to load OHLC for {symbol}: {exc}")
            self._load_failures.add(symbol)
            return pd.DataFrame()
        return self._frames[symbol]

    def load_many(self, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = self.load(sym)
            if not df.empty:
                out[sym] = df
        return out

    # ── access ──────────────────────────────────────────────────────────────
    def get(self, symbol: str, as_of: pd.Timestamp | date | str | None = None) -> pd.DataFrame:
        """History up to and including `as_of` (default: everything).

        Every indicator in `investor/` is built from this, which is how the
        "signal on close of t, fill at open of t+1" contract is enforced: a
        caller holding a `PricePanel.get(sym, t)` slice physically cannot see
        day t+1.
        """
        df = self.load(symbol)
        if df.empty:
            return df
        if as_of is None:
            return df
        return df.loc[:pd.Timestamp(as_of)]

    def close(self, symbol: str, as_of) -> Optional[float]:
        df = self.get(symbol, as_of)
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])

    def bar(self, symbol: str, as_of) -> Optional[pd.Series]:
        df = self.get(symbol, as_of)
        if df.empty:
            return None
        return df.iloc[-1]

    def first_trading_day_on_or_after(self, symbol: str, when) -> Optional[pd.Timestamp]:
        df = self.load(symbol)
        if df.empty:
            return None
        ts = pd.Timestamp(when)
        later = df.index[df.index >= ts]
        return later[0] if len(later) else None

    def is_tradable(self, symbol: str, as_of) -> bool:
        """True if the symbol has a bar dated exactly `as_of`."""
        df = self.get(symbol, as_of)
        return (not df.empty) and (df.index[-1] == pd.Timestamp(as_of))

    def trading_days(self, symbols: Iterable[str]) -> pd.DatetimeIndex:
        """Union calendar across the given symbols."""
        days: Set[pd.Timestamp] = set()
        for sym in symbols:
            df = self.load(sym)
            if not df.empty:
                days.update(df.index)
        return pd.DatetimeIndex(sorted(days))


# ─────────────────────────────────────────────────────────────────────────────
# Sectors
# ─────────────────────────────────────────────────────────────────────────────

#: Substring test — "Financial Services" and "Financials" both map to FINANCIAL,
#: which is the only sector with its own hard-filter set.
_FINANCIAL_PATTERNS = ("financ", "bank", "nbfc", "insurance", "asset manage")


def _normalize_sector(raw: str) -> str:
    if not raw:
        return "UNKNOWN"
    s = str(raw).strip().upper()
    if any(p in s.lower() for p in _FINANCIAL_PATTERNS):
        return "FINANCIAL"
    return s.replace(" ", "_").replace("&", "AND")


class SectorMap:
    """Symbol -> sector, built from the repo's universe CSVs.

    `backtest_v2.data.sector_taxonomy.DEFAULT_SECTOR_MAPPING` is a hardcoded
    snapshot that covers most large caps and almost none of the MidCap 150.
    For a 20-30 holding portfolio the 25% sector cap is meaningless if half the
    book reads UNKNOWN, so this prefers the CSVs and only falls back there.
    """

    def __init__(self, csv_paths: Optional[Iterable[Path]] = None):
        self._map: Dict[str, str] = {}
        paths = list(csv_paths) if csv_paths is not None else [
            BASE_DIR / "config" / "universe_midcap150.csv",
            BASE_DIR / "config" / "universe_nifty500.csv",
        ]
        for path in paths:
            self._load_csv(path)
        self._load_fallback()

    def _load_csv(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            df = pd.read_csv(path)
            if {"symbol", "sector"} <= set(df.columns):
                for sym, sec in zip(df["symbol"], df["sector"]):
                    self._map[str(sym).strip().upper()] = _normalize_sector(sec)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Sector map could not read {path}: {exc}")

    def _load_fallback(self) -> None:
        try:
            from backtest_v2.data.sector_taxonomy import DEFAULT_SECTOR_MAPPING

            for sym, sec in DEFAULT_SECTOR_MAPPING.items():
                self._map.setdefault(sym.upper(), _normalize_sector(sec))
        except Exception:
            pass

    def get(self, symbol: str) -> str:
        return self._map.get(symbol.upper(), "UNKNOWN")

    def is_financial(self, symbol: str) -> bool:
        return self.get(symbol) == "FINANCIAL"

    def __contains__(self, symbol: str) -> bool:
        return symbol.upper() in self._map

    def items(self):
        return self._map.items()

    def __len__(self) -> int:
        return len(self._map)


# ─────────────────────────────────────────────────────────────────────────────
# Point-in-time fundamentals
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FundamentalRecord:
    """One statement period for one symbol, with its usability date."""

    symbol: str
    period_end: pd.Timestamp
    filing_date: Optional[pd.Timestamp]
    usable_from: pd.Timestamp
    frequency: str = "yearly"      # yearly (fiscal) | quarterly (calendar)
    values: Dict[str, float] = field(default_factory=dict)

    def get(self, key: str, default: Optional[float] = None) -> Optional[float]:
        v = self.values.get(key, default)
        if v is None:
            return default
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return default if np.isnan(f) else f

    def has(self, key: str) -> bool:
        return self.get(key) is not None


def resolve_usable_from(
    period_end: pd.Timestamp | date | str,
    filing_date: Optional[pd.Timestamp | date | str] = None,
    filing_lag_days: Optional[int] = None,
    fallback_days: Optional[int] = None,
) -> pd.Timestamp:
    """The first date this quarter's numbers may legally be used.

    Rule (from the brief): a quarter becomes usable the day AFTER its actual
    filing date; if no filing date is known, assume the statutory worst case of
    ``period_end + 60 days``. Filing dates are therefore never assumed early —
    the fallback is deliberately pessimistic because a too-early fallback is
    the one error that silently manufactures returns.
    """
    cfg = investor_config.fundamentals
    lag = int(filing_lag_days if filing_lag_days is not None else cfg.get("filing_lag_days", 1))
    fb = int(fallback_days if fallback_days is not None else cfg.get("fallback_period_end_days", 60))

    pe = pd.Timestamp(period_end).normalize()
    if filing_date is not None:
        fd = pd.Timestamp(filing_date)
        if pd.isna(fd):
            # NaT means "no filing date known" — take the pessimistic fallback.
            return pe + pd.Timedelta(days=fb)
        return fd.normalize() + pd.Timedelta(days=lag)
    return pe + pd.Timedelta(days=fb)


class PointInTimeFundamentals:
    """Filing-lag-aware fundamental store.

    `as_of(symbol, when)` returns the newest quarter whose `usable_from` is
    <= `when`. There is no code path anywhere in Investor Mode that reads a
    row without going through here, so the look-ahead guarantee is enforced by
    construction rather than by convention.
    """

    def __init__(self, csv_path: Optional[Path] = None):
        cfg_path = csv_path or (BASE_DIR / "data" / "raw" / "fundamentals_investor.csv")
        self.path = Path(cfg_path)
        self.records: Dict[str, List[FundamentalRecord]] = {}
        self.loaded = False
        self.source = "none"
        if self.path.exists():
            self._load(self.path)

    # ── loading ─────────────────────────────────────────────────────────────
    def _load(self, path: Path) -> None:
        try:
            df = pd.read_csv(path)
        except Exception as exc:
            logger.warning(f"Investor fundamentals unreadable at {path}: {exc}")
            return
        if df.empty:
            return

        df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
        # Map vendor spellings onto canonical names.
        renamed = {}
        for col in df.columns:
            canon = _FIELD_ALIASES.get(col, col)
            if canon not in renamed.values():
                renamed[col] = canon
        df = df.rename(columns=renamed)

        if "symbol" not in df.columns:
            logger.warning("Investor fundamentals file has no 'symbol' column; ignoring.")
            return
        if "period_end" not in df.columns:
            logger.warning("Investor fundamentals file has no 'period_end' column; ignoring.")
            return

        df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce")
        df["filing_date"] = pd.to_datetime(df.get("filing_date"), errors="coerce")
        df = df[df["period_end"].notna()]
        df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
        if "frequency" not in df.columns:
            df["frequency"] = "yearly"
        df["frequency"] = df["frequency"].astype(str).str.strip().str.lower().replace(
            {"annual": "yearly", "quarterly": "quarterly", "a": "yearly", "q": "quarterly"}
        )

        value_cols = [c for c in FUNDAMENTAL_FIELDS if c in df.columns]
        for _, row in df.iterrows():
            sym = row["symbol"]
            filing = row.get("filing_date")
            rec = FundamentalRecord(
                symbol=sym,
                period_end=row["period_end"],
                filing_date=None if pd.isna(filing) else filing,
                usable_from=resolve_usable_from(row["period_end"], filing),
                frequency=row["frequency"],
                values={c: row.get(c) for c in value_cols},
            )
            self.records.setdefault(sym, []).append(rec)

        for sym in self.records:
            self.records[sym].sort(key=lambda r: r.usable_from)
        self.loaded = True
        self.source = str(path)
        logger.info(
            f"Investor fundamentals: {len(self.records)} symbols, "
            f"{sum(len(v) for v in self.records.values())} quarters from {self.source}"
        )

    # ── queries ─────────────────────────────────────────────────────────────
    def symbols(self) -> List[str]:
        return sorted(self.records.keys())

    def as_of(self, symbol: str, when) -> Optional[FundamentalRecord]:
        """Newest record usable on `when`, or None."""
        recs = self.records.get(symbol.upper())
        if not recs:
            return None
        ts = pd.Timestamp(when)
        best: Optional[FundamentalRecord] = None
        for rec in recs:
            if rec.usable_from <= ts:
                best = rec
            else:
                break  # sorted by usable_from
        return best

    def history(self, symbol: str) -> List[FundamentalRecord]:
        return self.records.get(symbol.upper(), [])

    def annual(self, symbol: str, as_of) -> List[FundamentalRecord]:
        """Fiscal-year records usable at `as_of`, oldest first.

        Annual (fiscal-year) periods are the right grain for 3-year CAGRs,
        3-year average OCF/PAT and the "no loss-making year" rule — Indian
        companies report on a March fiscal year, so calendar-year aggregation
        silently mixes half-years from two different fiscal years.
        """
        ts = pd.Timestamp(as_of)
        recs = [r for r in self.history(symbol)
                if r.usable_from <= ts and r.frequency == "yearly"]
        return sorted(recs, key=lambda r: r.period_end)

    def quarterly(self, symbol: str, as_of) -> List[FundamentalRecord]:
        """Calendar-quarter records usable at `as_of`, oldest first.

        Used for the YoY acceleration input (last two quarters) and for a
        fresher EPS than the last full fiscal year.
        """
        ts = pd.Timestamp(as_of)
        recs = [r for r in self.history(symbol)
                if r.usable_from <= ts and r.frequency == "quarterly"]
        return sorted(recs, key=lambda r: r.period_end)

    def latest_annual(self, symbol: str, as_of) -> Optional[FundamentalRecord]:
        recs = self.annual(symbol, as_of)
        return recs[-1] if recs else None

    def latest_quarterly(self, symbol: str, as_of) -> Optional[FundamentalRecord]:
        recs = self.quarterly(symbol, as_of)
        return recs[-1] if recs else None

    def panel(self, when, symbols: Optional[Iterable[str]] = None) -> pd.DataFrame:
        """Wide frame of everything knowable at `when`, indexed by symbol.

        This is the cross-section the scoring layer ranks. Symbols with no
        usable record at `when` are simply absent — they cannot be scored, and
        an unscorable stock must never be treated as a pass.
        """
        syms = list(symbols) if symbols is not None else self.symbols()
        rows: Dict[str, Dict[str, float]] = {}
        for sym in syms:
            rec = self.as_of(sym, when)
            if rec is None:
                continue
            row = dict(rec.values)
            row["_period_end"] = rec.period_end
            row["_filing_date"] = rec.filing_date
            row["_usable_from"] = rec.usable_from
            rows[sym.upper()] = row
        if not rows:
            return pd.DataFrame(columns=FUNDAMENTAL_FIELDS)
        df = pd.DataFrame.from_dict(rows, orient="index")
        df.index.name = "symbol"
        return df

    def coverage(self, when) -> Dict[str, int]:
        """How many symbols have usable fundamentals at `when` (for logging)."""
        return {
            "symbols_known": len(self.panel(when)),
            "symbols_total": len(self.records),
        }


@lru_cache(maxsize=1)
def get_sector_map() -> SectorMap:
    return SectorMap()


@lru_cache(maxsize=1)
def build_default_price_panel() -> PricePanel:
    return PricePanel()


__all__ = [
    "PricePanel",
    "SectorMap",
    "FundamentalRecord",
    "PointInTimeFundamentals",
    "resolve_usable_from",
    "get_sector_map",
    "build_default_price_panel",
    "FUNDAMENTAL_FIELDS",
    "PRICE_COLS",
]