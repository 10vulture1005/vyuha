# tests/test_config.py
"""Unit tests for VYUHA configuration loading and validation.

Verifies:
    - Default settings load cleanly without external .env
    - Invalid DATABASE_URL prefixes are rejected by the validator
    - thresholds.yaml contains all required quantitative keys
"""
import pytest
from pydantic import ValidationError


def test_settings_default_loading():
    """Verify default settings load cleanly without external .env file."""
    from config.settings import Settings

    s = Settings(DATABASE_URL="sqlite:///test.db")
    assert s.MONTHLY_SIP_AMOUNT == 1000
    assert s.MAX_POSITIONS == 5
    assert s.LIVE_TRADING_ENABLED is False
    assert s.DP_CHARGE_PER_SELL == 15.0
    assert s.DEFAULT_LLM_MODEL == "claude-3-5-sonnet-20241022"


def test_settings_valid_postgres_url():
    """Verify PostgreSQL URLs are accepted."""
    from config.settings import Settings

    s = Settings(DATABASE_URL="postgresql://user:pass@localhost:5432/test")
    assert s.DATABASE_URL.startswith("postgresql://")


def test_settings_valid_sqlite_url():
    """Verify SQLite URLs are accepted."""
    from config.settings import Settings

    s = Settings(DATABASE_URL="sqlite:///vyuha.db")
    assert s.DATABASE_URL.startswith("sqlite:///")


def test_settings_invalid_db_url():
    """Verify pydantic rejects malformed database URLs."""
    from config.settings import Settings

    with pytest.raises(ValidationError):
        Settings(DATABASE_URL="mysql://invalid_engine@localhost/db")


def test_settings_invalid_sip_amount():
    """Verify pydantic rejects SIP below minimum."""
    from config.settings import Settings

    with pytest.raises(ValidationError):
        Settings(DATABASE_URL="sqlite:///test.db", MONTHLY_SIP_AMOUNT=100)


def test_thresholds_yaml_structure():
    """Verify thresholds.yaml contains all required quantitative keys."""
    from config import load_thresholds

    t = load_thresholds()

    # Fundamental section
    assert "fundamental" in t
    assert t["fundamental"]["min_roe"] >= 10.0
    assert t["fundamental"]["max_debt_equity"] > 0
    assert t["fundamental"]["min_eps_cagr_3y"] > 0
    assert t["fundamental"]["degradation_pct"] > 0

    # Technical section
    assert "technical" in t
    assert t["technical"]["atr_period"] > 0
    assert t["technical"]["atr_multiplier"] > 0
    assert t["technical"]["w_bottom_tolerance"] > 0
    assert t["technical"]["bb_lookback"] > 0
    assert t["technical"]["bb_squeeze_percentile"] > 0

    # Allocator section
    assert "allocator" in t
    assert t["allocator"]["max_months_to_afford"] >= 1
    assert t["allocator"]["min_liquidity_vol_20d"] > 0
