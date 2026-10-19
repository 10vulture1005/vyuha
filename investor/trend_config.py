"""Strict, independent Phase 1 settings for the backtest-only trend profile.

No strategy thresholds default in Python. Missing keys must fail at startup,
not silently borrow legacy investor fees or change a robustness perturbation.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Mapping


def _number(value, name: str, *, rate=False, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a YAML number, not a string or boolean")
    value = float(value)
    if not isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    if rate and value > 1:
        raise ValueError(f"{name} must be a decimal fraction in [0, 1]")
    return value


def _integer(value, name: str, *, positive=True) -> int:
    _number(value, name, positive=positive)
    if not isinstance(value, int):
        raise ValueError(f"{name} must be a YAML integer")
    return value


def _boolean(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a YAML boolean")
    return value


@dataclass(frozen=True)
class CostConfig:
    stt_buy_rate: float
    stt_sell_rate: float
    stamp_buy_rate: float
    exchange_rates: Mapping[str, float]
    sebi_rate: float
    gst_rate: float
    brokerage_rate: float
    brokerage_cap_inr: float
    dp_sell_day_inr: float
    dp_includes_gst: bool
    multiplier: float

    def __post_init__(self):
        for key in ("stt_buy_rate", "stt_sell_rate", "stamp_buy_rate", "sebi_rate", "gst_rate", "brokerage_rate"):
            _number(getattr(self, key), key, rate=True)
        for key in ("brokerage_cap_inr", "dp_sell_day_inr", "multiplier"):
            _number(getattr(self, key), key)
        _boolean(self.dp_includes_gst, "dp_includes_gst")
        if not self.exchange_rates:
            raise ValueError("exchange_rates must not be empty")
        for exchange, rate in self.exchange_rates.items():
            if not isinstance(exchange, str) or not exchange:
                raise ValueError("exchange must be a nonempty string")
            _number(rate, exchange, rate=True)


@dataclass(frozen=True)
class ExecutionConfig:
    exchange: str
    buy_slippage: float
    sell_slippage: float

    def __post_init__(self):
        for key in ("buy_slippage", "sell_slippage"):
            if _number(getattr(self, key), key, rate=True) >= 1:
                raise ValueError("slippage must be strictly below 1")


@dataclass(frozen=True)
class SleeveConfig:
    annual_yield: float
    day_count: float

    def __post_init__(self):
        _number(self.annual_yield, "annual_yield", rate=True)
        _number(self.day_count, "day_count", positive=True)


@dataclass(frozen=True)
class TaxConfig:
    scenario: str
    law_verified: bool
    stcg_rate: float
    ltcg_rate: float
    ltcg_exemption_inr: float
    cess_rate: float
    surcharge_rate: float
    long_term_after_months: int
    fiscal_year_start_month: int
    loss_carry_years: int
    stt_deductible: bool
    liquid_income_rate: float
    defer_soft_exits: bool
    defer_window_days: tuple[int, int]

    def __post_init__(self):
        for key in ("stcg_rate", "ltcg_rate", "cess_rate", "surcharge_rate", "liquid_income_rate"):
            _number(getattr(self, key), key, rate=True)
        _number(self.ltcg_exemption_inr, "ltcg_exemption_inr")
        for key in ("long_term_after_months", "fiscal_year_start_month", "loss_carry_years"):
            _integer(getattr(self, key), key)
        if not 1 <= self.fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be a calendar month")
        for key in ("law_verified", "stt_deductible", "defer_soft_exits"):
            _boolean(getattr(self, key), key)
        if len(self.defer_window_days) != 2 or self.defer_window_days[0] > self.defer_window_days[1]:
            raise ValueError("defer_window_days must be an ordered pair")
        for value in self.defer_window_days:
            _integer(value, "defer_window_days", positive=False)


@dataclass(frozen=True)
class ReportingConfig:
    xirr_day_count: float
    xirr_lower_rate: float
    xirr_upper_rate: float
    xirr_tolerance: float
    xirr_max_iterations: int

    def __post_init__(self):
        for key in ("xirr_day_count", "xirr_tolerance"):
            _number(getattr(self, key), key, positive=True)
        _integer(self.xirr_max_iterations, "xirr_max_iterations")
        if not (-1 < self.xirr_lower_rate < 0 < self.xirr_upper_rate) or not isfinite(self.xirr_upper_rate):
            raise ValueError("invalid XIRR search bracket")


@dataclass(frozen=True)
class CapitalScenario:
    initial_capital: float
    monthly_sip: float

    def __post_init__(self):
        _number(self.initial_capital, "initial_capital", positive=True)
        _number(self.monthly_sip, "monthly_sip")


@dataclass(frozen=True)
class TrendInfrastructureConfig:
    costs: CostConfig
    execution: ExecutionConfig
    sleeve: SleeveConfig
    tax: TaxConfig
    reporting: ReportingConfig
    capital_scenarios: Mapping[str, CapitalScenario]

    @classmethod
    def from_mapping(cls, block: Mapping) -> "TrendInfrastructureConfig":
        def section(name, kind):
            # Enumerate required dataclass fields: neither .get defaults nor
            # legacy config fallbacks can make a missing fee look legitimate.
            values = {key: block[name][key] for key in kind.__dataclass_fields__}
            if kind is TaxConfig:
                values["defer_window_days"] = tuple(values["defer_window_days"])
            if kind is CostConfig:
                values["exchange_rates"] = dict(values["exchange_rates"])
            return kind(**values)

        result = cls(
            costs=section("costs", CostConfig), execution=section("execution", ExecutionConfig),
            sleeve=section("sleeve", SleeveConfig), tax=section("tax", TaxConfig),
            reporting=section("reporting", ReportingConfig),
            capital_scenarios={key: CapitalScenario(**value) for key, value in
                               block["backtest"]["capital_scenarios"].items()},
        )
        if result.execution.exchange not in result.costs.exchange_rates:
            raise ValueError("execution.exchange is missing its fee rate")
        return result


def load_trend_config() -> TrendInfrastructureConfig:
    from config import load_thresholds

    return TrendInfrastructureConfig.from_mapping(load_thresholds()["trend_long"])
