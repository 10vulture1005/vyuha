"""Phase 0 measurements: distinguish idle days from mostly idle capital.

Root conftest redirects DATABASE_URL before importing any application code.
No classifier or model is needed to aggregate replay observations.
"""
import pandas as pd
import pytest

from scripts.diagnose_investor_entry_funnel import exposure_summary, funnel_summary


def test_days_invested_are_not_capital_weight():
    equity = pd.DataFrame({
        "holdings": [0, 1, 1], "cash_pct": [1.0, 0.95, 0.90],
        "invested": [0, 50, 100], "equity": [1000, 1000, 1000],
    }, index=pd.to_datetime(["2021-12-31", "2022-01-03", "2022-01-04"]))
    rows = exposure_summary(equity, "2022-01-01").set_index("period")
    assert rows.loc["pre_2022", "days_invested_pct"] == 0
    assert rows.loc["post_2022", "days_invested_pct"] == 100
    assert rows.loc["post_2022", "fully_cash_days_pct"] == 0
    assert rows.loc["post_2022", "mean_cash_weight_pct"] == pytest.approx(92.5)
    assert rows.loc["post_2022", "mean_equity_weight_pct"] == pytest.approx(7.5)


def test_funnel_is_name_day_weighted_not_average_daily_percentage():
    daily = pd.DataFrame({
        "eligible": [1, 9], "snapshots": [1, 9], "trend": [1, 0],
        "momentum": [1, 0], "both": [1, 0], "breakout": [1, 0],
        "pullback": [0, 0], "any_entry": [1, 0], "buy_orders": [0, 0],
    }, index=pd.to_datetime(["2022-01-03", "2022-01-04"]))
    row = funnel_summary(daily, "2022-01-01").set_index("period").loc["post_2022"]
    assert row.eligible_name_days == 10
    assert row.any_entry_pct == 10  # not 50%, and not the actual buy fill rate
    assert row.buy_orders == 0


def test_empty_funnel_is_undefined_not_zero_percent():
    columns = ["eligible", "snapshots", "trend", "momentum", "both", "breakout",
               "pullback", "any_entry", "buy_orders"]
    daily = pd.DataFrame(0, index=pd.to_datetime(["2021-12-31"]), columns=columns)
    row = funnel_summary(daily, "2022-01-01").set_index("period").loc["pre_2022"]
    assert row.eligible_name_days == 0
    assert pd.isna(row.any_entry_pct)
    assert row.any_entry_name_days == 0
