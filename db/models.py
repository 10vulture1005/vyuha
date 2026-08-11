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
