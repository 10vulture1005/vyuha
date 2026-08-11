# scripts/live_monitor.py
import time
from decimal import Decimal
from loguru import logger
from db.session import get_session
from db.models import PortfolioHolding, HoldingStatus
from data.live_market_api import LiveMarketAPI
from core.capital_allocator import execute_sell

def check_live_stops():
    """Polls the DB for open holdings and checks them against live API prices."""
    with get_session() as session:
        holdings = session.query(PortfolioHolding).filter(
            PortfolioHolding.status == HoldingStatus.OPEN.value
        ).all()
        
        if not holdings:
            logger.debug("No open holdings to monitor.")
            return

        symbols_to_check = [h.symbol for h in holdings]
        logger.info(f"Checking live stops for: {symbols_to_check}")
        
        # We fetch without units for fast numeric parsing
        live_quotes = LiveMarketAPI.get_batch_quotes(symbols_to_check, with_units=False)
        
        if not live_quotes:
            logger.error("Failed to fetch live quotes. Network issue?")
            return
            
        quote_map = {q.get("symbol"): q for q in live_quotes}
        
        for h in holdings:
            quote = quote_map.get(h.symbol)
            if not quote:
                logger.warning(f"No live data returned for {h.symbol}")
                continue
                
            last_price = Decimal(str(quote.get("last_price", 0)))
            if last_price <= 0:
                continue
                
            logger.debug(f"[{h.symbol}] Live: ₹{last_price} | Stop: ₹{h.trailing_stop_price}")
            
            # Intraday Stop Breach!
            if last_price <= h.trailing_stop_price:
                reason = f"LIVE INTRADAY STOP BREACH: {h.symbol} hit ₹{last_price} (Stop: ₹{h.trailing_stop_price})"
                logger.warning(reason)
                # Ensure we execute sell. The allocator logs it natively.
                execute_sell(session, h.symbol, h.qty, last_price, reason)

if __name__ == "__main__":
    logger.add("logs/live_monitor.log", rotation="10 MB", level="INFO")
    logger.info("Starting VYUHA Live Stop-Loss Monitor...")
    
    # In a real environment, this might run continuously or via cron every 5 minutes.
    # For demo purposes, we will run one iteration.
    check_live_stops()
