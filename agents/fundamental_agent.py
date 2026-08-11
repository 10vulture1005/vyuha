import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from datetime import datetime, timezone
from decimal import Decimal

from db.session import get_session
from db.models import FundamentalSnapshot, Watchlist, WatchlistStatus
from universe.computed_point_in_time import PointInTimeUniverse
from agents.tools.fundamental_tools import (
    scrape_symbol_fundamentals,
    passes_hard_filters,
    compute_relative_conviction_scores
)

class FundamentalAgent:
    def __init__(self):
        pass

    def generate_watchlist_execution(self, use_adaptive: bool = True, top_n: int = 150):
        logger.info("Starting Phase 2 Fundamental Watchlist Generation Scan...")
        universe = PointInTimeUniverse()
        symbols = universe.get_midcap_universe(datetime.now(timezone.utc).date(), top_n=top_n)
        logger.info(f"Loaded {len(symbols)} symbols from Point-in-Time universe.")

        valid_snapshots = []
        failed_count = 0

        with get_session() as session:
            for sym in symbols:
                try:
                    snap = scrape_symbol_fundamentals(sym)
                    if passes_hard_filters(snap):
                        valid_snapshots.append(snap)
                    else:
                        failed_count += 1
                        
                    # Persist snapshot
                    db_snap = FundamentalSnapshot(
                        symbol=sym,
                        snapshot_date=datetime.now(timezone.utc).date(),
                        roe=Decimal(str(snap.roe)),
                        debt_equity=Decimal(str(snap.debt_to_equity)),
                        eps_cagr_3y=Decimal(str(snap.eps_growth_3y)),
                    )
                    session.add(db_snap)
                except Exception as e:
                    logger.error(f"Error processing {sym}: {e}")
                    
            logger.info(f"{failed_count} failed hard fundamental filters.")
            
            scores = compute_relative_conviction_scores(valid_snapshots)
            
            # Upsert Watchlist
            for snap in valid_snapshots:
                sym = snap.symbol
                score = scores.get(sym, 50.0)
                
                w = session.query(Watchlist).filter_by(symbol=sym).first()
                if not w:
                    w = Watchlist(symbol=sym)
                    session.add(w)
                    
                w.status = WatchlistStatus.ACTIVE.value
                w.conviction_score = Decimal(str(score))
                w.last_updated = datetime.now(timezone.utc)
                
            session.commit()
            
        # Sort active watchlist symbols highest conviction first
        sorted_symbols = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
        return sorted_symbols
