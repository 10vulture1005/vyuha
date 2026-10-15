# scripts/run_investor_backtest.py
"""Full Investor Mode backtest study: ablations, benchmarks, robustness.

Runs the whole deliverable in one go:

    1. In-sample and out-of-sample replays of the full strategy
    2. Lump-sum CAGR and SIP XIRR, reported separately
    3. Benchmark comparison on identical cash flows
    4. The five layer ablations
    5. Robustness sweeps with collapse flags
    6. Stats + equity-curve PNG, QuantStats tearsheet, markdown summary

Usage:
    python scripts/run_investor_backtest.py
    python scripts/run_investor_backtest.py --quick     # skip robustness
    python scripts/run_investor_backtest.py --start 2022-01-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from backtest_investor.ablations import run_ablations, ablation_table, stats_from_result  # noqa: E402
from backtest_investor.benchmarks import run_benchmarks  # noqa: E402
from backtest_investor.engine import InvestorBacktester  # noqa: E402
from backtest_investor.report import write_report  # noqa: E402
from backtest_investor.robustness import run_robustness, summarize  # noqa: E402
from investor.config import investor_config  # noqa: E402
from investor.data import PointInTimeFundamentals, PricePanel, SectorMap  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    cfg = investor_config.backtest
    ap.add_argument("--start", default=cfg.get("start", "2016-09-12"))
    ap.add_argument("--end", default=cfg.get("end", "2026-09-11"))
    ap.add_argument("--oos-start", default=cfg.get("oos_start", "2022-01-01"))
    ap.add_argument("--capital", type=float, default=float(cfg.get("initial_capital", 1_000_000)))
    ap.add_argument("--sip", type=float, default=float(cfg.get("monthly_sip", 10_000)))
    ap.add_argument("--quick", action="store_true", help="skip robustness sweeps")
    ap.add_argument("--no-ablations", action="store_true")
    ap.add_argument("--outdir", default="docs/investor_backtest")
    args = ap.parse_args()

    panel = PricePanel()
    store = PointInTimeFundamentals()
    sectors = SectorMap()
    universe = [s for s in panel.available_symbols() if not s.startswith("INDEX_")]
    if not universe:
        logger.error("No OHLC data found in data/raw/ohlc/")
        return 1

    logger.info(f"Universe: {len(universe)} symbols")
    logger.info(f"Fundamentals: {len(store.symbols())} symbols "
                f"({'loaded' if store.loaded else 'MISSING — F layer will be empty'})")

    # The first usable fundamental date bounds the F layer. Reporting a
    # full-history CAGR for a strategy whose F layer only switched on in 2022
    # would credit four years of a different strategy.
    earliest_fund = None
    for recs in store.records.values():
        for r in recs:
            if earliest_fund is None or r.usable_from < earliest_fund:
                earliest_fund = r.usable_from

    # ── 1. head-to-head: full sample + OOS, lump sum and SIP ────────────────
    runs = {}
    for name, (s, e) in {
        "full_sample_lump": (args.start, args.end),
        "oos_lump": (args.oos_start, args.end),
        "full_sample_sip": (args.start, args.end),
    }.items():
        sip = args.sip if name.endswith("sip") else 0.0
        logger.info(f"═══ run: {name} ({s} -> {e}, SIP {sip})")
        bt = InvestorBacktester(
            start=s, end=e, universe=universe, panel=panel, store=store,
            sector_map=sectors, initial_capital=args.capital, monthly_sip=sip,
            label=name,
        )
        runs[name] = bt.run()

    # ── 2. benchmarks on matched flows ──────────────────────────────────────
    logger.info("═══ benchmarks")
    primary = runs["oos_lump"]
    bench_lump = run_benchmarks(
        panel, args.oos_start, args.end, args.capital, flows=primary.flows,
    )
    bench_sip = run_benchmarks(
        panel, args.start, args.end, args.capital, flows=runs["full_sample_sip"].flows,
    )

    # ── 3. ablations ────────────────────────────────────────────────────────
    ablations = {}
    if not args.no_ablations:
        logger.info("═══ ablations")
        ablations = run_ablations(
            args.oos_start, args.end, universe, panel=panel, store=store,
            sector_map=sectors, initial_capital=args.capital,
        )

    # ── 4. robustness ───────────────────────────────────────────────────────
    robustness = None
    robustness_summary = None
    if not args.quick:
        logger.info("═══ robustness")
        robustness = run_robustness(
            args.oos_start, args.end, universe, panel=panel, store=store,
            sector_map=sectors, initial_capital=args.capital,
        )
        robustness_summary = summarize(robustness)
        logger.info(f"Robustness: {robustness_summary['verdict']}")

    # ── 5. report ───────────────────────────────────────────────────────────
    stats = {
        k: stats_from_result(v) for k, v in runs.items()
    }
    out = write_report(
        outdir=args.outdir,
        stats=stats,
        runs=runs,
        benchmarks_lump=bench_lump,
        benchmarks_sip=bench_sip,
        ablations=ablations,
        robustness=robustness,
        robustness_summary=robustness_summary,
        universe=universe,
        earliest_fundamentals=str(earliest_fund) if earliest_fund is not None else None,
        args=vars(args),
    )
    print("\n" + "=" * 78)
    for k, s in stats.items():
        print(s.one_line())
    print("=" * 78)
    print(f"Report written to: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())