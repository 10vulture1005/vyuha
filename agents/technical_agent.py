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
        
        # Regime checks
        nifty50 = load_ohlc_df("INDEX_NIFTY50")
        nifty500 = load_ohlc_df("INDEX_NIFTY500")
        indiavix = load_ohlc_df("INDEX_INDIAVIX")
        
        if nifty50.empty or nifty500.empty:
            logger.warning("Index data missing. Cannot determine market regime. Proceeding with caution.")
        else:
            regime = determine_market_regime(nifty50, nifty500)
            if regime == "RED":
                logger.warning("🔴 MARKET REGIME IS RED (Both indices below 200 DMA). Blocking all new setups.")
                return []
                
        if not indiavix.empty and is_vix_extreme(indiavix):
            logger.warning("🌋 VIX IS EXTREME (>80th percentile). Blocking all new setups and moving to cash regime.")
            return []
            
        with get_session() as session:
            active_watchlist = session.query(Watchlist).filter(Watchlist.status == WatchlistStatus.ACTIVE.value).all()
            symbols = [w.symbol for w in active_watchlist]
            logger.info(f"Loaded {len(symbols)} ACTIVE symbols for technical timing analysis.")
            
            signals_generated = []
            
            for sym in symbols:
                df = load_ohlc_df(sym)
                if df.empty:
                    continue
                    
                sig_res = detect_w_bottom(df) or detect_bb_squeeze(df)
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
