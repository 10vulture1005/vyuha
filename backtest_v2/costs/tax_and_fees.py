from typing import Optional
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass
from datetime import date, timedelta
from loguru import logger

from backtest_v2.config import breakout_v2_config


@dataclass
class TransactionCosts:
    """Breakdown of transaction costs."""
    stt: float = 0.0
    exchange_charge: float = 0.0
    sebi_fee: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    dp_charge: float = 0.0  # Only on sell
    
    @property
    def total(self) -> float:
        return (self.stt + self.exchange_charge + self.sebi_fee + 
                self.stamp_duty + self.gst + self.dp_charge)


@dataclass
class TaxResult:
    """Tax calculation result."""
    stcg_tax: float = 0.0
    holding_days: int = 0
    is_stcg: bool = True  # True if < 365 days (STCG), False if LTCG


class TransactionCostModel:
    """
    Indian equity transaction cost model.
    
    Charges (as of 2024):
    - STT: 0.1% on sell side (delivery)
    - Exchange transaction charge: 0.00345% (NSE)
    - SEBI turnover fee: 0.0001%
    - Stamp duty: 0.003% on buy side (delivery)
    - GST: 18% on (brokerage + exchange charges + SEBI fee)
    - DP charge: ₹15 per sell transaction (per ISIN)
    
    STCG: 20% on gains if holding < 12 months (post-Budget 2024)
    LTCG: 12.5% on gains > ₹1.25L if holding >= 12 months
    """
    
    def __init__(self, config=None):
        self.config = config or breakout_v2_config
        self.include_costs = self.config.costs.include_stt_gst_stamp
        self.stcg_rate = self.config.costs.stcg_tax_rate
        
        # Rate constants (as decimals)
        self.STT_RATE = 0.001  # 0.1%
        self.EXCHANGE_RATE = 0.0000345  # 0.00345%
        self.SEBI_RATE = 0.000001  # 0.0001%
        self.STAMP_RATE = 0.00003  # 0.003% on buy
        self.GST_RATE = 0.18  # 18%
        self.DP_CHARGE = 15.0  # ₹15 per sell
        
        # Brokerage assumption (for GST calculation)
        self.BROKERAGE_RATE = 0.0003  # 0.03% typical discount broker
    
    def calculate_buy_costs(self, notional: float) -> TransactionCosts:
        """Calculate costs for BUY transaction."""
        if not self.include_costs:
            return TransactionCosts()
        
        costs = TransactionCosts()
        
        # STT on buy: 0 for delivery (STT is on sell side for delivery)
        costs.stt = 0.0
        
        # Exchange charges
        costs.exchange_charge = notional * self.EXCHANGE_RATE
        
        # SEBI fee
        costs.sebi_fee = notional * self.SEBI_RATE
        
        # Stamp duty (on buy side)
        costs.stamp_duty = notional * self.STAMP_RATE
        
        # GST on brokerage + exchange + SEBI
        brokerage = notional * self.BROKERAGE_RATE
        taxable = brokerage + costs.exchange_charge + costs.sebi_fee
        costs.gst = taxable * self.GST_RATE
        
        return costs
    
    def calculate_sell_costs(self, notional: float) -> TransactionCosts:
        """Calculate costs for SELL transaction."""
        if not self.include_costs:
            return TransactionCosts()
        
        costs = TransactionCosts()
        
        # STT on sell (delivery)
        costs.stt = notional * self.STT_RATE
        
        # Exchange charges
        costs.exchange_charge = notional * self.EXCHANGE_RATE
        
        # SEBI fee
        costs.sebi_fee = notional * self.SEBI_RATE
        
        # Stamp duty: 0 on sell side
        costs.stamp_duty = 0.0
        
        # GST on brokerage + exchange + SEBI
        brokerage = notional * self.BROKERAGE_RATE
        taxable = brokerage + costs.exchange_charge + costs.sebi_fee
        costs.gst = taxable * self.GST_RATE
        
        # DP charge
        costs.dp_charge = self.DP_CHARGE
        
        return costs
    
    def calculate_round_trip_costs(self, buy_notional: float, sell_notional: float) -> TransactionCosts:
        """Calculate total round-trip costs."""
        buy_costs = self.calculate_buy_costs(buy_notional)
        sell_costs = self.calculate_sell_costs(sell_notional)
        
        total = TransactionCosts(
            stt=buy_costs.stt + sell_costs.stt,
            exchange_charge=buy_costs.exchange_charge + sell_costs.exchange_charge,
            sebi_fee=buy_costs.sebi_fee + sell_costs.sebi_fee,
            stamp_duty=buy_costs.stamp_duty + sell_costs.stamp_duty,
            gst=buy_costs.gst + sell_costs.gst,
            dp_charge=buy_costs.dp_charge + sell_costs.dp_charge
        )
        
        return total
    
    def calculate_stcg_tax(
        self,
        realized_pnl: float,
        entry_date: date,
        exit_date: date
    ) -> TaxResult:
        """
        Calculate Short-Term Capital Gains tax.
        
        Post-Budget 2024: 20% STCG for holding < 12 months.
        LTCG: 12.5% for holding >= 12 months on gains > ₹1.25L.
        """
        holding_days = (exit_date - entry_date).days
        is_stcg = holding_days < 365
        
        result = TaxResult(
            holding_days=holding_days,
            is_stcg=is_stcg
        )
        
        if realized_pnl <= 0:
            result.stcg_tax = 0.0
            return result
        
        if is_stcg:
            # STCG @ 20%
            result.stcg_tax = realized_pnl * self.stcg_rate
        else:
            # LTCG @ 12.5% on gains > ₹1.25L
            exemption = 125000
            taxable_gain = max(0, realized_pnl - exemption)
            result.stcg_tax = taxable_gain * 0.125
            result.is_stcg = False
        
        return result


def create_cost_model(config=None) -> TransactionCostModel:
    return TransactionCostModel(config)