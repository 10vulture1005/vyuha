"""Transparent capital-gains tax SCENARIO, not a personal tax return.

FY2026-27 parameters are deliberately backcast for comparisons, not switched
silently to old historical rates. All filing dates remain the data layer's
responsibility. Timely loss-return filing and unused LTCG allowance are assumed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Sequence

import pandas as pd

from investor.trend_config import TaxConfig


@dataclass(frozen=True)
class LossCarry:
    origin_fy: int             # starting calendar year of Indian FY
    kind: str                 # ST or LT
    amount: float

    def __post_init__(self):
        if self.kind not in {"ST", "LT"} or not isfinite(self.amount) or self.amount < 0:
            raise ValueError("Invalid carried capital loss")
        if isinstance(self.origin_fy, bool) or not isinstance(self.origin_fy, int):
            raise ValueError("origin_fy must be an integer year")


@dataclass(frozen=True)
class TaxYear:
    fiscal_year: int
    settlement_date: pd.Timestamp
    short_gains: float
    long_gains: float
    short_losses: float
    long_losses: float
    short_loss_used: float
    long_loss_used: float
    taxable_short: float
    taxable_long: float
    exemption_used: float
    liquid_income: float
    base_tax: float
    surcharge: float
    cess: float
    total_tax: float
    expired_losses: float
    carry_short: float
    carry_long: float


@dataclass(frozen=True)
class TaxReport:
    as_of: pd.Timestamp
    scenario: str
    law_verified: bool
    years: tuple[TaxYear, ...]
    losses: tuple[LossCarry, ...]

    @property
    def total_tax(self) -> float:
        return sum(year.total_tax for year in self.years)


def fiscal_year(when, config: TaxConfig) -> int:
    date = pd.Timestamp(when)
    return date.year - int(date.month < config.fiscal_year_start_month)


def is_long_term(open_date, close_date, config: TaxConfig) -> bool:
    opened, closed = pd.Timestamp(open_date), pd.Timestamp(close_date)
    if pd.isna(opened) or pd.isna(closed) or closed < opened:
        raise ValueError("Invalid holding interval")
    # 'More than 12 months', not >=365 days: leap years and anniversaries are
    # real boundary cases. The 335-366 soft-exit window does not define tax age.
    return closed > opened + pd.DateOffset(months=config.long_term_after_months)


def _dated_slice(frame: pd.DataFrame | None, date_column: str, as_of) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result[date_column] = pd.to_datetime(result[date_column], errors="raise")
    if result[date_column].isna().any():
        raise ValueError("Missing realization/income date")
    result = result.set_index(date_column).sort_index().loc[:as_of]
    return result


def annual_tax(realizations: pd.DataFrame, as_of, config: TaxConfig, *,
               opening_losses: Sequence[LossCarry] = (),
               liquid_income: pd.DataFrame | None = None,
               acknowledge_unverified_law=False) -> TaxReport:
    """Rows: open_date, close_date, taxable_gain (NOT economic net_pnl).

    Optional liquid_income rows: date, amount. Yield is taxed at a configured
    personal-rate assumption; capital losses cannot offset it. No slab/rebate,
    other portfolio, surcharge threshold, advance-tax interest or grandfathering
    calculation is inferred from a strategy-only account.
    """
    if not config.law_verified and not acknowledge_unverified_law:
        raise ValueError("Tax law is not fully verified; explicitly acknowledge a scenario-only model")
    as_of = pd.Timestamp(as_of)
    if pd.isna(as_of) or as_of.tzinfo is not None:
        raise ValueError("as_of must be a naive date")
    trades = _dated_slice(realizations, "close_date", as_of)
    income = _dated_slice(liquid_income, "date", as_of)
    by_year: dict[int, dict[str, float]] = {}
    for closed, row in trades.iterrows():
        gain = float(row["taxable_gain"])
        if not isfinite(gain):
            raise ValueError("Taxable gains must be finite")
        long_term = is_long_term(row["open_date"], closed, config)
        year = fiscal_year(closed, config)
        bucket = by_year.setdefault(year, {})
        key = ("long" if long_term else "short") + ("_gains" if gain >= 0 else "_losses")
        bucket[key] = bucket.get(key, 0.0) + abs(gain)
    for when, row in income.iterrows():
        amount = float(row["amount"])
        if not isfinite(amount) or amount < 0:
            raise ValueError("Liquid income must be finite and nonnegative")
        bucket = by_year.setdefault(fiscal_year(when, config), {})
        bucket["liquid_income"] = bucket.get("liquid_income", 0.0) + amount
    final_year = fiscal_year(as_of, config)
    first_year = min(by_year, default=final_year)
    losses = list(opening_losses)
    if any(loss.origin_fy >= first_year for loss in losses):
        raise ValueError("Opening losses must originate before the reporting period")
    years = []
    for year in range(first_year, final_year + 1):
        bucket = by_year.get(year, {})
        sg, lg = bucket.get("short_gains", 0.0), bucket.get("long_gains", 0.0)
        sl, ll = bucket.get("short_losses", 0.0), bucket.get("long_losses", 0.0)
        short_used, long_used = min(sg, sl), min(lg, ll)
        sg, sl = sg - short_used, sl - short_used
        lg, ll = lg - long_used, ll - long_used
        used = min(lg, sl)
        lg, sl, short_used = lg - used, sl - used, short_used + used
        expired = sum(loss.amount for loss in losses if year - loss.origin_fy > config.loss_carry_years)
        losses = [loss for loss in losses if year - loss.origin_fy <= config.loss_carry_years]
        # Current-year losses first, then brought-forward. Preserve ST losses
        # where possible: LT losses can never shelter short gains. Oldest
        # vintages are consumed first so expiry cannot create artificial tax.
        for kind in ("LT", "ST"):
            for i, loss in sorted(enumerate(losses), key=lambda item: item[1].origin_fy):
                if loss.kind != kind:
                    continue
                remaining = loss.amount
                if kind == "ST":
                    used = min(sg, remaining)
                    sg, remaining = sg - used, remaining - used
                    short_used += used
                used = min(lg, remaining)
                lg, remaining = lg - used, remaining - used
                if kind == "ST":
                    short_used += used
                else:
                    long_used += used
                losses[i] = replace(loss, amount=remaining)
        losses = [loss for loss in losses if loss.amount > 0]
        if sl > 0:
            losses.append(LossCarry(year, "ST", sl))
        if ll > 0:
            losses.append(LossCarry(year, "LT", ll))
        exemption = min(lg, config.ltcg_exemption_inr)
        taxable_long = lg - exemption
        yield_income = bucket.get("liquid_income", 0.0)
        base = sg * config.stcg_rate + taxable_long * config.ltcg_rate + yield_income * config.liquid_income_rate
        surcharge = base * config.surcharge_rate
        cess = (base + surcharge) * config.cess_rate
        # Annual liabilities are dated at FY end; the final partial FY is
        # reserved at the backtest horizon, never counted as a future inflow.
        fy_end = pd.Timestamp(year=year + 1, month=config.fiscal_year_start_month, day=1) - pd.Timedelta(days=1)
        years.append(TaxYear(
            year, min(fy_end, as_of), bucket.get("short_gains", 0.0), bucket.get("long_gains", 0.0),
            bucket.get("short_losses", 0.0), bucket.get("long_losses", 0.0), short_used, long_used,
            sg, taxable_long, exemption, yield_income, base, surcharge, cess, base + surcharge + cess,
            expired, sum(loss.amount for loss in losses if loss.kind == "ST"),
            sum(loss.amount for loss in losses if loss.kind == "LT"),
        ))
    return TaxReport(as_of, config.scenario, config.law_verified, tuple(years), tuple(losses))
