from datetime import datetime, timezone
from loguru import logger
from decimal import Decimal

from db.session import get_session
from db.models import Watchlist, WatchlistStatus, TechnicalSignal
from agents.tools.ta_tools import (
    load_ohlc_df, detect_w_bottom, detect_bb_squeeze,
)
from agents.tools.regime_tools import determine_market_regime, is_vix_extreme

class TechnicalAgent:
    def __init__(self):
        pass

    def run_technical_scan_execution(self):
        logger.info("Starting Phase 4 Technical Pattern Timing Scan...")
        
        # Regime checks — FIX: fail CLOSED. Previously missing index data logged
        # "proceeding with caution" and kept buying blind into potential RED regimes.
        nifty50 = load_ohlc_df("INDEX_NIFTY50")
        nifty500 = load_ohlc_df("INDEX_NIFTY500")
        indiavix = load_ohlc_df("INDEX_INDIAVIX")
        
        if nifty50.empty or nifty500.empty:
            logger.warning("Index data missing. Blocking new setups (fail-closed) until regime is known.")
            return []
        else:
            regime = determine_market_regime(nifty50, nifty500)
            if regime == "RED":
                logger.warning("🔴 MARKET REGIME IS RED (Both indices below 200 DMA). Blocking all new setups.")
                return []
                
        if not indiavix.empty and is_vix_extreme(indiavix):
            logger.warning("🌋 VIX IS EXTREME (>80th percentile). Blocking all new setups and moving to cash regime.")
            return []
        elif indiavix.empty:
            logger.warning("VIX feed empty — proceeding without VIX block (index regime gate still enforced).")
            
        with get_session() as session:
            # FIX: freshness gate — daily scans previously reused rotting ACTIVE
            # rows for weeks (fundamentals/sentiment run weekly-only). Require a
            # watchlist refresh within 7 days, forcing the weekly pipeline to run.
            from datetime import datetime as _dt, timedelta as _td, date as _date
            active_watchlist = session.query(Watchlist).filter(Watchlist.status == WatchlistStatus.ACTIVE.value).all()
            fresh_cutoff = _dt.utcnow() - _td(days=7)
            fresh_symbols = []
            stale = 0
            for w in active_watchlist:
                lu = w.last_updated
                if isinstance(lu, _date) and not isinstance(lu, _dt):
                    lu_dt = _dt.combine(lu, _dt.min.time())
                else:
                    lu_dt = lu
                try:
                    if lu_dt is None or lu_dt.replace(tzinfo=None) < fresh_cutoff.replace(tzinfo=None):
                        stale += 1
                        continue
                except Exception:
                    pass
                fresh_symbols.append(w.symbol)
            if stale:
                logger.warning(f"Skipping {stale} stale watchlist symbols (no refresh in 7d). Run pipeline with --weekly.")
            symbols = fresh_symbols
            logger.info(f"Loaded {len(symbols)} FRESH ACTIVE symbols for technical timing analysis.")
            
            signals_generated = []
            
            for sym in symbols:
                df = load_ohlc_df(sym)
                if df.empty:
                    continue
                    
                # FIX: evaluate BOTH patterns and keep the strongest instead of
                # `w_bottom or bb_squeeze` short-circuit. The lax W detector
                # previously starved BB_SQUEEZE (100% of live signals were W).
                candidates = []
                try:
                    w = detect_w_bottom(df)
                    if w:
                        candidates.append(w)
                except Exception:
                    pass
                try:
                    b = detect_bb_squeeze(df)
                    if b:
                        candidates.append(b)
                except Exception:
                    pass
                if not candidates:
                    continue
                sig_res = max(candidates, key=lambda s: float(s.signal_strength))
                if sig_res:
                    sig = TechnicalSignal(
                        symbol=sym,
                        signal_date=datetime.now(timezone.utc).date(),
                        pattern_type=sig_res.pattern_type.value,
                        atr_14=Decimal(str(sig_res.atr_14)),
                        entry_price=Decimal(str(sig_res.entry_price)),
                        structural_stop_price=Decimal(str(sig_res.structural_stop_price)),
                        signal_strength=Decimal(str(sig_res.signal_strength)),
                        vol_ratio=Decimal(str(sig_res.vol_ratio)),
                    )
                    session.add(sig)
                    signals_generated.append(sig_res)
                    
            session.commit()
            return signals_generated
