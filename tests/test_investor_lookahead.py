# tests/test_investor_lookahead.py
"""Look-ahead protection tests.

These are the tests that matter most in this codebase. Every other kind of
strategy bug costs money slowly; a look-ahead bug produces a beautiful equity
curve that evaporates in production, and it is invisible unless something
actively asserts against it.

The rule under test, from the brief: a quarter's numbers become usable only
from the day AFTER its actual filing date, falling back to
`period_end + 60 days` when no filing date is known.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from investor.data import (
    FundamentalRecord, PointInTimeFundamentals, resolve_usable_from,
)


class TestResolveUsableFrom:
    def test_filing_date_plus_one_day(self):
        """A known filing date makes the quarter usable the very next day."""
        got = resolve_usable_from("2023-12-31", "2024-02-15", filing_lag_days=1,
                                  fallback_days=60)
        assert got == pd.Timestamp("2024-02-16")

    def test_missing_filing_date_uses_conservative_fallback(self):
        """No filing date -> period_end + 60 days, NOT period_end.

        Asserted as a relationship rather than a literal date: 2024 is a leap
        year, so 2023-12-31 + 60d lands on 2024-02-29, and hardcoding that
        would make this test silently wrong in a non-leap window.
        """
        got = resolve_usable_from("2023-12-31", None, filing_lag_days=1,
                                  fallback_days=60)
        assert got == pd.Timestamp("2023-12-31") + pd.Timedelta(days=60)
        assert got == pd.Timestamp("2024-02-29")
        assert got > pd.Timestamp("2024-02-01"), "fallback must be well after period end"

    def test_nat_filing_date_falls_back_rather_than_crashing(self):
        got = resolve_usable_from("2023-12-31", pd.NaT, filing_lag_days=1,
                                  fallback_days=60)
        assert got == pd.Timestamp("2023-12-31") + pd.Timedelta(days=60)

    def test_filing_lag_is_respected_when_larger_than_one(self):
        got = resolve_usable_from("2023-12-31", "2024-02-15", filing_lag_days=45,
                                  fallback_days=60)
        assert got == pd.Timestamp("2024-03-31")

    def test_earlier_filing_date_is_respected_not_clamped(self):
        """A filing date before the period end must not be silently pushed later."""
        got = resolve_usable_from("2023-12-31", "2023-10-15", filing_lag_days=1,
                                  fallback_days=60)
        assert got == pd.Timestamp("2023-10-16")

    def test_fallback_is_never_earlier_than_the_real_filing_lag(self):
        """The pessimistic fallback must not beat a known filing date path."""
        with_filing = resolve_usable_from("2023-12-31", "2024-02-15",
                                         filing_lag_days=1, fallback_days=60)
        without = resolve_usable_from("2023-12-31", None,
                                      filing_lag_days=1, fallback_days=60)
        assert without > with_filing


def _write_csv(tmp_path: Path, rows) -> Path:
    path = tmp_path / "fundamentals_investor.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


class TestPointInTimeStore:
    def _store(self, tmp_path):
        rows = [
            # Filed 2024-02-15 -> usable 2024-02-16
            dict(symbol="AAA", period_end="2023-12-31", filing_date="2024-02-15",
                 frequency="yearly", revenue=100.0, pat=10.0, equity=100.0),
            # No filing date -> usable 2023-12-31 + 60d
            dict(symbol="AAA", period_end="2023-06-30", filing_date="",
                 frequency="yearly", revenue=80.0, pat=8.0, equity=90.0),
            # Filed before the period end (vendor oddity) -> still respected
            dict(symbol="BBB", period_end="2023-12-31", filing_date="2023-11-01",
                 frequency="yearly", revenue=50.0, pat=5.0, equity=60.0),
        ]
        return PointInTimeFundamentals(_write_csv(tmp_path, rows))

    def test_returns_none_before_the_filing_date(self, tmp_path):
        """The newest quarter is invisible on its own filing date.

        On 2024-02-15 the 2023-12-31 numbers are NOT yet usable; the store must
        still fall back to the previous (2023-06-30) quarter rather than
        leaking the newer one.
        """
        store = self._store(tmp_path)
        rec = store.as_of("AAA", "2024-02-15")
        assert rec is not None
        assert rec.period_end == pd.Timestamp("2023-06-30"), \
            "a quarter became visible on its filing date, not the day after"

    def test_symbol_with_no_prior_quarter_returns_none_before_filing(self, tmp_path):
        store = self._store(tmp_path)
        # BBB has exactly one quarter, filed 2023-11-01 -> visible 2023-11-02.
        assert store.as_of("BBB", "2023-10-15") is None

    def test_returns_record_the_day_after_the_filing_date(self, tmp_path):
        store = self._store(tmp_path)
        rec = store.as_of("AAA", "2024-02-16")
        assert rec is not None
        assert rec.period_end == pd.Timestamp("2023-12-31")

    def test_falls_back_to_fileless_quarter_when_the_newer_one_is_unusable(self, tmp_path):
        store = self._store(tmp_path)
        # Before 2024-02-16 the only usable record is the fileless 2023-06-30 one.
        rec = store.as_of("AAA", "2024-01-10")
        assert rec is not None
        assert rec.period_end == pd.Timestamp("2023-06-30")

    def test_earlier_filing_date_makes_record_visible_sooner(self, tmp_path):
        store = self._store(tmp_path)
        assert store.as_of("BBB", "2023-10-31") is None
        assert store.as_of("BBB", "2023-11-02") is not None

    def test_annual_and_quarterly_are_separated(self, tmp_path):
        rows = [
            dict(symbol="CCC", period_end="2023-03-31", filing_date="2023-05-15",
                 frequency="yearly", revenue=10.0, pat=1.0, equity=10.0),
            dict(symbol="CCC", period_end="2023-06-30", filing_date="2023-08-15",
                 frequency="quarterly", revenue=3.0, pat=0.3, equity=11.0),
        ]
        store = PointInTimeFundamentals(_write_csv(tmp_path, rows))
        as_of = "2023-09-01"
        assert store.latest_annual("CCC", as_of).period_end == pd.Timestamp("2023-03-31")
        assert store.latest_quarterly("CCC", as_of).period_end == pd.Timestamp("2023-06-30")
        assert len(store.annual("CCC", as_of)) == 1
        assert len(store.quarterly("CCC", as_of)) == 1

    def test_panel_omits_symbols_with_nothing_usable_yet(self, tmp_path):
        rows = [
            dict(symbol="DDD", period_end="2024-12-31", filing_date="2025-03-01",
                 frequency="yearly", revenue=1.0, pat=0.1, equity=1.0),
        ]
        store = PointInTimeFundamentals(_write_csv(tmp_path, rows))
        assert store.panel("2024-01-01").empty
        assert "DDD" in store.panel("2025-03-02").index

    def test_unknown_symbol_returns_none(self, tmp_path):
        store = self._store(tmp_path)
        assert store.as_of("NOPE", "2024-06-01") is None

    def test_as_of_is_monotonic_over_time(self, tmp_path):
        """Once a quarter is visible it must never disappear."""
        store = self._store(tmp_path)
        seen = []
        for day in pd.date_range("2024-01-01", "2024-06-01", freq="W"):
            rec = store.as_of("AAA", day)
            seen.append(rec.period_end if rec else None)
        visible = [s for s in seen if s is not None]
        assert visible == sorted(visible), "a previously visible quarter vanished"


class TestNoLookaheadInValuation:
    """Valuation multiples must come from the price panel, not the store.

    A stored 'current P/E' column read in 2018 is the single most common way a
    fundamentals backtest manufactures cheapness.
    """

    def test_metrics_take_price_from_the_as_of_date(self, tmp_path):
        from investor.data import PricePanel
        from investor.fundamentals import compute_metrics

        rows = [dict(symbol="EEE", period_end="2023-12-31", filing_date="2024-02-15",
                     frequency="yearly", revenue=100.0, pat=10.0, equity=200.0,
                     eps_diluted=10.0, shares=10.0)]
        store = PointInTimeFundamentals(_write_csv(tmp_path, rows))

        import numpy as np

        idx = pd.bdate_range("2023-06-01", "2024-06-28")
        # Price doubles over the period; P/E must halve, not stay pinned.
        px = pd.Series(np.linspace(100, 200, len(idx)), index=idx)
        panel = PricePanel.__new__(PricePanel)
        panel.dir = tmp_path / "ohlc"
        panel.dir.mkdir()
        frame = pd.DataFrame({"Open": px, "High": px, "Low": px, "Close": px,
                              "Volume": 1000.0}, index=idx)
        frame.index.name = "Date"
        frame.to_csv(panel.dir / "EEE.csv")
        panel._frames = {}
        panel._load_failures = set()

        early = compute_metrics(store, panel, "EEE", "2024-03-01")
        late = compute_metrics(store, panel, "EEE", "2024-06-01")
        assert early.pe is not None and late.pe is not None
        assert late.pe > early.pe, "P/E ignored the later (higher) price"