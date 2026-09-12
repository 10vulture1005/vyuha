# core/capital_allocator.py
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Union, List, Optional
from pydantic import BaseModel
from loguru import logger
from sqlalchemy.orm import Session

from db.session import get_session
from db.models import (
    CapitalLedger, LedgerTxnType, PortfolioHolding, HoldingStatus,
    TradeLog, TradeTxnType, TechnicalSignal, Watchlist, WatchlistStatus
)
from core.position_sizer import calculate_risk_sized_position
from core.stop_loss_engine import compute_new_trailing_stop
from config.settings import settings
from config import thresholds

class BuyDecision(BaseModel):
    action: str = "BUY"
    symbol: str
    qty: int
    price: float
    total_cost: float
    rationale: str

class SellDecision(BaseModel):
    action: str = "SELL"
    symbol: str
    qty: int
    price: float
    net_proceeds: float
    dp_charge: float
    rationale: str

class HoldCash(BaseModel):
    action: str = "HOLD_CASH"
    cash_balance: float
    rationale: str

def _latest_mtm_equity(session: Session) -> Optional[Decimal]:
    """Returns latest MTM total_value if a valuation snapshot exists."""
    try:
        from db.models import PortfolioValueHistory
        row = session.query(PortfolioValueHistory).order_by(PortfolioValueHistory.date.desc()).first()
        if row and row.total_value and row.total_value > 0:
            return Decimal(str(row.total_value))
    except Exception:
        pass
    return None


def is_circuit_breaker_active(session: Session) -> bool:
    """Monthly drawdown breaker: halt buys if MTD drawdown <= -MAX_MONTHLY_DRAWDOWN_PCT.

    Compares latest MTM equity vs first snapshot of current month (or month-start
    fallback to cost-based equity). Mirrors backtest/engine.py month logic.
    """
    try:
        from datetime import date as _date
        from db.models import PortfolioValueHistory
        today = _date.today()
        month_rows = (
            session.query(PortfolioValueHistory)
            .filter(PortfolioValueHistory.date >= today.replace(day=1))
            .order_by(PortfolioValueHistory.date.asc())
            .all()
        )
        latest = session.query(PortfolioValueHistory).order_by(PortfolioValueHistory.date.desc()).first()
        if not latest or not month_rows:
            return False
        month_start = Decimal(str(month_rows[0].total_value))
        current = Decimal(str(latest.total_value))
        if month_start <= 0:
            return False
        mtd_dd = (current - month_start) / month_start * Decimal("100")
        return mtd_dd <= -Decimal(str(settings.MAX_MONTHLY_DRAWDOWN_PCT))
    except Exception:
        return False


def _total_buy_friction(session: Session, symbol: str) -> Decimal:
    """Sums BUY friction already paid for a symbol (for honest realized PnL)."""
    try:
        rows = session.query(TradeLog).filter(
            TradeLog.symbol == symbol, TradeLog.txn_type == TradeTxnType.BUY.value
        ).all()
        return sum((r.friction_charge or Decimal("0")) for r in rows) if rows else Decimal("0")
    except Exception:
        return Decimal("0")
    """Credits monthly SIP capital into the ledger. Called via monthly cron."""
    with get_session() as session:
        latest = session.query(CapitalLedger).order_by(CapitalLedger.id.desc()).first()
        current_balance = latest.running_balance if latest else Decimal("0")
        new_balance = current_balance + Decimal(str(amount))
        
        session.add(CapitalLedger(
            txn_date=datetime.now(timezone.utc),
            amount=Decimal(str(amount)),
            txn_type=LedgerTxnType.SIP_CREDIT.value,
            running_balance=new_balance
        ))
        session.flush()
        logger.info(f"Credited monthly SIP: ₹{amount}. New Ledger Balance: ₹{new_balance}")

def get_current_cash(session: Session) -> Decimal:
    latest = session.query(CapitalLedger).order_by(CapitalLedger.id.desc()).first()
    if latest:
        return latest.running_balance
    return Decimal(str(settings.INITIAL_CAPITAL))

def execute_buy(session: Session, symbol: str, qty: int, price: Decimal, rationale: str, init_stop: Decimal):
    """Executes atomic BUY transaction across ledger, holdings, and trade log."""
    cash = get_current_cash(session)
    gross_cost = price * Decimal(str(qty))
    stt = gross_cost * Decimal(str(settings.STT_PCT)) / Decimal("100.0")
    exch_charge = gross_cost * Decimal(str(settings.EXCHANGE_TXN_CHARGE_PCT)) / Decimal("100.0")
    sebi_fee = gross_cost * Decimal(str(settings.SEBI_TURNOVER_FEE_PCT)) / Decimal("100.0")
    total_friction = round(stt + exch_charge + sebi_fee, 2)
    total_cost = gross_cost + total_friction
    
    if total_cost > cash:
        raise ValueError(f"Insufficient funds: Cost {total_cost} > Cash {cash}")
        
    new_balance = cash - total_cost
    session.add(CapitalLedger(
        amount=-total_cost,
        txn_type=LedgerTxnType.BUY_DEBIT.value,
        running_balance=new_balance
    ))
    
    holding = session.query(PortfolioHolding).filter(PortfolioHolding.symbol == symbol).first()
    
    signal = session.query(TechnicalSignal).filter(TechnicalSignal.symbol == symbol).order_by(TechnicalSignal.id.desc()).first()
    entry_pattern = signal.pattern_type if signal else None
    
    if holding and holding.status == HoldingStatus.OPEN.value:
        # FIX: no pyramiding / averaging-down. One position per symbol.
        # Previously this silently accumulated (6+6+7 shares) and corrupted
        # initial_qty/initial_risk. Now blocked — caller skips existing symbols.
        raise ValueError(f"Position already OPEN for {symbol}: pyramiding blocked. Sell first before re-entry.")
    elif holding:
        holding.qty = qty
        holding.initial_qty = qty
        holding.avg_buy_price = price
        holding.first_buy_date = date.today()
        holding.trailing_stop_price = init_stop
        holding.initial_risk = price - init_stop
        holding.entry_pattern = entry_pattern
        holding.tiers_hit = []
        holding.status = HoldingStatus.OPEN.value
    else:
        holding = PortfolioHolding(
            symbol=symbol,
            qty=qty,
            initial_qty=qty,
            avg_buy_price=price,
            first_buy_date=date.today(),
            trailing_stop_price=init_stop,
            initial_risk=price - init_stop,
            entry_pattern=entry_pattern,
            tiers_hit=[],
            status=HoldingStatus.OPEN.value
        )
        session.add(holding)
        
    session.add(TradeLog(
        symbol=symbol, txn_type=TradeTxnType.BUY.value, qty=qty, price=price,
        dp_charge=Decimal("0.0"), friction_charge=total_friction, 
        reason=rationale, triggered_by="CapitalAllocator", entry_pattern=entry_pattern
    ))
    session.flush()
    logger.info(f"EXECUTED BUY: {qty}x {symbol} @ ₹{price}. Friction: ₹{total_friction}. Rem Cash: ₹{new_balance}")
    return BuyDecision(symbol=symbol, qty=qty, price=float(price), total_cost=float(total_cost), rationale=rationale)

def execute_sell(session: Session, symbol: str, qty: int, price: Decimal, reason: str):
    """Executes atomic SELL transaction, debiting DP and friction charges from net proceeds."""
    cash = get_current_cash(session)
    gross_proceeds = price * Decimal(str(qty))
    dp_charge = Decimal(str(settings.DP_CHARGE_PER_SELL))
    stt = gross_proceeds * Decimal(str(settings.STT_PCT)) / Decimal("100.0")
    exch_charge = gross_proceeds * Decimal(str(settings.EXCHANGE_TXN_CHARGE_PCT)) / Decimal("100.0")
    sebi_fee = gross_proceeds * Decimal(str(settings.SEBI_TURNOVER_FEE_PCT)) / Decimal("100.0")
    total_friction = round(stt + exch_charge + sebi_fee, 2)
    
    net_proceeds = gross_proceeds - dp_charge - total_friction
    new_balance = cash + net_proceeds
    
    session.add(CapitalLedger(amount=net_proceeds, txn_type=LedgerTxnType.SELL_CREDIT.value, running_balance=new_balance))
    # FIX: DP already deducted inside net_proceeds above. Previously a second
    # DP_CHARGE ledger row double-counted DP in ledger-sum reconciliations.
    
    holding = session.query(PortfolioHolding).filter(PortfolioHolding.symbol == symbol, PortfolioHolding.status == HoldingStatus.OPEN.value).first()
    if holding:
        holding.status = HoldingStatus.CLOSED.value
        holding.qty = 0
        
    # FIX: do NOT mark watchlist VETOED on a mechanical stop-out. VETOED is a
    # governance verdict (fraud/SEBI); exits should leave watchlist untouched
    # so the symbol can re-qualify on a fresh setup.

    realized_pnl = None
    realized_r = None
    if holding:
        buy_cost = holding.avg_buy_price * Decimal(str(qty))
        # FIX: include buy-side friction so realized PnL is honest
        # (previously overstated by ~0.1% per buy).
        buy_friction = _total_buy_friction(session, symbol)
        realized_pnl = net_proceeds - buy_cost - buy_friction
        if holding.initial_risk > 0:
            realized_r = (price - holding.avg_buy_price) / holding.initial_risk

    session.add(TradeLog(
        symbol=symbol, txn_type=TradeTxnType.SELL.value, qty=qty, price=price,
        dp_charge=dp_charge, friction_charge=total_friction, 
        realized_pnl=realized_pnl, realized_r=realized_r,
        reason=reason, triggered_by="RiskExitAgent"
    ))
    session.flush()
    logger.info(f"EXECUTED SELL: {qty}x {symbol} @ ₹{price}. Net: ₹{net_proceeds}. Friction: ₹{total_friction}. Rationale: {reason}")
    return SellDecision(symbol=symbol, qty=qty, price=float(price), net_proceeds=float(net_proceeds), dp_charge=float(dp_charge), rationale=reason)

def execute_partial_sell(session: Session, symbol: str, sell_qty: int, price: Decimal, reason: str):
    """Executes a partial SELL transaction, debiting DP and friction charges from net proceeds."""
    cash = get_current_cash(session)
    gross_proceeds = price * Decimal(str(sell_qty))
    dp_charge = Decimal(str(settings.DP_CHARGE_PER_SELL))
    stt = gross_proceeds * Decimal(str(settings.STT_PCT)) / Decimal("100.0")
    exch_charge = gross_proceeds * Decimal(str(settings.EXCHANGE_TXN_CHARGE_PCT)) / Decimal("100.0")
    sebi_fee = gross_proceeds * Decimal(str(settings.SEBI_TURNOVER_FEE_PCT)) / Decimal("100.0")
    total_friction = round(stt + exch_charge + sebi_fee, 2)
    
    net_proceeds = gross_proceeds - dp_charge - total_friction
    new_balance = cash + net_proceeds
    
    session.add(CapitalLedger(amount=net_proceeds, txn_type=LedgerTxnType.SELL_CREDIT.value, running_balance=new_balance))
    # FIX: DP already deducted inside net_proceeds (see execute_sell).
        
    holding = session.query(PortfolioHolding).filter(PortfolioHolding.symbol == symbol, PortfolioHolding.status == HoldingStatus.OPEN.value).first()
    if holding:
        holding.qty -= sell_qty
        
    realized_pnl = None
    realized_r = None
    if holding:
        buy_cost = holding.avg_buy_price * Decimal(str(sell_qty))
        # FIX: pro-rate buy friction so partial PnL is honest too.
        total_buy_friction = _total_buy_friction(session, symbol)
        denom = holding.qty + sell_qty  # pre-sale qty (holding.qty already reduced above)
        friction_share = (total_buy_friction * Decimal(str(sell_qty)) / Decimal(str(denom))) if denom > 0 else Decimal("0")
        realized_pnl = net_proceeds - buy_cost - friction_share
        if holding.initial_risk > 0:
            realized_r = (price - holding.avg_buy_price) / holding.initial_risk

    session.add(TradeLog(
        symbol=symbol, txn_type=TradeTxnType.SELL.value, qty=sell_qty, price=price,
        dp_charge=dp_charge, friction_charge=total_friction, 
        realized_pnl=realized_pnl, realized_r=realized_r,
        reason=reason, triggered_by="RiskExitAgent_Partial"
    ))
    session.flush()
    logger.info(f"EXECUTED PARTIAL SELL: {sell_qty}x {symbol} @ ₹{price}. Net: ₹{net_proceeds}. Friction: ₹{total_friction}. Rationale: {reason}")
    return SellDecision(symbol=symbol, qty=sell_qty, price=float(price), net_proceeds=float(net_proceeds), dp_charge=float(dp_charge), rationale=reason)

def compute_account_equity(session: Session, cash: Decimal, open_holdings: List[PortfolioHolding]) -> Decimal:
    """Computes total account equity: cash + MTM of open holdings.
    FIX: previously used avg_buy_price (cost) which understated equity in
    winners / overstated in losers and caused pro-cyclical oversizing into
    drawdowns. Now prefers the latest MTM valuation snapshot; falls back to
    cost only when no snapshot exists (e.g. fresh backtest DB).
    """
    mtm_equity = _latest_mtm_equity(session)
    if mtm_equity is not None:
        return mtm_equity
    mtm = sum([h.avg_buy_price * Decimal(str(h.qty)) for h in open_holdings])
    return cash + mtm

def select_and_execute_buy_candidate(signal_date: date = None, circuit_breaker_active: bool = False) -> Union[BuyDecision, HoldCash]:
    """Evaluates today's technical signals against cash and priority rules."""
    if signal_date is None:
        signal_date = date.today()
        
    with get_session() as session:
        cash = get_current_cash(session)
        
        # FIX: wire the monthly-drawdown breaker internally so both the daily
        # pipeline and the paper engine halt buys into drawdowns even when the
        # caller forgets to pass the flag (previously default False = never halted).
        if circuit_breaker_active or is_circuit_breaker_active(session):
            return HoldCash(cash_balance=float(cash), rationale="Monthly Circuit Breaker Active. MTD Drawdown exceeded limit.")
            
        open_holdings = session.query(PortfolioHolding).filter(PortfolioHolding.status == HoldingStatus.OPEN.value).all()
        open_symbols = {h.symbol for h in open_holdings}
        equity = compute_account_equity(session, cash, open_holdings)
        if equity <= 0:
            return HoldCash(cash_balance=float(cash), rationale="Zero/negative equity — cannot size positions.")
        
        current_heat = sum([(Decimal(str(h.qty)) * h.initial_risk) / equity for h in open_holdings if h.initial_risk and h.initial_risk > 0])
        max_heat = Decimal(str(settings.MAX_PORTFOLIO_HEAT_PCT))
        if current_heat >= max_heat:
            return HoldCash(cash_balance=float(cash), rationale=f"Max portfolio heat reached: {current_heat*100:.2f}% >= {max_heat*100}%")
            
        signals = session.query(TechnicalSignal).filter(TechnicalSignal.signal_date == signal_date).order_by(TechnicalSignal.signal_strength.desc()).all()
        if not signals:
            return HoldCash(cash_balance=float(cash), rationale="No technical entry signals generated today.")
            
        for sig in signals:
            # FIX: one position per symbol — no pyramiding. Previously an
            # existing holding bypassed MAX_POSITIONS and accumulated every day
            # a fresh W_BOTTOM fired (SCHNEIDER 6+6+7, PREMIERENE 11+9+9+9).
            if sig.symbol in open_symbols:
                continue
            if len(open_holdings) >= settings.MAX_POSITIONS:
                continue
                
            risk_per_share = sig.entry_price - sig.structural_stop_price
            if risk_per_share <= 0:
                continue
            # FIX: floor risk_per_share at 0.5x ATR so penny-tight structural
            # stops can't produce max-concentration sizing into noise.
            atr_floor = float(sig.atr_14) * 0.5 if sig.atr_14 and float(sig.atr_14) > 0 else 0.0
            if float(risk_per_share) < atr_floor:
                risk_per_share = Decimal(str(atr_floor))
                if risk_per_share <= 0:
                    continue
                
            qty = calculate_risk_sized_position(float(equity), float(settings.RISK_PER_TRADE_PCT), float(risk_per_share), float(sig.entry_price), float(settings.MAX_POSITION_CONCENTRATION_PCT))
            if qty <= 0:
                continue
            # FIX: incremental heat check — previously the gate was evaluated
            # once before the loop, allowing a single trade to overshoot the
            # 6% cap. Now candidate risk must fit inside remaining budget.
            candidate_heat = (Decimal(str(qty)) * risk_per_share) / equity
            if current_heat + candidate_heat > max_heat:
                continue
            gross_cost = sig.entry_price * Decimal(str(qty))
            stt = gross_cost * Decimal(str(settings.STT_PCT)) / Decimal("100.0")
            exch_charge = gross_cost * Decimal(str(settings.EXCHANGE_TXN_CHARGE_PCT)) / Decimal("100.0")
            sebi_fee = gross_cost * Decimal(str(settings.SEBI_TURNOVER_FEE_PCT)) / Decimal("100.0")
            total_friction = round(stt + exch_charge + sebi_fee, 2)
            total_cost = gross_cost + total_friction
            
            if total_cost <= cash:
                rationale = f"Priority 2 New Slot: {sig.pattern_type} setup (Strength: {sig.signal_strength}). Risk-sized: {qty} shares."
                return execute_buy(session, sig.symbol, qty, sig.entry_price, rationale, sig.structural_stop_price)
                
        return HoldCash(cash_balance=float(cash), rationale="Portfolio at MAX_POSITIONS capacity, heat budget exhausted, or no affordable candidates.")
