from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from decimal import Decimal
from db.models import PortfolioHolding, CapitalLedger, HoldingStatus

async def compute_live_portfolio_metrics(session: AsyncSession) -> dict:
    """
    Computes live performance metrics.
    For this implementation stub, we calculate basic metrics.
    """
    # Get cash balance
    stmt_cash = select(CapitalLedger).order_by(desc(CapitalLedger.id)).limit(1)
    res_cash = await session.execute(stmt_cash)
    last_ledger = res_cash.scalar_one_or_none()
    cash_balance = float(last_ledger.running_balance) if last_ledger else 0.0

    # Get holdings
    stmt_holdings = select(PortfolioHolding).where(PortfolioHolding.status == HoldingStatus.OPEN)
    res_holdings = await session.execute(stmt_holdings)
    open_holdings = res_holdings.scalars().all()
    
    invested_value = sum([float(h.qty * h.avg_buy_price) for h in open_holdings])
    total_value = cash_balance + invested_value

    # Stubs for historical metrics as this requires historical equity curve
    return {
        "cagr_pct": 0.0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "max_drawdown_pct": 0.0,
        "total_value_inr": total_value,
        "cash_balance_inr": cash_balance
    }
