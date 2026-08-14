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

def credit_monthly_sip_execution(amount: int):
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
        total_qty = holding.qty + qty
        holding.avg_buy_price = ((holding.avg_buy_price * holding.qty) + gross_cost) / total_qty
        holding.qty = total_qty
        holding.initial_qty = total_qty
        holding.trailing_stop_price = max(holding.trailing_stop_price, init_stop)
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
    if dp_charge > 0:
        session.add(CapitalLedger(amount=-dp_charge, txn_type=LedgerTxnType.DP_CHARGE.value, running_balance=new_balance))
    
    holding = session.query(PortfolioHolding).filter(PortfolioHolding.symbol == symbol, PortfolioHolding.status == HoldingStatus.OPEN.value).first()
    if holding:
        holding.status = HoldingStatus.CLOSED.value
        holding.qty = 0
        
    w_item = session.query(Watchlist).filter(Watchlist.symbol == symbol).first()
    if w_item:
        w_item.status = WatchlistStatus.VETOED.value

    realized_pnl = None
    realized_r = None
    if holding:
        buy_cost = holding.avg_buy_price * Decimal(str(qty))
        realized_pnl = net_proceeds - buy_cost
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
    if dp_charge > 0:
        session.add(CapitalLedger(amount=-dp_charge, txn_type=LedgerTxnType.DP_CHARGE.value, running_balance=new_balance))
        
    holding = session.query(PortfolioHolding).filter(PortfolioHolding.symbol == symbol, PortfolioHolding.status == HoldingStatus.OPEN.value).first()
    if holding:
        holding.qty -= sell_qty
        
    realized_pnl = None
    realized_r = None
    if holding:
        buy_cost = holding.avg_buy_price * Decimal(str(sell_qty))
        realized_pnl = net_proceeds - buy_cost
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
    """Computes total account equity: cash + MTM of open holdings. Uses avg_buy_price if MTM isn't strictly available here, but ideally uses recent price."""
    mtm = sum([h.avg_buy_price * Decimal(str(h.qty)) for h in open_holdings])
    return cash + mtm

def select_and_execute_buy_candidate(signal_date: date = None, circuit_breaker_active: bool = False) -> Union[BuyDecision, HoldCash]:
    """Evaluates today's technical signals against cash and priority rules."""
    if signal_date is None:
        signal_date = date.today()
        
    with get_session() as session:
        cash = get_current_cash(session)
        
        if circuit_breaker_active:
            return HoldCash(cash_balance=float(cash), rationale="Monthly Circuit Breaker Active. MTD Drawdown exceeded limit.")
            
        open_holdings = session.query(PortfolioHolding).filter(PortfolioHolding.status == HoldingStatus.OPEN.value).all()
        equity = compute_account_equity(session, cash, open_holdings)
        
        current_heat = sum([(h.qty * h.initial_risk) / equity for h in open_holdings if h.initial_risk > 0])
        max_heat = Decimal(str(settings.MAX_PORTFOLIO_HEAT_PCT))
        if current_heat >= max_heat:
            return HoldCash(cash_balance=float(cash), rationale=f"Max portfolio heat reached: {current_heat*100:.2f}% >= {max_heat*100}%")
            
        signals = session.query(TechnicalSignal).filter(TechnicalSignal.signal_date == signal_date).order_by(TechnicalSignal.signal_strength.desc()).all()
        if not signals:
            return HoldCash(cash_balance=float(cash), rationale="No technical entry signals generated today.")
            
        for sig in signals:
            if len(open_holdings) >= settings.MAX_POSITIONS and not any(h.symbol == sig.symbol for h in open_holdings):
                continue
                
            risk_per_share = sig.entry_price - sig.structural_stop_price
            if risk_per_share <= 0:
                continue
                
            qty = calculate_risk_sized_position(float(equity), float(settings.RISK_PER_TRADE_PCT), float(risk_per_share), float(sig.entry_price), float(settings.MAX_POSITION_CONCENTRATION_PCT))
            if qty > 0:
                gross_cost = sig.entry_price * Decimal(str(qty))
                stt = gross_cost * Decimal(str(settings.STT_PCT)) / Decimal("100.0")
                exch_charge = gross_cost * Decimal(str(settings.EXCHANGE_TXN_CHARGE_PCT)) / Decimal("100.0")
                sebi_fee = gross_cost * Decimal(str(settings.SEBI_TURNOVER_FEE_PCT)) / Decimal("100.0")
                total_friction = round(stt + exch_charge + sebi_fee, 2)
                total_cost = gross_cost + total_friction
                
                if total_cost <= cash:
                    rationale = f"Priority 2 New Slot: {sig.pattern_type} setup (Strength: {sig.signal_strength}). Risk-sized: {qty} shares."
                    if any(h.symbol == sig.symbol for h in open_holdings):
                        rationale = f"Priority 1 Accumulation: Fresh {sig.pattern_type} signal on existing holding."
                        
                    return execute_buy(session, sig.symbol, qty, sig.entry_price, rationale, sig.structural_stop_price)
                
        return HoldCash(cash_balance=float(cash), rationale="Portfolio at MAX_POSITIONS capacity or no affordable candidates.")
