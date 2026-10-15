# tests/test_investor_pipeline.py
"""End-to-end pipeline tests: the t -> t+1 execution contract and exit rules.

These use synthetic price series so the assertions are about BEHAVIOUR
(fills never happen on the signal bar, stops ratchet, the 60-day floor binds)
rather than about any particular historical result.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest_investor.engine import InvestorBacktester
from investor.config import investor_config
from investor.data import PricePanel, SectorMap
from investor.pipeline import InvestorEngine, Position
from investor.indicators import IndicatorCache
from investor.technical import ChandelierTracker


def _price_panel(tmp_path: Path, series: dict, open_ratio: float = 0.995) -> PricePanel:
    """Writes OHLC CSVs and returns a PricePanel pointed at them.

    `open_ratio` makes the OPEN differ from the CLOSE within each bar, so a
    test can tell an open-based fill from a close-based one. A fixture with
    Open == Close cannot distinguish the two at all.
    """
    panel = PricePanel(tmp_path)
    panel._frames = {}
    panel._load_failures = set()
    for sym, closes in series.items():
        idx = pd.bdate_range("2023-01-02", periods=len(closes))
        px = pd.Series(closes, index=idx, dtype="float64")
        frame = pd.DataFrame({
            "Open": px * open_ratio, "High": px * 1.01,
            "Low": px * (open_ratio - 0.005),
            "Close": px, "Volume": 1_000_000.0,
        }, index=idx)
        frame.index.name = "Date"
        frame.to_csv(tmp_path / f"{sym}.csv")
    return panel


class _EmptyStore:
    """Fundamentals stand-in that reports nothing knowable."""

    def __init__(self):
        self.records = {}
        self.loaded = False
        self.source = "test"

    def symbols(self):
        return []

    def as_of(self, symbol, when):
        return None

    def annual(self, symbol, when):
        return []

    def quarterly(self, symbol, when):
        return []


def _bare_engine() -> InvestorEngine:
    """An engine with only the state the fill helpers touch.

    Used for unit-testing position accounting without standing up a price
    panel; `evaluate_day` is not called on these.
    """
    engine = InvestorEngine.__new__(InvestorEngine)
    engine.positions = {}
    engine.sectors = SectorMap()
    engine.cache = None
    return engine


class TestExecutionContract:
    def test_orders_decided_on_t_fill_at_the_open_of_t_plus_1(self, tmp_path):
        """The single most important invariant in the whole engine.

        `evaluate_day` produces orders priced at day t's close. `_execute` must
        fill them at day t+1's OPEN. The two prices are deliberately different
        here (a +8% overnight gap) so a fill at the wrong price is unmissable.
        """
        # 300 flat bars (enough for a 200DMA) then a +8% overnight gap, so the
        # fill price and the signal price are unmistakably different.
        closes = [100.0] * 300 + [108.0] * 10
        panel = _price_panel(tmp_path, {"AAA": closes})
        sectors = SectorMap()

        bt = InvestorBacktester(
            start=panel.get("AAA").index[305].date().isoformat(),
            end=panel.get("AAA").index[-1].date().isoformat(),
            universe=["AAA"],
            panel=panel, store=_EmptyStore(), sector_map=sectors,
            use_fundamentals=False, use_technical=False, label="t",
        )
        # Pin the regime so exposure is 100% and the book actually opens
        # regardless of what the synthetic index series implies.
        bt.engine.regime_engine.hysteresis.reset("RISK_ON")
        res = bt.run()

        assert len(res.fills) > 0, "no fills at all — the harness proved nothing"
        # Every fill must be at that session's OPEN (plus the side's slippage),
        # never at its CLOSE — the price that produced the signal.
        slip = float(investor_config.costs["midcap_slippage_pct"]) / 100.0
        for _, row in res.fills.iterrows():
            frame = panel.get(row["symbol"], row["date"])
            open_px = float(frame["Open"].iloc[-1])
            close_px = float(frame["Close"].iloc[-1])
            sign = 1.0 if row["side"] == "BUY" else -1.0
            assert row["price"] == pytest.approx(open_px * (1 + sign * slip), rel=1e-6), \
                f"fill at {row['price']} is not the open of {row['date'].date()}"
            assert abs(row["price"] - close_px * (1 + sign * slip)) > 1e-6, \
                "fill price is indistinguishable from the signal-bar close"

    def test_no_fill_occurs_on_a_day_with_no_bar(self, tmp_path):
        """A stock suspended today must not be filled at a stale price."""
        closes = [100.0] * 60
        panel = _price_panel(tmp_path, {"AAA": closes})
        sectors = SectorMap()
        bt = InvestorBacktester(
            start="2023-03-01", end="2023-04-15", universe=["AAA"],
            panel=panel, store=_EmptyStore(), sector_map=sectors,
            use_fundamentals=False, use_technical=False, label="t",
        )
        res = bt.run()
        for _, row in res.fills.iterrows():
            frame = panel.get(row["symbol"], row["date"])
            assert frame.index[-1] == row["date"]


class TestChandelierStop:
    def test_stop_never_lowers(self):
        tr = ChandelierTracker(atr_period=22, multiplier=3.0)
        stop, high = tr.update(100.0, 110.0, 5.0, None)
        assert high == 110.0
        assert stop == pytest.approx(110.0 - 15.0)
        # A pullback must not widen the stop.
        stop2, high2 = tr.update(high, 100.0, 5.0, stop)
        assert high2 == 110.0, "high-water mark must not fall"
        assert stop2 >= stop

    def test_stop_ratchets_with_a_new_high(self):
        tr = ChandelierTracker(atr_period=22, multiplier=3.0)
        stop, high = tr.update(100.0, 100.0, 5.0, None)
        stop, high = tr.update(high, 130.0, 5.0, stop)
        assert high == 130.0
        assert stop == pytest.approx(130.0 - 15.0)
        assert stop > 85.0

    def test_missing_atr_leaves_the_stop_unchanged(self):
        tr = ChandelierTracker(atr_period=22, multiplier=3.0)
        stop, _ = tr.update(100.0, 100.0, 5.0, None)
        stop2, _ = tr.update(100.0, 105.0, None, stop)
        assert stop2 == stop

    def test_initial_stop_is_entry_minus_multiple_atr(self):
        tr = ChandelierTracker(atr_period=22, multiplier=3.5)
        assert tr.initial_stop(100.0, 4.0) == pytest.approx(100.0 - 14.0)

    def test_initial_stop_is_none_without_atr(self):
        tr = ChandelierTracker()
        assert tr.initial_stop(100.0, None) is None
        assert tr.initial_stop(100.0, 0.0) is None


class TestPositionAccounting:
    def test_average_price_weights_by_quantity(self):
        engine = _bare_engine()
        d = pd.Timestamp("2024-01-01")
        engine.apply_buy("AAA", 10, 100.0, d)
        engine.apply_buy("AAA", 10, 120.0, d)
        pos = engine.positions["AAA"]
        assert pos.qty == 20
        assert pos.avg_price == pytest.approx(110.0)

    def test_full_sell_removes_the_position(self):
        engine = _bare_engine()
        d = pd.Timestamp("2024-01-01")
        engine.apply_buy("AAA", 10, 100.0, d)
        engine.apply_sell("AAA", 10, d)
        assert "AAA" not in engine.positions

    def test_partial_sell_keeps_the_position(self):
        engine = _bare_engine()
        d = pd.Timestamp("2024-01-01")
        engine.apply_buy("AAA", 10, 100.0, d)
        engine.apply_sell("AAA", 4, d)
        assert engine.positions["AAA"].qty == 6

    def test_days_held_uses_calendar_days(self):
        pos = Position(symbol="A", qty=1, avg_price=1.0,
                       first_buy_date=pd.Timestamp("2024-01-01"),
                       highest_close=1.0)
        assert pos.days_held("2024-03-01") == 60

    def test_weight_is_value_over_equity(self):
        pos = Position(symbol="A", qty=10, avg_price=100.0,
                       first_buy_date=pd.Timestamp("2024-01-01"),
                       highest_close=100.0)
        assert pos.weight(100.0, 10_000.0) == pytest.approx(0.10)


class TestNoLookaheadInSnapshots:
    def test_snapshot_at_a_date_ignores_later_bars(self, tmp_path):
        """A snapshot must be identical whether or not future bars are loaded."""
        from investor import technical as T
        closes = list(np.linspace(100, 200, 300))
        panel = _price_panel(tmp_path, {"AAA": closes})
        cut = panel.get("AAA").index[250]

        snap_full = T.snapshot_from_cache(
            IndicatorCache(panel), "AAA", cut
        )
        # Same series, truncated at `cut`.
        truncated = panel.get("AAA").copy()
        truncated = truncated.loc[:cut]
        panel2 = PricePanel(tmp_path / "sub")
        panel2._frames = {"AAA": truncated}
        panel2._load_failures = set()
        snap_cut = T.snapshot_from_cache(
            IndicatorCache(panel2), "AAA", cut
        )

        assert snap_full.close == snap_cut.close
        assert snap_full.sma_slow == snap_cut.sma_slow
        assert snap_full.rsi == snap_cut.rsi

    def test_truncated_history_yields_no_snapshot_below_the_200dma(self, tmp_path):
        from investor import technical as T
        closes = [100.0 + i * 0.1 for i in range(120)]
        panel = _price_panel(tmp_path, {"AAA": closes})
        cache = IndicatorCache(panel)
        assert T.snapshot_from_cache(cache, "AAA", panel.get("AAA").index[100]) is None


class TestTechnicalSetupGates:
    def _snap(self, **kw):
        from investor.technical import TechSnapshot
        base = dict(symbol="X", date=pd.Timestamp("2024-01-01"), close=110.0,
                    sma_fast=100.0, sma_slow=95.0, sma_exit=95.0, atr=5.0,
                    rsi=55.0, rsi_prev1=35.0, rsi_prev2=30.0, rsi_prev3=32.0,
                    breakout_high=115.0, vol_ratio=1.5, mom_12_1=0.2,
                    rel_strength=0.05, closes_below_sma=0, ready=True)
        base.update(kw)
        return TechSnapshot(**base)

    def test_trend_filter_requires_both_conditions(self):
        assert self._snap().trend_ok is True
        assert self._snap(sma_slow=120.0).trend_ok is False      # close below 200
        assert self._snap(sma_fast=90.0).trend_ok is False       # 50 below 200

    def test_breakout_needs_price_and_volume(self):
        assert self._snap(close=120.0, vol_ratio=1.2).breakout_setup is True
        assert self._snap(close=120.0, vol_ratio=1.1).breakout_setup is False
        assert self._snap(close=110.0, vol_ratio=2.0).breakout_setup is False

    def test_pullback_needs_the_ma_touch_and_an_rsi_crossover(self):
        assert self._snap(close=101.0).pullback_setup is True
        # No crossover: RSI already above 40 yesterday too.
        assert self._snap(close=101.0, rsi_prev1=50.0, rsi_prev2=48.0,
                          rsi_prev3=47.0).pullback_setup is False
        # Too far from the 50DMA.
        assert self._snap(close=120.0).pullback_setup is False

    def test_rsi_below_the_level_is_not_a_pullback(self):
        assert self._snap(close=101.0, rsi=38.0).pullback_setup is False

    def test_momentum_requires_both_legs_positive(self):
        assert self._snap().momentum_ok is True
        assert self._snap(mom_12_1=-0.1).momentum_ok is False
        assert self._snap(rel_strength=-0.01).momentum_ok is False

    def test_entry_is_refused_without_trend(self):
        from investor.technical import entry_signal
        assert entry_signal(self._snap(sma_slow=120.0), eligible=True) is None

    def test_entry_is_refused_for_ineligible_names(self):
        from investor.technical import entry_signal
        assert entry_signal(self._snap(close=120.0), eligible=False) is None

    def test_ineligible_name_is_never_bought_even_on_a_setup(self):
        from investor.technical import entry_signal
        snap = self._snap(close=120.0, vol_ratio=2.0)
        assert entry_signal(snap, eligible=False) is None
        assert entry_signal(snap, eligible=True) is not None


class TestTrancheTwo:
    def _snap(self, close):
        from investor.technical import TechSnapshot
        return TechSnapshot(
            symbol="X", date=pd.Timestamp("2024-02-01"), close=close,
            sma_fast=100.0, sma_slow=95.0, sma_exit=95.0, atr=5.0,
            rsi=55.0, rsi_prev1=50.0, breakout_high=120.0, vol_ratio=1.0,
            mom_12_1=0.2, rel_strength=0.05, ready=True,
        )

    def test_tranche_two_waits_the_delay(self):
        from investor.technical import tranche2_signal
        assert tranche2_signal(self._snap(105.0), 9, 100.0) is None

    def test_tranche_two_fires_after_ten_days(self):
        from investor.technical import tranche2_signal
        sig = tranche2_signal(self._snap(105.0), 10, 100.0)
        assert sig is not None
        assert sig.tranche == 2
        assert sig.size_fraction == pytest.approx(0.5)

    def test_tranche_two_needs_price_above_the_50dma(self):
        from investor.technical import tranche2_signal
        assert tranche2_signal(self._snap(95.0), 15, 100.0) is None