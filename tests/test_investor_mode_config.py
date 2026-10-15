# tests/test_investor_mode_config.py
"""Strategy-mode gating and config-surface tests.

`STRATEGY_MODE` is the switch that keeps Investor Mode from silently running
while the user believes the swing engine is protecting their money, so its
validation and default are worth pinning.
"""
from __future__ import annotations

import pytest

from config.settings import Settings
from investor.config import InvestorConfig, investor_config, reload_investor_config


class TestStrategyMode:
    def test_default_is_swing(self):
        """The swing engine must stay the default for existing users."""
        assert Settings().STRATEGY_MODE == "swing"

    @pytest.mark.parametrize("raw,expected", [
        ("investor", "investor"),
        ("INVESTOR", "investor"),
        ("  investor  ", "investor"),
        ("swing", "swing"),
        ("SWING", "swing"),
    ])
    def test_accepts_valid_modes_case_insensitively(self, raw, expected):
        assert Settings(STRATEGY_MODE=raw).STRATEGY_MODE == expected

    @pytest.mark.parametrize("bad", ["hodl", "swingg", "INVEST", "long", "s wing"])
    def test_rejects_unknown_modes(self, bad):
        """A typo must fail loudly rather than falling back to some default.

        Running the wrong strategy because of a typo is the expensive failure
        mode; refusing to start is the cheap one.
        """
        with pytest.raises(Exception):
            Settings(STRATEGY_MODE=bad)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_value_defaults_to_swing(self, blank):
        """`STRATEGY_MODE=` left in .env is a common accident.

        Defaulting to the safe profile beats refusing to boot the engine.
        """
        assert Settings(STRATEGY_MODE=blank).STRATEGY_MODE == "swing"

    def test_live_trading_is_off_by_default(self):
        """Investor Mode is paper/advisory unless explicitly enabled."""
        assert Settings().LIVE_TRADING_ENABLED is False


class TestInvestorConfigSurface:
    def test_every_required_section_is_present(self):
        cfg = investor_config
        for section in ("regime", "fundamentals", "technical", "news",
                        "portfolio", "costs", "tax", "backtest"):
            assert getattr(cfg, section), f"missing investor.{section}"

    def test_exposure_table_is_complete_and_ordered(self):
        exp = investor_config.exposure
        assert float(exp["risk_off"]) < float(exp["mixed"]) < float(exp["risk_on"])
        assert float(exp["risk_on"]) == 1.0

    def test_defensive_instruments_are_configured(self):
        d = investor_config.defensive
        assert d["cash_proxy"] and d["gold_proxy"]
        assert 0 < float(d["max_gold_weight"]) <= 0.20

    def test_score_weights_sum_to_one(self):
        w = investor_config.score_weights
        assert abs(float(w["quality"]) + float(w["growth"]) + float(w["valuation"]) - 1.0) < 1e-9

    def test_hard_filters_match_the_brief(self):
        f = investor_config.non_financial_filters
        assert float(f["min_roce"]) == 15.0
        assert float(f["max_debt_equity"]) == 1.0
        assert float(f["min_interest_coverage"]) == 4.0
        assert float(f["min_ocf_to_pat_3y"]) == 0.8
        assert float(f["max_promoter_pledge_pct"]) == 10.0
        assert float(f["min_revenue_cagr_3y"]) == 10.0
        assert float(f["min_eps_cagr_3y"]) == 12.0

    def test_financial_filters_match_the_brief(self):
        f = investor_config.financial_filters
        assert float(f["min_roe"]) == 14.0
        assert float(f["max_gnpa_pct"]) == 3.0

    def test_technical_params_match_the_brief(self):
        t = investor_config.technical
        assert int(t["trend_ma_days"]) == 200
        assert int(t["fast_ma_days"]) == 50
        assert int(t["breakout_days"]) == 55
        assert float(t["breakout_vol_multiple"]) == 1.2
        assert int(t["rsi_period"]) == 14
        assert float(t["rsi_turn_up_level"]) == 40.0
        assert int(t["atr_period"]) == 22
        assert float(t["chandelier_atr_multiple"]) == 3.5
        assert int(t["exit_consecutive_closes"]) == 3

    def test_portfolio_band_and_caps_match_the_brief(self):
        p = investor_config.portfolio
        assert int(p["min_holdings"]) == 20
        assert int(p["max_holdings"]) == 30
        assert float(p["max_position_pct"]) == 8.0
        assert float(p["max_sector_pct"]) == 25.0
        assert float(p["min_position_pct"]) == 2.0
        assert int(p["max_new_buys_per_day"]) == 3
        assert int(p["min_hold_days"]) == 60
        assert float(p["overweight_trigger"]) == 1.5

    def test_regime_confirmation_is_three_closes(self):
        assert int(investor_config.regime["confirm_days"]) == 3
        assert int(investor_config.regime["trend_ma_days"]) == 200
        assert float(investor_config.regime["vix_percentile"]) == 80.0

    def test_slippage_band_is_within_the_specified_range(self):
        c = investor_config.costs
        mid, large = float(c["midcap_slippage_pct"]), float(c["largecap_slippage_pct"])
        for v in (mid, large):
            assert 0.1 <= v <= 0.2

    def test_tax_aware_logic_is_opt_in(self):
        """Tax rates must not silently change net returns before they are verified."""
        assert investor_config.tax["enabled"] is False
        assert float(investor_config.tax["stcg_rate"]) == 0.20
        assert float(investor_config.tax["ltcg_rate"]) == 0.125
        assert int(investor_config.tax["ltcg_min_days"]) == 365


class TestConfigReloadIsInPlace:
    def test_reload_mutates_the_same_object(self):
        """Every layer does `from investor.config import investor_config`.

        If reload rebound the module global, those layers would keep reading
        the old thresholds forever — which silently turns the robustness
        sweep's threshold perturbations into no-ops that still report a
        confident verdict.
        """
        import investor.technical as tech_mod

        before_id = id(investor_config)
        before_raw_id = id(investor_config.raw)
        original = tech_mod.investor_config.technical["chandelier_atr_multiple"]

        tech_mod.investor_config.raw["technical"]["chandelier_atr_multiple"] = 9.99
        try:
            assert tech_mod.ChandelierTracker().multiplier == 9.99, \
                "a mutation of the live config did not reach an importing module"
        finally:
            reload_investor_config()

        assert id(investor_config) == before_id
        assert id(investor_config.raw) == before_raw_id
        assert tech_mod.investor_config.technical["chandelier_atr_multiple"] == original

    def test_reload_restores_values_from_disk(self):
        original = investor_config.technical["atr_period"]
        investor_config.raw["technical"]["atr_period"] = 999
        try:
            assert investor_config.technical["atr_period"] == 999
            reload_investor_config()
            assert investor_config.technical["atr_period"] == original
        finally:
            reload_investor_config()


class TestSwingConfigUntouched:
    def test_swing_sections_still_present(self):
        from config import thresholds

        for section in ("fundamental", "technical", "allocator", "risk"):
            assert section in thresholds, f"swing section {section} disappeared"

    def test_investor_is_a_separate_top_level_block(self):
        from config import thresholds

        assert "investor" in thresholds
        # Investor thresholds must not leak into the swing sections.
        assert "trend_ma_days" not in thresholds["technical"]
        assert "max_position_pct" not in thresholds["technical"]