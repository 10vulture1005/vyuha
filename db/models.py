# db/models.py
"""SQLAlchemy ORM models for VYUHA trading engine.

Defines all database tables for universe tracking, fundamental/technical signals,
portfolio management, capital ledger, and observability.
"""
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, Date, Text, Boolean,
    ForeignKey, Index, JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ─── Enums ───────────────────────────────────────────────────────────────────

class PatternType(str, Enum):
    VCP = "VCP"
    HTF = "HTF"
    W_BOTTOM = "W_BOTTOM"
    BB_SQUEEZE = "BB_SQUEEZE"
    BREAKOUT = "BREAKOUT"
    MEAN_REVERSION = "MEAN_REVERSION"


class HoldingStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    STOPPED_OUT = "STOPPED_OUT"


class WatchlistStatus(str, Enum):
    ACTIVE = "ACTIVE"
    VETOED = "VETOED"
    EXPIRED = "EXPIRED"
    BOUGHT = "BOUGHT"


class TradeTxnType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class LedgerTxnType(str, Enum):
    SIP_CREDIT = "SIP_CREDIT"
    BUY_DEBIT = "BUY_DEBIT"
    SELL_CREDIT = "SELL_CREDIT"
    DP_CHARGE = "DP_CHARGE"
    FRICTION_CHARGE = "FRICTION_CHARGE"


# ─── Tables ──────────────────────────────────────────────────────────────────

class Universe(Base):
    """Master list of tracked symbols with sector and exchange metadata."""

    __tablename__ = "universe"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    market_cap_cr: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    exchange: Mapped[str] = mapped_column(String(10), default="NSE", nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


class FundamentalSnapshot(Base):
    """Quarterly fundamental data snapshot per symbol."""

    __tablename__ = "fundamental_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    roe: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    debt_equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    eps_cagr_3y: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    revenue_growth_yoy: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    promoter_holding: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    conviction_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    data_source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    __table_args__ = (
        Index("idx_fund_symbol_date", "symbol", "snapshot_date"),
    )


class SentimentFlag(Base):
    """Red-flag verdicts per symbol per scan, with source + reason."""

    __tablename__ = "sentiment_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="CASCADE"),
        nullable=False,
    )
    checked_date: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    red_flag: Mapped[bool] = mapped_column(default=False, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("idx_sent_symbol_flag", "symbol", "red_flag"),
    )


class Watchlist(Base):
    """Current status per symbol: active / vetoed / expired, with conviction score."""

    __tablename__ = "watchlist"

    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), default=WatchlistStatus.ACTIVE.value, nullable=False
    )
    conviction_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_watch_status_score", "status", "conviction_score"),
    )


class TechnicalSignal(Base):
    """Entry signals: pattern type, ATR(14), entry price zone, signal strength."""

    __tablename__ = "technical_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="CASCADE"),
        nullable=False,
    )
    signal_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    pattern_type: Mapped[str] = mapped_column(String(20), nullable=False)
    atr_14: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    structural_stop_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    signal_strength: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    vol_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    momentum_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    regime_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    trend_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)

    __table_args__ = (
        Index("idx_tech_symbol_date", "symbol", "signal_date"),
    )


class CapitalLedger(Base):
    """Every cash movement: SIP credit, buy debit, sell credit, DP charge."""

    __tablename__ = "capital_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_date: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    txn_type: Mapped[str] = mapped_column(String(20), nullable=False)
    running_balance: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)


class PortfolioHolding(Base):
    """Current open positions: qty, avg buy price, trailing stop, status."""

    __tablename__ = "portfolio_holdings"

    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="RESTRICT"),
        primary_key=True,
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_buy_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    first_buy_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    trailing_stop_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    initial_risk: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    entry_pattern: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    tiers_hit: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=None)
    status: Mapped[str] = mapped_column(
        String(20), default=HoldingStatus.OPEN.value, nullable=False
    )


class TradeLog(Base):
    """Immutable record of every executed BUY/SELL with trigger reason."""

    __tablename__ = "trade_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="RESTRICT"),
        nullable=False,
    )
    txn_type: Mapped[str] = mapped_column(String(10), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    txn_date: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    dp_charge: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0.0, nullable=False)
    friction_charge: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0.0, nullable=False)
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    realized_r: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(100), nullable=False)
    entry_pattern: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    __table_args__ = (
        Index("idx_trade_symbol_date", "symbol", "txn_date"),
    )


class PortfolioValueHistory(Base):
    """Daily mark-to-market snapshot — feeds core/metrics.py."""

    __tablename__ = "portfolio_value_history"

    date: Mapped[date] = mapped_column(Date, primary_key=True, default=date.today)
    total_value: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    invested_value: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0.0, nullable=False)


class AgentRunLog(Base):
    """Per-agent execution status/duration/errors per run — feeds observability."""

    __tablename__ = "agent_run_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_agent_run_date", "agent_name", "run_date"),
    )


class TradingAgentsDecision(Base):
    """Immutable record of every Vyuha Agent LLM pipeline run.

    Each row corresponds to one ``propagate(ticker, date)`` invocation.
    Reports are stored as JSON blobs to keep the table column count low
    while preserving every agent's output verbatim — useful for
    debugging and backfilling the reflection log.

    NB: the table name stays ``tradingagents_decisions`` for backwards
    compatibility with existing DBs; the Python class name is kept as
    ``TradingAgentsDecision`` to mirror the upstream framework.
    """

    __tablename__ = "tradingagents_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(30), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    trade_decision: Mapped[str] = mapped_column(String(20), nullable=False)
    final_signal: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    confirmed_by_rules: Mapped[bool] = mapped_column(default=False, nullable=False)
    acted_on: Mapped[bool] = mapped_column(default=False, nullable=False)
    llm_provider: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    deep_model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    quick_model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    market_report: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sentiment_report: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    news_report: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fundamentals_report: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    investment_plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trader_plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    debate_history: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    risk_history: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    raw_state: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_ta_ticker_date", "ticker", "trade_date"),
        Index("idx_ta_decision_date", "trade_date", "trade_decision"),
    )


class TradingAgentsOutcome(Base):
    """Realized outcome for a Vyuha Agent decision, filled by the
    reflection layer once the holding window has elapsed.

    Mirrors the upstream ``TradingMemoryLog`` to keep all vyuha-side state
    in the database (easier queries, joins with ``trade_log``).
    """

    __tablename__ = "tradingagents_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(30), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    resolution_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    holding_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    raw_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    alpha_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    benchmark: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    reflection: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_ta_out_ticker_date", "ticker", "trade_date"),
    )


# ─── Investor Mode ──────────────────────────────────────────────────────────
#
# Investor Mode (STRATEGY_MODE=investor) keeps its own state rather than
# sharing the swing tables. The swing engine is built around single-position
# risk (PortfolioHolding.initial_risk, heat budgets, time stops) that has no
# meaning in a 25-name monthly-rebalanced book, and mixing the two would let a
# swing row silently corrupt an investor target or vice versa.


class RebalanceLog(Base):
    """One monthly rebalance: what the book was told to hold, and why.

    ``target_weights`` is the JSON weight map produced on the rebalance date.
    Recording it (rather than recomputing it later) means a later config
    change cannot rewrite history: what the book was actually told on the
    first of the month stays auditable.
    """

    __tablename__ = "investor_rebalance_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rebalance_date: Mapped[date] = mapped_column(Date, nullable=False)
    regime: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    exposure: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    equity: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), nullable=True)
    n_positions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    target_weights: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    defensive_weights: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    eligible: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_investor_rebal_date", "rebalance_date"),
    )


class InvestorTarget(Base):
    """Target weight per symbol for a given date (the daily target book).

    One row per symbol per rebalance. ``as_of`` is the CLOSE of the decision
    day; orders derived from these targets fill at the NEXT open, which is why
    the trade date and the effective date are both recorded.
    """

    __tablename__ = "investor_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="CASCADE"),
        nullable=False,
    )
    target_weight: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    sector: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fundamental_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    is_defensive: Mapped[Optional[bool]] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_investor_target_date", "as_of"),
        Index("idx_investor_target_symbol", "symbol", "as_of"),
    )


class NewsEventRow(Base):
    """Every classified headline, persisted daily.

    This table is the reason the news layer can ever be backtested properly.
    Until enough real classified history accumulates, Layer 3 runs on the
    earnings-gap PROXY in backtests and on FinBERT/LLM in the live path; both
    write here, with ``tier`` distinguishing them, so a real news backtest
    becomes possible later instead of being impossible forever.

    ``is_proxy`` is explicit rather than inferred: a synthetic event written by
    a backtest run must never be mistaken for classified real news.
    """

    __tablename__ = "investor_news_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbol: Mapped[Optional[str]] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="CASCADE"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    # HARD_NEGATIVE | POSITIVE | OVERREACTION | MACRO | NEUTRAL
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subtype: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    materiality: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 4), nullable=True
    )
    sentiment: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 6), nullable=True
    )
    finbert_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 6), nullable=True
    )
    # TIER1 (FinBERT) | TIER2 (LLM) | REGEX | PROXY
    tier: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    is_macro: Mapped[Optional[bool]] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_proxy: Mapped[Optional[bool]] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_news_date_symbol", "trade_date", "symbol"),
        Index("idx_news_type", "event_type", "trade_date"),
        Index("idx_news_proxy", "is_proxy"),
    )


class InvestorFundamentalSnapshot(Base):
    """Point-in-time fundamental record WITH its filing date.

    The brief's hard rule is that a quarter's numbers may only be used from the
    day AFTER its actual filing date. Storing ``filing_date`` alongside
    ``usable_from`` makes that rule checkable in SQL rather than a convention
    buried in Python: a query for "what did we know on date X" is a plain
    ``usable_from <= X`` filter.
    """

    __tablename__ = "investor_fundamental_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("universe.symbol", ondelete="CASCADE"),
        nullable=False,
    )
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    filing_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    #: First date these numbers may be used = filing_date + lag (or
    #: period_end + fallback when no filing date is known).
    usable_from: Mapped[date] = mapped_column(Date, nullable=False)
    frequency: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    roce: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    roe: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    debt_to_equity: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    interest_coverage: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    ocf_to_pat: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    promoter_pledge_pct: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    revenue_cagr_3y: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    eps_cagr_3y: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    loss_years_3y: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pe: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    pb: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    composite_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 4), nullable=True
    )
    data_source: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("idx_invfund_symbol_period", "symbol", "period_end"),
        Index("idx_invfund_usable", "usable_from"),
    )
