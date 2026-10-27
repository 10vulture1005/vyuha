"""Trend-only execution/accounting substrate, NOT a strategy or live adapter.

Phase 0 found legacy Investor stops check the signal bar's low and fill later
at the open. Keep that behavior intact; enforce the amended contract here.
Prices enter as pandas frames; state and results are independent dataclasses.
No DB, indicators, settings singleton, news model, or broker is imported.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Mapping, Sequence

import pandas as pd

from investor.trend_config import ExecutionConfig
from investor.trend_costs import DeliveryCostModel, DeliveryCosts


@dataclass(frozen=True)
class TrendOrder:
    symbol: str
    side: str
    qty: int
    signal_date: pd.Timestamp
    reason: str = ""
    # Absolute stop published at close t, NOT re-anchored to a gapped fill.
    # Entry reference/ATR calculations belong to the pure Phase 2 strategy.
    initial_stop: float | None = None
    hard_exit: bool = False

    def __post_init__(self):
        if not self.symbol or self.side not in {"BUY", "SELL"}:
            raise ValueError("Invalid symbol or side")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty <= 0:
            raise ValueError("Orders require positive integer shares")
        date = pd.Timestamp(self.signal_date)
        if pd.isna(date) or date.tzinfo is not None or date != date.normalize():
            raise ValueError("signal_date must be a naive daily close date")
        if self.initial_stop is not None and (not isfinite(self.initial_stop) or self.initial_stop <= 0):
            raise ValueError("initial_stop must be positive or None")
        if not isinstance(self.hard_exit, bool):
            raise ValueError("hard_exit must be a boolean")


@dataclass(frozen=True)
class TrendPosition:
    symbol: str
    qty: int
    entry_date: pd.Timestamp
    entry_price: float
    stop: float | None
    stop_as_of: pd.Timestamp
    initial_risk_per_share: float | None
    buy_cost_per_share: float
    deductible_buy_cost_per_share: float


@dataclass(frozen=True)
class TrendFill:
    date: pd.Timestamp
    symbol: str
    side: str
    qty: int
    basis_price: float
    price: float
    costs: DeliveryCosts
    net_cash: float
    reason: str
    execution: str             # market_open | gap_stop | intraday_stop | final_open
    signal_date: pd.Timestamp


@dataclass(frozen=True)
class TrendRealization:
    symbol: str
    open_date: pd.Timestamp
    close_date: pd.Timestamp
    qty: int
    gross_pnl: float
    buy_costs: float
    sell_costs: float
    net_pnl: float
    taxable_gain: float
    reason: str


@dataclass(frozen=True)
class Rejection:
    symbol: str
    side: str
    reason: str


@dataclass(frozen=True)
class ExecutionBatch:
    date: pd.Timestamp
    cash: float
    fills: tuple[TrendFill, ...]
    realizations: tuple[TrendRealization, ...]
    rejected: tuple[Rejection, ...]
    unliquidated: tuple[str, ...]


def fresh_bar(history: pd.DataFrame | None, as_of, *, open_only=False) -> pd.Series | None:
    """Fail closed: exact date, finite positive OHLCV, internally valid range.

    Final-open liquidation needs only Open/Volume: the final day's later range
    or close cannot decide whether a sale at its opening was possible.

    Slicing precedes every read, even validation. Appending tomorrow's corrupt
    or extreme prints must not alter whether today's bar was executable.
    """
    if history is None or history.empty:
        return None
    day = pd.Timestamp(as_of)
    frame = history.loc[:day]
    if (frame.empty or frame.index[-1] != day or not frame.index.is_unique
            or not frame.index.is_monotonic_increasing):
        return None
    required = ["Open", "Volume"] if open_only else ["Open", "High", "Low", "Close", "Volume"]
    if not set(required) <= set(frame.columns):
        return None
    bar = frame.iloc[-1]
    try:
        if not all(isfinite(float(bar[key])) and float(bar[key]) > 0 for key in required):
            return None
        if not open_only and not (bar.Low <= bar.Open <= bar.High and bar.Low <= bar.Close <= bar.High):
            return None
    except (TypeError, ValueError):
        return None
    return bar


class TrendExecutor:
    """Sequential sessions, explicit close publication, no signal generation.

    run_session(t) -> queue_close(t) -> run_session(t+1). Pending day orders
    expire if that next session has no symbol bar. Hard SELL orders persist
    until filled; protective stops persist with their positions. Final session
    cancels buys and attempts liquidation at the final OPEN, never its close.
    """
    def __init__(self, initial_cash: float, config: ExecutionConfig,
                 costs: DeliveryCostModel, *, stt_deductible: bool):
        if not isfinite(initial_cash) or initial_cash < 0:
            raise ValueError("Invalid initial cash")
        if config.exchange not in costs.config.exchange_rates:
            raise ValueError("No fee rate for configured exchange")
        self.cash = float(initial_cash)
        self.config = config
        self.costs = costs
        self.stt_deductible = stt_deductible
        self.positions: dict[str, TrendPosition] = {}
        self.fills: list[TrendFill] = []
        self.realizations: list[TrendRealization] = []
        self._pending: tuple[TrendOrder, ...] = ()
        self._hard_pending: tuple[TrendOrder, ...] = ()
        self._blocked_adds: frozenset[str] = frozenset()
        self._last_session: pd.Timestamp | None = None
        self._published: pd.Timestamp | None = None
        self._finished = False

    def queue_close(self, as_of, orders: Sequence[TrendOrder],
                    stops: Mapping[str, float | None]) -> None:
        day = pd.Timestamp(as_of)
        if self._finished or day != self._last_session or day == self._published:
            raise ValueError("Publish exactly once, after this session executes")
        updated = dict(self.positions)
        for symbol, stop in stops.items():
            if symbol not in updated:
                raise ValueError(f"Cannot publish a stop for unheld {symbol}")
            pos = updated[symbol]
            # ATR unavailable does not fabricate a stop or erase a known one.
            if stop is not None:
                if not isfinite(stop) or stop <= 0 or (pos.stop is not None and stop < pos.stop):
                    raise ValueError("A stop must be positive, finite and never decrease")
                pos = replace(pos, stop=float(stop))
            updated[symbol] = replace(pos, stop_as_of=day)
        for order in orders:
            if pd.Timestamp(order.signal_date) != day:
                raise ValueError("Order signal date must equal this published close")
        # A missing-bar news veto must not disappear when tomorrow's close
        # publishes a fresh list (the legacy pending-order lifecycle incident).
        hard_symbols = {o.symbol for o in self._hard_pending}
        self._pending = self._hard_pending + tuple(o for o in orders if o.symbol not in hard_symbols)
        self._blocked_adds = frozenset(o.symbol for o in self._pending
                                      if o.side == "BUY" and o.symbol in self.positions)
        self.positions = updated
        self._published = day

    def credit_cash(self, amount: float) -> None:
        """Replay-owned SIP/accrual credit; the caller records dated flows.

        Deliberately not a DB ledger fix (FIN-03 is outside this profile).
        """
        if self._finished or not isfinite(amount) or amount < 0:
            raise ValueError("Invalid replay cash credit")
        self.cash += amount

    def run_session(self, as_of, panels: Mapping[str, pd.DataFrame], *,
                    market_ready: bool, final=False) -> ExecutionBatch:
        day = pd.Timestamp(as_of)
        if pd.isna(day) or day.tzinfo is not None or day != day.normalize():
            raise ValueError("Session date must be a naive daily date")
        if self._finished or (self._last_session is not None and day <= self._last_session):
            raise ValueError("Replay sessions must increase strictly")
        if self._last_session is not None and self._published != self._last_session:
            raise ValueError("Publish the preceding close before the next session")
        fills, realized, rejected = [], [], []
        sold_today: set[str] = set()
        pending = self._pending
        self._pending = ()
        # A 200-seed placebo replay must not revalidate every archived name
        # for a session that can act only on its holdings and queued orders.
        watched = set(self.positions) | {order.symbol for order in pending}
        bars = {symbol: fresh_bar(panels.get(symbol), day, open_only=final) for symbol in watched}

        def sell(symbol, qty, basis, reason, execution, signal_date):
            pos = self.positions.get(symbol)
            if pos is None:
                return
            qty = min(qty, pos.qty)
            price = float(basis) * (1 - self.config.sell_slippage)
            costs = self.costs.quote("SELL", price * qty, self.config.exchange,
                                     dp_already_charged=symbol in sold_today)
            sold_today.add(symbol)
            cash = price * qty - costs.total
            self.cash += cash
            gross = (price - pos.entry_price) * qty
            buy_cost = pos.buy_cost_per_share * qty
            realized.append(TrendRealization(
                symbol, pos.entry_date, day, qty, gross, buy_cost, costs.total,
                gross - buy_cost - costs.total,
                gross - pos.deductible_buy_cost_per_share * qty - costs.deductible(stt_deductible=self.stt_deductible),
                reason,
            ))
            fills.append(TrendFill(day, symbol, "SELL", qty, float(basis), price,
                                   costs, cash, reason, execution, pd.Timestamp(signal_date)))
            if qty == pos.qty:
                del self.positions[symbol]
            else:
                self.positions[symbol] = replace(pos, qty=pos.qty - qty)

        if final:
            for symbol, pos in list(self.positions.items()):
                bar = bars.get(symbol)
                if bar is None:
                    rejected.append(Rejection(symbol, "SELL", "final_bar_missing_or_invalid"))
                else:
                    sell(symbol, pos.qty, bar.Open, "Final liquidation", "final_open", pos.stop_as_of)
            for order in pending:
                if order.side == "BUY":
                    rejected.append(Rejection(order.symbol, "BUY", "final_session_no_new_buys"))
            self._finished = True
        else:
            # OPEN events first. Intraday proceeds cannot pay for an opening
            # buy: doing all stops first would spend money not yet received.
            for symbol, pos in list(self.positions.items()):
                bar = bars.get(symbol)
                if bar is not None and pos.stop is not None and bar.Open <= pos.stop:
                    sell(symbol, pos.qty, bar.Open, "Protective stop", "gap_stop", pos.stop_as_of)
            for order in pending:
                if order.side != "SELL":
                    continue
                pos, bar = self.positions.get(order.symbol), bars.get(order.symbol)
                if pos is None:
                    continue
                if bar is None:
                    rejected.append(Rejection(order.symbol, "SELL", "bar_missing_or_invalid"))
                    continue
                sell(order.symbol, order.qty, bar.Open, order.reason, "market_open", order.signal_date)
            for order in pending:
                if order.side != "BUY":
                    continue
                bar = bars.get(order.symbol)
                if market_ready is not True:
                    rejected.append(Rejection(order.symbol, "BUY", "market_data_not_ready"))
                    continue
                if bar is None:
                    rejected.append(Rejection(order.symbol, "BUY", "bar_missing_or_invalid"))
                    continue
                if order.symbol in self.positions or order.symbol in self._blocked_adds:
                    rejected.append(Rejection(order.symbol, "BUY", "adds_disabled"))
                    continue
                price = float(bar.Open) * (1 + self.config.buy_slippage)
                stop = order.initial_stop
                costs = self.costs.quote("BUY", price * order.qty, self.config.exchange)
                debit = price * order.qty + costs.total
                if debit > self.cash:
                    rejected.append(Rejection(order.symbol, "BUY", "insufficient_cash"))
                    continue
                self.cash -= debit
                self.positions[order.symbol] = TrendPosition(
                    order.symbol, order.qty, day, price, stop, pd.Timestamp(order.signal_date),
                    price - stop if stop is not None else None, costs.total / order.qty,
                    costs.deductible(stt_deductible=self.stt_deductible) / order.qty,
                )
                fills.append(TrendFill(day, order.symbol, "BUY", order.qty, float(bar.Open),
                                       price, costs, -debit, order.reason, "market_open", pd.Timestamp(order.signal_date)))
            for symbol, pos in list(self.positions.items()):
                bar = bars.get(symbol)
                if bar is not None and pos.stop is not None and bar.Low <= pos.stop:
                    # A new entry can gap below its published initial stop.
                    # Never credit a sale above the opening price as an
                    # 'intraday' fill, or move the stop to fit today's entry.
                    gap = bar.Open <= pos.stop
                    sell(symbol, pos.qty, bar.Open if gap else pos.stop, "Protective stop",
                         "gap_stop" if gap else "intraday_stop", pos.stop_as_of)

        self._hard_pending = tuple(o for o in pending if o.side == "SELL" and o.hard_exit
                                   and o.symbol in self.positions
                                   and bars.get(o.symbol) is None)
        self._last_session = day
        self.fills.extend(fills)
        self.realizations.extend(realized)
        return ExecutionBatch(day, self.cash, tuple(fills), tuple(realized), tuple(rejected),
                              tuple(sorted(self.positions)) if final else ())

    def realized_frame(self) -> pd.DataFrame:
        return pd.DataFrame([r.__dict__ for r in self.realizations],
                            columns=list(TrendRealization.__dataclass_fields__))
