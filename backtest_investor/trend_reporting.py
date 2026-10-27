"""Trend return reporting, independent of legacy Investor metrics bugs.

The legacy SIP statistics omit opening capital and reject negative XIRRs.
Do not fix the reference implementation here. This path includes EVERY dated
contribution, accepts losses, and reports taxes as dated external cash costs.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import pandas as pd

from investor.trend_config import ReportingConfig
from investor.trend_tax import TaxReport


@dataclass(frozen=True)
class DatedCashFlow:
    date: pd.Timestamp
    amount: float             # investor viewpoint: contributions/taxes negative
    label: str


@dataclass(frozen=True)
class TrendReturnReport:
    net_of_cost_xirr: float | None
    post_tax_xirr: float | None
    total_contributed: float
    final_value_net_of_costs: float
    total_tax: float
    net_cashflows: tuple[DatedCashFlow, ...]
    post_tax_cashflows: tuple[DatedCashFlow, ...]
    tax_law_verified: bool


def dated_xirr(cashflows: tuple[DatedCashFlow, ...], config: ReportingConfig) -> float | None:
    if not cashflows:
        return None
    grouped: dict[pd.Timestamp, float] = {}
    for flow in cashflows:
        if not isfinite(flow.amount) or pd.isna(flow.date):
            raise ValueError("Nonfinite cashflow or missing date")
        grouped[flow.date] = grouped.get(flow.date, 0.0) + flow.amount
    dates = sorted(grouped)
    amounts = [grouped[day] for day in dates]
    # A contribution-only account has one change of sign: all funding/taxes
    # negative, then a terminal positive receipt. Multiple-root streams must
    # not be assigned a cherry-picked annual rate by a Newton starting guess.
    signs = [1 if amount > 0 else -1 for amount in amounts if amount != 0]
    if not signs or signs[0] != -1 or signs[-1] != 1 or sum(a != b for a, b in zip(signs, signs[1:])) != 1:
        return None
    years = [(day - dates[0]).days / config.xirr_day_count for day in dates]

    def npv(rate):
        return sum(amount / (1 + rate) ** age for amount, age in zip(amounts, years))

    lo, hi = config.xirr_lower_rate, config.xirr_upper_rate
    f_lo, f_hi = npv(lo), npv(hi)
    if not isfinite(f_lo) or not isfinite(f_hi) or f_lo * f_hi > 0:
        return None
    for _ in range(config.xirr_max_iterations):
        mid = (lo + hi) / 2
        value = npv(mid)
        if abs(value) <= config.xirr_tolerance or hi - lo <= config.xirr_tolerance:
            return mid
        if f_lo * value <= 0:
            hi = mid
        else:
            lo, f_lo = mid, value
    return None  # never label an unconverged rate a measured return


def return_report(initial_date, initial_capital: float, contributions: pd.DataFrame,
                  terminal_date, final_value: float, taxes: TaxReport,
                  config: ReportingConfig, *, fully_liquidated: bool) -> TrendReturnReport:
    """Contributions: date, amount (positive investor deposits, not profits).

    Tax is a reporting overlay funded OUTSIDE the simulated account at each
    FY end. Therefore it does not assume free in-account liquidity, force sells,
    or silently alter the strategy/benchmark flow schedule. Final partial-year
    liability is charged at terminal date. This is not a tax-debited replay.
    """
    start, end = pd.Timestamp(initial_date), pd.Timestamp(terminal_date)
    if not fully_liquidated:
        raise ValueError("No realized terminal return: final-open positions remain unfilled")
    if (pd.isna(start) or pd.isna(end) or end <= start or taxes.as_of != end
            or not isfinite(initial_capital) or initial_capital <= 0
            or not isfinite(final_value) or final_value < 0):
        raise ValueError("Invalid return-report dates, capital, value or tax horizon")
    flows = [DatedCashFlow(start, -initial_capital, "opening_capital")]
    total_contributed = float(initial_capital)
    if not contributions.empty:
        frame = contributions.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        if frame.date.isna().any():
            raise ValueError("Missing contribution date")
        frame = frame.set_index("date").sort_index().loc[:end]
        for date, row in frame.iterrows():
            amount = float(row["amount"])
            if date < start or not isfinite(amount) or amount < 0:
                raise ValueError("Invalid contribution")
            total_contributed += amount
            flows.append(DatedCashFlow(date, -amount, "contribution"))
    flows.append(DatedCashFlow(end, final_value, "final_open_proceeds"))
    tax_flows = [DatedCashFlow(year.settlement_date, -year.total_tax, "tax_scenario")
                 for year in taxes.years if year.total_tax > 0]
    if any(flow.date < start or flow.date > end for flow in tax_flows):
        raise ValueError("Tax report includes liabilities outside this replay")
    net = tuple(sorted(flows, key=lambda flow: flow.date))
    post_tax = tuple(sorted(flows + tax_flows, key=lambda flow: flow.date))
    return TrendReturnReport(dated_xirr(net, config), dated_xirr(post_tax, config),
                             total_contributed, final_value, taxes.total_tax, net, post_tax,
                             taxes.law_verified)
