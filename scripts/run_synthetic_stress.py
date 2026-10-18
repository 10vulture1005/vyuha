#!/usr/bin/env python3
"""Run controlled synthetic-market stress tests of Investor Mode.

Example: python scripts/run_synthetic_stress.py --seeds 20 --sessions 2520
Outputs per-run results and scenario summaries as CSV. These are engineering
stress tests, not evidence that future or live returns will match simulations.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from loguru import logger

from backtest_investor.engine import InvestorBacktester
from backtest_investor.metrics import max_drawdown
from backtest_investor.synthetic import SCENARIOS, generate_market
from investor.data import PointInTimeFundamentals, SectorMap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--sessions", type=int, default=2520)
    ap.add_argument("--symbols", type=int, default=30)
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--outdir", default="docs/synthetic_stress")
    args = ap.parse_args()
    if min(args.seeds, args.sessions, args.symbols) < 1:
        ap.error("seeds, sessions, and symbols must be positive")

    syms = [f"S{i:03d}" for i in range(args.symbols)]
    records = []
    for scenario in SCENARIOS:
        for seed in range(args.seeds):
            panel = generate_market(syms, sessions=args.sessions,
                                   scenario=scenario, seed=seed)
            days = panel.trading_days(syms)
            bt = InvestorBacktester(
                start=str(days[0].date()), end=str(days[-1].date()),
                universe=syms, panel=panel,
                store=PointInTimeFundamentals(), sector_map=SectorMap(),
                initial_capital=args.capital, use_fundamentals=False,
                use_news=False, use_regime=False,
                label=f"{scenario.name}_seed{seed}",
            )
            result = bt.run()
            equity = result.equity["equity"]
            drawdown, _, _ = max_drawdown(equity)
            rows = result.closed_trades
            records.append({
                "scenario": scenario.name, "seed": seed,
                "annual_drift_assumption": scenario.annual_drift,
                "annual_vol_assumption": scenario.annual_volatility,
                "sessions": args.sessions, "symbols": args.symbols,
                "final_equity": result.final_value,
                "net_return_pct": (result.final_value / args.capital - 1) * 100,
                "max_drawdown_pct": drawdown * 100,
                "fills": len(result.fills), "closed_trades": len(rows),
                "total_costs": result.total_costs,
                "cash_fraction_end": float(result.equity["cash"].iloc[-1] / result.final_value)
                    if result.final_value else None,
            })
            logger.info(f"{scenario.name} seed={seed}: final={result.final_value:.0f}, "
                        f"drawdown={drawdown:.1%}, fills={len(result.fills)}")

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    raw = pd.DataFrame(records)
    raw.to_csv(out / "runs.csv", index=False)
    summary = raw.groupby("scenario").agg(
        runs=("seed", "count"), median_return_pct=("net_return_pct", "median"),
        worst_return_pct=("net_return_pct", "min"), median_drawdown_pct=("max_drawdown_pct", "median"),
        worst_drawdown_pct=("max_drawdown_pct", "min"), median_fills=("fills", "median"),
        median_costs=("total_costs", "median"),
    ).reset_index()
    summary.to_csv(out / "summary.csv", index=False)
    print(summary.to_string(index=False))
    print(f"\nSaved {out / 'runs.csv'} and {out / 'summary.csv'}")
    print("Synthetic outcomes validate behavior only; they do not establish real-world edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
