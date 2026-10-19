"""Delivery equity fees for trend_long only; no broker or database access.

Legacy backtest_v2 omits delivery buy STT. Do NOT reuse or repair that model
here: changing it would invalidate the reference study (Phase 0 incident).
Quotes are unrounded model amounts, not a replica of a broker contract note.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite

from investor.trend_config import CostConfig, SleeveConfig


@dataclass(frozen=True)
class DeliveryCosts:
    stt: float = 0.0
    stamp: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    brokerage: float = 0.0
    gst: float = 0.0
    dp: float = 0.0

    @property
    def total(self) -> float:
        return sum(getattr(self, item.name) for item in fields(self))

    def deductible(self, *, stt_deductible: bool) -> float:
        # STT reduces cash, but is not a deductible capital-gains expense.
        return self.total if stt_deductible else self.total - self.stt


class DeliveryCostModel:
    def __init__(self, config: CostConfig):
        self.config = config

    def quote(self, side: str, notional: float, exchange: str, *, dp_already_charged=False) -> DeliveryCosts:
        cfg = self.config
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"Unknown side: {side!r}")
        if not isfinite(notional) or notional <= 0:
            raise ValueError("Notional must be finite and positive")
        if exchange not in cfg.exchange_rates:
            raise ValueError(f"No fee rate for exchange {exchange!r}")
        brokerage = notional * cfg.brokerage_rate
        if cfg.brokerage_cap_inr > 0:
            brokerage = min(brokerage, cfg.brokerage_cap_inr)
        exchange_fee = notional * cfg.exchange_rates[exchange]
        sebi = notional * cfg.sebi_rate
        dp = cfg.dp_sell_day_inr if side == "SELL" and not dp_already_charged else 0.0
        taxable = exchange_fee + sebi + brokerage
        if not cfg.dp_includes_gst:
            taxable += dp
        values = dict(
            stt=notional * (cfg.stt_buy_rate if side == "BUY" else cfg.stt_sell_rate),
            stamp=notional * cfg.stamp_buy_rate if side == "BUY" else 0.0,
            exchange=exchange_fee, sebi=sebi, brokerage=brokerage,
            gst=taxable * cfg.gst_rate, dp=dp,
        )
        return DeliveryCosts(**{key: value * cfg.multiplier for key, value in values.items()})


def accrue_liquid(balance: float, start, end, config: SleeveConfig) -> float:
    """Interest only, not the ending balance; ACT/configured calendar basis.

    Accrue the OLD balance before a new SIP or fill; a contribution must not
    earn yield for the weekend before it arrived. No ETF price claim is made.
    """
    import pandas as pd

    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if not isfinite(balance) or balance < 0 or pd.isna(start) or pd.isna(end) or end < start:
        raise ValueError("Invalid liquid balance or accrual interval")
    return balance * ((1 + config.annual_yield) ** ((end - start).days / config.day_count) - 1)
