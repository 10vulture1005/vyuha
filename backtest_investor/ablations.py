# backtest_investor/ablations.py
"""Layer ablation study.

Runs the same backtest with layers switched off one at a time so the report can
state what each layer actually ADDS to CAGR and to drawdown, rather than
asserting it.

    fundamental_only    F
    technical_only      T
    fundamental_technical  F+T
    fundamental_technical_news  F+T+N(proxy)
    full                F+T+N(proxy)+R

Each run is INDEPENDENT — same start, end, capital and universe — so the deltas
are attributable to the layer and not to a different sample.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import pandas as pd
from loguru import logger

from backtest_investor.engine import BacktestResult, InvestorBacktester
from backtest_investor.metrics import BacktestStats, compute_stats
from investor.data import PricePanel, PointInTimeFundamentals, SectorMap

#: name -> (use_fundamentals, use_technical, use_news, use_regime)
ABLATIONS: Dict[str, tuple] = {
    "fundamental_only":              (True,  False, False, False),
    "technical_only":                (False, True,  False, False),
    "fundamental_technical":         (True,  True,  False, False),
    "fundamental_technical_news":    (True,  True,  True,  False),
    "full":                          (True,  True,  True,  True),
}

ABLATION_LABELS = {
    "fundamental_only": "Fundamentals only",
    "technical_only": "Technicals only",
    "fundamental_technical": "Fundamentals + Technicals",
    "fundamental_technical_news": "Fundamentals + Technicals + News (proxy)",
    "full": "Full (F+T+News+Regime)",
}


@dataclass
class AblationResult:
    name: str
    label: str
    stats: BacktestStats
    result: Optional[BacktestResult] = None


def run_ablations(
    start,
    end,
    universe: Sequence[str],
    panel: Optional[PricePanel] = None,
    store: Optional[PointInTimeFundamentals] = None,
    sector_map: Optional[SectorMap] = None,
    initial_capital: Optional[float] = None,
    monthly_sip: float = 0.0,
    rf: float = 0.06,
    names: Optional[Sequence[str]] = None,
) -> Dict[str, AblationResult]:
    """Runs each ablation and computes its full statistics block."""
    panel = panel or PricePanel()
    store = store or PointInTimeFundamentals()
    sector_map = sector_map or SectorMap()

    results: Dict[str, AblationResult] = {}
    for name in (names or ABLATIONS.keys()):
        use_f, use_t, use_n, use_r = ABLATIONS[name]
        logger.info(f"── ablation: {name} (F={use_f} T={use_t} N={use_n} R={use_r})")
        bt = InvestorBacktester(
            start=start, end=end, universe=universe, panel=panel, store=store,
            sector_map=sector_map, initial_capital=initial_capital,
            monthly_sip=monthly_sip, use_fundamentals=use_f,
            use_technical=use_t, use_news=use_n, use_regime=use_r,
            label=name,
        )
        res = bt.run()
        stats = stats_from_result(res, rf=rf)
        results[name] = AblationResult(
            name=name, label=ABLATION_LABELS.get(name, name),
            stats=stats, result=res,
        )
    return results


def stats_from_result(res: BacktestResult, rf: float = 0.06) -> BacktestStats:
    """Builds the statistics block from a completed replay."""
    from backtest_investor.metrics import Flow

    flows = [Flow(date=d, amount=a) for d, a in res.flows]
    return compute_stats(
        res.equity["equity"],
        label=res.label,
        initial_capital=res.initial_capital,
        flows=flows or None,
        holdings=res.equity["holdings"] if "holdings" in res.equity else None,
        cash_pct=res.equity["cash_pct"] if "cash_pct" in res.equity else None,
        defensive_pct=res.equity["defensive_pct"] if "defensive_pct" in res.equity else None,
        closed_trades=res.closed_trades if len(res.closed_trades) else None,
        total_costs=res.total_costs,
        rf=rf,
    )


def ablation_table(results: Dict[str, AblationResult]) -> pd.DataFrame:
    """Wide table showing what each layer adds, relative to `technical_only`."""
    rows = []
    base = results.get("technical_only")
    for name, ab in results.items():
        s = ab.stats
        d_cagr = d_mdd = d_sharpe = None
        if base is not None:
            d_cagr = (s.cagr_pct or 0.0) - (base.stats.cagr_pct or 0.0)
            d_mdd = s.max_drawdown_pct - base.stats.max_drawdown_pct
            d_sharpe = s.sharpe - base.stats.sharpe
        rows.append({
            "ablation": name,
            "description": ab.label,
            "CAGR %": round(s.cagr_pct, 2) if s.cagr_pct else None,
            "XIRR %": round(s.xirr_pct, 2) if s.xirr_pct is not None else None,
            "MaxDD %": round(s.max_drawdown_pct, 2),
            "Sharpe": round(s.sharpe, 2),
            "Sortino": round(s.sortino, 2),
            "Calmar": round(s.calmar, 2),
            "AvgHoldings": round(s.avg_holdings, 1),
            "%Cash": round(s.pct_time_cash, 1),
            "Trades": s.n_trades,
            "Costs": round(s.total_costs, 0),
            "dCAGR vs T-only": round(d_cagr, 2) if d_cagr is not None else None,
            "dMaxDD vs T-only": round(d_mdd, 2) if d_mdd is not None else None,
            "dSharpe vs T-only": round(d_sharpe, 2) if d_sharpe is not None else None,
        })
    return pd.DataFrame(rows)


__all__ = [
    "ABLATIONS", "ABLATION_LABELS", "AblationResult",
    "run_ablations", "stats_from_result", "ablation_table",
]