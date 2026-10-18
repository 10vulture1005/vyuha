# backtest_investor/benchmarks.py
"""Benchmark comparison on matched cash flows.

Two details decide whether a benchmark comparison means anything:

*   **The same money must go in on the same days.** A benchmark that receives
    the whole lump sum on day 1 while the strategy receives it over three years
    of SIP is not a benchmark, it is a different experiment. `buy_and_hold`
    therefore accepts the strategy's actual flow schedule.

*   **Price index vs TRI.** The repo's OHLC is ingested with
    `yfinance(auto_adjust=True)`, so historical prices are adjusted for
    dividends and the return series behaves like a total-return index. The
    gap to the headline Nifty price index is therefore small but real, and it
    always favours the benchmark. `tri_note` records that caveat on every
    report so nobody reads the alpha as larger than it is.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from backtest_investor.metrics import Flow, compute_stats
from investor.data import PricePanel


TRI_NOTE = (
    "Benchmark series come from the same yfinance(auto_adjust=True) OHLC feed "
    "as the strategy (dividend-adjusted, i.e. closer to TRI than to the price "
    "index). Small residual gaps against the headline Nifty price index remain "
    "and always favour the benchmark."
)


@dataclass
class BenchmarkResult:
    label: str
    equity: pd.Series
    stats: object
    flows: List[Flow]
    note: str = TRI_NOTE


def buy_and_hold(
    panel: PricePanel,
    symbol: str,
    start,
    end,
    initial_capital: float,
    flows: Optional[Sequence[Tuple]] = None,
    label: Optional[str] = None,
    rf: float = 0.06,
    cost_multiplier: float = 1.0,
) -> BenchmarkResult:
    """Buys one instrument on day 1 and (optionally) holds SIP contributions.

    SIP contributions are converted to units at that day's close, so the
    benchmark compounds contributions exactly like the strategy does.
    """
    from backtest_v2.costs.tax_and_fees import TransactionCostModel

    df = panel.get(symbol, end)
    df = df.loc[df.index >= pd.Timestamp(start)]
    if df.empty:
        return BenchmarkResult(label or symbol, pd.Series(dtype="float64"), None, [])

    cost_model = TransactionCostModel()
    cost_model.include_costs = cost_multiplier > 0

    prices = df["Close"]
    units = 0.0
    cash = float(initial_capital)

    first = prices.iloc[0]
    units += cash / float(first)
    cash = 0.0

    flow_list: List[Flow] = []
    flow_by_date = {pd.Timestamp(d): float(a) for d, a in (flows or [])}

    equity: Dict[pd.Timestamp, float] = {}
    for ts, px in prices.items():
        contribution = flow_by_date.get(ts, 0.0)
        if contribution > 0:
            units += contribution / float(px)
            flow_list.append(Flow(date=ts.date(), amount=contribution))
        equity[ts] = units * float(px)

    eq = pd.Series(equity).sort_index()
    stats = compute_stats(
        eq,
        label=label or f"{symbol} buy & hold",
        initial_capital=initial_capital,
        flows=flow_list,
        rf=rf,
    )
    return BenchmarkResult(stats.label, eq, stats, flow_list)


def cash_benchmark(
    panel: PricePanel,
    start,
    end,
    initial_capital: float,
    flows: Optional[Sequence[Tuple]] = None,
    annual_rate: float = 0.06,
    label: str = "Cash @ 6% (T-bill proxy)",
) -> BenchmarkResult:
    """A risk-free reference on the identical flow schedule.

    Without this, "we beat the index" can hide "we simply held less equity and
    the market fell" — the regime layer cuts exposure to 40% in drawdowns, and
    a cash line is the only fair control for that.
    """
    if flows:
        dates = sorted({pd.Timestamp(d) for d, _ in flows})
        start_ts = pd.Timestamp(dates[0]) if dates else pd.Timestamp(start)
    else:
        start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    idx = pd.bdate_range(start_ts, end_ts)
    if len(idx) == 0:
        return BenchmarkResult(label, pd.Series(dtype="float64"), None, [])

    daily = (1.0 + annual_rate) ** (1.0 / 252.0)
    flow_by_date = {pd.Timestamp(d): float(a) for d, a in (flows or [])}
    flow_list: List[Flow] = []

    value = float(initial_capital)
    out: Dict[pd.Timestamp, float] = {}
    for ts in idx:
        contribution = flow_by_date.get(ts, 0.0)
        if contribution > 0:
            value += contribution
            flow_list.append(Flow(date=ts.date(), amount=contribution))
        value *= daily
        out[ts] = value

    eq = pd.Series(out)
    stats = compute_stats(
        eq, label=label, initial_capital=initial_capital,
        flows=flow_list, rf=annual_rate,
    )
    return BenchmarkResult(label, eq, stats, flow_list)


def run_benchmarks(
    panel: PricePanel,
    start,
    end,
    initial_capital: float,
    flows: Optional[Sequence[Tuple]] = None,
    rf: float = 0.06,
    symbols: Sequence[str] = ("INDEX_NIFTY500", "INDEX_NIFTY50"),
) -> Dict[str, BenchmarkResult]:
    """Nifty 500 / Nifty 50 buy-and-hold plus a cash control."""
    out: Dict[str, BenchmarkResult] = {}
    for sym in symbols:
        res = buy_and_hold(
            panel, sym, start, end, initial_capital, flows=flows, rf=rf
        )
        out[sym] = res
    out["CASH"] = cash_benchmark(
        panel, start, end, initial_capital, flows=flows, annual_rate=rf
    )
    return out


__all__ = ["BenchmarkResult", "buy_and_hold", "cash_benchmark", "run_benchmarks", "TRI_NOTE"]