# tests/test_investor_regime.py
"""Regime classification and hysteresis tests.

The hysteresis is the part worth testing hard. Without the 3-consecutive-close
confirmation, a single down day near the 200DMA drags the book to 40% equity
and back again weeks later, and the defensive sleeve churns on noise — paying
STT and a Rs 15 DP charge on every flip.
"""
from __future__ import annotations

import pandas as pd
import pytest

from investor import regime as R
from investor.config import investor_config


EXPOSURE = {k.lower(): float(v) for k, v in investor_config.exposure.items()}
CONFIRM = int(investor_config.regime["confirm_days"])
BREADTH_ON = float(investor_config.regime["breadth_risk_on"])


class TestClassification:
    def test_risk_off_requires_both_conditions(self):
        """Nifty below 200DMA AND VIX high. Either alone is not enough."""
        assert R.classify(False, 10.0, True) == R.RISK_OFF
        assert R.classify(False, 90.0, False) != R.RISK_OFF
        assert R.classify(True, 10.0, True) != R.RISK_OFF

    def test_risk_on_requires_index_and_breadth(self):
        assert R.classify(True, BREADTH_ON + 5, False) == R.RISK_ON
        assert R.classify(True, BREADTH_ON - 5, False) == R.MIXED

    def test_index_up_weak_breadth_is_mixed_not_risk_on(self):
        """One hot index with poor breadth is a narrow rally, not risk-on."""
        assert R.classify(True, 30.0, False) == R.MIXED

    def test_index_down_low_vix_is_mixed_not_risk_off(self):
        assert R.classify(False, 50.0, False) == R.MIXED

    def test_index_down_high_vix_and_poor_breadth_is_risk_off(self):
        assert R.classify(False, 10.0, True) == R.RISK_OFF


class TestExposureMapping:
    def test_each_regime_maps_to_its_configured_exposure(self):
        assert R.RegimeEngine.__init__ is not None   # cheap import guard
        cfg = investor_config.exposure
        assert float(cfg["risk_on"]) == 1.00
        assert float(cfg["mixed"]) == 0.70
        assert float(cfg["risk_off"]) == 0.40

    def test_exposure_lookup_is_case_insensitive(self):
        """Regression: YAML keys are lower-case and labels upper-case.

        A silent case mismatch here pinned the whole book at the `mixed` 70%
        level in a raging bull market — the backtest looked low-risk for a
        reason that had nothing to do with the strategy.
        """
        engine = R.RegimeEngine.__new__(R.RegimeEngine)
        assert engine.exposure_for("RISK_ON") == 1.00
        assert engine.exposure_for("risk_on") == 1.00
        assert engine.exposure_for("MIXED") == 0.70
        assert engine.exposure_for("RISK_OFF") == 0.40

    def test_unknown_regime_falls_back_to_mixed(self):
        engine = R.RegimeEngine.__new__(R.RegimeEngine)
        assert engine.exposure_for("SOMETHING_ELSE") == EXPOSURE["mixed"]


class TestHysteresis:
    def test_first_observation_seeds_without_confirmation(self):
        h = R.RegimeHysteresis(confirm_days=3)
        regime, changed, pending, count = h.update(R.MIXED)
        assert regime == R.MIXED
        assert changed is True
        assert count == 0

    def test_single_change_does_not_take_effect(self):
        h = R.RegimeHysteresis(confirm_days=3)
        h.update(R.RISK_ON)
        regime, changed, pending, count = h.update(R.RISK_OFF)
        assert regime == R.RISK_ON, "a single opposing day flipped the regime"
        assert changed is False
        assert pending == R.RISK_OFF
        assert count == 1

    def test_two_consecutive_changes_still_do_not_take_effect(self):
        h = R.RegimeHysteresis(confirm_days=3)
        h.update(R.RISK_ON)
        assert h.update(R.RISK_OFF)[0] == R.RISK_ON
        assert h.update(R.RISK_OFF)[0] == R.RISK_ON

    def test_three_consecutive_changes_take_effect(self):
        h = R.RegimeHysteresis(confirm_days=3)
        h.update(R.RISK_ON)
        h.update(R.RISK_OFF)
        h.update(R.RISK_OFF)
        regime, changed, pending, count = h.update(R.RISK_OFF)
        assert regime == R.RISK_OFF
        assert changed is True
        assert pending is None

    def test_flapping_resets_the_counter(self):
        """OSC RISK_ON, RISK_OFF, RISK_ON, RISK_OFF... must never confirm."""
        h = R.RegimeHysteresis(confirm_days=3)
        h.update(R.RISK_ON)
        for _ in range(20):
            h.update(R.RISK_OFF)
            h.update(R.RISK_ON)
        assert h.current == R.RISK_ON, "flapping regimes must not confirm a change"

    def test_counter_resets_when_the_old_regime_prints(self):
        h = R.RegimeHysteresis(confirm_days=3)
        h.update(R.RISK_ON)
        h.update(R.RISK_OFF)
        h.update(R.RISK_OFF)
        h.update(R.RISK_ON)          # back to the incumbent resets the run
        assert h.update(R.RISK_OFF)[3] == 1, "counter did not reset"

    def test_no_pending_state_when_regime_is_stable(self):
        h = R.RegimeHysteresis(confirm_days=3)
        h.update(R.RISK_ON)
        regime, changed, pending, count = h.update(R.RISK_ON)
        assert (regime, changed, pending, count) == (R.RISK_ON, False, None, 0)

    def test_reset_forces_a_specific_regime(self):
        h = R.RegimeHysteresis(confirm_days=3)
        h.update(R.RISK_ON)
        h.reset(R.RISK_OFF)
        assert h.current == R.RISK_OFF
        assert h.update(R.RISK_OFF)[1] is False   # already there, no change event


class TestDefensiveSleeve:
    def _state(self, regime):
        return R.RegimeState(date=pd.Timestamp("2024-01-01"), raw_regime=regime,
                             regime=regime, exposure=EXPOSURE[regime.lower()],
                             confirmed=True)

    def test_risk_on_needs_no_sleeve(self):
        assert R.defensive_targets(self._state(R.RISK_ON)) == {}

    def test_risk_off_sleeve_matches_the_shortfall(self):
        sleeve = R.defensive_targets(self._state(R.RISK_OFF))
        assert abs(sum(sleeve.values()) - 0.60) < 1e-9

    def test_mixed_sleeve_matches_the_shortfall(self):
        sleeve = R.defensive_targets(self._state(R.MIXED))
        assert abs(sum(sleeve.values()) - 0.30) < 1e-9

    def test_gold_is_capped_at_the_configured_maximum(self):
        max_gold = float(investor_config.defensive["max_gold_weight"])
        sleeve = R.defensive_targets(self._state(R.RISK_OFF))
        gold_key = str(investor_config.defensive["gold_proxy"])
        assert sleeve[gold_key] <= max_gold + 1e-9

    def test_instruments_come_from_config(self):
        sleeve = R.defensive_targets(self._state(R.RISK_OFF))
        assert str(investor_config.defensive["cash_proxy"]) in sleeve
        assert str(investor_config.defensive["gold_proxy"]) in sleeve

    def test_gold_never_exceeds_the_cash_leg(self):
        """Gold wants half the sleeve but is capped at 15% of the book."""
        sleeve = R.defensive_targets(self._state(R.RISK_OFF))
        gold = sleeve[str(investor_config.defensive["gold_proxy"])]
        cash = sleeve[str(investor_config.defensive["cash_proxy"])]
        assert gold <= cash


class TestVixPercentile:
    def _panel(self, values):
        from investor.data import PricePanel
        idx = pd.bdate_range("2023-01-02", periods=len(values))
        px = pd.Series(values, index=idx, dtype="float64")
        frame = pd.DataFrame({"Open": px, "High": px, "Low": px,
                              "Close": px, "Volume": 1000.0}, index=idx)
        panel = PricePanel.__new__(PricePanel)
        panel._frames = {"V": frame}
        panel._load_failures = set()
        return panel

    def test_percentile_is_high_when_vix_spikes(self):
        panel = self._panel([10.0] * 60 + [40.0])
        pct = R.vix_percentile(panel, "2023-06-01", symbol="V")
        assert pct > 80.0

    def test_percentile_is_low_when_vix_is_calm(self):
        # Ends at the window MINIMUM, so its rank is at the bottom.
        panel = self._panel([12.0] * 40 + [10.0])
        pct = R.vix_percentile(panel, "2023-06-01", symbol="V")
        assert pct < 5.0

    def test_short_history_returns_zero_rather_than_raising(self):
        panel = self._panel([10.0, 11.0, 30.0])
        assert R.vix_percentile(panel, "2023-06-01", symbol="V") == 0.0