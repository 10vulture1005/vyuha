# data/live_market_api.py
import requests
from typing import Dict, List, Optional
from loguru import logger

class LiveMarketAPI:
    """Wrapper for the free Indian Stock Market API (65.0.104.9).
    Provides real-time pricing data for NSE and BSE equities.
    """
    
    BASE_URL = "http://65.0.104.9"
    
    @classmethod
    def _format_symbol(cls, symbol: str, exchange: str = "NSE") -> str:
        """Formats the symbol according to the API requirements."""
        # Clean symbol
        symbol = symbol.strip().upper()
        
        # If it already has a valid suffix, return as is
        if symbol.endswith(".NS") or symbol.endswith(".BO"):
            return symbol
            
        if exchange.upper() == "BSE":
            return f"{symbol}.BO"
        # API defaults to NSE (.NS) if no suffix is provided, but we can be explicit
        return f"{symbol}.NS"
        
    @classmethod
    def get_stock_quote(cls, symbol: str, exchange: str = "NSE", with_units: bool = False) -> Optional[Dict]:
        """Fetches a single stock quote.
        
        Args:
            symbol: Ticker symbol (e.g. 'RELIANCE')
            exchange: 'NSE' or 'BSE'
            with_units: If True, returns values with units (res=val). Otherwise returns numeric values (res=num).
        """
        formatted_symbol = cls._format_symbol(symbol, exchange)
        res_format = "val" if with_units else "num"
        
        url = f"{cls.BASE_URL}/stock"
        params = {
            "symbol": formatted_symbol,
            "res": res_format
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") == "success":
                return data.get("data")
            else:
                logger.error(f"API Error for {formatted_symbol}: {data.get('message')}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch live quote for {formatted_symbol}: {str(e)}")
            return None

    @classmethod
    def get_batch_quotes(cls, symbols: List[str], exchange: str = "NSE", with_units: bool = False) -> List[Dict]:
        """Fetches multiple stock quotes in a single request."""
        if not symbols:
            return []
            
        formatted_symbols = [cls._format_symbol(sym, exchange) for sym in symbols]
        symbols_str = ",".join(formatted_symbols)
        res_format = "val" if with_units else "num"
        
        url = f"{cls.BASE_URL}/stock/list"
        params = {
            "symbols": symbols_str,
            "res": res_format
        }
        
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            if data.get("status") == "success":
                return data.get("stocks", [])
            else:
                logger.error(f"API Error for batch fetch: {data.get('message')}")
                return []
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch batch quotes: {str(e)}")
            return []
