from typing import Tuple
from decimal import Decimal, ROUND_DOWN
from loguru import logger

from backtest_v2.config import breakout_v2_config
from config.settings import settings


class PositionSizer:
    """
    Position sizing with risk_capped_by_sip mode.
    
    TargetShares = floor((risk_pct * AccountEquity) / (EntryPrice - StopPrice))
    MaxAffordableShares = floor(AvailableCash / EntryPrice)
    Shares = min(TargetShares, MaxAffordableShares)
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.mode = self.config.sizing.mode
        self.risk_per_trade = self.config.sizing.risk_per_trade
    
    def calculate_size(
        self,
        account_equity: float,
        entry_price: float,
        stop_price: float,
        available_cash: float
    ) -> int:
        """
        Calculate position size in whole shares.
        
        Args:
            account_equity: Total account equity (cash + positions)
            entry_price: Expected entry price
            stop_price: Initial stop loss price (SL_0)
            available_cash: Cash available for new positions
        
        Returns:
            Number of shares (integer, 0 if not affordable)
        """
        if entry_price <= stop_price:
            logger.warning(f"Invalid prices: entry={entry_price}, stop={stop_price}")
            return 0
        
        if available_cash <= 0:
            return 0
        
        risk_amount = account_equity * self.risk_per_trade
        risk_per_share = entry_price - stop_price
        
        if risk_per_share <= 0:
            return 0
        
        # Target shares based on risk
        target_shares = int(risk_amount / risk_per_share)
        
        # Max affordable shares
        max_affordable = int(available_cash / entry_price)
        
        # Max concentration cap
        max_concentration_value = account_equity * settings.MAX_POSITION_CONCENTRATION_PCT
        max_concentration_shares = int(max_concentration_value / entry_price)
        
        # Apply mode logic
        if self.mode == "risk_capped_by_sip":
            shares = min(target_shares, max_affordable, max_concentration_shares)
        elif self.mode == "sip_capped_by_risk":
            # Risk is primary, ignore available_cash ceiling and rely on engine total_cash fallback
            shares = min(target_shares, max_concentration_shares)
        elif self.mode == "risk_only":
            shares = min(target_shares, max_concentration_shares)
        else:
            shares = min(target_shares, max_affordable, max_concentration_shares)
        
        return max(0, shares)
    
    def calculate_notional(self, shares: int, entry_price: float) -> float:
        """Calculate notional value of position."""
        return shares * entry_price
    
    def calculate_risk_amount(self, shares: int, entry_price: float, stop_price: float) -> float:
        """Calculate actual risk amount for position."""
        return shares * (entry_price - stop_price)


def calculate_position_size(
    account_equity: float,
    entry_price: float,
    stop_price: float,
    available_cash: float,
    risk_per_trade: float = 0.005,
    mode: str = "risk_capped_by_sip"
) -> int:
    """Convenience function for position sizing."""
    sizer = PositionSizer()
    sizer.risk_per_trade = risk_per_trade
    sizer.mode = mode
    return sizer.calculate_size(account_equity, entry_price, stop_price, available_cash)