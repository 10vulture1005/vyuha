#!/usr/bin/env python
"""Step-by-step runner for both VYUHA pipelines.

The production entrypoints (``scripts/run_daily_pipeline.py`` and
``vyuha invest daily``) execute every stage in a single process. This module
exposes the *same* stages as individually invokable steps so you can run,
inspect and stop between any two of them.

Design notes
------------
* One stage per process. Each step opens its own DB session, so state you
  cannot see in the previous step's output is lost. Use the ``inspect``
  stage between steps to see what actually landed in the DB.
* Stages that mutate the ledger (SIP credit, capital allocation, paper
  fills) are NOT idempotent. Re-running one credits or spends real ledger
  rows. They are marked ``mutating`` in ``--list`` for that reason.
* Investor Mode keeps ``_pending_orders`` in memory only, so a
  decide/fill pair split across two processes would silently drop every
  order. This script checkpoints the decision to ``.state/`` and restores
  it in the ``fill`` step, which is what makes the split safe.

Usage
-----
    python scripts/run_step_by_step.py --list
    python scripts/run_step_by_step.py swing     --stage technical
    python scripts/run_step_by_step.py swing     --stage all
    python scripts/run_step_by_step.py swing     --stage inspect
    python scripts/run_step_by_step.py investor  --stage decide
    python scripts/run_step_by_step.py investor  --stage fill
"""
from __future__ import annotations

import argparse
import json
import pickle
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import BASE_DIR, settings  # noqa: E402

STATE_DIR = BASE_DIR / ".state"
PENDING_FILE = STATE_DIR / "investor_pending.pkl"
EVENTS_FILE = STATE_DIR / "investor_news.pkl"
DECISION_FILE = STATE_DIR / "investor_decision.pkl"


def _hr(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _ok(msg: str) -> None:
    print(f"  [ok]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


# ── shared inspection stage ─────────────────────────────────────────────────

def stage_inspect(_args) -> int:
    """Read-only snapshot of everything the pipeline writes to."""
    from db.models import (
        FundamentalSnapshot,
        InvestorTarget,
        PortfolioHolding,
        SentimentFlag,
        TechnicalSignal,
        Watchlist,
        WatchlistStatus,
    )
    from db.session import get_session

    _hr("INSPECT — pipeline state")
    print(f"  strategy mode   : {settings.STRATEGY_MODE}")
    print(f"  live trading    : {settings.LIVE_TRADING_ENABLED}")
    print(f"  initial capital : ₹{settings.INITIAL_CAPITAL:,.0f}")
    print(f"  monthly SIP     : ₹{settings.MONTHLY_SIP_AMOUNT:,.0f}")
    print(f"  tradingagents   : {settings.TRADINGAGENTS_ENABLED}")

    ohlc_dir = BASE_DIR / "data" / "raw" / "ohlc"
    if ohlc_dir.exists():
        files = sorted(ohlc_dir.glob("*.csv"))
        idx = [f.stem for f in files if f.stem.startswith("INDEX_")]
        print(f"  OHLC files      : {len(files)} ({len(idx)} index)")

    with get_session() as s:
        active = s.query(Watchlist).filter(
            Watchlist.status == WatchlistStatus.ACTIVE.value
        ).count()
        vetoed = s.query(Watchlist).filter(
            Watchlist.status == WatchlistStatus.VETOED.value
        ).count()
        print(f"  Watchlist       : {active} ACTIVE / {vetoed} VETOED")
        print(f"  SentimentFlag   : {s.query(SentimentFlag).count()} rows")
        print(f"  FundamentalSnap : {s.query(FundamentalSnapshot).count()} rows")
        print(f"  TechnicalSignal : {s.query(TechnicalSignal).count()} rows")
        print(f"  PortfolioHolding: {s.query(PortfolioHolding).count()} rows")

        rows = (
            s.query(Watchlist.symbol, Watchlist.conviction_score)
            .filter(Watchlist.status == WatchlistStatus.ACTIVE.value)
            .all()
        )
        if rows:
            rows.sort(key=lambda r: float(r[1] or 0), reverse=True)
            print("\n  ACTIVE watchlist by conviction:")
            for sym, score in rows[:15]:
                print(f"    {sym:<16} {float(score or 0):6.2f}")

        cash = _current_cash(s)
        print(f"\n  Ledger cash     : ₹{cash:,.2f}")
        print(f"  Cash today      : {date.today()}  (day {date.today().day})")

        tgt = s.query(InvestorTarget).order_by(InvestorTarget.as_of.desc()).first()
        if tgt is not None:
            print(f"  Latest targets  : as_of={tgt.as_of}")

    return 0


def _current_cash(session) -> float:
    from core.capital_allocator import get_current_cash
    return float(get_current_cash(session))


# ── swing pipeline stages ───────────────────────────────────────────────────

def stage_preflight(_args) -> int:
    """Verify the DB is migrated and the LLM route is reachable-ish."""
    _hr("SWING / preflight")
    import alembic.command
    import alembic.config
    from db.session import get_session
    from sqlalchemy import text

    cfg = alembic.config.Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "db" / "migrations"))
    try:
        alembic.command.current(cfg, verbose=False)
        _ok("alembic reachable")
    except Exception as exc:
        _warn(f"alembic check skipped: {exc}")

    try:
        with get_session() as s:
            s.execute(text("SELECT 1"))
        _ok("vyuha.db reachable")
    except Exception as exc:
        _fail(f"vyuha.db unreachable — run `alembic upgrade head`: {exc}")
        return 1

    from config.llm_routing import fallback_route, primary_route
    p, f = primary_route(), fallback_route()
    _ok(f"primary LLM : {p.provider}/{p.model} usable={p.is_usable}")
    _ok(f"fallback LLM: {f'{f.provider}/{f.model}' if f else 'none'}")

    for name in ("fundamentals_investor.csv", "fundamentals.csv"):
        path = BASE_DIR / "data" / "raw" / name
        if path.exists():
            n = sum(1 for _ in path.open()) - 1
            _ok(f"{name}: {n} rows")
        else:
            _warn(f"{name} missing — fundamental scans will fail closed")
    return 0


def stage_ohlc(_args) -> int:
    """Refresh 10y daily OHLCV for indices + ACTIVE watchlist (network)."""
    _hr("SWING / ohlc — download daily OHLCV")
    from scripts.ingest_ohlc import ingest_active_ohlc, OHLC_DIR

    before = {p.stem: p.stat().st_mtime for p in OHLC_DIR.glob("*.csv")}
    ingest_active_ohlc()
    after = {p.stem: p.stat().st_mtime for p in OHLC_DIR.glob("*.csv")}
    changed = sorted(k for k, m in after.items() if before.get(k) != m)
    _ok(f"{len(changed)} files refreshed of {len(after)} total")
    for k in changed[:20]:
        _ok(f"  refreshed {k}")
    return 0


def stage_fundamentals_fetch(args) -> int:
    """Fetch/merge verified Screener.in statements (network)."""
    _hr("SWING / fundamentals-fetch — Screener.in statements")
    cmd = [sys.executable, str(BASE_DIR / "scripts" / "fetch_screener_snapshots.py")]
    if getattr(args, "refresh", False):
        cmd.append("--refresh")
    if getattr(args, "since", None):
        cmd += ["--since", args.since]
    print("  $ " + " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(BASE_DIR))
    if rc != 0:
        _warn(f"fetch exited {rc} (some symbols may have failed)")
    return 0


def stage_sip(args) -> int:
    """Credit the monthly SIP. MUTATING — credits real ledger cash."""
    _hr("SWING / sip — monthly SIP credit  [MUTATING]")
    from core.capital_allocator import credit_monthly_sip_execution

    amt = args.amount if args.amount is not None else settings.MONTHLY_SIP_AMOUNT
    print(f"  amount: ₹{amt:,.0f}   today is day {date.today().day}")
    if date.today().day != 1 and not args.force:
        _warn("not the 1st — production pipeline would skip. Re-run with --force to credit anyway.")
        return 0
    credited = credit_monthly_sip_execution(amt)
    _ok(f"credited ₹{float(credited):,.2f}")
    return 0


def stage_watchlist(args) -> int:
    """Phase 2 — fundamental hard filters → ACTIVE watchlist (weekly)."""
    _hr("SWING / watchlist — Phase 2 fundamental scan")
    from agents.fundamental_agent import FundamentalAgent

    symbols = FundamentalAgent().generate_watchlist_execution(
        use_adaptive=not args.no_adaptive, top_n=args.top_n
    )
    _ok(f"{len(symbols)} ACTIVE symbols by conviction")
    for i, s in enumerate(symbols[:20], 1):
        print(f"    {i:>2}. {s}")
    return 0


def stage_sentiment(_args) -> int:
    """Phase 3 — governance/news veto over the ACTIVE watchlist (weekly)."""
    _hr("SWING / sentiment — Phase 3 governance veto")
    from agents.sentiment_agent import run_sentiment_pass_execution
    from db.models import SentimentFlag, Watchlist, WatchlistStatus
    from db.session import get_session

    # Snapshot the ACTIVE set BEFORE scanning, then diff. Deriving "vetoed"
    # from the current table instead would sweep up rows a PREVIOUS run left
    # as VETOED and report them as this run's vetoes.
    with get_session() as s:
        scanned = set(
            r[0] for r in s.query(Watchlist.symbol)
            .filter(Watchlist.status == WatchlistStatus.ACTIVE.value).all()
        )
    survivors = run_sentiment_pass_execution()
    with get_session() as s:
        reasons = {
            r[0]: r[1] for r in s.query(SentimentFlag.symbol, SentimentFlag.reason)
            .filter(SentimentFlag.red_flag.is_(True)).all()
        }
    surviving = set(survivors)
    vetoed = sorted(scanned - surviving)
    _ok(f"{len(survivors)}/{len(scanned)} survived governance vetting")
    for sym in vetoed:
        print(f"    VETOED {sym}: {reasons.get(sym, '(no reason recorded)')}")
    if not vetoed:
        _ok("no vetoes this run")
    return 0


def stage_technical(_args) -> int:
    """Phase 4 — W-Bottom / BB-Squeeze timing scan over fresh watchlist."""
    _hr("SWING / technical — Phase 4 pattern scan")
    from agents.technical_agent import TechnicalAgent

    signals = TechnicalAgent().run_technical_scan_execution()
    if not signals:
        _warn("0 signals. Either the regime gate blocked, the watchlist is "
              "stale (>7d, rerun --stage watchlist), or no pattern formed.")
        return 0
    for s in signals:
        print(f"    {s.symbol:<14} {s.pattern_type.value:<12} strength={s.signal_strength} "
              f"entry={s.entry_price} stop={s.structural_stop_price} atr={s.atr_14}")
    _ok(f"{len(signals)} signal(s)")
    return 0


def stage_risk(_args) -> int:
    """Phase 5 — trailing-stop / degradation exits on OPEN holdings. MUTATING."""
    _hr("SWING / risk — Phase 5 exit review  [MUTATING]")
    from agents.risk_exit_agent import RiskExitAgent

    sells = RiskExitAgent().evaluate_exits()
    if not sells:
        _ok("no stops breached, no degradation exits")
        return 0
    for s in sells:
        print(f"    SELL {getattr(s, 'symbol', s)} — {getattr(s, 'reason', '')}")
    _ok(f"{len(sells)} exit(s) executed")
    return 0


def _alloc_state() -> Path:
    return STATE_DIR / "swing_alloc.pkl"


def _load_alloc_decision():
    p = _alloc_state()
    if not p.exists():
        return None
    with p.open("rb") as fh:
        return pickle.load(fh)


def stage_alloc(args) -> int:
    """Phase 6 — capital allocation decision. MUTATING (executes buys)."""
    _hr("SWING / alloc — Phase 6 capital allocator  [MUTATING]")
    from core.capital_allocator import select_and_execute_buy_candidate

    decision = select_and_execute_buy_candidate(
        signal_date=args.date, circuit_breaker_active=args.circuit_breaker
    )
    payload = json.loads(decision.model_dump_json())
    print(json.dumps(payload, indent=2, default=str))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _alloc_state().open("wb") as fh:
        pickle.dump(payload, fh)
    _ok(f"checkpointed decision → {_alloc_state()}")
    return 0


def stage_valuation(_args) -> int:
    """Append today's mark-to-market point to the equity curve."""
    _hr("SWING / valuation — record daily NAV")
    from core.paper_engine import ForwardTestEngine

    try:
        engine = ForwardTestEngine()
    except RuntimeError as exc:
        _warn(f"skipped: {exc}")
        return 0
    engine._record_daily_valuation()
    _ok("valuation recorded")
    return 0


def stage_digest(args) -> int:
    """Send the Telegram digest. Requires a decision payload."""
    _hr("SWING / digest — Telegram")
    from notifications.digest_builder import build_daily_digest, send_daily_digest_execution

    payload = _load_alloc_decision()
    if payload is None:
        _warn("no checkpointed allocation decision found — run `--stage alloc` first.")
        print("  dry-run preview of the default HOLD_CASH digest:")
        print(build_daily_digest({"action": "HOLD_CASH", "rationale": "preview"}))
        return 1
    if args.dry_run:
        print(build_daily_digest(payload))
        return 0
    sent = send_daily_digest_execution(payload)
    _ok("digest sent" if sent else "digest not sent (check TELEGRAM_BOT_TOKEN/CHAT_ID)")
    return 0


def stage_crosscheck(_args) -> int:
    """Phase 7 — optional LLM cross-check. Needs an LLM key; never fatal."""
    _hr("SWING / crosscheck — Phase 7 Vyuha Agent")
    if not settings.TRADINGAGENTS_ENABLED:
        _warn("TRADINGAGENTS_ENABLED is false — production skips this too.")
    from agents.llm_crosscheck_agent import run_cross_check_execution

    try:
        confirmed = run_cross_check_execution()
        _ok(f"confirmed: {confirmed}")
    except Exception as exc:
        _warn(f"cross-check failed (non-fatal in production too): {exc}")
    return 0


SWING_STAGES = [
    ("preflight", stage_preflight, "Verify DB migration + LLM routing + data files", False),
    ("ohlc", stage_ohlc, "Download 10y daily OHLCV for indices + ACTIVE watchlist", False),
    ("fundamentals-fetch", stage_fundamentals_fetch, "Fetch/merge Screener.in statements", False),
    ("sip", stage_sip, "Credit monthly SIP (only on the 1st)", True),
    ("watchlist", stage_watchlist, "Phase 2: fundamental filters → ACTIVE watchlist", False),
    ("sentiment", stage_sentiment, "Phase 3: governance/news veto", False),
    ("technical", stage_technical, "Phase 4: W-Bottom / BB-Squeeze timing", False),
    ("risk", stage_risk, "Phase 5: trailing-stop & degradation exits", True),
    ("alloc", stage_alloc, "Phase 6: capital allocation decision", True),
    ("valuation", stage_valuation, "Append today's NAV point to equity curve", False),
    ("digest", stage_digest, "Send Telegram digest", False),
    ("crosscheck", stage_crosscheck, "Phase 7: optional LLM cross-check", False),
    ("inspect", stage_inspect, "Read-only state dump (works between any steps)", False),
]


# ── investor pipeline stages ────────────────────────────────────────────────

def _investor_engine():
    from core.investor_engine import InvestorModeEngine
    return InvestorModeEngine()


def stage_inv_preflight(_args) -> int:
    _hr("INVESTOR / preflight")
    if settings.STRATEGY_MODE != "investor":
        _fail(f"STRATEGY_MODE={settings.STRATEGY_MODE!r}. Investor commands require "
              "`STRATEGY_MODE=investor` in .env (see scripts/invest_cli.py).")
        return 1
    _ok("investor mode active")
    from investor.data import PricePanel, PointInTimeFundamentals
    panel = PricePanel()
    syms = panel.available_symbols()
    _ok(f"price panel: {len(syms)} symbols")
    store = PointInTimeFundamentals()
    _ok(f"fundamental store: {len(store.records)} symbols, source={store.source!r}")
    eng = _investor_engine()
    _ok(f"universe: {len(eng.universe)} symbols")
    _ok(f"open positions restored: {len(eng.engine.positions)}")
    return 0


def stage_inv_sip(args) -> int:
    """Monthly SIP credit. MUTATING."""
    _hr("INVESTOR / sip — monthly SIP credit  [MUTATING]")
    import pandas as pd
    from core.capital_allocator import credit_monthly_sip_execution

    eng = _investor_engine()
    as_of = pd.Timestamp(args.date) if args.date else eng._latest_trading_day()
    if not eng._should_credit_sip(as_of) and not args.force:
        _warn(f"SIP for {as_of:%Y-%m} already credited (ledger check) — skipping.")
        return 0
    credit_monthly_sip_execution(settings.MONTHLY_SIP_AMOUNT)
    _ok(f"credited ₹{settings.MONTHLY_SIP_AMOUNT:,.0f} for {as_of:%Y-%m}")
    return 0


def stage_inv_news(args) -> int:
    """Fetch + classify headlines, persist to NewsEventRow. Uses FinBERT/LLM."""
    _hr("INVESTOR / news — collect & classify headlines  [uses LLM/FinBERT]")
    import pandas as pd
    from db.session import get_session
    from db.models import NewsEventRow

    eng = _investor_engine()
    as_of = pd.Timestamp(args.date) if args.date else eng._latest_trading_day()
    events = eng.collect_and_persist_news(as_of.date(), limit=args.limit)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("wb") as fh:
        pickle.dump({k: v for k, v in events.items()}, fh)
    total = sum(len(v) for v in events.values())
    _ok(f"{total} classified events across {len(events)} symbols")
    with get_session() as s:
        n = s.query(NewsEventRow).filter(NewsEventRow.is_proxy.is_(False)).count()
    _ok(f"NewsEventRow (is_proxy=False) total: {n}")
    return 0


def stage_inv_fill(args) -> int:
    """Paper-fill yesterday's orders at today's open. MUTATING.
    Reads the decision checkpointed by `decide`."""
    _hr("INVESTOR / fill — paper-fill pending orders  [MUTATING]")
    import pandas as pd

    if not PENDING_FILE.exists():
        _warn("no pending orders checkpointed — run `--stage decide` first, "
              "or accept that this day has nothing to fill.")
        return 0
    with PENDING_FILE.open("rb") as fh:
        orders = pickle.load(fh)
    if not orders:
        _warn("checkpointed decision contained 0 orders")
        return 0
    eng = _investor_engine()
    as_of = pd.Timestamp(args.date) if args.date else eng._latest_trading_day()
    filled = eng.paper_fill(orders, as_of)
    for f in filled:
        print("    " + json.dumps(f, default=str))
    _ok(f"{len(filled)}/{len(orders)} order(s) filled")
    PENDING_FILE.unlink(missing_ok=True)
    return 0


def stage_inv_decide(args) -> int:
    """Layers 0-3: regime → scoring → target weights → orders. Checkpoints orders."""
    _hr("INVESTOR / decide — evaluate_day (Layers 0-3)")
    import pandas as pd

    eng = _investor_engine()
    as_of = pd.Timestamp(args.date) if args.date else eng._latest_trading_day()

    events = None
    if EVENTS_FILE.exists() and not args.ignore_news:
        with EVENTS_FILE.open("rb") as fh:
            events = pickle.load(fh) or None

    equity, cash = eng._equity_and_cash()
    _ok(f"equity ₹{equity:,.2f} | cash ₹{cash:,.2f}")

    decision = eng.engine.evaluate_day(
        as_of, equity=equity, cash=cash, external_news=events
    )
    _ok(f"regime={decision.regime}  exposure={decision.exposure:.2%}  "
        f"rebalance={decision.is_rebalance}")
    _ok(f"eligible universe: {len(decision.eligible)}")
    print("\n  target weights:")
    for sym, w in sorted(decision.target_weights.items(), key=lambda kv: -kv[1]):
        print(f"    {sym:<14} {w:7.2%}")
    for sym, w in decision.defensive_targets.items():
        print(f"    {sym:<14} {w:7.2%}  (defensive)")
    print(f"\n  orders: {len(decision.orders)}")
    for o in decision.orders:
        print(f"    {o.symbol:<14} {o.side:<5} qty={o.qty} @ {o.price}")
    for n in decision.notes:
        print(f"    note: {n}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PENDING_FILE.open("wb") as fh:
        pickle.dump(list(decision.orders), fh)
    # persist/digest must NOT re-run evaluate_day: a second call re-reads
    # prices and can disagree with the orders already committed to disk.
    with DECISION_FILE.open("wb") as fh:
        pickle.dump({"decision": decision, "as_of": as_of, "equity": equity,
                     "cash": cash}, fh)
    _ok(f"checkpointed {len(decision.orders)} pending order(s) → {PENDING_FILE}")
    return 0


def _load_decision():
    if not DECISION_FILE.exists():
        return None
    with DECISION_FILE.open("rb") as fh:
        return pickle.load(fh)


def stage_inv_persist(args) -> int:
    """Write today's target book + rebalance log. Requires `decide` output."""
    _hr("INVESTOR / persist — target book & rebalance log")
    import pandas as pd

    saved = _load_decision()
    if saved is None:
        _warn("no decision checkpointed — run `--stage decide` first.")
        return 1
    eng = _investor_engine()
    decision, as_of = saved["decision"], saved["as_of"]
    eng.persist_targets(decision, as_of.date())
    _ok(f"persisted {len(decision.target_weights)} targets as_of={as_of.date()}")

    if args.persist_fundamentals:
        n = eng.persist_fundamentals(as_of.date())
        _ok(f"snapshotted {n} fundamental records")
    return 0


def stage_inv_digest(args) -> int:
    """Build + send the Investor Mode Telegram digest."""
    _hr("INVESTOR / digest — Telegram")
    from notifications.digest_builder import build_investor_digest, send_investor_digest

    saved = _load_decision()
    if saved is None:
        _warn("no decision checkpointed — run `--stage decide` first.")
        return 1
    eng = _investor_engine()
    run = _rebuild_daily_run(eng, saved)
    msg = build_investor_digest(run)
    if args.dry_run:
        print(msg)
        return 0
    _ok("digest sent" if send_investor_digest(msg) else "digest not sent")
    return 0


def _rebuild_daily_run(eng, saved):
    """Assemble the DailyRun object digest_builder expects, without refetching news."""
    from core.investor_engine import DailyRun
    decision, as_of = saved["decision"], saved["as_of"]
    events = []
    if EVENTS_FILE.exists():
        with EVENTS_FILE.open("rb") as fh:
            evs = pickle.load(fh)
        events = [e.as_row(as_of.date()) for v in evs.values() for e in v][:200]
    holdings = {}
    for sym, pos in eng.engine.positions.items():
        px = eng._last_close(sym) or pos.avg_price
        holdings[sym] = round(pos.market_value(px), 2)
    return DailyRun(
        as_of=as_of, regime=decision.regime, exposure=decision.exposure,
        equity=saved["equity"], cash=saved["cash"], targets=decision.target_weights,
        defensive=decision.defensive_targets, orders=list(decision.orders),
        filled=[], holdings=holdings, eligible=list(decision.eligible),
        news=events, is_rebalance=decision.is_rebalance, notes=list(decision.notes),
    )


def stage_inv_status(_args) -> int:
    """Human-readable book snapshot. Equivalent to `vyuha invest status`."""
    _hr("INVESTOR / status")
    sys.argv = ["vyuha", "invest", "status"]
    from scripts.invest_cli import main as invest_main
    return invest_main(["status"])


INVESTOR_STAGES = [
    ("preflight", stage_inv_preflight, "Verify investor mode + data stores", False),
    ("sip", stage_inv_sip, "Monthly SIP credit (ledger-guarded)", True),
    ("news", stage_inv_news, "Fetch + classify headlines, persist (LLM/FinBERT)", False),
    ("fill", stage_inv_fill, "Paper-fill pending orders at today's open", True),
    ("decide", stage_inv_decide, "Layers 0-3: regime, scoring, targets, orders", False),
    ("persist", stage_inv_persist, "Write target book + rebalance log", False),
    ("digest", stage_inv_digest, "Build + send Investor Telegram digest", False),
    ("status", stage_inv_status, "Read-only book snapshot", False),
    ("inspect", stage_inspect, "Read-only state dump", False),
]

PIPELINES = {"swing": SWING_STAGES, "investor": INVESTOR_STAGES}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _print_timings(timings) -> None:
    """Elapsed wall-clock per stage, slowest first — the cost breakdown."""
    total = sum(t for _, t in timings)
    print(f"\n{'-' * 70}\n  STAGE TIMINGS (total {total:.1f}s)\n{'-' * 70}")
    for name, secs in sorted(timings, key=lambda kv: -kv[1]):
        pct = (secs / total * 100) if total else 0.0
        print(f"  {name:<20}{secs:>8.2f}s  {pct:>5.1f}%")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pipeline", nargs="?", choices=sorted(PIPELINES),
                    help="which pipeline to step through")
    ap.add_argument("--stage", help="stage name, or 'all'")
    ap.add_argument("--list", action="store_true", help="list stages and exit")
    ap.add_argument("--date", help="ISO date override (investor stages)")
    ap.add_argument("--amount", type=float, help="SIP amount override")
    ap.add_argument("--top-n", type=int, default=150, help="watchlist universe size")
    ap.add_argument("--no-adaptive", action="store_true")
    ap.add_argument("--limit", type=int, help="cap symbols for news collection")
    ap.add_argument("--ignore-news", action="store_true",
                    help="decide without the checkpointed news events")
    ap.add_argument("--circuit-breaker", action="store_true",
                    help="force the drawdown breaker on for the allocator")
    ap.add_argument("--force", action="store_true", help="bypass day-of-month guards")
    ap.add_argument("--refresh", action="store_true",
                    help="fundamentals-fetch: re-download instead of replaying cache")
    ap.add_argument("--since", help="fundamentals-fetch: only periods on/after this date")
    ap.add_argument("--dry-run", action="store_true", help="print digest, do not send")
    ap.add_argument("--persist-fundamentals", action="store_true")
    args = ap.parse_args(argv)

    stages = PIPELINES.get(args.pipeline or "swing")

    if args.list or args.stage is None:
        if args.pipeline is None and not args.list:
            print("usage: run_step_by_step.py {swing|investor} --stage <name|all>")
            print("       run_step_by_step.py --list")
            print()
        for name in ("swing", "investor"):
            print(f"\n{name.upper()} PIPELINE")
            for sname, _fn, desc, mut in PIPELINES[name]:
                flag = "  [MUTATING]" if mut else ""
                print(f"  {sname:<20}{desc}{flag}")
        return 0

    by_name = {s[0]: s[1] for s in stages}
    if args.stage == "all":
        order = [s[0] for s in stages if s[0] not in ("inspect", "crosscheck", "status")]
        timings = []
        for name in order:
            started = time.perf_counter()
            rc = by_name[name](args)
            timings.append((name, time.perf_counter() - started))
            if rc != 0:
                _fail(f"stage '{name}' returned {rc}; stopping.")
                _print_timings(timings)
                return rc
        stage_inspect(args)
        _print_timings(timings)
        return 0

    if args.stage not in by_name:
        print(f"unknown stage '{args.stage}'. Valid: "
              f"{[s[0] for s in stages]} or 'all'", file=sys.stderr)
        return 2

    started = time.perf_counter()
    rc = by_name[args.stage](args)
    _print_timings([(args.stage, time.perf_counter() - started)])
    return rc


if __name__ == "__main__":
    sys.exit(main())