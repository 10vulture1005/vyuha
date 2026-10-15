#!/usr/bin/env python
"""CLI group for Investor Mode: `vyuha invest <daily|backtest|status|news>`.

Registered as an `invest` subcommand of the main `vyuha` launcher, so it is
reached the same way as every other vyuha command. Kept in its own module
because the interactive setup wizard that owns the parent CLI has no business
knowing about backtests.

Examples
--------
    python scripts/invest_cli.py daily
    python scripts/invest_cli.py daily --no-news --no-telegram
    python scripts/invest_cli.py backtest --quick
    python scripts/invest_cli.py backtest --full          # incl. robustness
    python scripts/invest_cli.py status
    python scripts/invest_cli.py news --symbol RELIANCE
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402


def _require_investor_mode() -> None:
    """Investor commands require STRATEGY_MODE=investor.

    Refusing loudly beats quietly running a portfolio strategy while the user
    believes the swing engine is what is protecting their money.
    """
    if settings.STRATEGY_MODE != "investor":
        print(
            f"✗ Investor Mode is not active (STRATEGY_MODE={settings.STRATEGY_MODE}).\n"
            f"  Set STRATEGY_MODE=investor in .env to run this command.\n"
            f"  The swing strategy remains the default and is unaffected.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def cmd_daily(args) -> int:
    _require_investor_mode()
    from core.investor_engine import InvestorModeEngine

    engine = InvestorModeEngine()
    run = engine.run_daily(
        as_of=args.date,
        fetch_news=not args.no_news,
        send_telegram=not args.no_telegram,
        fill=not args.no_fill,
    )
    if args.json:
        print(json.dumps(run.as_dict(), indent=2, default=str))
    else:
        print("\n".join(run.summary_lines()))

    if args.persist_fundamentals:
        n = engine.persist_fundamentals(run.as_of.date())
        print(f"Snapshotted {n} fundamental records to investor_fundamental_snapshots")
    return 0


def cmd_backtest(args) -> int:
    _require_investor_mode()
    from backtest_investor.ablations import ablation_table, run_ablations
    from backtest_investor.ablations import stats_from_result
    from backtest_investor.benchmarks import run_benchmarks
    from backtest_investor.engine import InvestorBacktester
    from backtest_investor.report import write_report
    from backtest_investor.robustness import run_robustness, summarize
    from investor.config import investor_config
    from investor.data import PointInTimeFundamentals, PricePanel, SectorMap

    cfg = investor_config.backtest
    start = args.start or cfg.get("start", "2016-09-12")
    end = args.end or cfg.get("end", "2026-09-11")
    oos = args.oos_start or cfg.get("oos_start", "2022-01-01")
    capital = args.capital or float(cfg.get("initial_capital", 1_000_000))
    sip = args.sip if args.sip is not None else float(cfg.get("monthly_sip", 10_000))

    panel = PricePanel()
    store = PointInTimeFundamentals()
    sectors = SectorMap()
    universe = [s for s in panel.available_symbols() if not s.startswith("INDEX_")]
    if not universe:
        print("✗ No OHLC data in data/raw/ohlc/", file=sys.stderr)
        return 1

    earliest_fund = None
    for recs in store.records.values():
        for r in recs:
            if earliest_fund is None or r.usable_from < earliest_fund:
                earliest_fund = r.usable_from

    runs = {}
    for name, (s, e) in {
        "full_sample_lump": (start, end),
        "oos_lump": (oos, end),
        "full_sample_sip": (start, end),
    }.items():
        run_sip = sip if name.endswith("sip") else 0.0
        print(f"→ {name} ({s} → {e}, SIP {run_sip})")
        runs[name] = InvestorBacktester(
            start=s, end=e, universe=universe, panel=panel, store=store,
            sector_map=sectors, initial_capital=capital, monthly_sip=run_sip,
            label=name,
        ).run()

    primary = runs["oos_lump"]
    bench_lump = run_benchmarks(panel, oos, end, capital, flows=primary.flows)
    bench_sip = run_benchmarks(panel, start, end, capital,
                               flows=runs["full_sample_sip"].flows)

    ablations = {}
    if not args.no_ablations:
        print("→ ablations")
        ablations = run_ablations(
            oos, end, universe, panel=panel, store=store,
            sector_map=sectors, initial_capital=capital,
        )

    robustness = robustness_summary = None
    if not args.quick:
        print("→ robustness")
        robustness = run_robustness(
            oos, end, universe, panel=panel, store=store,
            sector_map=sectors, initial_capital=capital,
        )
        robustness_summary = summarize(robustness)

    stats = {k: stats_from_result(v) for k, v in runs.items()}
    out = write_report(
        outdir=args.outdir, stats=stats, runs=runs,
        benchmarks_lump=bench_lump, benchmarks_sip=bench_sip,
        ablations=ablations, robustness=robustness,
        robustness_summary=robustness_summary, universe=universe,
        earliest_fundamentals=str(earliest_fund) if earliest_fund is not None else None,
        args={"start": start, "end": end, "oos_start": oos},
    )

    print("\n" + "=" * 78)
    for k, s in stats.items():
        print(s.one_line())
    if ablations:
        print()
        print(ablation_table(ablations).to_string(index=False))
    if robustness_summary:
        print(f"\nRobustness: {robustness_summary['verdict']}")
    print("=" * 78)
    print(f"Report: {out}")
    return 0


def cmd_status(args) -> int:
    _require_investor_mode()
    from db.models import InvestorTarget, NewsEventRow, PortfolioHolding, RebalanceLog
    from db.session import get_session
    from investor.data import PricePanel, SectorMap

    panel = PricePanel()
    sectors = SectorMap()
    try:
        with get_session() as session:
            holdings = session.query(PortfolioHolding).filter(
                PortfolioHolding.status == "OPEN"
            ).all()
            last_rebal = session.query(RebalanceLog).order_by(
                RebalanceLog.id.desc()
            ).first()
            news_n = session.query(NewsEventRow).filter(
                NewsEventRow.is_proxy.is_(False)
            ).count()
            last_targets = session.query(InvestorTarget).order_by(
                InvestorTarget.as_of.desc()
            ).limit(40).all()
    except Exception as exc:
        print(f"✗ DB not initialised ({exc}). Run `alembic upgrade head`.", file=sys.stderr)
        return 1

    print(f"Strategy mode    : {settings.STRATEGY_MODE}")
    print(f"Live trading     : {settings.LIVE_TRADING_ENABLED}")
    print(f"Open holdings    : {len(holdings)}")
    if holdings:
        total = sum(float(h.avg_buy_price) * h.qty for h in holdings)
        print(f"  invested (cost): ₹{total:,.0f}")
    if last_rebal:
        print(f"Last rebalance   : {last_rebal.rebalance_date} "
              f"({last_rebal.regime}, {len(last_rebal.target_weights or {})} targets)")
    print(f"Classified news  : {news_n} rows stored (is_proxy=False)")
    if last_targets:
        as_of = max(t.as_of for t in last_targets)
        print(f"\nTop targets as of {as_of}:")
        rows = sorted(
            [t for t in last_targets if t.as_of == as_of and not t.is_defensive],
            key=lambda t: -float(t.target_weight),
        )[:12]
        for t in rows:
            score = f" score {float(t.fundamental_score):.1f}" if t.fundamental_score is not None else ""
            print(f"  {t.symbol:<14} {float(t.target_weight):6.2%}  "
                  f"{(t.sector or '?')[:24]:<24}{score}")
    return 0


def cmd_news(args) -> int:
    _require_investor_mode()
    from investor import news as news_mod
    from agents.tools.news_tools import fetch_recent_headlines

    events = []
    for sym in args.symbol:
        try:
            for h in fetch_recent_headlines(sym, sym, lookback_days=args.days):
                events.append(news_mod.classify_headline(sym, h))
        except Exception as exc:
            print(f"  {sym}: fetch failed ({exc})", file=sys.stderr)

    if not events:
        print("No headlines retrieved.")
        return 0

    print(f"{'SYMBOL':<14}{'TYPE':<16}{'SUBTYPE':<22}{'MAT':>5}  HEADLINE")
    for e in sorted(events, key=lambda x: (x.event_type, -x.materiality)):
        print(f"{e.symbol:<14}{e.event_type:<16}{(e.subtype or '-'):<22}"
              f"{e.materiality:>5.2f}  {e.title[:80]}")
    if args.json:
        print(json.dumps([e.as_row(None) for e in events], indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vyuha invest", description="Investor Mode (long-horizon portfolio)"
    )
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("daily", help="run today's Investor Mode cycle")
    d.add_argument("--date", default=None, help="ISO date (default: last trading day)")
    d.add_argument("--no-news", action="store_true", help="skip headline collection")
    d.add_argument("--no-telegram", action="store_true", help="skip the digest")
    d.add_argument("--no-fill", action="store_true", help="decide but do not paper-fill")
    d.add_argument("--json", action="store_true", help="JSON output")
    d.add_argument("--persist-fundamentals", action="store_true",
                   help="also snapshot fundamentals to investor_fundamental_snapshots")
    d.set_defaults(func=cmd_daily)

    b = sub.add_parser("backtest", help="run the backtest study")
    b.add_argument("--start", default=None)
    b.add_argument("--end", default=None)
    b.add_argument("--oos-start", default=None)
    b.add_argument("--capital", type=float, default=None)
    b.add_argument("--sip", type=float, default=None)
    b.add_argument("--quick", action="store_true", help="skip robustness sweeps")
    b.add_argument("--no-ablations", action="store_true")
    b.add_argument("--outdir", default="docs/investor_backtest")
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("status", help="show the current investor book")
    s.set_defaults(func=cmd_status)

    n = sub.add_parser("news", help="classify recent headlines")
    n.add_argument("--symbol", action="append", required=True,
                   help="repeatable")
    n.add_argument("--days", type=int, default=3)
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_news)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())