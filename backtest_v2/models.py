from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from enum import Enum

from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime, Date, Text, Boolean,
    ForeignKey, Index, JSON
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class V2HoldingStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    STOPPED_OUT = "STOPPED_OUT"
    RISK_CAP_EXIT = "RISK_CAP_EXIT"
    PYRAMIDED = "PYRAMIDED"


class V2ExitReason(str, Enum):
    TRAILING_STOP = "TRAILING_STOP"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    REGIME_TIGHTEN = "REGIME_TIGHTEN"
    SIGNAL_DECAY = "SIGNAL_DECAY"
    RISK_CAP = "RISK_CAP"
    SECTOR_CAP = "SECTOR_CAP"
    MANUAL = "MANUAL"


class V2PortfolioHolding(Base):
    """V2 Portfolio holdings with enhanced tracking."""
    
    __tablename__ = "v2_portfolio_holdings"
    
    symbol: Mapped[str] = mapped_column(
        String(20), primary_key=True
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_buy_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    first_buy_date: Mapped[date] = mapped_column(Date, nullable=False)
    
    # V2-specific stop tracking
    signal_day_low: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    initial_stop: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)  # SL_0
    current_stop: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)  # TS_t
    highest_close: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    atr_at_entry: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    
    # Breakeven tracking
    breakeven_triggered: Mapped[bool] = mapped_column(default=False, nullable=False)
    
    # Pyramiding
    pyramid_entries: Mapped[Optional[List]] = mapped_column(JSON, nullable=True)
    
    # Status
    status: Mapped[str] = mapped_column(
        String(20), default=V2HoldingStatus.OPEN.value, nullable=False
    )
    exit_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    realized_r: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    
    # Signal info
    entry_s_tech: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    entry_s_raw: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    entry_T: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    entry_M: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    entry_B: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    entry_C: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)


class V2TradeLog(Base):
    """V2 Trade log with enhanced metrics."""
    
    __tablename__ = "v2_trade_log"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    txn_type: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY/SELL
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    txn_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # Costs
    stt: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    exchange_charge: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    sebi_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    stamp_duty: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    gst: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    dp_charge: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=0)
    slippage_pct: Mapped[Decimal] = mapped_column(Numeric(8, 6), default=0)
    
    # PnL
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    realized_r: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    
    # Tax
    stcg_tax: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    holding_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    
    # Signal info
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    entry_s_tech: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    entry_atr: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    highest_close: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    breakeven_triggered: Mapped[bool] = mapped_column(default=False)


class V2Signal(Base):
    """V2 Signal with full diagnostic components."""
    
    __tablename__ = "v2_signals"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    signal_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False, index=True)
    
    # Raw components
    T_raw: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    M_raw: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    B_raw: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    BBW: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    C_raw: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)  # = 1 - P_120(BBW)
    
    # Percentile components
    T_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    M_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    B_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    C_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    
    # Composite
    S_raw: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    S_tech: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    
    # Trigger data
    H_20: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    signal_low: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    close_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    atr_14: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    vol_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    
    # Regime
    regime_filter_pass: Mapped[bool] = mapped_column(default=True)
    nifty_above_sma200: Mapped[bool] = mapped_column(default=True)
    
    # Action
    action_taken: Mapped[str] = mapped_column(String(20), default="NONE")  # ENTRY, FILTERED, COOLDOWN, etc.
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class V2DailyMetrics(Base):
    """Daily portfolio metrics for tearsheet."""
    
    __tablename__ = "v2_daily_metrics"
    
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_equity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    invested: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    
    # Position counts
    open_positions: Mapped[int] = mapped_column(default=0)
    sectors_active: Mapped[int] = mapped_column(default=0)
    
    # Risk
    aggregate_risk_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    portfolio_heat_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=0)
    
    # Signals
    signals_generated: Mapped[int] = mapped_column(default=0)
    signals_filtered: Mapped[int] = mapped_column(default=0)
    warmup_excluded: Mapped[int] = mapped_column(default=0)
    nodata_excluded: Mapped[int] = mapped_column(default=0)
    
    # SIP
    sip_credited: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)


class V2RunConfig(Base):
    """Store run configuration for reproducibility."""
    
    __tablename__ = "v2_run_config"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    run_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    config_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    symbols_count: Mapped[int] = mapped_column(default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


def create_v2_tables(engine):
    """Create all V2 tables."""
    Base.metadata.create_all(bind=engine)