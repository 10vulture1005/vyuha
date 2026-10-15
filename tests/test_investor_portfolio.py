# tests/test_investor_portfolio.py
"""Position sizing, concentration caps and rebalance-cadence tests.

The cap tests use adversarial inputs on purpose: a single very low-volatility
name, one enormous sector, and a dust-heavy book. Those are the cases where a
cap implementation quietly fails to bind.
"""
from __future__ import annotations

import pandas as pd
import pytest

from investor import portfolio as pf
from investor.data import SectorMap
from investor.config import investor_config


CFG = investor_config.portfolio
MAX_POS = float(CFG["max_position_pct"]) / 100.0
MAX_SEC = float(CFG["max_sector_pct"]) / 100.0
MIN_POS = float(CFG["min_position_pct"]) / 100.0


class TestInverseAtrWeights:
    def test_weights_sum_to_one(self):
        raw = pf.inverse_atr_vol_weights(
            ["A", "B", "C"],
            {"A": 10.0, "B": 20.0, "C": 30.0},
            {"A": 100.0, "B": 200.0, "C": 300.0},
        )
        assert abs(sum(raw.values()) - 1.0) < 1e-9

    def test_uses_relative_volatility_not_raw_atr(self):
        """ATR must be normalised by price.

        A Rs 100 stock with Rs 10 ATR is 10% volatile; a Rs 1,000 stock with
        the same Rs 10 ATR is 1%. Weighting on raw ATR would hand the cheap
        stock 10x the allocation.
        """
        raw = pf.inverse_atr_vol_weights(
            ["CHEAP", "DEAR"],
            {"CHEAP": 10.0, "DEAR": 10.0},
            {"CHEAP": 100.0, "DEAR": 1000.0},
        )
        assert raw["DEAR"] > raw["CHEAP"]

    def test_lower_volatility_gets_more_weight(self):
        raw = pf.inverse_atr_vol_weights(
            ["LOWVOL", "HIGHVOL"],
            {"LOWVOL": 5.0, "HIGHVOL": 20.0},
            {"LOWVOL": 100.0, "HIGHVOL": 100.0},
        )
        assert raw["LOWVOL"] > raw["HIGHVOL"]

    def test_missing_or_zero_atr_names_are_dropped(self):
        raw = pf.inverse_atr_vol_weights(
            ["A", "B", "C"],
            {"A": 10.0, "B": None, "C": 0.0},
            {"A": 100.0, "B": 100.0, "C": 100.0},
        )
        assert set(raw) == {"A"}

    def test_all_invalid_returns_empty(self):
        assert pf.inverse_atr_vol_weights(["A"], {"A": None}, {"A": 100.0}) == {}


class TestCaps:
    def _uniform_sectors(self, n):
        return {f"S{i}": f"SEC_{i}" for i in range(n)}

    def test_single_name_cap_binds(self):
        """One dominant low-vol name must be capped at 8%, not ride at 60%.

        The five 1% names fall below the 2% minimum position and are dropped,
        which leaves a one-name book. A one-name book cannot satisfy an 8%
        name cap at 100% weight, so the cap wins and the residue stays in
        cash — renormalising back to 100% is exactly the bug this guards.
        """
        sectors = {"GOLDMINE": "SPECIAL"}
        raw = {"GOLDMINE": 0.95, "B": 0.01, "C": 0.01, "D": 0.01, "E": 0.01, "F": 0.01}
        _, w = pf.apply_caps(raw, sectors)
        assert w["GOLDMINE"] <= MAX_POS + 1e-6
        assert abs(sum(w.values()) - MAX_POS) < 1e-6, \
            "the un-holdable remainder must stay in cash, not be renormalised in"

    def test_cap_holds_with_a_single_sector_book(self):
        """Every name in one sector: the 25% sector cap cannot be filled.

        With an 8% single-name cap, at most three positions fit inside a 25%
        sector cap. The correct behaviour is to honour BOTH caps and leave the
        remainder unallocated (cash) — not to renormalise back to 100% and blow
        through the name cap, and not to return nothing and strand the book.
        """
        raw = {f"S{i}": 0.2 for i in range(8)}
        sectors = {f"S{i}": "ONLY_SECTOR" for i in range(8)}
        _, w = pf.apply_caps(raw, sectors)
        assert w
        assert all(v <= MAX_POS + 1e-6 for v in w.values())
        assert sum(w.values()) <= MAX_SEC + 1e-6, \
            "sector cap violated when the book is too small to fill it"
        assert sum(w.values()) < 1.0, \
            "a book that cannot satisfy its own caps must leave residue in cash"

    def test_sector_cap_binds_when_feasible(self):
        sectors = {}
        raw = {}
        # 6 names in BANKING (60% raw) and 6 spread over other sectors.
        for i in range(6):
            sym = f"BANK{i}"
            sectors[sym] = "FINANCIAL"
            raw[sym] = 0.10
        for i in range(6):
            sym = f"OTHER{i}"
            sectors[sym] = f"SEC_{i}"
            raw[sym] = 0.10 / 6
        _, w = pf.apply_caps(raw, sectors)
        fin = sum(v for k, v in w.items() if sectors[k] == "FINANCIAL")
        assert fin <= MAX_SEC + 1e-6

    def test_name_and_sector_caps_together(self):
        """Regression: the sector cap used to run after the name cap and push an
        already-capped name back over its limit."""
        sectors = {"A": "X", "B": "X", "C": "X", "D": "X",
                   "E": "Y", "F": "Y", "G": "Z", "H": "W"}
        raw = {"A": 0.30, "B": 0.10, "C": 0.10, "D": 0.10,
               "E": 0.15, "F": 0.10, "G": 0.08, "H": 0.07}
        _, w = pf.apply_caps(raw, sectors)
        assert all(v <= MAX_POS + 1e-6 for v in w.values()), \
            f"single-name cap violated: {w}"
        for sec in {"X", "Y", "Z", "W"}:
            exposure = sum(v for k, v in w.items() if sectors[k] == sec)
            assert exposure <= MAX_SEC + 1e-6, f"sector {sec} at {exposure}"
        # 8 names x an 8% name cap caps total deployable weight at 64%. The
        # allocator must NOT renormalise past that to hit 100%.
        assert sum(w.values()) <= 8 * MAX_POS + 1e-6
        assert sum(w.values()) > 0.5, "caps should still deploy most of the book"

    def test_dust_positions_are_dropped(self):
        raw = {"BIG": 0.97, **{f"T{i}": 0.003 for i in range(10)}}
        sectors = {"BIG": "S", **{f"T{i}": f"S{i}" for i in range(10)}}
        _, w = pf.apply_caps(raw, sectors)
        assert "T0" not in w
        assert all(v >= MIN_POS - 1e-9 for v in w.values())

    def test_empty_input_returns_empty(self):
        assert pf.apply_caps({}, {}) == ([], {})
        assert pf.apply_caps({"A": 0.0}, {"A": "S"}) == ([], {})

    def test_all_weights_positive_after_capping(self):
        raw = {"A": 0.5, "B": 0.2, "C": 0.15, "D": 0.1, "E": 0.05}
        sectors = {k: f"S{i}" for i, k in enumerate(raw)}
        _, w = pf.apply_caps(raw, sectors)
        assert all(v > 0 for v in w.values())


class TestBuildTargets:
    def test_respects_the_holdings_band(self):
        sectors = SectorMap()
        prices = {f"S{i}": 100.0 for i in range(40)}
        atrs = {f"S{i}": 5.0 for i in range(40)}
        scores = {f"S{i}": float(i) for i in range(40)}
        targets = pf.build_targets(
            list(scores), atrs, prices, sectors, scores,
            min_holdings=20, max_holdings=30,
        )
        assert len(targets) <= 30

    def test_pads_toward_the_floor_when_candidates_allow(self):
        sectors = SectorMap()
        prices = {f"S{i}": 100.0 for i in range(25)}
        atrs = {f"S{i}": 5.0 for i in range(25)}
        scores = {f"S{i}": float(i) for i in range(25)}
        targets = pf.build_targets(
            list(scores), atrs, prices, sectors, scores,
            min_holdings=20, max_holdings=30,
        )
        assert len(targets) >= 20

    def test_names_without_prices_are_excluded(self):
        sectors = SectorMap()
        prices = {"REAL": 100.0, "GHOST": 0.0}
        atrs = {"REAL": 5.0, "GHOST": 5.0}
        targets = pf.build_targets(["REAL", "GHOST"], atrs, prices, sectors, {})
        assert "GHOST" not in targets


class TestRebalanceCadence:
    def test_first_ever_day_is_a_rebalance(self):
        assert pf.is_rebalance_day(pd.Timestamp("2024-01-02"), None) is True

    def test_first_trading_day_of_new_month_is_a_rebalance(self):
        # Previous trading day was 31 Jan, so 1 Feb IS the month's first day.
        prev = pd.Timestamp("2024-01-31")
        assert pf.is_rebalance_day(pd.Timestamp("2024-02-01"), prev) is True
        # With 1 Feb already seen, 2 Feb is mid-month.
        assert pf.is_rebalance_day(pd.Timestamp("2024-02-02"),
                                   pd.Timestamp("2024-02-01")) is False

    def test_mid_month_is_not_a_rebalance(self):
        prev = pd.Timestamp("2024-03-04")
        assert pf.is_rebalance_day(pd.Timestamp("2024-03-15"), prev) is False

    def test_month_rollover_over_a_holiday_still_triggers(self):
        """1 Jan can be a holiday; the 2nd is still 'the first trading day'."""
        prev = pd.Timestamp("2023-12-29")
        assert pf.is_rebalance_day(pd.Timestamp("2024-01-02"), prev) is True

    def test_day_other_than_the_first_is_not_a_rebalance(self):
        # day_of_month default is 1, so the 31st of a running month must not
        # trigger, even though `day >= dom` is true.
        assert pf.is_rebalance_day(pd.Timestamp("2024-01-31"),
                                   pd.Timestamp("2024-01-30")) is False


class TestMinimumHold:
    def test_blocks_discretionary_sales_inside_the_window(self):
        need = int(CFG["min_hold_days"])
        assert pf.min_hold_satisfied(need - 1, forced=False) is False
        assert pf.min_hold_satisfied(need, forced=False) is True

    def test_forced_override_only_applies_when_set(self):
        """Stops and hard-negative vetoes are the only two overrides."""
        assert pf.min_hold_satisfied(0, forced=True) is True


class TestOverweightTrigger:
    def test_flags_only_above_the_threshold(self):
        out = pf.overweighted(
            {"A": 0.12, "B": 0.07, "C": 0.04},
            {"A": 0.05, "B": 0.05, "C": 0.05},
            threshold=1.5,
        )
        flagged = {s for s, _, _ in out}
        assert "A" in flagged      # 0.12 > 1.5 x 0.05
        assert "B" not in flagged  # 0.07 < 0.075
        assert "C" not in flagged

    def test_names_dropped_from_targets_are_flagged_for_removal(self):
        out = pf.overweighted({"GONE": 0.05}, {"OTHER": 0.05}, threshold=1.5)
        assert out == [("GONE", 0.05, 0.0)]


class TestSipDeployment:
    def test_goes_to_the_most_underweight_eligible_name(self):
        targets = {"A": 0.10, "B": 0.05}
        current = {"A": 0.10, "B": 0.00}
        sym, _ = pf.sip_deployment(targets, current, {}, ["A", "B"])
        assert sym == "B"

    def test_prefers_the_larger_size_weighted_gap(self):
        """A 5% position 5% underweight is a bigger hole than a 10% position
        1% underweight; the size-weighted gap gets that right."""
        targets = {"BIG": 0.10, "SMALL": 0.05}
        current = {"BIG": 0.09, "SMALL": 0.00}
        sym, _ = pf.sip_deployment(targets, current, {}, ["BIG", "SMALL"])
        assert sym == "SMALL"

    def test_falls_back_to_the_defensive_sleeve(self):
        sym, _ = pf.sip_deployment({"A": 0.05}, {"A": 0.05},
                                   {"LIQUIDBEES": 0.6, "GOLDBEES": 0.15}, ["A"])
        assert sym == "LIQUIDBEES"

    def test_falls_back_to_cash_when_nothing_qualifies(self):
        sym, _ = pf.sip_deployment({}, {}, {}, [])
        assert sym == "CASH"


class TestSectorExposure:
    def test_aggregates_by_sector(self):
        sectors = SectorMap()
        w = {"A": 0.05, "B": 0.05, "C": 0.05}
        out = pf.sector_exposure(w, sectors)
        assert abs(sum(out.values()) - 0.15) < 1e-9