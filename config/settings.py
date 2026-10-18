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
    MISTRAL_API_KEY: Optional[SecretStr] = Field(
        default=None, description="Mistral API Key"
    )
    NVIDIA_API_KEY: Optional[SecretStr] = Field(
        default=None, description="NVIDIA NIM API Key"
    )
    OPENAI_COMPATIBLE_API_KEY: Optional[SecretStr] = Field(
        default=None, description="API key for a custom OpenAI-compatible endpoint"
    )
    OPENAI_COMPATIBLE_BASE_URL: Optional[str] = Field(
        default=None, description="Base URL for a custom OpenAI-compatible endpoint"
    )
    OPENROUTER_API_KEY: Optional[SecretStr] = Field(
        default=None, description="OpenRouter API Key"
    )
    SENTIMENT_TIER2_ENABLED: bool = Field(
        default=True,
        description=(
            "Master switch for the Tier 2 LLM governance pass in "
            "agents/tools/news_tools.py. Set false to run Tier 1 regex "
            "screening only. Vetoes are still applied from Tier 1; Tier 2 "
            "only ever *adds* vetoes, so disabling it widens the funnel "
            "(more names reach the technical scan), it never relaxes a "
            "hard filter. Records are still written as SCREENING_FAILED "
            "rather than reported clean."
        ),
    )
    SENTIMENT_TIER2_TIMEOUT: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Per-provider HTTP timeout in seconds for the Tier 2 LLM call. "
            "The OpenAI SDK defaults to 600s with 2 retries per provider; "
            "across four candidate routes that is ~48 minutes for a single "
            "symbol when the provider is unhealthy."
        ),
    )
    SENTIMENT_FINBERT_ENABLED: bool = Field(
        default=True,
        description=(
            "Use local FinBERT (ProsusAI/finbert) as a pre-LLM gate. FinBERT "
            "scores headline polarity in one batched pass; only symbols it "
            "reads as negative are escalated to the LLM. It is an OR-gate: "
            "the Tier 1 regex still vetoes on its own, so enabling this can "
            "only add escalations, never remove a veto. Requires torch + "
            "transformers (~1.3GB). NOTE: the FinBERT WEIGHTS are licensed "
            "CC BY-NC-SA 4.0 (non-commercial only); the GitHub training code "
            "is Apache-2.0 but that does not cover the weights."
        ),
    )
    SENTIMENT_FINBERT_THRESHOLD: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description=(
            "Negative-polarity probability at or above which a symbol is "
            "escalated to the LLM. FinBERT returns three labels; this is "
            "P(negative) across the batch."
        ),
    )
    SENTIMENT_TIER2_BATCH_SIZE: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "Symbols per chunked Tier 2 LLM request. One request returns a "
            "JSON array of verdicts, so the daily LLM cost scales with "
            "cache misses / batch_size rather than misses."
        ),
    )
    DEFAULT_LLM_MODEL: str = Field(
        default="claude-3-5-sonnet-20241022",
        description="Default model for CrewAI reasoning",
    )

    # ─── News Provider ────────────────────────────────────────────────────────
    DRISHTI_API_KEY: Optional[SecretStr] = Field(
        default=None,
        description="Drishti API key for batched news (one call covers many symbols).",
    )
    NEWS_PROVIDER: str = Field(
        default="auto",
        description=(
            "News source for the sentiment stage. 'marketaux' scans an "
            "alternating slice of the watchlist (free tier is 100 req/day at "
            "3 articles each), 'drishti' uses the batched Drishti API, and "
            "'auto' runs MarketAux then Drishti. Symbols no provider covers "
            "are recorded SCREENING_FAILED, never as clean."
        ),
    )
    DRISHTI_BATCH_SIZE: int = Field(
        default=20,
        ge=1,
        le=20,
        description=(
            "Symbols per Drishti request. The API starts returning HTTP 400 "
            "somewhere above 20. 20 is the measured sweet spot: a 105-symbol "
            "watchlist costs ~11s in 6 requests."
        ),
    )
    DRISHTI_PAGE_LIMIT: int = Field(
        default=50,
        ge=1,
        le=50,
        description=(
            "Max page size per Drishti request. The API rejects any value "
            "above 50 with HTTP 422."
        ),
    )
    DRISHTI_MAX_PAGES: int = Field(
        default=1,
        ge=1,
        le=10,
        description=(
            "Pages fetched per batch. One page x 50 already averages ~3.5 "
            "headlines per covered symbol; a second page doubles the request "
            "count and trips the per-minute 429 quota, which costs far more "
            "than the extra headlines are worth."
        ),
    )
    DRISHTI_MIN_INTERVAL: float = Field(
        default=1.2,
        ge=0.0,
        description=(
            "Seconds to wait between Drishti batches. The API enforces a "
            "per-minute request quota; firing a 105-symbol watchlist as 7 "
            "back-to-back batches trips HTTP 429 partway through, which then "
            "forces the slow per-symbol RSS fallback for everything missed."
        ),
    )

    # ─── MarketAux (current primary news provider) ─────────────────────────
    MARKETAUX_API_KEY: Optional[SecretStr] = Field(
        default=None,
        description="MarketAux API token for news with per-entity sentiment.",
    )
    MARKET_MAX_REQUESTS_PER_DAY: int = Field(
        default=50,
        ge=1,
        le=100,
        description=(
            "MarketAux requests spent per day. The free plan allows 100 "
            "daily; half is reserved so a re-run on the same day has budget "
            "left rather than hard-failing."
        ),
    )
    MARKET_MAX_PAGES: int = Field(
        default=8,
        ge=1,
        le=20,
        description=(
            "Pages fetched per 20-symbol group. Every response is capped at 3 "
            "articles on the free plan, so paging is the only way to convert "
            "the 100-requests/day allowance into coverage. At 6 groups x 8 "
            "pages that is ~48 requests and ~144 articles; deep paging has "
            "diminishing returns because the most recent articles cluster on "
            "a handful of names."
        ),
    )
    MARKET_SYMBOLS_PER_REQUEST: int = Field(
        default=20,
        ge=1,
        le=100,
        description=(
            "Symbols per MarketAux request. One request covers many symbols, "
            "but the free plan returns only 3 articles for it, so a wide "
            "symbol slice per request is what makes the budget go further."
        ),
    )
    MARKET_SLICE_SIZE: int = Field(
        default=50,
        ge=1,
        description=(
            "Watchlist symbols scanned per run. The watchlist is split into "
            "alternating slices and each day's run covers the next slice, "
            "persisted so a re-run resumes instead of double-spending quota."
        ),
    )
    NEWS_USE_INDIANAPI: bool = Field(
        default=False,
        description=(
            "DEPRECATED and unused. The IndianAPI.in and Google News RSS "
            "per-symbol path has been removed; news now comes only from the "
            "batched MarketAux/Drishti providers."
        ),
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

    @field_validator("NEWS_PROVIDER")
    @classmethod
    def validate_news_provider(cls, v: str) -> str:
        """Same philosophy as STRATEGY_MODE: blank defaults, typo raises.

        'auto' is the safe default because Drishti needs an API key that may
        not be configured, in which case it silently degrades to RSS.
        """
        allowed = {"auto", "marketaux", "drishti"}
        provider = (v or "").strip().lower()
        if not provider:
            return "auto"
        if provider not in allowed:
            raise ValueError(
                f"NEWS_PROVIDER must be one of {sorted(allowed)}, got {v!r}"
            )
        return provider

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
