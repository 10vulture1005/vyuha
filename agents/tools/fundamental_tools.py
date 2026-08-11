# agents/tools/fundamental_tools.py
from pydantic import BaseModel
from typing import Dict, List, Any
from db.models import FundamentalSnapshot

class FundamentalSnapshotSchema(BaseModel):
    symbol: str
    roe: float
    debt_to_equity: float
    eps_growth_3y: float
    raw_json: Dict[str, Any]

def scrape_symbol_fundamentals(symbol: str) -> FundamentalSnapshotSchema:
    # Use fallback stylized generator since live scraping is complex
    from data.fundamentals_provider import FundamentalsProvider
    from datetime import date
    prov = FundamentalsProvider()
    metrics = prov.get_latest_fundamentals(symbol, date.today())
    return FundamentalSnapshotSchema(
        symbol=symbol,
        roe=metrics.get("roce_ttm", 0.0),
        debt_to_equity=metrics.get("de_ratio", 0.0),
        eps_growth_3y=metrics.get("eps_growth_yoy", 0.0),
        raw_json=metrics
    )

def passes_hard_filters(snapshot: FundamentalSnapshotSchema) -> bool:
    from config import thresholds
    t = thresholds.get("fundamental", {})
    if snapshot.roe < t.get("min_roe", 15.0):
        return False
    if snapshot.debt_to_equity > t.get("max_debt_equity", 1.0):
        return False
    if snapshot.eps_growth_3y < t.get("min_eps_cagr_3y", 10.0):
        return False
    return True

def compute_relative_conviction_scores(snapshots: List[FundamentalSnapshotSchema]) -> Dict[str, float]:
    scores = {}
    for snap in snapshots:
        score = 50.0 # Base
        score += snap.roe * 0.5
        score += snap.eps_growth_3y * 0.5
        score -= snap.debt_to_equity * 10
        scores[snap.symbol] = min(max(score, 0.0), 100.0)
    return scores
