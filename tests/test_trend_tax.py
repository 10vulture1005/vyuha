"""Tax scenario tests; no network or model loads, isolated root conftest DB."""
from dataclasses import replace

import pandas as pd
import pytest

from investor.trend_config import load_trend_config
from investor.trend_tax import LossCarry, annual_tax, fiscal_year, is_long_term


@pytest.fixture
def cfg():
    return load_trend_config().tax


def trades(*rows):
    return pd.DataFrame(rows, columns=["open_date", "close_date", "taxable_gain"])


def calculate(frame, cfg, end="2027-03-31", **kwargs):
    return annual_tax(frame, end, cfg, acknowledge_unverified_law=True, **kwargs)


def test_unverified_current_law_requires_explicit_acknowledgement(cfg):
    assert cfg.law_verified is False
    with pytest.raises(ValueError, match="not fully verified"):
        annual_tax(trades(), "2027-03-31", cfg)
    report = calculate(trades(), cfg)
    assert report.law_verified is False
    assert report.scenario == "FY2026-27"


@pytest.mark.parametrize("opened,closed,long", [
    ("2025-04-01", "2026-04-01", False), ("2025-04-01", "2026-04-02", True),
    ("2023-03-01", "2024-02-29", False), ("2023-03-01", "2024-03-01", False),
    ("2023-03-01", "2024-03-02", True), ("2024-02-29", "2025-02-28", False),
    ("2024-02-29", "2025-03-01", True),
])
def test_calendar_month_boundary_not_365_day_shortcut(cfg, opened, closed, long):
    assert is_long_term(opened, closed, cfg) is long


def test_fiscal_year_boundaries(cfg):
    assert fiscal_year("2026-03-31", cfg) == 2025
    assert fiscal_year("2026-04-01", cfg) == 2026


def test_stcg_and_cess(cfg):
    report = calculate(trades(("2026-05-01", "2026-12-01", 100_000)), cfg)
    year = report.years[0]
    assert year.taxable_short == 100_000
    assert year.base_tax == 20_000
    assert year.cess == 800
    assert report.total_tax == 20_800


def test_ltcg_exemption_aggregates_per_fy_not_per_trade(cfg):
    report = calculate(trades(("2024-01-01", "2026-06-01", 100_000),
                               ("2024-01-01", "2026-12-01", 100_000)), cfg)
    year = report.years[0]
    assert year.exemption_used == 125_000
    assert year.taxable_long == 75_000
    assert report.total_tax == pytest.approx(75_000 * 0.125 * 1.04)
    under = calculate(trades(("2024-01-01", "2026-12-01", 125_000)), cfg)
    assert under.total_tax == 0


def test_exemption_resets_each_fy(cfg):
    report = calculate(trades(("2023-01-01", "2025-12-01", 125_000),
                               ("2023-01-01", "2026-12-01", 125_000)), cfg)
    assert len(report.years) == 2
    assert [year.exemption_used for year in report.years] == [125_000, 125_000]
    assert report.total_tax == 0


def test_short_losses_offset_both_short_and_long(cfg):
    report = calculate(trades(("2026-04-01", "2026-05-01", -100_000),
                               ("2026-04-01", "2026-06-01", 40_000),
                               ("2024-01-01", "2026-12-01", 200_000)), cfg)
    year = report.years[0]
    assert year.short_loss_used == 100_000
    assert year.taxable_short == 0
    assert year.taxable_long == 15_000
    assert report.total_tax == pytest.approx(15_000 * 0.125 * 1.04)


def test_long_losses_never_offset_short_gains(cfg):
    report = calculate(trades(("2024-01-01", "2026-12-01", -200_000),
                               ("2026-04-01", "2026-12-01", 50_000)), cfg)
    year = report.years[0]
    assert year.taxable_short == 50_000
    assert year.carry_long == 200_000
    assert report.total_tax == 10_400
    assert report.losses == (LossCarry(2026, "LT", 200_000),)


def test_loss_setoff_precedes_ltcg_allowance(cfg):
    report = calculate(trades(("2024-01-01", "2026-12-01", -50_000),
                               ("2024-01-01", "2026-12-02", 150_000)), cfg)
    year = report.years[0]
    assert year.long_loss_used == 50_000
    assert year.exemption_used == 100_000
    assert report.total_tax == 0


def test_current_loss_carries_to_next_fy_without_cross_type_leak(cfg):
    report = calculate(trades(("2025-04-01", "2025-12-01", -100_000),
                               ("2025-04-01", "2026-07-01", 200_000)), cfg)
    assert report.years[0].carry_short == 100_000
    assert report.years[1].short_loss_used == 100_000
    assert report.years[1].taxable_long == 0
    assert report.losses == ()


def test_eight_succeeding_fys_then_expiry(cfg):
    loss = LossCarry(2018, "ST", 50_000)
    eighth = calculate(trades(("2026-05-01", "2026-12-01", 50_000)), cfg, opening_losses=[loss])
    assert eighth.total_tax == 0
    ninth = calculate(trades(("2027-05-01", "2027-12-01", 50_000)), cfg,
                      end="2028-03-31", opening_losses=[loss])
    assert ninth.years[0].expired_losses == 50_000
    assert ninth.total_tax == 10_400


def test_oldest_loss_used_first_and_lt_precedes_st_for_long_gains(cfg):
    carry = [LossCarry(2024, "LT", 100_000), LossCarry(2023, "LT", 60_000),
             LossCarry(2023, "ST", 50_000)]
    report = calculate(trades(("2024-01-01", "2026-12-01", 80_000)), cfg, opening_losses=carry)
    assert LossCarry(2024, "LT", 80_000) in report.losses
    assert LossCarry(2023, "ST", 50_000) in report.losses
    assert all(loss.origin_fy != 2023 or loss.kind != "LT" for loss in report.losses)


def test_partial_year_reserves_tax_at_horizon_not_future_fy_end(cfg):
    report = calculate(trades(("2026-04-01", "2026-09-01", 100_000)), cfg, end="2026-09-11")
    assert report.years[0].settlement_date == pd.Timestamp("2026-09-11")
    assert report.total_tax == 20_800


def test_tax_and_income_reads_ignore_future_rows(cfg):
    frame = trades(("2026-04-01", "2026-09-01", 100_000))
    future = trades(("2026-04-01", "2027-01-01", 1_000_000))
    assert calculate(frame, cfg, end="2026-09-11") == calculate(pd.concat([frame, future]), cfg, end="2026-09-11")
    income = pd.DataFrame({"date": ["2026-09-01", "2027-01-01"], "amount": [1000, 1_000_000]})
    report = calculate(trades(), cfg, end="2026-09-11", liquid_income=income)
    assert report.total_tax == 312


def test_yield_income_not_sheltered_by_capital_losses_and_surcharge_configurable(cfg):
    income = pd.DataFrame({"date": ["2026-12-01"], "amount": [10_000]})
    report = calculate(trades(("2026-04-01", "2026-12-01", -50_000)),
                       replace(cfg, surcharge_rate=0.10), liquid_income=income)
    assert report.years[0].carry_short == 50_000
    assert report.years[0].base_tax == 3000
    assert report.years[0].surcharge == 300
    assert report.total_tax == 3432


def test_tax_perturbation_lands(cfg):
    rates = replace(cfg, stcg_rate=0.24)
    assert rates.stcg_rate == 0.24
    assert calculate(trades(("2026-04-01", "2026-12-01", 100_000)), rates).total_tax == 24_960


def test_invalid_dates_and_future_origin_losses_raise(cfg):
    with pytest.raises(ValueError):
        calculate(trades(("2027-01-01", "2026-12-01", 1000)), cfg)
    with pytest.raises(ValueError):
        calculate(trades(("2026-04-01", "2026-12-01", float("nan"))), cfg)
    with pytest.raises(ValueError):
        calculate(trades(), cfg, opening_losses=[LossCarry(2026, "ST", 100)])
