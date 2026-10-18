# core/investor_engine.py
"""Investor Mode live orchestrator — daily signals, targets, orders, paper fills.

This is the live counterpart of `backtest_investor/engine.py`. Both drive the
SAME `investor.pipeline.InvestorEngine`, so the rules cannot drift between
paper and backtest — the failure mode this repo already paid for once, when
`backtest/engine.py` and `core/paper_engine.py` disagreed about stops.

Safety:
    `run_daily` refuses to place anything with a broker. LIVE_TRADING_ENABLED
    stays false and the paper path writes to the same DB tables the dashboard
    reads, so forward-test performance is visible without risking capital.

What it does each day:
    1. Credit the monthly SIP on the first trading day
    2. Classify and PERSIST every headline for holdings + watchlist
    3. Run Layers 0-3 against the persisted news
    4. Write target weights + a rebalance log row when it rebalances
    5. Emit an orders list for the NEXT open
    6. Paper-fill yesterday's orders at today's open
    7. Send the Telegram digest
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
from loguru import logger

from config.settings import settings
from db.models import (
    CapitalLedger, InvestorFundamentalSnapshot, InvestorTarget, LedgerTxnType,
    NewsEventRow, PortfolioHolding, RebalanceLog, TradeLog, TradeTxnType,
    Universe, Watchlist, WatchlistStatus,
)
from db.session import get_session
from investor.config import investor_config
from investor.data import PricePanel, PointInTimeFundamentals, SectorMap
from investor.indicators import IndicatorCache
from investor.pipeline import InvestorEngine, Order
from investor import news as news_mod


# ─────────────────────────────────────────────────────────────────────────────
# Live state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LivePosition:
    """An open Investor Mode position, reconstructed from the DB."""

    symbol: str
    qty: int
    avg_price: float
    first_buy_date: pd.Timestamp
    trailing_stop: Optional[float]
    tranche1_done: bool = True
    tranche1_date: Optional[pd.Timestamp] = None
    tranche2_done: bool = False
    score: float = 0.0


@dataclass
class DailyRun:
    """Everything one `vyuha invest daily` produced."""

    as_of: pd.Timestamp
    regime: str = ""
    exposure: float = 0.0
    equity: float = 0.0
    cash: float = 0.0
    targets: Dict[str, float] = field(default_factory=dict)
    defensive: Dict[str, float] = field(default_factory=dict)
    orders: List[Order] = field(default_factory=list)
    filled: List[Dict[str, Any]] = field(default_factory=list)
    holdings: Dict[str, float] = field(default_factory=dict)
    eligible: List[str] = field(default_factory=list)
    news: List[Dict[str, Any]] = field(default_factory=list)
    is_rebalance: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of,
            "regime": self.regime,
            "exposure": self.exposure,
            "equity": self.equity,
            "cash": self.cash,
            "holdings": self.holdings,
            "targets": self.targets,
            "defensive": self.defensive,
            "eligible": self.eligible,
            "orders": [o.as_dict() for o in self.orders],
            "filled": self.filled,
            "news": self.news,
            "is_rebalance": self.is_rebalance,
            "notes": self.notes,
        }

    def summary_lines(self) -> List[str]:
        lines = [
            f"Investor Mode — {self.as_of.date()}",
            f"  Regime      : {self.regime} (equity target {self.exposure:.0%})",
            f"  Equity      : ₹{self.equity:,.0f}  (cash ₹{self.cash:,.0f})",
            f"  Holdings    : {len(self.holdings)}",
        ]
        if self.defensive:
            sleeve = ", ".join(f"{k} {v:.1%}" for k, v in self.defensive.items())
            lines.append(f"  Defensive   : {sleeve}")
        lines.append(f"  Eligible    : {len(self.eligible)}")
        if self.is_rebalance:
            lines.append("  Rebalance   : YES (monthly rebalance day)")
        if self.orders:
            lines.append(f"  Orders for next open ({len(self.orders)}):")
            for o in self.orders:
                lines.append(
                    f"    {o.side:<4} {o.qty:>6} {o.symbol:<12} "
                    f"~₹{o.limit_hint:,.1f}  [{o.layer}] {o.reason[:60]}"
                )
        else:
            lines.append("  Orders      : none")
        if self.filled:
            lines.append(f"  Paper fills : {len(self.filled)}")
        return lines


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class InvestorModeEngine:
    """Daily Investor Mode cycle: news -> signals -> targets -> orders -> paper."""

    def __init__(
        self,
        panel: Optional[PricePanel] = None,
        store: Optional[PointInTimeFundamentals] = None,
        sector_map: Optional[SectorMap] = None,
        universe: Optional[Sequence[str]] = None,
        paper: bool = True,
    ):
        if not paper and settings.LIVE_TRADING_ENABLED:
            raise RuntimeError(
                "CRITICAL: InvestorModeEngine refuses live execution while "
                "LIVE_TRADING_ENABLED is true."
            )
        self.paper = paper
        self.panel = panel or PricePanel()
        self.store = store or PointInTimeFundamentals()
        self.sectors = sector_map or SectorMap()
        self.universe = list(universe) if universe else self._default_universe()
        self.cache = IndicatorCache(
            self.panel, investor_config.regime.get("nifty_proxy", "INDEX_NIFTY500")
        )
        self.engine = InvestorEngine(
            panel=self.panel, store=self.store, sector_map=self.sectors,
            universe=self.universe, cache=self.cache,
        )
        # Orders decided on the previous close, awaiting today's open.
        self._pending_orders: List[Order] = []
        self._last_sip_month: Optional[Tuple[int, int]] = None
        self._restore_positions()

    # ── universe ────────────────────────────────────────────────────────────
    def _default_universe(self) -> List[str]:
        """Watchlist ∪ invested universe ∪ OHLC symbols with price data.

        Prefers the DB watchlist (what the user actually tracks) and falls back
        to everything in the OHLC archive so a fresh install still runs.
        """
        syms: List[str] = []
        try:
            with get_session() as session:
                rows = session.query(Watchlist.symbol).filter(
                    Watchlist.status == WatchlistStatus.ACTIVE.value
                ).all()
                syms = [r[0] for r in rows]
                rows = session.query(Universe.symbol).filter(
                    Universe.is_active.is_(True)
                ).all()
                syms += [r[0] for r in rows]
        except Exception as exc:
            logger.debug(f"DB universe unavailable ({exc}); using OHLC archive")
        available = set(self.panel.available_symbols())
        out = [s for s in dict.fromkeys(syms) if s in available]
        if len(out) < 20:
            out = sorted(available - {"INDEX_NIFTY50", "INDEX_NIFTY500", "INDEX_INDIAVIX"})
        return out

    # ── positions ───────────────────────────────────────────────────────────
    def _restore_positions(self) -> None:
        """Rebuilds engine state from the DB so the 60-day floor, Chandelier
        ratchet and tranche flags survive a process restart."""
        from investor.pipeline import Position

        try:
            with get_session() as session:
                holdings = session.query(PortfolioHolding).filter(
                    PortfolioHolding.status == "OPEN"
                ).all()
                for h in holdings:
                    pos = Position(
                        symbol=h.symbol,
                        qty=int(h.qty),
                        avg_price=float(h.avg_buy_price),
                        first_buy_date=pd.Timestamp(h.first_buy_date),
                        highest_close=float(h.avg_buy_price),
                        stop=float(h.trailing_stop_price) if h.trailing_stop_price else None,
                        tranche1_date=pd.Timestamp(h.first_buy_date),
                        tranche1_done=True,
                        sector=self.sectors.get(h.symbol),
                    )
                    self.engine.positions[h.symbol] = pos
                    # Lift the stop anchor to the highest close seen so far;
                    # without it a restart would reset the Chandelier ratchet
                    # to the entry price and immediately widen the stop.
                    df = self.panel.get(h.symbol)
                    if not df.empty:
                        pos.highest_close = float(df["Close"].max())
        except Exception as exc:
            logger.warning(f"Could not restore investor positions: {exc}")

    def _equity_and_cash(self) -> Tuple[float, float]:
        from core.capital_allocator import get_current_cash

        with get_session() as session:
            cash = float(get_current_cash(session))
        invested = 0.0
        for sym, pos in self.engine.positions.items():
            px = self._last_close(sym)
            if px is None:
                # No usable recent price: value at cost rather than at a stale
                # close from a suspended stock.
                px = pos.avg_price
            invested += px * pos.qty
        return cash + invested, cash

    def _last_close(self, symbol: str, max_stale_days: int = 7) -> Optional[float]:
        """Most recent close, or None if it is too stale to size the book on."""
        frame = self.cache.frame(symbol)
        if frame.empty:
            return None
        age = (pd.Timestamp.utcnow().normalize().tz_localize(None) - frame.index[-1]).days
        if age > max_stale_days:
            logger.debug(f"{symbol}: last close is {age}d old — treating as unpriceable")
            return None
        return float(frame["Close"].iloc[-1])

    # ── news ────────────────────────────────────────────────────────────────
    def collect_and_persist_news(
        self, trade_date: date_cls, limit: Optional[int] = None
    ) -> Dict[str, List[news_mod.NewsEvent]]:
        """Fetches, classifies and PERSISTS headlines for holdings + watchlist.

        Persisting everything is what makes a real news backtest possible later.
        Rows are written with `is_proxy=False`; only the backtest writes
        proxy rows, so the two can never be confused.
        """
        from agents.tools.news_tools import fetch_marketaux_batch, fetch_drishti_batch

        watch = list(self.engine.positions.keys())
        watch += [s for s in self.eligible_watchlist() if s not in watch]
        if limit:
            watch = watch[:limit]

        out: Dict[str, List[news_mod.NewsEvent]] = {}
        rows: List[NewsEventRow] = []
        # Batched providers only — the per-symbol Google News RSS path has
        # been removed. One call covers the whole watch list rather than one
        # scrape per symbol.
        by_symbol: Dict[str, list] = {}
        try:
            by_symbol.update(fetch_marketaux_batch(watch, lookback_days=2))
            by_symbol.update(fetch_drishti_batch(watch, lookback_days=2))
        except Exception as exc:
            logger.warning(f"Batched news fetch failed: {exc}")

        for sym in watch:
            headlines = by_symbol.get(sym) or []
            if not headlines:
                logger.debug(f"No provider headlines for {sym}; not persisted.")
                continue
            events = [
                news_mod.classify_headline(sym, h, use_finbert=True, use_llm=True)
                for h in headlines
            ]
            events = [e for e in events if e.title]
            if not events:
                continue
            out[sym] = events
            for e in events:
                rows.append(NewsEventRow(
                    trade_date=trade_date,
                    symbol=sym,
                    title=e.title[:2000],
                    url=(e.url or "")[:1000] or None,
                    source=(e.source or "")[:100] or None,
                    published_at=e.published_at,
                    event_type=e.event_type,
                    subtype=(e.subtype or "")[:60] or None,
                    materiality=Decimal(str(round(float(e.materiality), 4))),
                    sentiment=Decimal(str(round(float(e.sentiment), 4))),
                    finbert_score=(Decimal(str(round(float(e.finbert_score), 4)))
                                   if e.finbert_score is not None else None),
                    tier=e.tier,
                    is_macro=bool(e.is_macro),
                    is_proxy=False,
                ))

        if rows:
            try:
                with get_session() as session:
                    session.add_all(rows)
                logger.info(f"Persisted {len(rows)} classified headlines for {trade_date}")
            except Exception as exc:
                logger.error(f"Failed to persist news events: {exc}")
        return out

    def eligible_watchlist(self) -> List[str]:
        return list(self.engine.eligible) or list(self.universe[:40])

    # ── persistence ─────────────────────────────────────────────────────────
    def persist_targets(self, decision, as_of: date_cls) -> None:
        """Writes the daily target book and, on a rebalance day, a log row."""
        try:
            with get_session() as session:
                # Replace today's targets so a re-run is idempotent.
                session.query(InvestorTarget).filter(
                    InvestorTarget.as_of == as_of
                ).delete()
                scored = decision.scored
                for sym, w in decision.target_weights.items():
                    score = None
                    if scored is not None and sym in scored.index:
                        try:
                            score = Decimal(str(round(float(scored.loc[sym, "score"]), 4)))
                        except Exception:
                            score = None
                    session.add(InvestorTarget(
                        as_of=as_of,
                        symbol=sym,
                        target_weight=Decimal(str(round(float(w), 6))),
                        sector=self.sectors.get(sym)[:100],
                        fundamental_score=score,
                        is_defensive=False,
                    ))
                for sym, w in decision.defensive_targets.items():
                    session.add(InvestorTarget(
                        as_of=as_of,
                        symbol=sym,
                        target_weight=Decimal(str(round(float(w), 6))),
                        sector="DEFENSIVE",
                        is_defensive=True,
                    ))
                if decision.is_rebalance:
                    # One row per rebalance date. Re-running the same day must
                    # overwrite, not append — otherwise "what the book was
                    # told on the 1st" becomes ambiguous after a cron retry.
                    session.query(RebalanceLog).filter(
                        RebalanceLog.rebalance_date == as_of
                    ).delete()
                    session.add(RebalanceLog(
                        rebalance_date=as_of,
                        regime=decision.regime,
                        exposure=Decimal(str(round(float(decision.exposure), 4))),
                        equity=Decimal(str(round(float(decision.equity), 2))),
                        n_positions=len(decision.target_weights),
                        target_weights={k: round(float(v), 6)
                                        for k, v in decision.target_weights.items()},
                        defensive_weights={k: round(float(v), 6)
                                           for k, v in decision.defensive_targets.items()},
                        eligible=list(decision.eligible),
                        notes="; ".join(decision.notes)[:2000] or None,
                    ))
        except Exception as exc:
            logger.error(f"Failed to persist targets: {exc}")

    def persist_fundamentals(self, as_of: date_cls) -> int:
        """Snapshots the latest usable fundamental row per symbol into the DB."""
        rows: List[InvestorFundamentalSnapshot] = []
        try:
            for sym in self.universe:
                rec = self.store.as_of(sym, pd.Timestamp(as_of))
                if rec is None:
                    continue
                m = self.engine.fund_engine.metrics_for(sym, as_of)
                rows.append(InvestorFundamentalSnapshot(
                    symbol=sym,
                    period_end=rec.period_end.date(),
                    filing_date=rec.filing_date.date() if rec.filing_date is not None else None,
                    usable_from=rec.usable_from.date(),
                    frequency=rec.frequency,
                    roce=_dec(m.roce), roe=_dec(m.roe),
                    debt_to_equity=_dec(m.debt_to_equity, 4),
                    interest_coverage=_dec(m.interest_coverage),
                    ocf_to_pat=_dec(m.ocf_to_pat),
                    promoter_pledge_pct=_dec(m.promoter_pledge_pct),
                    revenue_cagr_3y=_dec(m.revenue_cagr_3y),
                    eps_cagr_3y=_dec(m.eps_cagr_3y),
                    loss_years_3y=m.loss_years_3y,
                    pe=_dec(m.pe), pb=_dec(m.pb),
                    data_source=(self.store.source or "")[:60] or None,
                ))
            if rows:
                with get_session() as session:
                    session.add_all(rows)
            return len(rows)
        except Exception as exc:
            logger.error(f"Failed to persist fundamental snapshots: {exc}")
            return 0

    # ── paper execution ─────────────────────────────────────────────────────
    def paper_fill(self, orders: Sequence[Order], as_of) -> List[Dict[str, Any]]:
        """Fills orders at TODAY'S open and writes them to TradeLog.

        Orders decided on the previous close arrive here; the fill price is
        this session's open, never the price that produced the signal.
        """
        from core.capital_allocator import get_current_cash
        from backtest_v2.costs.tax_and_fees import TransactionCostModel

        cm = TransactionCostModel()
        filled: List[Dict[str, Any]] = []
        day = pd.Timestamp(as_of)

        with get_session() as session:
            cash = float(get_current_cash(session))
            sells = [o for o in orders if o.side in ("SELL", "TRIM")]
            buys = [o for o in orders if o.side == "BUY"]

            for o in sells:
                bar = self._open_bar(o.symbol, day)
                if bar is None:
                    continue
                pos = self.engine.positions.get(o.symbol)
                if pos is None:
                    continue
                qty = min(o.qty, pos.qty)
                if qty <= 0:
                    continue
                notional = bar * qty
                costs = cm.calculate_sell_costs(notional).total
                net = notional - costs
                cash += net
                pos.qty -= qty
                if pos.qty <= 0:
                    self.engine.positions.pop(o.symbol, None)
                    h = session.query(PortfolioHolding).filter(
                        PortfolioHolding.symbol == o.symbol
                    ).first()
                    if h:
                        h.status = "CLOSED"
                        h.qty = 0
                else:
                    h = session.query(PortfolioHolding).filter(
                        PortfolioHolding.symbol == o.symbol
                    ).first()
                    if h:
                        h.qty = int(pos.qty)
                session.add(TradeLog(
                    symbol=o.symbol, txn_type=TradeTxnType.SELL.value, qty=qty,
                    price=Decimal(str(round(bar, 4))), txn_date=datetime.now(timezone.utc),
                    dp_charge=Decimal(str(round(costs, 2))),
                    friction_charge=Decimal(str(round(costs, 2))),
                    reason=f"[investor] {o.reason}"[:2000],
                    triggered_by="InvestorModeEngine",
                ))
                session.add(CapitalLedger(
                    amount=Decimal(str(round(net, 2))),
                    txn_type=LedgerTxnType.SELL_CREDIT.value,
                    running_balance=Decimal(str(round(cash, 2))),
                    txn_date=datetime.now(timezone.utc),
                ))
                filled.append({"symbol": o.symbol, "side": "SELL", "qty": qty,
                               "price": round(bar, 2), "reason": o.reason})

            for o in buys:
                bar = self._open_bar(o.symbol, day)
                if bar is None or bar <= 0:
                    continue
                qty = o.qty
                while qty > 0:
                    notional = bar * qty
                    costs = cm.calculate_buy_costs(notional).total
                    if notional + costs <= cash:
                        break
                    qty -= max(1, int(qty * 0.05))
                if qty <= 0:
                    continue
                notional = bar * qty
                costs = cm.calculate_buy_costs(notional).total
                cash -= (notional + costs)
                self.engine.apply_buy(o.symbol, qty, bar, day, tranche=o.tranche or 1)
                self._upsert_holding(session, o.symbol, qty, bar,
                                       pattern=o.layer or "TARGET")
                session.add(TradeLog(
                    symbol=o.symbol, txn_type=TradeTxnType.BUY.value, qty=qty,
                    price=Decimal(str(round(bar, 4))), txn_date=datetime.now(timezone.utc),
                    dp_charge=Decimal("0.0"),
                    friction_charge=Decimal(str(round(costs, 2))),
                    reason=f"[investor] {o.reason}"[:2000],
                    triggered_by="InvestorModeEngine",
                ))
                session.add(CapitalLedger(
                    amount=-Decimal(str(round(notional + costs, 2))),
                    txn_type=LedgerTxnType.BUY_DEBIT.value,
                    running_balance=Decimal(str(round(cash, 2))),
                    txn_date=datetime.now(timezone.utc),
                ))
                filled.append({"symbol": o.symbol, "side": "BUY", "qty": qty,
                               "price": round(bar, 2), "reason": o.reason})

        return filled

    def _upsert_holding(
        self, session, symbol: str, qty: int, price: float, pattern: str = "TARGET"
    ) -> None:
        """Mirrors a paper fill into `portfolio_holdings`.

        Investor Mode reuses this table so the dashboard and
        `_restore_positions` can see the book, but the stop stored here is the
        INITIAL one. The authoritative daily Chandelier stop lives in
        `investor.pipeline.Position`, which sees every intervening close; a
        restart re-derives it from the high-water mark.
        """
        holding = session.query(PortfolioHolding).filter(
            PortfolioHolding.symbol == symbol
        ).first()
        gross = Decimal(str(price)) * qty
        new_stop = Decimal(str(price)) - Decimal("3.5") * (Decimal(str(price)) * Decimal("0.03"))

        if holding is not None and holding.status == "OPEN":
            total_cost = Decimal(str(holding.avg_buy_price)) * Decimal(int(holding.qty)) + gross
            holding.qty += qty
            holding.initial_qty += qty
            holding.avg_buy_price = (total_cost / holding.qty).quantize(Decimal("0.0001"))
            holding.trailing_stop_price = Decimal(str(round(
                max(float(holding.trailing_stop_price), float(new_stop)), 4
            )))
        elif holding is not None:
            holding.qty = qty
            holding.initial_qty = qty
            holding.avg_buy_price = Decimal(str(round(price, 4)))
            holding.first_buy_date = date_cls.today()
            holding.trailing_stop_price = Decimal(str(round(new_stop, 4)))
            holding.initial_risk = Decimal(str(round(price - new_stop, 4)))
            holding.entry_pattern = (pattern or "TARGET")[:50]
            holding.status = "OPEN"
        else:
            session.add(PortfolioHolding(
                symbol=symbol, qty=qty, initial_qty=qty,
                avg_buy_price=Decimal(str(round(price, 4))),
                first_buy_date=date_cls.today(),
                trailing_stop_price=Decimal(str(round(new_stop, 4))),
                initial_risk=Decimal(str(round(price - new_stop, 4))),
                entry_pattern=(pattern or "TARGET")[:50],
                tiers_hit=[], status="OPEN",
            ))

    def _open_bar(self, symbol: str, day: pd.Timestamp) -> Optional[float]:
        df = self.panel.get(symbol, day)
        if df.empty or df.index[-1] != day:
            return None
        return float(df["Open"].iloc[-1])

    # ── the daily cycle ─────────────────────────────────────────────────────
    def run_daily(
        self,
        as_of: Optional[pd.Timestamp] = None,
        fetch_news: bool = True,
        send_telegram: bool = True,
        fill: bool = True,
    ) -> DailyRun:
        """One full Investor Mode day."""
        from investor import portfolio as pf_mod

        if as_of is None:
            as_of = self._latest_trading_day()
        as_of = pd.Timestamp(as_of)
        trade_date = as_of.date()

        # 1. Monthly SIP credit on the first trading day of a new month.
        try:
            if self._should_credit_sip(as_of):
                from core.capital_allocator import credit_monthly_sip_execution
                credit_monthly_sip_execution(settings.MONTHLY_SIP_AMOUNT)
                self._last_sip_month = (as_of.year, as_of.month)
        except Exception as exc:
            logger.warning(f"SIP credit skipped: {exc}")

        # 2. News: classify and persist BEFORE deciding, so Layer 3 sees it.
        events: Dict[str, List[news_mod.NewsEvent]] = {}
        if fetch_news:
            try:
                events = self.collect_and_persist_news(trade_date)
            except Exception as exc:
                logger.warning(f"News collection skipped: {exc}")

        # 3. Paper-fill any orders carried from yesterday BEFORE deciding today,
        #    so live ordering matches backtest ordering.
        filled: List[Dict[str, Any]] = []
        pending = list(self._pending_orders)
        self._pending_orders = []
        if fill:
            try:
                filled = self.paper_fill(pending, as_of)
            except Exception as exc:
                logger.error(f"Paper fill failed: {exc}")

        equity, cash = self._equity_and_cash()

        # 4. Layers 0-3.
        decision = self.engine.evaluate_day(
            as_of, equity=equity, cash=cash, external_news=events or None
        )

        # 5. Persist targets / rebalance log.
        self.persist_targets(decision, trade_date)

        self._pending_orders = list(decision.orders)

        holdings = {}
        for sym, pos in self.engine.positions.items():
            px = self._last_close(sym) or pos.avg_price
            holdings[sym] = round(pos.market_value(px), 2)

        run = DailyRun(
            as_of=as_of,
            regime=decision.regime,
            exposure=decision.exposure,
            equity=equity,
            cash=cash,
            targets=decision.target_weights,
            defensive=decision.defensive_targets,
            orders=list(decision.orders),
            filled=filled,
            holdings=holdings,
            eligible=list(decision.eligible),
            news=[e.as_row(trade_date) for evs in events.values() for e in evs][:200],
            is_rebalance=decision.is_rebalance,
            notes=list(decision.notes),
        )

        # 6. Telegram digest.
        if send_telegram:
            try:
                from notifications.digest_builder import build_investor_digest, \
                    send_investor_digest
                msg = build_investor_digest(run)
                send_investor_digest(msg)
            except Exception as exc:
                logger.debug(f"Investor digest not sent: {exc}")

        return run

    def _should_credit_sip(self, as_of: pd.Timestamp) -> bool:
        """True only if this month has not already been credited.

        Checks the LEDGER, not process memory. A per-process flag looks right
        for a single run but silently credits twice (or more) whenever the
        daily job is re-run in the same month — which cron retries, manual
        re-runs and backfills all do. The ledger is the only state that
        survives the process.
        """
        key = (as_of.year, as_of.month)
        if self._last_sip_month == key:
            return False
        try:
            month_start = datetime(as_of.year, as_of.month, 1, tzinfo=timezone.utc)
            with get_session() as session:
                existing = session.query(CapitalLedger).filter(
                    CapitalLedger.txn_type == LedgerTxnType.SIP_CREDIT.value,
                    CapitalLedger.txn_date >= month_start,
                ).first()
                if existing is not None:
                    self._last_sip_month = key
                    return False
        except Exception as exc:
            # Fail open: a duplicate credit is fixable by hand, a missed month
            # of contributions is not.
            logger.warning(f"Could not verify SIP credit state: {exc}")
        return True

    def _latest_trading_day(self) -> pd.Timestamp:
        df = self.panel.load(investor_config.regime.get("nifty_proxy", "INDEX_NIFTY500"))
        if df.empty:
            return pd.Timestamp.utcnow().normalize().tz_localize(None)
        last = df.index[-1]
        # Never decide on a forming bar.
        if last.date() >= date_cls.today():
            last = df.index[-2] if len(df) > 1 else last
        return last


def _dec(v: Optional[float], scale: int = 4) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(round(float(v), scale)))
    except (TypeError, ValueError):
        return None


__all__ = ["InvestorModeEngine", "DailyRun", "LivePosition"]