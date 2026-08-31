#!/usr/bin/env python
"""Standalone CLI for the TradingAgents LLM pipeline.

Runs the full multi-agent LangGraph pipeline (4 analysts + bull/bear
researchers + trader + 3 risk debators + portfolio manager) for an
Indian ticker and prints the final decision.

Examples
--------
First-time setup — pick your LLM provider and paste its API key:
    python scripts/run_tradingagents.py setup

Single ticker (one-off analysis):
    python scripts/run_tradingagents.py single RELIANCE
    python scripts/run_tradingagents.py single TATASTEEL --date 2026-01-15

Portfolio mode — evaluate every ACTIVE watchlist symbol that has a
fresh TechnicalSignal today:
    python scripts/run_tradingagents.py --portfolio

Resolve realised outcomes for past decisions (writes to
``tradingagents_outcomes``):
    python scripts/run_tradingagents.py --resolve-outcomes

Showcase the framework's standalone CLI (delegates to upstream
``cli_ta`` if you want to use the rich interactive UI):
    python scripts/run_tradingagents.py --cli-ux

If you run ``single``/``portfolio``/``resolve`` without first
configuring a provider key, the CLI auto-launches the setup wizard
so you never see an opaque authentication error. Pass
``--no-setup`` to skip the wizard (for scripted/CI runs).

This script is the vyuha-side counterpart to TradingAgents' own
``tradingagents`` command. The two coexist — ``--cli-ux`` lets you
launch the upstream interactive shell directly without going through
vyuha's settings/config.
"""
import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List

# Allow running from repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.models import (
    TechnicalSignal,
    TradingAgentsDecision,
    TradingAgentsOutcome,
    Watchlist,
    WatchlistStatus,
)
from db.session import get_session


def _print_section(title: str) -> None:
    bar = "─" * 70
    print(f"\n{bar}\n{title}\n{bar}")


def _persist_summary(result) -> None:
    """Persist a CLI run to the same DB tables the bridge uses."""
    try:
        with get_session() as session:
            session.add(
                TradingAgentsDecision(
                    ticker=result.ticker,
                    trade_date=result.trade_date,
                    trade_decision=result.action,
                    final_signal=result.raw_signal,
                    market_report=result.market_report,
                    sentiment_report=result.sentiment_report,
                    news_report=result.news_report,
                    fundamentals_report=result.fundamentals_report,
                    investment_plan=result.investment_plan,
                    trader_plan=result.trader_plan,
                    debate_history=result.debate_history,
                    risk_history=result.risk_history,
                    raw_state=result.raw_state,
                    error_msg=result.error,
                    duration_ms=result.duration_ms,
                    created_at=datetime.now(timezone.utc),
                )
            )
    except Exception:
        # Bridge already persists; this is a fallback for raw
        # TradingAgentsResult instances returned without DB writes.
        pass


def cmd_setup(args: argparse.Namespace) -> int:
    """Run the interactive LLM provider + API-key setup wizard."""
    from scripts.setup_wizard import run_setup

    run_setup()
    return 0


def cmd_single(args: argparse.Namespace) -> int:
    from agents.llm_trader import LLMPipelineAgent

    if not _maybe_setup_interactive():
        return 1

    trade_date = date.fromisoformat(args.date) if args.date else date.today()
    agent = LLMPipelineAgent()
    result = agent.evaluate_symbol(args.ticker, trade_date)

    _print_section(f"TradingAgents Decision: {result.ticker} ({trade_date})")
    print(f"Action (vyuha):  {result.action}")
    print(f"Raw signal:     {result.raw_signal}")
    print(f"Duration:       {result.duration_ms} ms")
    if result.error:
        print(f"Error:          {result.error}")

    if args.full:
        if result.market_report:
            _print_section("Market Analyst")
            print(result.market_report)
        if result.fundamentals_report:
            _print_section("Fundamentals Analyst")
            print(result.fundamentals_report)
        if result.sentiment_report:
            _print_section("Sentiment Analyst")
            print(result.sentiment_report)
        if result.news_report:
            _print_section("News Analyst")
            print(result.news_report)
        if result.debate_history.get("judge_decision"):
            _print_section("Research Manager")
            print(result.debate_history["judge_decision"])
        if result.trader_plan:
            _print_section("Trader")
            print(result.trader_plan)
        if result.risk_history.get("judge_decision"):
            _print_section("Risk Manager")
            print(result.risk_history["judge_decision"])
        if result.final_decision_text:
            _print_section("Portfolio Manager (Final)")
            print(result.final_decision_text)

    _persist_summary(result)
    return 0 if not result.is_error else 1


def cmd_portfolio(args: argparse.Namespace) -> int:
    from agents.llm_trader import run_cross_check

    if not _maybe_setup_interactive():
        return 1

    trade_date = date.fromisoformat(args.date) if args.date else date.today()
    with get_session() as session:
        active_rows = (
            session.query(Watchlist.symbol)
            .filter(Watchlist.status == WatchlistStatus.ACTIVE.value)
            .all()
        )
        active = sorted({r[0] for r in active_rows})

        signal_rows = (
            session.query(TechnicalSignal.symbol)
            .filter(TechnicalSignal.signal_date == trade_date)
            .distinct()
            .all()
        )
        buy_symbols = {r[0] for r in signal_rows}

    rule_based = {sym: ("BUY" if sym in buy_symbols else "HOLD") for sym in active}
    symbols: List[str] = (
        sorted(buy_symbols) if args.trade_date_only else active
    )

    if not symbols:
        print(f"No symbols to evaluate for {trade_date}.")
        return 0

    print(f"Evaluating {len(symbols)} symbol(s) for {trade_date}…")
    results = run_cross_check(symbols, rule_based, trade_date)

    agree = disagree = errors = 0
    for r in results:
        marker = "✓" if r.confirmed_by_rules else "✗"
        if r.is_error:
            errors += 1
            status = "ERROR"
        elif r.confirmed_by_rules:
            agree += 1
            status = "AGREE"
        else:
            disagree += 1
            status = "DISAGREE"
        print(f"  {marker} {r.ticker:>12}  llm={r.action:<6}  rule={rule_based.get(r.ticker, '?'):<6}  {status}")

    print(
        f"\nSummary: total={len(results)} agree={agree} disagree={disagree} errors={errors}"
    )
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    from agents.llm_trader import resolve_pending_outcomes

    if not _maybe_setup_interactive():
        return 1

    written = resolve_pending_outcomes(args.holding_days)
    print(f"Wrote {written} TradingAgents outcome row(s).")
    return 0


def cmd_cli_ux(args: argparse.Namespace) -> int:
    """Delegate to the upstream TradingAgents interactive CLI."""
    import runpy

    try:
        runpy.run_module("cli_ta.main", run_name="__main__")
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def _maybe_setup_interactive() -> bool:
    """Auto-launch the setup wizard if no API key is configured.

    Reads ``_GLOBAL_NO_SETUP`` (set by ``--no-setup`` on the CLI) and
    bails out without prompting when set. Reads ``_GLOBAL_FORCE_SETUP``
    (``--setup``) to force the wizard even if a key looks present
    (handy when the user wants to rotate providers).

    Returns ``True`` if a key is (still) available for the chosen
    provider, ``False`` if setup was attempted but couldn't run (non-
    TTY stdin) or failed. Callers should bail out cleanly when this
    returns ``False`` rather than making doomed API calls.
    """
    from config import settings

    if _GLOBAL_NO_SETUP:
        return True  # honour the override; let the call fail naturally

    from scripts.setup_wizard import (
        maybe_run_setup,
        needs_api_key,
        PROVIDER_ENV_VAR,
    )

    if not needs_api_key(settings.TRADINGAGENTS_LLM_PROVIDER):
        return True

    result = maybe_run_setup(force=_GLOBAL_FORCE_SETUP)
    if result is None:
        # Either setup was needed but couldn't run (non-TTY), or the
        # user cancelled. Either way we have no usable key.
        env_var = PROVIDER_ENV_VAR.get(settings.TRADINGAGENTS_LLM_PROVIDER) or "<unknown>"
        if not settings.TRADINGAGENTS_ENABLED:
            return True  # nothing to do
        print(
            f"[!] {env_var} not configured. Set it in .env or run "
            f"`python scripts/run_tradingagents.py setup`.",
            file=sys.stderr,
        )
        return False
    return True


# Set by argparse before any subcommand runs.
_GLOBAL_NO_SETUP: bool = False
_GLOBAL_FORCE_SETUP: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the TradingAgents LLM pipeline for Indian tickers.",
    )
    parser.add_argument(
        "--no-setup", action="store_true",
        help="Skip the auto-setup wizard (for CI / scripted runs)",
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="Force the interactive setup wizard even if a key looks set",
    )
    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", help="Pick LLM provider + paste API key.")
    p_setup.set_defaults(func=cmd_setup)

    p_single = sub.add_parser("single", help="Run the pipeline for one ticker.")
    p_single.add_argument("ticker", help="NSE/BSE ticker (bare or .NS/.BO-suffixed)")
    p_single.add_argument("--date", default="", help="ISO date (default: today)")
    p_single.add_argument(
        "--full", action="store_true",
        help="Print every agent's report, not just the final decision",
    )
    p_single.add_argument(
        "--no-setup", action="store_true",
        help="Skip the auto-setup wizard (for CI / scripted runs)",
    )
    p_single.add_argument(
        "--setup", action="store_true",
        help="Force the interactive setup wizard even if a key looks set",
    )
    p_single.set_defaults(func=cmd_single)

    p_port = sub.add_parser("portfolio", help="Cross-check every active watchlist symbol.")
    p_port.add_argument(
        "--date", default="",
        help="ISO date (default: today)",
    )
    p_port.add_argument(
        "--trade-date-only", action="store_true",
        help="Only evaluate symbols with fresh signals on --date",
    )
    p_port.add_argument(
        "--no-setup", action="store_true",
        help="Skip the auto-setup wizard (for CI / scripted runs)",
    )
    p_port.add_argument(
        "--setup", action="store_true",
        help="Force the interactive setup wizard even if a key looks set",
    )
    p_port.set_defaults(func=cmd_portfolio)

    p_resolve = sub.add_parser("resolve", help="Backfill realised outcomes.")
    p_resolve.add_argument(
        "--holding-days", type=int, default=None,
        help="Override Settings.TRADINGAGENTS_HOLDING_DAYS",
    )
    p_resolve.add_argument(
        "--no-setup", action="store_true",
        help="Skip the auto-setup wizard (for CI / scripted runs)",
    )
    p_resolve.add_argument(
        "--setup", action="store_true",
        help="Force the interactive setup wizard even if a key looks set",
    )
    p_resolve.set_defaults(func=cmd_resolve)

    p_cli = sub.add_parser("cli", help="Launch the upstream TradingAgents interactive CLI.")
    p_cli.add_argument(
        "--no-setup", action="store_true",
        help="Skip the auto-setup wizard (for CI / scripted runs)",
    )
    p_cli.add_argument(
        "--setup", action="store_true",
        help="Force the interactive setup wizard even if a key looks set",
    )
    p_cli.set_defaults(func=cmd_cli_ux)

    # Back-compat: bare args default to "single <ticker>".
    parser.add_argument(
        "ticker_pos", nargs="?",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--portfolio", action="store_true")
    parser.add_argument("--resolve-outcomes", action="store_true")
    parser.add_argument("--cli-ux", action="store_true")
    parser.add_argument("--date", default="")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--holding-days", type=int, default=None)
    parser.add_argument("--trade-date-only", action="store_true")

    return parser


def main() -> int:
    global _GLOBAL_NO_SETUP, _GLOBAL_FORCE_SETUP

    args = build_parser().parse_args()
    # Prefer the top-level flags; fall back to subparser-level copies
    # so `single X --no-setup` works the same as `--no-setup single X`.
    _GLOBAL_NO_SETUP = bool(
        getattr(args, "no_setup", False)
    )
    _GLOBAL_FORCE_SETUP = bool(
        getattr(args, "setup", False)
        and not (args.command == "setup")
    )

    if args.command == "single":
        return cmd_single(args)
    if args.command == "portfolio":
        return cmd_portfolio(args)
    if args.command == "resolve":
        return cmd_resolve(args)
    if args.command == "cli":
        return cmd_cli_ux(args)

    # Bare flags (back-compat with the original ad-hoc usage).
    if args.setup and not args.command:
        return cmd_setup(args)
    if args.cli_ux:
        return cmd_cli_ux(args)
    if args.portfolio:
        return cmd_portfolio(args)
    if args.resolve_outcomes:
        return cmd_resolve(args)
    if args.ticker_pos:
        args.ticker = args.ticker_pos
        return cmd_single(args)

    build_parser().print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())