# scripts/ingest_ohlc.py
import sys
from pathlib import Path
import pandas as pd
import yfinance as yf
from loguru import logger

from db.session import get_session
from db.models import Watchlist, WatchlistStatus
from config.settings import BASE_DIR

OHLC_DIR = BASE_DIR / "data" / "raw" / "ohlc"
OHLC_DIR.mkdir(parents=True, exist_ok=True)

def ingest_active_ohlc():
    """Downloads 2 years of daily OHLCV data for all ACTIVE watchlist symbols."""
    logger.info("Starting Daily EOD OHLCV Ingestion...")
    
    with get_session() as session:
        active_symbols = [r[0] for r in session.query(Watchlist.symbol).filter(
            Watchlist.status == WatchlistStatus.ACTIVE.value
        ).all()]
        
    logger.info(f"Targeting {len(active_symbols)} ACTIVE symbols for OHLCV refresh.")
    
    # Ingest Index OHLC for Regime and Momentum filtering
    indices = {"^NSEI": "INDEX_NIFTY50", "^CRSLDX": "INDEX_NIFTY500"}
    for ticker, filename in indices.items():
        try:
            df = yf.download(ticker, period="10y", interval="1d", progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 50:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.index.name = "Date"
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
                out_path = OHLC_DIR / f"{filename}.csv"
                df.to_csv(out_path)
                logger.debug(f"[{filename}] Saved {len(df)} index bars.")
            else:
                logger.warning(f"[{filename}] Insufficient index data fetched.")
        except Exception as e:
            logger.error(f"[{filename}] Index ingestion failed: {e}")
            
    for symbol in active_symbols:
        # NSE symbols on Yahoo Finance carry .NS suffix
        ticker = f"{symbol}.NS"
        try:
            df = yf.download(ticker, period="10y", interval="1d", progress=False, auto_adjust=True)
            if df.empty or len(df) < 50:
                logger.warning(f"[{symbol}] Insufficient OHLCV data fetched ({len(df)} rows).")
                continue
                
            # Flatten multi-index columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
                
            df.index.name = "Date"
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            
            out_path = OHLC_DIR / f"{symbol}.csv"
            df.to_csv(out_path)
            logger.debug(f"[{symbol}] Successfully saved {len(df)} bars to {out_path}")
        except Exception as e:
            logger.error(f"[{symbol}] OHLCV ingestion failed: {e}")

if __name__ == "__main__":
    try:
        ingest_active_ohlc()
        logger.info("EOD OHLCV Ingestion completed successfully.")
    except Exception as e:
        logger.exception(f"Fatal error during OHLCV ingestion: {e}")
        sys.exit(1)
