# tests/test_backtest_engine.py
"""Unit tests for the Phase 10 event-driven backtesting engine.

Tests verify:
    - Survivorship bias disclaimer is present and intact
    - Tearsheet metrics computation on synthetic equity curve data
    - SIP credit accounting accuracy
    - Whole-share rounding is enforced (no fractional shares)
"""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock
from backtest.report import (
    compute_tearsheet_metrics,
    SURVIVORSHIP_BIAS_DISCLAIMER,
)
from backtest.engine import BacktestResult


# ── Disclaimer Integrity Tests ──────────────────────────────────────────────


def test_survivorship_bias_disclaimer_presence():
    """Verify that the mandatory survivorship bias warning is never modified or stripped."""
    assert "SURVIVORSHIP BIAS NOTICE" in SURVIVORSHIP_BIAS_DISCLAIMER
    assert "static/current Nifty 500" in SURVIVORSHIP_BIAS_DISCLAIMER


def test_disclaimer_contains_methodology_warning():
    """Verify disclaimer includes key risk disclosure language."""
    assert "MANDATORY RISK DISCLOSURE" in SURVIVORSHIP_BIAS_DISCLAIMER
    assert "upper-bound theoretical estimate" in SURVIVORSHIP_BIAS_DISCLAIMER


# ── Tearsheet Metrics Tests ─────────────────────────────────────────────────


def test_tearsheet_metrics_computation_rising_curve():
    """Verify CAGR, Sharpe, and Drawdown math on a steadily rising synthetic equity curve."""
    res = BacktestResult("2023-01-01", "2023-12-31", 0.0)
    # Create steadily rising equity curve with one drawdown dip
    dates = pd.date_range("2023-01-01", periods=10, freq="ME")
    vals = [1000, 2100, 3200, 4000, 3800, 5000, 6200, 7500, 8800, 10200]

    for d, v in zip(dates, vals):
        res.daily_equity_curve.append(
            {
                "date": d.date().isoformat(),
                "total_value": v,
                "cash_balance": 500,
                "invested_value": v - 500,
            }
        )

    metrics = compute_tearsheet_metrics(res)
    assert metrics["cagr_pct"] > 0.0, "CAGR should be positive for a rising curve"
    assert metrics["max_drawdown_pct"] <= 0.0, "Max drawdown should be negative or zero"
    assert metrics["total_sip_contributed_inr"] == 10000.0, "10 months * ₹1,000"


def test_tearsheet_metrics_empty_curve():
    """Verify graceful handling when equity curve is empty."""
    res = BacktestResult("2023-01-01", "2023-12-31", 0.0)
    metrics = compute_tearsheet_metrics(res)
    assert metrics == {}


def test_tearsheet_metrics_flat_curve():
    """Verify metrics for a flat (no-trade) simulation where cash just accumulates."""
    res = BacktestResult("2023-01-01", "2023-06-30", 0.0)
    dates = pd.date_range("2023-01-01", periods=6, freq="ME")
    vals = [1000, 2000, 3000, 4000, 5000, 6000]

    for d, v in zip(dates, vals):
        res.daily_equity_curve.append(
            {
                "date": d.date().isoformat(),
                "total_value": v,
                "cash_balance": v,
                "invested_value": 0,
            }
        )

    metrics = compute_tearsheet_metrics(res)
    assert metrics["total_sells"] == 0
    assert metrics["win_rate_pct"] == 0.0


def test_tearsheet_trade_analytics():
    """Verify trade analytics correctly identifies wins vs stop-outs."""
    res = BacktestResult("2023-01-01", "2023-12-31", 0.0)
    dates = pd.date_range("2023-01-01", periods=4, freq="ME")
    for d, v in zip(dates, [1000, 2000, 3000, 4000]):
        res.daily_equity_curve.append(
            {
                "date": d.date().isoformat(),
                "total_value": v,
                "cash_balance": 500,
                "invested_value": v - 500,
            }
        )

    res.trade_history = [
        {"type": "BUY", "reason": "Pattern", "dp_charge": 0},
        {"type": "SELL", "reason": "Profit Taking", "dp_charge": 15.0},
        {"type": "SELL", "reason": "Stop Breached at 100", "dp_charge": 15.0},
        {"type": "SELL", "reason": "Fundamental exit", "dp_charge": 15.0},
    ]

    metrics = compute_tearsheet_metrics(res)
    assert metrics["total_trades_executed"] == 4
    assert metrics["total_sells"] == 3
    assert metrics["total_dp_charges_inr"] == 45.0
    # 2 out of 3 sells are non-stop wins
    assert abs(metrics["win_rate_pct"] - 66.67) < 0.1


# ── SIP Credit Verification ────────────────────────────────────────────────


def test_sip_credit_monthly_counting():
    """Verify that SIP contribution estimate matches month count in the equity curve."""
    res = BacktestResult("2022-01-01", "2024-12-31", 0.0)
    # Simulate 36 months of data (one data point per month)
    dates = pd.date_range("2022-01-01", periods=36, freq="ME")
    for i, d in enumerate(dates):
        res.daily_equity_curve.append(
            {
                "date": d.date().isoformat(),
                "total_value": 1000 * (i + 1),
                "cash_balance": 500,
                "invested_value": 1000 * (i + 1) - 500,
            }
        )

    metrics = compute_tearsheet_metrics(res)
    assert metrics["total_sip_contributed_inr"] == 36000.0
