"""XIRR has dated funding, economic fees and a separately labelled tax overlay."""
import os
from pathlib import Path

import pandas as pd
import pytest

from backtest_investor.trend_execution import TrendExecutor, TrendOrder
from backtest_investor.trend_reporting import DatedCashFlow, dated_xirr, return_report
from investor.trend_config import load_trend_config
from investor.trend_costs import DeliveryCostModel
from investor.trend_tax import annual_tax


@pytest.fixture
def cfg():
    return load_trend_config()


def taxes(cfg, end="2027-01-01", gain=None):
    frame = pd.DataFrame([] if gain is None else [("2026-01-02", "2026-12-31", gain)],
                         columns=["open_date", "close_date", "taxable_gain"])
    return annual_tax(frame, end, cfg.tax, acknowledge_unverified_law=True)


@pytest.mark.parametrize("final,expected", [(110_000, 0.10), (100_000, 0), (90_000, -0.10)])
def test_positive_flat_and_negative_returns_all_report(cfg, final, expected):
    report = return_report("2026-01-01", 100_000, pd.DataFrame(), "2027-01-01",
                           final, taxes(cfg), cfg.reporting, fully_liquidated=True)
    assert report.net_of_cost_xirr == pytest.approx(expected, abs=1e-7)
    assert report.post_tax_xirr == report.net_of_cost_xirr


def test_sip_return_includes_opening_capital_and_exact_dates(cfg):
    contribution = pd.Timestamp("2026-07-01")
    end = pd.Timestamp("2027-01-01")
    final = 100_000 * 1.1 + 1000 * 1.1 ** ((end - contribution).days / 365)
    flows = pd.DataFrame({"date": [contribution], "amount": [1000]})
    report = return_report("2026-01-01", 100_000, flows, end, final, taxes(cfg), cfg.reporting,
                           fully_liquidated=True)
    assert report.total_contributed == 101_000
    assert report.net_of_cost_xirr == pytest.approx(0.10, abs=1e-7)
    assert report.net_cashflows[0].amount == -100_000
    assert report.net_cashflows[1].date == contribution
    assert report.net_cashflows[1].amount == -1000


def test_duplicate_same_day_flows_are_added_not_overwritten(cfg):
    flows = pd.DataFrame({"date": ["2026-01-01", "2026-01-01"], "amount": [1000, 2000]})
    report = return_report("2026-01-01", 100_000, flows, "2027-01-01", 103_000 * 1.1,
                           taxes(cfg), cfg.reporting, fully_liquidated=True)
    assert report.total_contributed == 103_000
    assert report.net_of_cost_xirr == pytest.approx(0.10, abs=1e-7)


def test_post_tax_is_external_fy_liability_with_no_fictional_reinvestment(cfg):
    report = return_report("2026-01-01", 100_000, pd.DataFrame(), "2027-01-01", 200_000,
                           taxes(cfg, gain=100_000), cfg.reporting, fully_liquidated=True)
    assert report.total_tax == 20_800
    assert report.net_of_cost_xirr == pytest.approx(1.0, abs=1e-7)
    assert report.post_tax_xirr == pytest.approx(0.792, abs=1e-7)
    assert report.tax_law_verified is False
    assert report.total_contributed == 100_000  # tax is not strategy funding
    tax_flows = [flow for flow in report.post_tax_cashflows if flow.label == "tax_scenario"]
    assert len(tax_flows) == 1
    assert tax_flows[0].date == pd.Timestamp("2027-01-01")
    assert tax_flows[0].amount == -20_800


def test_no_xirr_claim_when_final_bar_missing(cfg):
    with pytest.raises(ValueError, match="unfilled"):
        return_report("2026-01-01", 100_000, pd.DataFrame(), "2027-01-01", 100_000,
                      taxes(cfg), cfg.reporting, fully_liquidated=False)


def test_future_contribution_is_not_counted(cfg):
    flows = pd.DataFrame({"date": ["2027-01-02"], "amount": [1_000_000]})
    report = return_report("2026-01-01", 100_000, flows, "2027-01-01", 110_000,
                           taxes(cfg), cfg.reporting, fully_liquidated=True)
    assert report.total_contributed == 100_000
    assert report.net_of_cost_xirr == pytest.approx(0.10, abs=1e-7)


def test_xirr_no_root_or_multiple_roots_is_unavailable(cfg):
    assert dated_xirr((), cfg.reporting) is None
    assert dated_xirr((DatedCashFlow(pd.Timestamp("2026-01-01"), -100, "deposit"),), cfg.reporting) is None
    cashflows = tuple(DatedCashFlow(pd.Timestamp(date), amount, "test") for date, amount in
                      [("2024-01-01", -100), ("2025-01-01", 200), ("2026-01-01", -100)])
    assert dated_xirr(cashflows, cfg.reporting) is None


def test_costs_execution_tax_and_returns_reconcile_end_to_end(cfg):
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2027-01-01"])
    prices = pd.DataFrame({"Open": [100, 100, 120], "Low": [99, 99, 119],
                           "High": [102, 102, 122], "Close": [101, 101, 121],
                           "Volume": [1000, 1000, 1000]}, index=dates)
    ex = TrendExecutor(100_000, cfg.execution, DeliveryCostModel(cfg.costs),
                       stt_deductible=cfg.tax.stt_deductible)
    ex.run_session(dates[0], {"AAA": prices}, market_ready=True)
    ex.queue_close(dates[0], [TrendOrder("AAA", "BUY", 100, dates[0], initial_stop=90)], {})
    ex.run_session(dates[1], {"AAA": prices}, market_ready=True)
    ex.queue_close(dates[1], [], {})
    last = ex.run_session(dates[2], {"AAA": prices}, market_ready=True, final=True)
    realized = ex.realized_frame()
    assert ex.cash == pytest.approx(100_000 + realized.net_pnl.sum())
    tax = annual_tax(realized, dates[-1], cfg.tax, acknowledge_unverified_law=True)
    assert tax.total_tax == pytest.approx(realized.taxable_gain.sum() * 0.20 * 1.04)
    report = return_report(dates[0], 100_000, pd.DataFrame(), dates[-1], ex.cash,
                           tax, cfg.reporting, fully_liquidated=not last.unliquidated)
    assert report.net_of_cost_xirr == pytest.approx(ex.cash / 100_000 - 1, abs=1e-7)
    assert report.post_tax_xirr == pytest.approx((ex.cash - tax.total_tax) / 100_000 - 1, abs=1e-7)


def test_test_database_is_not_application_database():
    # SEC-02 regression tripwire; the root conftest sets this before imports.
    url = os.environ["DATABASE_URL"]
    assert "vyuha-tests" in url
    assert Path(url.removeprefix("sqlite:///" )).name == "vyuha-test.db"
