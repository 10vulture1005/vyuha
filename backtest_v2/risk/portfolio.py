from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from loguru import logger

from backtest_v2.config import breakout_v2_config
from backtest_v2.data.sector_taxonomy import get_sector_taxonomy


@dataclass
class PositionRisk:
    """Risk metrics for a single position."""
    symbol: str
    entry_price: float
    stop_price: float
    shares: int
    sector: str
    
    @property
    def risk_per_share(self) -> float:
        return max(0, self.entry_price - self.stop_price)
    
    @property
    def position_risk(self) -> float:
        return self.risk_per_share * self.shares
    
    @property
    def notional(self) -> float:
        return self.entry_price * self.shares


class PortfolioRiskManager:
    """
    Portfolio-level risk management.
    
    Enforces:
    1. Aggregate open risk cap: sum of position risks / equity <= 2.5%
    2. Sector concentration cap: max 2 positions per sector
    3. Max total positions: 5
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.aggregate_risk_cap = self.config.portfolio_risk.aggregate_open_risk_cap
        self.max_per_sector = self.config.portfolio_risk.max_positions_per_sector
        self.max_total_positions = self.config.portfolio_risk.max_total_positions
        
        self.sector_taxonomy = get_sector_taxonomy()
        
        # Current positions
        self.positions: Dict[str, PositionRisk] = {}
    
    def add_position(
        self,
        symbol: str,
        entry_price: float,
        stop_price: float,
        shares: int
    ) -> bool:
        """
        Add a position if risk limits allow.
        
        Returns:
            True if added, False if rejected
        """
        sector = self.sector_taxonomy.get_sector(symbol)
        
        # Check sector cap
        sector_count = sum(1 for p in self.positions.values() if p.sector == sector)
        if sector_count >= self.max_per_sector:
            logger.debug(f"Sector cap reached for {sector}: {sector_count}/{self.max_per_sector}")
            return False
        
        # Check total position cap
        if len(self.positions) >= self.max_total_positions:
            logger.debug(f"Max positions reached: {len(self.positions)}/{self.max_total_positions}")
            return False
        
        # Check aggregate risk cap (will be validated with account equity)
        position = PositionRisk(
            symbol=symbol,
            entry_price=entry_price,
            stop_price=stop_price,
            shares=shares,
            sector=sector
        )
        
        self.positions[symbol] = position
        return True
    
    def remove_position(self, symbol: str) -> bool:
        """Remove a position (closed)."""
        if symbol in self.positions:
            del self.positions[symbol]
            return True
        return False
    
    def check_aggregate_risk(self, account_equity: float) -> Tuple[bool, float]:
        """
        Check if aggregate risk is within cap.
        
        Returns:
            (within_cap, current_risk_pct)
        """
        total_risk = sum(p.position_risk for p in self.positions.values())
        risk_pct = total_risk / account_equity if account_equity > 0 else 0
        
        return risk_pct <= self.aggregate_risk_cap, risk_pct
    
    def would_breach_aggregate_risk(
        self,
        account_equity: float,
        new_position_risk: float
    ) -> bool:
        """Check if adding a new position would breach aggregate risk cap."""
        current_risk = sum(p.position_risk for p in self.positions.values())
        projected_risk = current_risk + new_position_risk
        projected_pct = projected_risk / account_equity if account_equity > 0 else 0
        
        return projected_pct > self.aggregate_risk_cap
    
    def check_sector_cap(self, symbol: str) -> bool:
        """Check if adding symbol would breach sector cap."""
        sector = self.sector_taxonomy.get_sector(symbol)
        sector_count = sum(1 for p in self.positions.values() if p.sector == sector)
        return sector_count < self.max_per_sector
    
    def get_available_slots(self) -> int:
        """Get number of available position slots."""
        return max(0, self.max_total_positions - len(self.positions))
    
    def get_sector_counts(self) -> Dict[str, int]:
        """Get current position count per sector."""
        counts = {}
        for p in self.positions.values():
            counts[p.sector] = counts.get(p.sector, 0) + 1
        return counts
    
    def get_total_risk(self) -> float:
        """Get total portfolio risk in currency."""
        return sum(p.position_risk for p in self.positions.values())
    
    def get_risk_by_sector(self) -> Dict[str, float]:
        """Get risk breakdown by sector."""
        risk = {}
        for p in self.positions.values():
            risk[p.sector] = risk.get(p.sector, 0) + p.position_risk
        return risk
    
    def update_stop(self, symbol: str, new_stop: float):
        """Update stop price for existing position."""
        if symbol in self.positions:
            self.positions[symbol].stop_price = new_stop
    
    def update_shares(self, symbol: str, new_shares: int):
        """Update shares for existing position (e.g., after partial sell)."""
        if symbol in self.positions:
            self.positions[symbol].shares = new_shares
    
    def clear(self):
        """Clear all positions."""
        self.positions.clear()


def create_portfolio_risk_manager(config=None) -> PortfolioRiskManager:
    return PortfolioRiskManager(config)