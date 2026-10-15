# config/settings.py
"""System-level configuration loaded from environment variables or .env file.

Uses pydantic-settings for strict type safety, validation, and casting.
All agents and subsystems import `settings` from this module as the
sole configuration authority.
"""
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """System-level configuration loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ─── Database ────────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        default="sqlite:///vyuha.db",
        description="Database connection URI (SQLite for dev, PostgreSQL for prod)",
    )

    # ─── API Keys ────────────────────────────────────────────────────────────
    ANTHROPIC_API_KEY: Optional[SecretStr] = Field(
        default=None, description="Anthropic API Key for CrewAI"
    )
    OPENAI_API_KEY: Optional[SecretStr] = Field(
        default=None, description="OpenAI API Key (fallback/embedding)"
    )
    XAI_API_KEY: Optional[SecretStr] = Field(
        default=None, description="xAI API Key for Grok models"
    )
    HF_API_TOKEN: Optional[SecretStr] = Field(
        default=None, description="Hugging Face API token for hosted FinBERT inference"
    )
    INDIAN_API_KEY: Optional[SecretStr] = Field(
        default=None, description="IndianAPI.in stock market API key"
    )
    GROQ_API_KEY: Optional[SecretStr] = Field(
        default=None, description="Groq API Key for LPU models"
    )
    GOOGLE_API_KEY: Optional[SecretStr] = Field(
        default=None, description="Google API Key (Gemini)"
    )
    DEEPSEEK_API_KEY: Optional[SecretStr] = Field(
        default=None, description="DeepSeek API Key"
    )
    OPENROUTER_API_KEY: Optional[SecretStr] = Field(
        default=None, description="OpenRouter API Key"
    )
    DEFAULT_LLM_MODEL: str = Field(
        default="claude-3-5-sonnet-20241022",
        description="Default model for CrewAI reasoning",
    )

    # ─── LLM Provider Routing ─────────────────────────────────────────────────
    # OpenRouter is the primary source of LLM calls for decisions; Groq is the
    # fallback. Both the CrewAI chain (crew/crew_definition.py) and the Vyuha
    # Agent (agents/vyuha_agent/config_builder.py) read these four keys, so
    # flipping the primary provider is a one-line .env edit rather than a code
    # change. The fallback stays INERT while its API key is unset — see
    # config/llm_routing.py for the shared resolution helper.
    LLM_PROVIDER: str = Field(
        default="openrouter",
        description="Primary LLM provider for decisions: openrouter|groq|openai|anthropic|...",
    )
    LLM_MODEL: str = Field(
        default="nvidia/nemotron-3-ultra-550b-a55b:free",
        description="Primary model ID. On OpenRouter this is the namespaced form (vendor/model:variant).",
    )
    LLM_FALLBACK_PROVIDER: str = Field(
        default="groq",
        description="Fallback provider used when the primary call fails for any reason. Empty string disables fallback.",
    )
    LLM_FALLBACK_MODEL: str = Field(
        default="openai/gpt-oss-20b",
        description="Fallback model ID, interpreted by LLM_FALLBACK_PROVIDER's naming scheme",
    )

    # ─── TradingAgents LLM Pipeline ──────────────────────────────────────────
    TRADINGAGENTS_ENABLED: bool = Field(
        default=False,
        description="Master switch: when True, TradingAgents LLM pipeline runs as a cross-check alongside rule-based agents",
    )
    TRADINGAGENTS_LLM_PROVIDER: str = Field(
        default="openrouter",
        description="LLM provider for TradingAgents: openai|anthropic|google|deepseek|openrouter|groq|ollama|openai_compatible",
    )
    TRADINGAGENTS_DEEP_THINK_LLM: str = Field(
        default="nvidia/nemotron-3-ultra-550b-a55b:free",
        description="Model used for complex reasoning nodes (portfolio manager, researcher judge)",
    )
    TRADINGAGENTS_QUICK_THINK_LLM: str = Field(
        default="nvidia/nemotron-3-ultra-550b-a55b:free",
        description="Model used for fast tool-calling nodes (analysts)",
    )
    TRADINGAGENTS_MAX_DEBATE_ROUNDS: int = Field(
        default=1, ge=1, le=3,
        description="Bull/bear research debate rounds",
    )
    TRADINGAGENTS_MAX_RISK_ROUNDS: int = Field(
        default=1, ge=1, le=3,
        description="Aggressive/conservative/neutral risk debate rounds",
    )
    TRADINGAGENTS_OUTPUT_LANGUAGE: str = Field(
        default="English",
        description="Language for analyst reports and final decision",
    )
    TRADINGAGENTS_CHECKPOINT_ENABLED: bool = Field(
        default=False,
        description="Enable LangGraph checkpoint resume (useful for long Indian-market backtests)",
    )
    TRADINGAGENTS_DATA_VENDORS: str = Field(
        default="yfinance",
        description="Comma-separated fallback chain: yfinance,alpha_vantage",
    )
    TRADINGAGENTS_REQUIRE_CONFIRMATION: bool = Field(
        default=True,
        description="If True, LLM recommendations are logged but only acted on when they agree with the rule-based pipeline",
    )
    TRADINGAGENTS_HOLDING_DAYS: int = Field(
        default=5, ge=1, le=30,
        description="Days used by TradingAgents reflection layer for realized-return lookups",
    )

    # ─── Telegram ────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: Optional[SecretStr] = Field(
        default=None, description="Telegram Bot API Token"
    )
    TELEGRAM_CHAT_ID: Optional[str] = Field(
        default=None, description="Target Chat ID for notifications"
    )

    # ─── Capital & Position Sizing ───────────────────────────────────────────
    INITIAL_CAPITAL: float = Field(
        default=100000.0, ge=10000.0,
        description="Initial lump sum capital in INR",
    )
    MONTHLY_SIP_AMOUNT: int = Field(
        default=1000, ge=1000,
        description="Monthly capital credit in INR",
    )
    MAX_POSITIONS: int = Field(
        default=8, ge=1,
        description="Maximum concurrent portfolio holdings",
    )
    RISK_PER_TRADE_PCT: float = Field(
        default=0.01, ge=0.001, le=0.05,
        description="Percentage of account equity risked per trade",
    )
    MAX_PORTFOLIO_HEAT_PCT: float = Field(
        default=0.06, ge=0.01, le=0.15,
        description="Maximum total risk across all open positions",
    )
    MAX_POSITION_CONCENTRATION_PCT: float = Field(
        default=0.25, ge=0.05, le=1.0,
        description="Maximum percentage of equity in a single position",
    )
    MAX_MONTHLY_DRAWDOWN_PCT: float = Field(
        default=5.0, ge=1.0, le=20.0,
        description="Max allowed MTD portfolio drawdown percentage before halting buys",
    )

    # ─── Transaction Friction ────────────────────────────────────────────────
    DP_CHARGE_PER_SELL: float = Field(
        default=15.0, ge=0.0,
        description="Flat DP charge debited per sell transaction",
    )
    INCLUDE_FRICTION: bool = Field(
        default=True,
        description="When False, backtests run gross: no STT/exchange/SEBI/DP/slippage",
    )
    STT_PCT: float = Field(
        default=0.1,
        description="Securities Transaction Tax (STT) percentage (0.1%)",
    )
    EXCHANGE_TXN_CHARGE_PCT: float = Field(
        default=0.00345,
        description="Exchange transaction charge percentage (0.00345%)",
    )
    SEBI_TURNOVER_FEE_PCT: float = Field(
        default=0.0001,
        description="SEBI turnover fee percentage (0.0001%)",
    )

    # ─── Operational ─────────────────────────────────────────────────────────
    LIVE_TRADING_ENABLED: bool = Field(
        default=False, description="Safety switch: False = Paper/Advisory mode"
    )
    EXIT_MODE: str = Field(
        default="full", description="Exit strategy: 'full' or 'trailing_stop_only'"
    )
    LOG_LEVEL: str = Field(
        default="INFO", description="Loguru logging level"
    )

    # ─── Strategy Mode ───────────────────────────────────────────────────────
    STRATEGY_MODE: str = Field(
        default="swing",
        description=(
            "Active strategy profile: 'swing' (default W-Bottom/BB-Squeeze "
            "swing engine, unchanged) or 'investor' (long-horizon portfolio "
            "profile driven by the `investor:` section of thresholds.yaml)."
        ),
    )
    INVESTOR_MAX_HOLDINGS: int = Field(
        default=30, ge=5, le=100,
        description="Hard ceiling on concurrent Investor Mode holdings",
    )
    INVESTOR_DEFENSIVE_SYMBOLS: str = Field(
        default="LIQUIDBEES,GOLDBEES",
        description="Comma-separated defensive-sleeve instruments used when equity exposure is cut",
    )

    @field_validator("STRATEGY_MODE")
    @classmethod
    def validate_strategy_mode(cls, v: str) -> str:
        """Accepts only the two known profiles.

        A BLANK value falls back to 'swing' rather than raising: `STRATEGY_MODE=`
        left in a .env file is a common accident, and refusing to boot the whole
        engine over it would be worse than defaulting safely. Any other
        unrecognised value DOES raise — a typo like 'invest' must not silently
        select a different strategy than the user asked for.
        """
        allowed = {"swing", "investor"}
        mode = (v or "").strip().lower()
        if not mode:
            return "swing"
        if mode not in allowed:
            raise ValueError(
                f"STRATEGY_MODE must be one of {sorted(allowed)}, got {v!r}"
            )
        return mode

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        if not v.startswith(
            ("sqlite:///", "postgresql://", "postgresql+psycopg2://", "postgresql+asyncpg://")
        ):
            raise ValueError(
                "DATABASE_URL must be a valid SQLite or PostgreSQL connection string"
            )
        return v


settings = Settings()
