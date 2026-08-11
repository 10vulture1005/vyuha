from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional, Any
from pydantic import BaseModel, Field

class HoldingResponse(BaseModel):
    symbol: str
    qty: int
    avg_buy_price: float
    first_buy_date: date
    trailing_stop_price: float
    status: str

    class Config:
        from_attributes = True

class LedgerResponse(BaseModel):
    id: int
    txn_date: datetime
    amount: float
    txn_type: str
    running_balance: float

    class Config:
        from_attributes = True

class TradeLogResponse(BaseModel):
    id: int
    symbol: str
    txn_type: str
    qty: int
    price: float
    txn_date: datetime
    dp_charge: float
    reason: str
    triggered_by: str

    class Config:
        from_attributes = True

class WatchlistResponse(BaseModel):
    symbol: str
    status: str
    conviction_score: float
    last_updated: datetime

    class Config:
        from_attributes = True

class PerformanceMetricsResponse(BaseModel):
    cagr_pct: float = Field(..., description="Compound Annual Growth Rate in %")
    sharpe_ratio: float = Field(..., description="Annualized Sharpe Ratio (Rf = 6.5%)")
    sortino_ratio: float = Field(..., description="Annualized Sortino Ratio")
    max_drawdown_pct: float = Field(..., description="Maximum peak-to-trough drawdown in %")
    total_value_inr: float = Field(..., description="Current total portfolio value (Holdings + Cash)")
    cash_balance_inr: float = Field(..., description="Unallocated cash sitting in ledger")

class HealthResponse(BaseModel):
    status: str = "healthy"
    database: str = "connected"
    last_successful_run: Optional[datetime] = None
    seconds_since_last_run: Optional[int] = None

class PipelineRunResponse(BaseModel):
    status: str
    execution_mode: str
    decision_summary: dict[str, Any]
