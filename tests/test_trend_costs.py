"""Delivery friction is separate from legacy Investor/backtest_v2 assumptions."""
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from config import load_thresholds
from investor.trend_config import TrendInfrastructureConfig, load_trend_config
from investor.trend_costs import DeliveryCostModel, accrue_liquid


@pytest.fixture
def cfg():
    return load_trend_config()


def test_delivery_stt_both_sides_stamp_buy_only(cfg):
    model = DeliveryCostModel(cfg.costs)
    buy, sell = [model.quote(side, 100_000, "NSE") for side in ("BUY", "SELL")]
    assert buy.stt == sell.stt == 100
    assert buy.stamp == pytest.approx(15)
    assert sell.stamp == 0
    assert buy.dp == 0
    assert sell.dp == cfg.costs.dp_sell_day_inr


def test_gst_and_brokerage_are_both_in_total(cfg):
    config = replace(cfg.costs, brokerage_rate=0.001, brokerage_cap_inr=20)
    costs = DeliveryCostModel(config).quote("BUY", 100_000, "NSE")
    assert costs.brokerage == 20
    assert costs.exchange == pytest.approx(2.97)
    assert costs.sebi == pytest.approx(0.1)
    assert costs.gst == pytest.approx((20 + 2.97 + 0.1) * 0.18)
    assert costs.total == pytest.approx(100 + 15 + 20 + 2.97 + 0.1 + 4.1526)
    assert costs.deductible(stt_deductible=False) == pytest.approx(costs.total - 100)
    assert costs.deductible(stt_deductible=True) == costs.total


def test_brokerage_uncapped_and_dp_gst_configuration(cfg):
    model = DeliveryCostModel(replace(cfg.costs, brokerage_rate=0.001, brokerage_cap_inr=0,
                                     dp_sell_day_inr=10, dp_includes_gst=False))
    sell = model.quote("SELL", 100_000, "BSE")
    assert sell.brokerage == 100
    assert sell.exchange == pytest.approx(3.75)
    assert sell.gst == pytest.approx((100 + 3.75 + 0.1 + 10) * 0.18)
    second = model.quote("SELL", 100_000, "BSE", dp_already_charged=True)
    assert second.dp == 0
    assert sell.total - second.total == pytest.approx(11.8)


def test_inclusive_dp_is_not_gst_charged_twice(cfg):
    model = DeliveryCostModel(cfg.costs)
    sell = model.quote("SELL", 100_000, "NSE")
    assert sell.gst == pytest.approx((2.97 + 0.1) * 0.18)
    second = model.quote("SELL", 100_000, "NSE", dp_already_charged=True)
    assert sell.total - second.total == pytest.approx(cfg.costs.dp_sell_day_inr)


def test_2x_cost_perturbation_lands_on_every_fee(cfg):
    base = DeliveryCostModel(cfg.costs).quote("SELL", 100_000, "NSE")
    doubled_cfg = replace(cfg.costs, multiplier=2)
    doubled = DeliveryCostModel(doubled_cfg).quote("SELL", 100_000, "NSE")
    assert doubled_cfg.multiplier == 2
    for key, value in base.__dict__.items():
        assert getattr(doubled, key) == pytest.approx(2 * value)
    assert DeliveryCostModel(replace(cfg.costs, multiplier=0)).quote("BUY", 100_000, "NSE").total == 0


@pytest.mark.parametrize("notional", [0, -1, float("nan"), float("inf")])
def test_bad_notional_fails_closed(cfg, notional):
    with pytest.raises(ValueError):
        DeliveryCostModel(cfg.costs).quote("BUY", notional, "NSE")


def test_unknown_side_or_venue_does_not_guess_fee(cfg):
    for side, venue in [("BUYY", "NSE"), ("BUY", "NSSE")]:
        with pytest.raises(ValueError):
            DeliveryCostModel(cfg.costs).quote(side, 100, venue)


def test_liquid_sleeve_is_six_percent_per_calendar_year(cfg):
    assert accrue_liquid(100_000, "2025-01-01", "2026-01-01", cfg.sleeve) == pytest.approx(6000)
    start, end = pd.Timestamp("2026-01-02"), pd.Timestamp("2026-01-05")
    assert accrue_liquid(100_000, start, end, cfg.sleeve) == pytest.approx(100_000 * (1.06 ** (3 / 365) - 1))
    assert accrue_liquid(0, start, end, cfg.sleeve) == 0
    with pytest.raises(ValueError):
        accrue_liquid(100, end, start, cfg.sleeve)


def test_capital_configs_are_not_pooled(cfg):
    assert cfg.capital_scenarios["lump"].initial_capital == 1_000_000
    assert cfg.capital_scenarios["lump"].monthly_sip == 0
    assert cfg.capital_scenarios["account_sip"].initial_capital == 100_000
    assert cfg.capital_scenarios["account_sip"].monthly_sip == 1000


def test_missing_threshold_is_error_not_legacy_fallback():
    block = deepcopy(load_thresholds()["trend_long"])
    del block["costs"]["stt_buy_rate"]
    with pytest.raises(KeyError, match="stt_buy_rate"):
        TrendInfrastructureConfig.from_mapping(block)


@pytest.mark.parametrize("section,key,value", [
    ("costs", "gst_rate", -0.18), ("costs", "dp_sell_day_inr", float("nan")),
    ("costs", "multiplier", -1), ("execution", "sell_slippage", 1),
    ("execution", "exchange", "TYPO"), ("tax", "law_verified", "false"),
    ("tax", "fiscal_year_start_month", 13), ("tax", "loss_carry_years", 1.5),
    ("reporting", "xirr_lower_rate", -1), ("sleeve", "day_count", 0),
])
def test_invalid_config_fails_early(section, key, value):
    block = deepcopy(load_thresholds()["trend_long"])
    block[section][key] = value
    with pytest.raises(ValueError):
        TrendInfrastructureConfig.from_mapping(block)
