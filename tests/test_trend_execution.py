"""Trend-only amended contract. Root conftest isolates all application DBs."""
import pandas as pd
import pytest

from backtest_investor.trend_execution import TrendExecutor, TrendOrder, fresh_bar
from investor.trend_config import load_trend_config
from investor.trend_costs import DeliveryCostModel

DAYS = pd.date_range("2026-01-05", periods=6, freq="B")


def frame(opens=(100, 100, 110, 115, 120, 120)):
    data = pd.DataFrame({"Open": opens}, index=DAYS, dtype=float)
    data["Low"] = data.Open - 1
    data["Close"] = data.Open + 2
    data["High"] = data.Open + 10
    data["Volume"] = 1000
    return data


@pytest.fixture
def cfg():
    return load_trend_config()


def executor(cfg, cash=100_000):
    return TrendExecutor(cash, cfg.execution, DeliveryCostModel(cfg.costs),
                         stt_deductible=cfg.tax.stt_deductible)


def opened(cfg, prices=None, *, cash=100_000):
    prices = frame() if prices is None else prices
    ex = executor(cfg, cash)
    ex.run_session(DAYS[0], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 10, DAYS[0], initial_stop=80)], {})
    batch = ex.run_session(DAYS[1], {"AAA": prices}, market_ready=True)
    assert len(batch.fills) == 1
    ex.queue_close(DAYS[1], [], {"AAA": 95})
    return ex


def test_signal_close_fills_next_open_not_signal_or_fill_close(cfg):
    prices = frame((80, 108, 110, 115, 120, 120))
    ex = executor(cfg)
    batch = ex.run_session(DAYS[0], {"AAA": prices}, market_ready=True)
    assert batch.fills == ()
    ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 10, DAYS[0], initial_stop=90)], {})
    assert ex.positions == {}
    batch = ex.run_session(DAYS[1], {"AAA": prices}, market_ready=True)
    fill = batch.fills[0]
    assert fill.date == DAYS[1]
    assert fill.signal_date == DAYS[0]
    assert fill.price == pytest.approx(108 * (1 + cfg.execution.buy_slippage))
    assert fill.price != prices.loc[DAYS[0], "Close"]
    assert fill.price != prices.loc[DAYS[1], "Close"]
    assert ex.positions["AAA"].stop == 90
    assert ex.positions["AAA"].initial_risk_per_share == pytest.approx(fill.price - 90)
    assert batch.cash == pytest.approx(100_000 + fill.net_cash)


@pytest.mark.parametrize("opening,low,expected,kind", [
    (90, 85, 90, "gap_stop"), (95, 90, 95, "gap_stop"),
    (110, 90, 95, "intraday_stop"), (110, 95, 95, "intraday_stop"),
])
def test_gap_through_and_intraday_touch_apply_sell_slippage(cfg, opening, low, expected, kind):
    prices = frame()
    prices.loc[DAYS[2], ["Open", "Low", "High", "Close"]] = [opening, low, max(opening, 112), opening + 1]
    ex = opened(cfg, prices)
    before = ex.cash
    batch = ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    assert len(batch.fills) == 1
    fill = batch.fills[0]
    assert fill.execution == kind
    assert fill.basis_price == expected
    assert fill.price == pytest.approx(expected * (1 - cfg.execution.sell_slippage))
    assert fill.signal_date == DAYS[1]
    assert ex.positions == {}
    assert ex.cash == pytest.approx(before + fill.net_cash)


def test_no_touch_no_fill_and_stop_does_not_see_today_high(cfg):
    prices = frame()
    prices.loc[DAYS[2], "High"] = 10_000
    ex = opened(cfg, prices)
    batch = ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    assert batch.fills == ()
    assert ex.positions["AAA"].stop == 95


def test_no_bar_does_not_stop_or_sell_at_stale_price(cfg):
    prices = frame().drop(DAYS[2])
    ex = opened(cfg, prices)
    before = ex.cash
    batch = ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    assert batch.fills == ()
    assert ex.cash == before
    assert ex.positions["AAA"].qty == 10


def test_unfilled_day_buy_expires_not_delayed_to_a_later_bar(cfg):
    prices = frame().drop(DAYS[1])
    ex = executor(cfg)
    ex.run_session(DAYS[0], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 10, DAYS[0])], {})
    batch = ex.run_session(DAYS[1], {"AAA": prices}, market_ready=True)
    assert batch.fills == ()
    assert batch.rejected[0].reason == "bar_missing_or_invalid"
    ex.queue_close(DAYS[1], [], {})
    assert ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True).fills == ()


def test_hard_exit_with_no_bar_persists_and_does_not_require_regime(cfg):
    prices = frame().drop(DAYS[3])
    ex = opened(cfg, prices)
    ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[2], [TrendOrder("AAA", "SELL", 10, DAYS[2], "news veto", hard_exit=True)], {})
    assert ex.run_session(DAYS[3], {"AAA": prices}, market_ready=False).fills == ()
    ex.queue_close(DAYS[3], [], {})
    batch = ex.run_session(DAYS[4], {"AAA": prices}, market_ready=False)
    assert len(batch.fills) == 1
    assert batch.fills[0].reason == "news veto"
    assert ex.positions == {}


def test_initial_atr_none_leaves_stop_none_and_stop_updates_cannot_decrease(cfg):
    ex = executor(cfg)
    prices = frame()
    ex.run_session(DAYS[0], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 10, DAYS[0])], {})
    ex.run_session(DAYS[1], {"AAA": prices}, market_ready=True)
    assert ex.positions["AAA"].stop is None
    ex.queue_close(DAYS[1], [], {"AAA": None})
    ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    assert ex.positions["AAA"].stop is None
    ex.queue_close(DAYS[2], [], {"AAA": 95})
    ex.run_session(DAYS[3], {"AAA": prices}, market_ready=True)
    with pytest.raises(ValueError, match="never decrease"):
        ex.queue_close(DAYS[3], [], {"AAA": 94})
    ex.queue_close(DAYS[3], [], {"AAA": None})
    assert ex.positions["AAA"].stop == 95


def test_final_liquidates_at_open_not_stop_or_close_and_cancels_buys(cfg):
    prices = frame()
    prices.loc[DAYS[3], ["Open", "Low", "High", "Close"]] = [115, 50, 200, 180]
    ex = opened(cfg, prices)
    ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[2], [TrendOrder("BBB", "BUY", 5, DAYS[2])], {"AAA": 95})
    batch = ex.run_session(DAYS[3], {"AAA": prices, "BBB": prices}, market_ready=False, final=True)
    assert len(batch.fills) == 1
    assert batch.fills[0].execution == "final_open"
    assert batch.fills[0].price == pytest.approx(115 * (1 - cfg.execution.sell_slippage))
    assert batch.unliquidated == ()
    assert batch.rejected[0].reason == "final_session_no_new_buys"
    assert ex.positions == {}


def test_final_missing_bar_reports_unliquidated_not_fake_realization(cfg):
    prices = frame().drop(DAYS[2])
    ex = opened(cfg, prices)
    batch = ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True, final=True)
    assert batch.fills == ()
    assert batch.realizations == ()
    assert batch.unliquidated == ("AAA",)
    assert batch.rejected[0].reason == "final_bar_missing_or_invalid"


def test_missing_regime_prevents_buy_but_never_blocks_protective_stop(cfg):
    prices = frame()
    ex = executor(cfg)
    ex.run_session(DAYS[0], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 10, DAYS[0])], {})
    batch = ex.run_session(DAYS[1], {"AAA": prices}, market_ready=False)
    assert batch.fills == ()
    assert batch.rejected[0].reason == "market_data_not_ready"
    prices.loc[DAYS[2], ["Open", "Low", "High", "Close"]] = [90, 85, 100, 91]
    ex = opened(cfg, prices)
    assert ex.run_session(DAYS[2], {"AAA": prices}, market_ready=False).fills[0].execution == "gap_stop"


def test_intraday_proceeds_cannot_fund_open_buy(cfg):
    prices = frame()
    ex = opened(cfg, prices, cash=1100)
    ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[2], [TrendOrder("BBB", "BUY", 5, DAYS[2])], {"AAA": 95})
    prices.loc[DAYS[3], "Low"] = 90
    batch = ex.run_session(DAYS[3], {"AAA": prices, "BBB": prices}, market_ready=True)
    assert [f.side for f in batch.fills] == ["SELL"]
    assert batch.fills[0].execution == "intraday_stop"
    assert batch.rejected[0].reason == "insufficient_cash"


def test_partial_exits_allocate_buy_cost_once_and_dp_once_per_scrip_day(cfg):
    ex = opened(cfg)
    ex.run_session(DAYS[2], {"AAA": frame()}, market_ready=True)
    ex.queue_close(DAYS[2], [TrendOrder("AAA", "SELL", 4, DAYS[2]),
                             TrendOrder("AAA", "SELL", 6, DAYS[2])], {})
    batch = ex.run_session(DAYS[3], {"AAA": frame()}, market_ready=True)
    buy = ex.fills[0]
    assert len(batch.fills) == 2
    assert sum(f.costs.dp for f in batch.fills) == cfg.costs.dp_sell_day_inr
    assert sum(r.buy_costs for r in batch.realizations) == pytest.approx(buy.costs.total)
    for realization, sell in zip(batch.realizations, batch.fills):
        assert realization.net_pnl == pytest.approx(realization.gross_pnl - realization.buy_costs - sell.costs.total)
        allocated_stt = buy.costs.stt * realization.qty / buy.qty
        assert realization.taxable_gain - realization.net_pnl == pytest.approx(allocated_stt + sell.costs.stt)
    assert ex.realized_frame().qty.sum() == 10


def test_dp_applies_again_on_a_different_sell_day(cfg):
    ex = opened(cfg)
    ex.run_session(DAYS[2], {"AAA": frame()}, market_ready=True)
    ex.queue_close(DAYS[2], [TrendOrder("AAA", "SELL", 4, DAYS[2])], {})
    one = ex.run_session(DAYS[3], {"AAA": frame()}, market_ready=True)
    ex.queue_close(DAYS[3], [TrendOrder("AAA", "SELL", 6, DAYS[3])], {})
    two = ex.run_session(DAYS[4], {"AAA": frame()}, market_ready=True)
    assert one.fills[0].costs.dp == two.fills[0].costs.dp == cfg.costs.dp_sell_day_inr


def test_stop_on_entry_day_uses_the_absolute_signal_close_stop(cfg):
    prices = frame()
    prices.loc[DAYS[1], "Low"] = 70
    ex = executor(cfg)
    ex.run_session(DAYS[0], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 10, DAYS[0], initial_stop=80)], {})
    batch = ex.run_session(DAYS[1], {"AAA": prices}, market_ready=True)
    assert [fill.side for fill in batch.fills] == ["BUY", "SELL"]
    assert batch.fills[1].basis_price == 80


def test_final_open_fill_does_not_read_final_close_or_range(cfg):
    prices = frame()
    ex = opened(cfg, prices)
    # The final session is being liquidated at its open; later prices may not
    # even be available yet, and cannot veto that already possible sale.
    prices.loc[DAYS[2], ["Low", "High", "Close"]] = float("nan")
    batch = ex.run_session(DAYS[2], {"AAA": prices}, market_ready=False, final=True)
    assert batch.unliquidated == ()
    assert batch.fills[0].basis_price == 110
    assert batch.fills[0].execution == "final_open"


def test_gap_below_initial_stop_cannot_sell_new_entry_above_the_open(cfg):
    ex = executor(cfg)
    prices = frame()
    ex.run_session(DAYS[0], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 10, DAYS[0], initial_stop=100.1)], {})
    batch = ex.run_session(DAYS[1], {"AAA": prices}, market_ready=True)
    assert [fill.side for fill in batch.fills] == ["BUY", "SELL"]
    assert batch.fills[1].execution == "gap_stop"
    assert batch.fills[1].basis_price == prices.loc[DAYS[1], "Open"]


def test_an_add_cannot_become_new_entry_when_a_gap_stop_removes_the_position(cfg):
    ex = opened(cfg)
    prices = frame()
    ex.run_session(DAYS[2], {"AAA": prices}, market_ready=True)
    ex.queue_close(DAYS[2], [TrendOrder("AAA", "BUY", 5, DAYS[2])], {"AAA": 95})
    prices.loc[DAYS[3], ["Open", "Low", "High", "Close"]] = [90, 85, 100, 92]
    batch = ex.run_session(DAYS[3], {"AAA": prices}, market_ready=True)
    assert [fill.side for fill in batch.fills] == ["SELL"]
    assert batch.rejected[0].reason == "adds_disabled"


def test_adds_disabled_and_cash_not_overspent(cfg):
    ex = opened(cfg)
    ex.run_session(DAYS[2], {"AAA": frame()}, market_ready=True)
    ex.queue_close(DAYS[2], [TrendOrder("AAA", "BUY", 5, DAYS[2])], {})
    batch = ex.run_session(DAYS[3], {"AAA": frame()}, market_ready=True)
    assert batch.rejected[0].reason == "adds_disabled"
    assert batch.fills == ()
    assert ex.cash >= 0


def test_cannot_publish_forming_bar_or_execute_same_session_twice(cfg):
    ex = executor(cfg)
    with pytest.raises(ValueError):
        ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 1, DAYS[0])], {})
    ex.run_session(DAYS[0], {"AAA": frame()}, market_ready=True)
    with pytest.raises(ValueError):
        ex.run_session(DAYS[0], {"AAA": frame()}, market_ready=True)
    with pytest.raises(ValueError):
        ex.queue_close(DAYS[0], [TrendOrder("AAA", "BUY", 1, DAYS[1])], {})
    ex.queue_close(DAYS[0], [], {})
    with pytest.raises(ValueError):
        ex.queue_close(DAYS[0], [], {})


@pytest.mark.parametrize("qty", [0, -1, 1.5, True])
def test_invalid_quantity_is_not_rounded(qty):
    with pytest.raises(ValueError):
        TrendOrder("AAA", "BUY", qty, DAYS[0])


@pytest.mark.parametrize("column,value", [("Open", 0), ("Low", float("nan")),
                                         ("Volume", 0), ("High", 1), ("Close", float("inf"))])
def test_invalid_bar_is_not_executable(column, value):
    prices = frame()
    prices.loc[DAYS[1], column] = value
    assert fresh_bar(prices, DAYS[1]) is None


def test_appending_future_bars_does_not_change_today_fill(cfg):
    prices = frame()
    changed = prices.copy()
    changed.loc[DAYS[2]:, ["Open", "Low", "High", "Close"]] *= 1000
    first, second = opened(cfg, prices), opened(cfg, changed)
    assert first.positions == second.positions
    assert first.fills == second.fills
    assert fresh_bar(prices, DAYS[1]).equals(fresh_bar(prices.loc[:DAYS[1]], DAYS[1]))
