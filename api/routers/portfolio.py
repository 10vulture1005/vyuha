from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from db.session import get_async_session
from db.models import PortfolioHolding, CapitalLedger, TradeLog, Watchlist, HoldingStatus
from api.schemas import (
    HoldingResponse, LedgerResponse, TradeLogResponse, WatchlistResponse, PerformanceMetricsResponse
)
from core.metrics import compute_live_portfolio_metrics

router = APIRouter(prefix="/portfolio", tags=["Portfolio & Watchlist"])

@router.get("/holdings", response_model=List[HoldingResponse])
async def get_open_holdings(session: AsyncSession = Depends(get_async_session)):
    """Returns all currently OPEN equity positions with their ratcheted trailing stops."""
    stmt = select(PortfolioHolding).where(PortfolioHolding.status == HoldingStatus.OPEN)
    result = await session.execute(stmt)
    return result.scalars().all()

@router.get("/ledger", response_model=List[LedgerResponse])
async def get_ledger_history(limit: int = 50, session: AsyncSession = Depends(get_async_session)):
    """Returns the trailing history of SIP credits, buy debits, and sell credits."""
    stmt = select(CapitalLedger).order_by(desc(CapitalLedger.id)).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()

@router.get("/trades", response_model=List[TradeLogResponse])
async def get_trade_log(limit: int = 50, session: AsyncSession = Depends(get_async_session)):
    """Returns the immutable audit trail of executed BUY and SELL orders."""
    stmt = select(TradeLog).order_by(desc(TradeLog.txn_date)).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()

@router.get("/metrics", response_model=PerformanceMetricsResponse)
async def get_performance_metrics(session: AsyncSession = Depends(get_async_session)):
    """Dynamically computes CAGR, Sharpe, Sortino, and Max Drawdown from equity history."""
    try:
        metrics_dict = await compute_live_portfolio_metrics(session)
        return PerformanceMetricsResponse(**metrics_dict)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to calculate metrics: {e}")

@router.get("/watchlist", response_model=List[WatchlistResponse], tags=["Watchlist"])
async def get_current_watchlist(session: AsyncSession = Depends(get_async_session)):
    """Returns all candidates with their status (active/vetoed/expired) and relative conviction scores."""
    stmt = select(Watchlist).order_by(desc(Watchlist.conviction_score))
    result = await session.execute(stmt)
    return result.scalars().all()
