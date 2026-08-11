# core/position_sizer.py
import math
from decimal import Decimal
from typing import List
from loguru import logger

from config import thresholds
from config.settings import settings
from db.models import PortfolioHolding

def calculate_risk_sized_position(
    account_equity: float,
    risk_pct: float,
    risk_per_share: float,
    entry_price: float,
    max_position_pct: float,
) -> int:
    """
    Calculates position size based on risk tolerance rather than affordability.
    Formula: Q = (Equity * Risk%) / Risk_per_share
    """
    if entry_price <= 0 or risk_per_share <= 0:
        logger.warning(f"Invalid risk per share: {risk_per_share} (entry: {entry_price})")
        return 0
    
    dollar_risk = account_equity * risk_pct
    raw_shares = dollar_risk / risk_per_share
    
    # Cap by max position concentration
    max_shares_by_concentration = (account_equity * max_position_pct) / entry_price
    
    shares = min(int(math.floor(raw_shares)), int(math.floor(max_shares_by_concentration)))
    
    logger.debug(f"Sizing: eq={account_equity}, risk_pct={risk_pct}, risk_per_share={risk_per_share}, raw={raw_shares}, cap={max_shares_by_concentration}, final={shares}")
    return max(0, shares)

def compute_portfolio_heat(open_holdings: List[PortfolioHolding], current_prices: dict) -> Decimal:
    """
    Computes total portfolio heat as the sum of all open risks as a percentage of total equity.
    """
    # This requires a more complex calculation that we'll handle in the allocator for now,
    # or by keeping a running total. We will implement heat checking in the allocator loop.
    pass

def is_heat_budget_available(current_heat: Decimal, max_heat: Decimal = Decimal(str(settings.MAX_PORTFOLIO_HEAT_PCT))) -> bool:
    """Gates new entries if portfolio heat exceeds maximum allowed."""
    return current_heat < max_heat
