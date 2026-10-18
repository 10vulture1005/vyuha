# backtest_investor/metrics.py
"""Performance measurement for Investor Mode.

Two rules this module enforces because conflating them is the most common way
a SIP backtest reports a fantasy number:

    *   **Lump-sum CAGR and SIP XIRR are never mixed.** CAGR on a lump sum is
        (final/initial)^(1/years) - 1 and is only meaningful when capital went
        in once. Once monthly contributions exist, the money-weighted return
        (XIRR) is the honest figure and CAGR on the ending balance is not. Both
        are computed; neither is derived from the other's inputs.
    *   **Cash flows are dated.** SIP credits land on the first trading day of
        each month and count as an outflow from the investor's pocket, dated
        that day, for XIRR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ─────────────────────────────────────────────────────────────────────────────
# XIRR
# ─────────────────────────────────────────────────────────────────────────────

def xnpv(rate: float, dates: Sequence[date], values: Sequence[float]) -> float:
    """Net present value of dated cash flows at an annual `rate`."""
    if not dates:
        return 0.0
    d0 = dates[0]
    out = 0.0
    for d, v in zip(dates, values):
        years = (d - d0).days / 365.0
        if years == 0:
            out += v
        else:
            out += v / ((1.0 + rate) ** years)
    return out


def xirr(
    dates: Sequence[date],
    values: Sequence[float],
    guess: float = 0.1,
    tolerance: float = 1e-7,
    max_iter: int = 200,
) -> Optional[float]:
    """Money-weighted annualised return of dated cash flows.

    Solves `xnpv(r) = 0` by Newton with a bisection fallback. Returns None when
    no sign change brackets the root (which happens when the portfolio never
    beat its contributions, or never went negative in between) — reporting 0.0
    there would read as "flat" when the truth is "not computable".
    """
    if not dates or len(dates) < 2:
        return None
    order = np.argsort([d for d in dates])
    ds = [dates[i] for i in order]
    vs = [float(values[i]) for i in order]
    if sum(vs) <= 0:
        return None

    # Bracket the root first; Newton alone can wander off a cliff.
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = xnpv(lo, ds, vs), xnpv(hi, ds, vs)
    if not np.isfinite(f_lo) or not np.isfinite(f_hi) or f_lo * f_hi > 0:
        return None

    r = guess
    for _ in range(max_iter):
        try:
            f = xnpv(r, ds, vs)
            # Numerical derivative.
            h = 1e-6
            df = (xnpv(r + h, ds, vs) - f) / h
            if df == 0 or not np.isfinite(df):
                break
            r_new = r - f / df
            if not np.isfinite(r_new):
                break
            if abs(r_new - r) < tolerance:
                return r_new
            r = r_new
        except (OverflowError, ZeroDivisionError, ValueError):
            break

    # Bisection fallback on the bracket we already established.
    lo, hi = -0.9999, 10.0
    for _ in range(300):
        mid = (lo + hi) / 2.0
        f_mid = xnpv(mid, ds, vs)
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Return series helpers
# ─────────────────────────────────────────────────────────────────────────────

def daily_returns(equity: pd.Series) -> pd.Series:
    """Simple daily returns of an equity curve (ignores same-day cash flows).

    On SIP days the raw return is understated by the contribution; that is why
    the SIP headline number is XIRR, not an average of this series.
    """
    eq = equity.dropna()
    if len(eq) < 2:
        return pd.Series(dtype="float64")
    return eq.pct_change().dropna()


def max_drawdown(equity: pd.Series) -> Tuple[float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """Deepest peak-to-trough decline, as a negative percentage."""
    eq = equity.dropna()
    if eq.empty:
        return 0.0, None, None
    peak = eq.cummax()
    dd = (eq - peak) / peak
    trough = dd.idxmin()
    mdd = float(dd.min())
    peak_dt = eq.loc[:trough].idxmax() if trough in eq.index else None
    return mdd, peak_dt, trough


def drawdown_series(equity: pd.Series) -> pd.Series:
    eq = equity.dropna()
    return (eq - eq.cummax()) / eq.cummax()


def annualised_vol(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    return float(r.std(ddof=1) * np.sqrt(periods))


def sharpe_ratio(returns: pd.Series, rf: float = 0.0, periods: int = TRADING_DAYS) -> float:
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    excess = r.mean() - (rf / periods)
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(excess / sd * np.sqrt(periods))


def sortino_ratio(returns: pd.Series, rf: float = 0.0, periods: int = TRADING_DAYS) -> float:
    """Sharpe against DOWNSIDE deviation only."""
    r = returns.dropna()
    if len(r) < 2:
        return 0.0
    excess = r - (rf / periods)
    downside = excess[excess < 0]
    dd = float(np.sqrt((downside ** 2).mean()) * np.sqrt(periods)) if len(downside) else 0.0
    if dd == 0:
        return 0.0
    return float(excess.mean() * periods / dd)


def calmar_ratio(cagr_pct: float, mdd_pct: float) -> float:
    """CAGR over |max drawdown|. Returns 0 when the drawdown is unknown."""
    if mdd_pct is None or mdd_pct == 0:
        return 0.0
    return float(cagr_pct / abs(mdd_pct))


def cagr(final_value: float, initial_value: float, years: float) -> float:
    """Compound annual growth rate in percent. Only valid for a single deposit."""
    if initial_value <= 0 or years <= 0 or final_value <= 0:
        return 0.0
    return float(((final_value / initial_value) ** (1.0 / years) - 1.0) * 100.0)


def rolling_cagr(
    equity: pd.Series, window_years: float = 3.0
) -> pd.Series:
    """Trailing CAGR of every `window_years` window, in percent.

    Reported as min/median/max because a single good window is not evidence of
    a repeatable process — the MINIMUM is the number that matters.
    """
    eq = equity.dropna()
    if len(eq) < 2:
        return pd.Series(dtype="float64")
    n = int(window_years * TRADING_DAYS)
    if len(eq) <= n:
        return pd.Series(dtype="float64")
    future = eq.shift(-n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = ((future / eq) ** (1.0 / window_years) - 1.0) * 100.0
    return out.replace([np.inf, -np.inf], np.nan).dropna()


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio statistics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Flow:
    date: date
    amount: float     # positive = money in from the investor
    label: str = ""


@dataclass
class BacktestStats:
    """Everything the report prints, for one run."""

    label: str = ""
    start: Optional[date] = None
    end: Optional[date] = None
    years: float = 0.0

    initial_capital: float = 0.0
    final_value: float = 0.0
    total_contributed: float = 0.0

    # Return measures — lump sum and SIP are kept strictly apart.
    cagr_pct: float = 0.0
    xirr_pct: Optional[float] = None

    max_drawdown_pct: float = 0.0
    drawdown_start: Optional[date] = None
    drawdown_trough: Optional[date] = None
    volatility_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0

    rolling_3y_cagr_min: Optional[float] = None
    rolling_3y_cagr_median: Optional[float] = None
    rolling_3y_cagr_max: Optional[float] = None

    turnover_pct: float = 0.0
    avg_holdings: float = 0.0
    pct_time_defensive: float = 0.0
    pct_time_cash: float = 0.0
    win_rate_pct: float = 0.0
    avg_holding_days: float = 0.0
    total_costs: float = 0.0
    n_trades: int = 0
    n_days: int = 0

    extra: Dict = field(default_factory=dict)

    def as_dict(self) -> Dict:
        d = {k: v for k, v in self.__dict__.items() if k != "extra"}
        d.update(self.extra)
        return d

    def one_line(self) -> str:
        irr = f"{self.xirr_pct:6.2f}%" if self.xirr_pct is not None else "   n/a"
        return (
            f"{self.label:<34s} CAGR {self.cagr_pct:6.2f}%  XIRR {irr}  "
            f"MDD {self.max_drawdown_pct:6.2f}%  Sharpe {self.sharpe:5.2f}  "
            f"Calmar {self.calmar:5.2f}"
        )


def compute_stats(
    equity: pd.Series,
    label: str = "",
    initial_capital: float = 0.0,
    flows: Optional[Sequence[Flow]] = None,
    holdings: Optional[pd.Series] = None,
    cash_pct: Optional[pd.Series] = None,
    defensive_pct: Optional[pd.Series] = None,
    closed_trades: Optional[pd.DataFrame] = None,
    total_costs: float = 0.0,
    rf: float = 0.06,
    monthly_sip: float = 0.0,
) -> BacktestStats:
    """Builds the full statistics block for one equity curve."""
    eq = equity.dropna()
    stats = BacktestStats(label=label, initial_capital=initial_capital)

    if eq.empty:
        return stats

    stats.start = eq.index[0].date() if hasattr(eq.index[0], "date") else eq.index[0]
    stats.end = eq.index[-1].date() if hasattr(eq.index[-1], "date") else eq.index[-1]
    stats.years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    stats.n_days = int(len(eq))
    stats.final_value = float(eq.iloc[-1])

    flows = list(flows or [])
    if not flows and monthly_sip > 0:
        flows = []  # caller supplies them; never guessed

    stats.total_contributed = initial_capital + sum(f.amount for f in flows)

    rets = daily_returns(eq)
    stats.volatility_pct = annualised_vol(rets) * 100.0
    stats.sharpe = sharpe_ratio(rets, rf=rf)
    stats.sortino = sortino_ratio(rets, rf=rf)
    mdd, pk, tr = max_drawdown(eq)
    stats.max_drawdown_pct = mdd * 100.0
    stats.drawdown_start = pk.date() if hasattr(pk, "date") else pk
    stats.drawdown_trough = tr.date() if hasattr(tr, "date") else tr

    # ── lump sum vs SIP, kept apart ────────────────────────────────────────
    if flows:
        # Investor's perspective: contributions are outflows, the terminal
        # value is the single inflow. Mixing the signs here is the classic way
        # to "solve" an XIRR into a nonsense number.
        xs_dates = [f.date for f in flows] + [stats.end]
        xs_values = [-float(f.amount) for f in flows] + [stats.final_value]
        irr = xirr(xs_dates, xs_values)
        stats.xirr_pct = (irr * 100.0) if irr is not None else None
        # CAGR is deliberately NOT reported on a contributed series: it would
        # silently credit the investor's own monthly deposits to the strategy.
        stats.cagr_pct = 0.0
    else:
        stats.cagr_pct = cagr(stats.final_value, initial_capital, stats.years)
        stats.xirr_pct = None

    stats.calmar = calmar_ratio(
        stats.cagr_pct if stats.cagr_pct else (stats.xirr_pct or 0.0),
        stats.max_drawdown_pct,
    )

    r3 = rolling_cagr(eq, 3.0)
    if not r3.empty:
        stats.rolling_3y_cagr_min = float(r3.min())
        stats.rolling_3y_cagr_median = float(r3.median())
        stats.rolling_3y_cagr_max = float(r3.max())

    if holdings is not None and not holdings.empty:
        stats.avg_holdings = float(holdings.mean())
    if cash_pct is not None and not cash_pct.empty:
        stats.pct_time_cash = float(cash_pct.mean()) * 100.0
    if defensive_pct is not None and not defensive_pct.empty:
        stats.pct_time_defensive = float(defensive_pct.mean()) * 100.0

    if closed_trades is not None and len(closed_trades) > 0:
        stats.n_trades = int(len(closed_trades))
        pnl = pd.to_numeric(closed_trades["net_pnl"], errors="coerce").dropna()
        if len(pnl):
            stats.win_rate_pct = float((pnl > 0).mean() * 100.0)
        if "holding_days" in closed_trades.columns:
            hd = pd.to_numeric(closed_trades["holding_days"], errors="coerce").dropna()
            if len(hd):
                stats.avg_holding_days = float(hd.mean())
        if "notional" in closed_trades.columns:
            notional = pd.to_numeric(closed_trades["notional"], errors="coerce").fillna(0.0)
            avg_equity = float(eq.mean())
            if avg_equity > 0:
                # Annualised turnover: how much changed hands per year, as a
                # multiple of average book size.
                stats.turnover_pct = float(notional.sum() / avg_equity / stats.years)

    stats.total_costs = float(total_costs)
    return stats


__all__ = [
    "Flow", "BacktestStats", "xirr", "xnpv", "cagr", "max_drawdown",
    "drawdown_series", "annualised_vol", "sharpe_ratio", "sortino_ratio",
    "calmar_ratio", "rolling_cagr", "compute_stats", "daily_returns",
    "TRADING_DAYS",
]