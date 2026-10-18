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
    """Fetch real statements or raise; synthetic providers are never consulted."""
    from data.screener_client import ScreenerClient
    from data.screener_fundamentals import normalize
    return snapshot_from_records(symbol, normalize(ScreenerClient().fetch(symbol)))


def snapshot_from_records(symbol, records, as_of=None) -> FundamentalSnapshotSchema:
    from datetime import date
    import math
    import pandas as pd
    from investor.data import resolve_usable_from

    when = pd.Timestamp(as_of or date.today())
    annual = sorted([
        r for r in records if r['symbol'] == symbol and r['frequency'] == 'yearly'
        and r.get('data_source') in ('yfinance', 'screener', 'yfinance+screener')
        and resolve_usable_from(r['period_end'], r.get('filing_date')) <= when
    ], key=lambda r: r['period_end'])
    if not annual:
        raise ValueError(f'{symbol}: no verified usable statements')
    last = annual[-1]
    target = pd.Timestamp(last['period_end']) - pd.DateOffset(years=3)
    first = next((r for r in annual if pd.Timestamp(r['period_end']) == target), None)
    if first is None:
        raise ValueError(f'{symbol}: missing three-year EPS history')
    def value(row, field):
        result = float(row.get(field, float('nan')))
        if not math.isfinite(result):
            raise ValueError(f'{symbol}: missing {field}')
        return result
    # Use the same EPS definition at both endpoints.
    eps_field = 'eps_diluted' if pd.notna(last.get('eps_diluted')) and pd.notna(first.get('eps_diluted')) else 'eps_basic'
    equity, debt = value(last, 'equity'), value(last, 'debt')
    start, end = value(first, eps_field), value(last, eps_field)
    if equity <= 0 or start <= 0 or end <= 0 or debt < 0:
        raise ValueError(f'{symbol}: invalid ratio denominator or EPS')
    return FundamentalSnapshotSchema(
        symbol=symbol,
        roe=100 * value(last, 'pat') / equity,
        debt_to_equity=debt / equity,
        eps_growth_3y=100 * ((end / start) ** (1 / 3) - 1),
        raw_json={'data_source': last['data_source'], 'period_end': last['period_end']},
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
