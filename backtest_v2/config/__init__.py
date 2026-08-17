from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, Dict, Literal
from pathlib import Path
import yaml


class UniverseConfig(BaseModel):
    index: str = "NIFTY_500"
    pit_data_required: bool = True
    warmup_days: int = 450
    sector_taxonomy: str = "NSE_SNAPSHOT"


class SignalsConfig(BaseModel):
    percentile_window: int = 252
    compression_window: int = 120
    weights: Dict[str, float] = Field(default_factory=lambda: {"T": 0.30, "M": 0.25, "C": 0.20, "B": 0.25})
    s_tech_threshold: float = 0.75


class FiltersConfig(BaseModel):
    regime_filter: bool = True
    liquidity_min_turnover_inr: float = 10000000
    min_turnover_multiplier: float = 20


class PortfolioRiskConfig(BaseModel):
    aggregate_open_risk_cap: float = 0.025
    max_positions_per_sector: int = 2
    max_total_positions: int = 5


class SizingConfig(BaseModel):
    mode: Literal["risk_capped_by_sip", "sip_capped_by_risk", "risk_only"] = "risk_capped_by_sip"
    risk_per_trade: float = 0.005


class EntryConfig(BaseModel):
    order_type: Literal["market_at_open", "gap_capped_limit"] = "market_at_open"
    gap_cap_pct: float = 0.01
    re_entry_cooldown_days: int = 10


class OptionalModulesConfig(BaseModel):
    pyramiding: bool = False
    regime_tighten: bool = False
    signal_decay: bool = False


class ExitsConfig(BaseModel):
    fail_safe_atr_mult: float = 2.0
    chandelier_atr_mult: float = 3.0
    breakeven_floor_r: float = 1.0
    exit_trigger: Literal["intraday_low", "close_only"] = "intraday_low"
    optional_modules: OptionalModulesConfig = Field(default_factory=OptionalModulesConfig)


class CostsConfig(BaseModel):
    stcg_tax_rate: float = 0.20
    include_stt_gst_stamp: bool = True


class DataConfig(BaseModel):
    ohlc_dir: str = "data/raw/ohlc"
    constituents_dir: str = "data/raw/constituents"
    sector_mapping_file: str = "config/sector_mapping.csv"


class BreakoutV2Config(BaseModel):
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    portfolio_risk: PortfolioRiskConfig = Field(default_factory=PortfolioRiskConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    exits: ExitsConfig = Field(default_factory=ExitsConfig)
    costs: CostsConfig = Field(default_factory=CostsConfig)
    data: DataConfig = Field(default_factory=DataConfig)


class BreakoutV2Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_nested_delimiter="__"
    )
    
    config_path: str = "backtest_v2/config/breakout_v2.yaml"
    
    def load_config(self) -> BreakoutV2Config:
        path = Path(self.config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return BreakoutV2Config(**data)


_breakout_v2_settings = BreakoutV2Settings()
breakout_v2_config = _breakout_v2_settings.load_config()