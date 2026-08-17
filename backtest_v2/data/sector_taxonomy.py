from pathlib import Path
from typing import Dict, Optional
import pandas as pd
from loguru import logger

from config.settings import BASE_DIR


SECTORS = [
    "FINANCIAL", "IT", "CONSUMER", "ENERGY", "PHARMA",
    "INDUSTRIAL", "METALS", "CEMENT", "AUTO", "TELECOM",
    "UTILITIES", "CHEMICALS", "INFRASTRUCTURE", "CONGLOMERATE"
]


DEFAULT_SECTOR_MAPPING = {
    "RELIANCE": "ENERGY",
    "TCS": "IT",
    "INFY": "IT",
    "HDFCBANK": "FINANCIAL",
    "ICICIBANK": "FINANCIAL",
    "HINDUNILVR": "CONSUMER",
    "ITC": "CONSUMER",
    "SBIN": "FINANCIAL",
    "BHARTIARTL": "TELECOM",
    "KOTAKBANK": "FINANCIAL",
    "LT": "INDUSTRIAL",
    "AXISBANK": "FINANCIAL",
    "ASIANPAINT": "CONSUMER",
    "MARUTI": "AUTO",
    "SUNPHARMA": "PHARMA",
    "TITAN": "CONSUMER",
    "ULTRACEMCO": "CEMENT",
    "BAJFINANCE": "FINANCIAL",
    "NESTLEIND": "CONSUMER",
    "WIPRO": "IT",
    "ONGC": "ENERGY",
    "POWERGRID": "UTILITIES",
    "NTPC": "UTILITIES",
    "COALINDIA": "ENERGY",
    "TATASTEEL": "METALS",
    "JSWSTEEL": "METALS",
    "HINDALCO": "METALS",
    "ADANIENT": "CONGLOMERATE",
    "ADANIPORTS": "INFRASTRUCTURE",
    "ADANIGREEN": "ENERGY",
    "ADANIPOWER": "ENERGY",
    "BAJAJFINSV": "FINANCIAL",
    "HDFCLIFE": "FINANCIAL",
    "SBILIFE": "FINANCIAL",
    "TECHM": "IT",
    "HCLTECH": "IT",
    "BRITANNIA": "CONSUMER",
    "DABUR": "CONSUMER",
    "GODREJCP": "CONSUMER",
    "MARICO": "CONSUMER",
    "COLPAL": "CONSUMER",
    "PIDILITIND": "CONSUMER",
    "BERGEPAINT": "CONSUMER",
    "HAVELLS": "INDUSTRIAL",
    "VOLTAS": "INDUSTRIAL",
    "CROMPTON": "INDUSTRIAL",
    "AMBUJACEM": "CEMENT",
    "SHREECEM": "CEMENT",
    "DALBHARAT": "CEMENT",
    "RAMCOCEM": "CEMENT",
    "JINDALSTEL": "METALS",
    "SAIL": "METALS",
    "NATIONALUM": "METALS",
    "VEDL": "METALS",
    "HINDZINC": "METALS",
    "COCHINSHIP": "INDUSTRIAL",
    "MAZAGONDOC": "INDUSTRIAL",
    "GRSE": "INDUSTRIAL",
    "BHEL": "INDUSTRIAL",
    "BEL": "INDUSTRIAL",
    "HAL": "INDUSTRIAL",
    "DRREDDY": "PHARMA",
    "CIPLA": "PHARMA",
    "LUPIN": "PHARMA",
    "AUROPHARMA": "PHARMA",
    "BIOCON": "PHARMA",
    "TORNTPHARM": "PHARMA",
    "ZYDUSLIFE": "PHARMA",
    "ALKEM": "PHARMA",
    "ABBOTINDIA": "PHARMA",
    "PFIZER": "PHARMA",
    "GLAXO": "PHARMA",
    "SANOFI": "PHARMA",
    "MERCK": "PHARMA",
    "ASTRAZEN": "PHARMA",
    "NOVARTIS": "PHARMA",
    "GSK": "PHARMA",
    "LINDEINDIA": "CHEMICALS",
    "PIDILITIND": "CHEMICALS",
    "SRF": "CHEMICALS",
    "NAVINFLUOR": "CHEMICALS",
    "TATACHEM": "CHEMICALS",
    "DEEPAKNTR": "CHEMICALS",
    "AARTIIND": "CHEMICALS",
    "VINATIORGA": "CHEMICALS",
    "ROSSARI": "CHEMICALS",
    "GUJGAS": "UTILITIES",
    "IGL": "UTILITIES",
    "MGL": "UTILITIES",
    "GAIL": "UTILITIES",
    "PETRONET": "UTILITIES",
    "IOC": "ENERGY",
    "BPCL": "ENERGY",
    "HPCL": "ENERGY",
    "OIL": "ENERGY",
    "GAIL": "UTILITIES",
    "INDEX_NIFTY50": "INDEX",
    "INDEX_NIFTY500": "INDEX",
}


class SectorTaxonomy:
    def __init__(self, mapping_file: Optional[str] = None):
        self.mapping = DEFAULT_SECTOR_MAPPING.copy()
        self._mock_sector_cache: Dict[str, str] = {}
        if mapping_file:
            self.load_from_file(mapping_file)
    
    def load_from_file(self, mapping_file: str):
        path = Path(mapping_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        if path.exists():
            try:
                df = pd.read_csv(path)
                if "symbol" in df.columns and "sector" in df.columns:
                    file_mapping = dict(zip(df["symbol"], df["sector"]))
                    self.mapping.update(file_mapping)
                    logger.info(f"Loaded {len(file_mapping)} sector mappings from {path}")
                else:
                    logger.warning(f"Sector mapping file {path} must have 'symbol' and 'sector' columns")
            except Exception as e:
                logger.warning(f"Failed to load sector mapping from {path}: {e}")
        else:
            logger.info(f"Sector mapping file not found at {path}, using defaults")
    
    def _assign_mock_sector(self, symbol: str) -> str:
        """Assign a consistent sector to mock symbols (STOCKxxxx)."""
        if symbol in self._mock_sector_cache:
            return self._mock_sector_cache[symbol]
        
        # Use hash of symbol for consistent assignment
        import hashlib
        hash_val = int(hashlib.md5(symbol.encode()).hexdigest(), 16)
        sector_idx = hash_val % len(SECTORS)
        sector = SECTORS[sector_idx]
        self._mock_sector_cache[symbol] = sector
        return sector
    
    def get_sector(self, symbol: str) -> str:
        if symbol in self.mapping:
            return self.mapping[symbol]
        
        # Handle mock symbols (STOCKxxxx format)
        if symbol.startswith("STOCK") and symbol[5:].isdigit():
            return self._assign_mock_sector(symbol)
        
        return "UNKNOWN"
    
    def get_all_sectors(self) -> Dict[str, str]:
        return self.mapping.copy()


_sector_taxonomy = None

def get_sector_taxonomy(mapping_file: Optional[str] = None) -> SectorTaxonomy:
    global _sector_taxonomy
    if _sector_taxonomy is None:
        _sector_taxonomy = SectorTaxonomy(mapping_file)
    return _sector_taxonomy