#!/usr/bin/env python3
"""Pivot Point Momentum Breakout-Retest backtest scaffold (NSE daily adaptation).

Faithful to the spec in the user request, with ONE documented adaptation:
  - Spec is intraday (prior-day pivots + 15m RSI/ADX retest + time-of-day filter).
  - yfinance intraday only goes back ~60d for 15m, so a multi-year walk-forward
    MUST run on daily bars. This scaffold therefore implements the DAILY-SWING
    translation of the same logic:
      * pivots from prior DAY (instead of prior session for intraday)
      * breakout = daily CLOSE above R1 / below S1 (instead of 15m closing candle)
      * RSI(14) / ADX(14) on daily closes (instead of 15m)
      * retest = a later daily bar whose LOW touches R1 but CLOSES back above it
        (instead of a 15m retest), entry at that bar's close
      * time-of-day filter (12-1pm lull) is NOT testable on daily bars and is
        skipped openly rather than faked.

No lookahead: pivots/indicators at bar t use data up to t only; entries fill
at t+1 or later; stops/targets fixed at signal time from ATR(t).

Usage:
    uv run python scripts/backtest_pivot_momentum.py \
        --symbols RELIANCE TCS INFY HDFCBANK ICICIBANK \
        --start 2021-01-01 --end 2026-01-01 --folds 3
    uv run python scripts/backtest_pivot_momentum.py --help
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------- indicators

def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi.fillna(50.0)


def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def adx_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat(
        [h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr.replace(0.0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean() / atr.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return adx.fillna(0.0)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["RSI14"] = wilder_rsi(df["Close"])
    df["ATR14"] = atr_wilder(df)
    df["ADX14"] = adx_wilder(df)
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["VolMA20"] = df["Volume"].shift(1).rolling(20).mean()  # excl. current bar
    # Classical daily pivots from PRIOR bar (shifted → no lookahead)
    pc = df["Close"].shift(1)
    ph = df["High"].shift(1)
    pl = df["Low"].shift(1)
    pp = (ph + pl + pc) / 3.0
    df["PP"] = pp
    df["R1"] = 2 * pp - pl
    df["S1"] = 2 * pp - ph
    df["R2"] = pp + (ph - pl)
    df["S2"] = pp - (ph - pl)
    df["R3"] = ph + 2 * (pp - pl)
    df["S3"] = pl - 2 * (ph - pp)
    return df


# ---------------------------------------------------------------- costs (NSE delivery, mirrors vyuha friction)

STT = 0.001
EXCH = 0.0000345
SEBI = 0.000001
DP = 15.0
SLIPPAGE_BPS = 5.0  # 5 bps each side


def buy_cash_out(price: float, qty: int) -> float:
    gross = price * qty
    slip = gross * SLIPPAGE_BPS / 10_000.0
    return gross + gross * (STT + EXCH + SEBI) + slip


def sell_cash_in(price: float, qty: int) -> float:
    gross = price * qty
    slip = gross * SLIPPAGE_BPS / 10_000.0
    return gross - gross * (STT + EXCH + SEBI) - slip - DP


# ---------------------------------------------------------------- structures

@dataclass
class Trade:
    symbol: str
    side: int  # +1 long, -1 short (shorts cash-settled, no stock-lending desk)
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    stop: float
    target: float
    risk_per_share: float
    pivot_level: float
    breakout_date: pd.Timestamp
    exit_date: pd.Timestamp | None = None
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl_net: float = 0.0
    r_mult: float = 0.0


@dataclass
class Params:
    rsi_long: float = 55.0
    rsi_short: float = 45.0
    adx_min: float = 20.0
    vol_mult: float = 1.5
    stop_atr_mult: float = 0.4
    retest_window: int = 3
    max_hold: int = 10
    risk_pct: float = 0.01
    max_positions: int = 5
    use_adx: bool = True
    use_volume: bool = True
    require_retest: bool = True
    allow_shorts: bool = True


def download(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        ticker = sym if sym.startswith("^") or sym.endswith(".NS") else f"{sym}.NS"
        try:
            df = yf.download(ticker, start=start, end=end, interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty or len(df) < 120:
                print(f"[{sym}] skipped: only {0 if df is None else len(df)} bars")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={c: c.capitalize() for c in df.columns})
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            df.index = pd.to_datetime(df.index).tz_localize(None)
            out[sym] = add_indicators(df)
            print(f"[{sym}] {len(df)} bars {df.index[0].date()} → {df.index[-1].date()}")
        except Exception as e:  # noqa: BLE001
            print(f"[{sym}] download failed: {e}")
    return out


def run_portfolio(data: dict[str, pd.DataFrame], p: Params,
                  start_cash: float = 100_000.0) -> tuple[list[Trade], pd.DataFrame]:
    equity = start_cash
    opens: list[Trade] = []
    closed: list[Trade] = []
    # armed breakout awaiting retest: sym -> dict
    armed: dict[str, dict] = {}
    equity_curve: list[dict] = []

    all_dates = sorted({d for df in data.values() for d in df.index})
    for today in all_dates:
        # 1) manage open positions at today's bar
        for t in list(opens):
            df = data[t.symbol]
            if today not in df.index:
                continue
            bar = df.loc[today]
            bars_held = len(df.loc[t.entry_date:today]) - 1
            exit_px, reason = 0.0, ""
            if t.side == 1:
                if bar["Low"] <= t.stop:
                    exit_px, reason = t.stop, "stop"
                elif bar["High"] >= t.target:
                    # target touched: book half conceptually — scaffold exits full
                    # at target unless EMA-trail would pay more; take target now
                    # only if close fails to clear R2, else trail EMA9
                    if bar["Close"] > t.target and bars_held >= 2:
                        if bar["Close"] < bar["EMA9"]:
                            exit_px, reason = bar["Close"], "ema9_trail"
                    else:
                        exit_px, reason = t.target, "target"
                    if not reason and bar["Close"] < bar["EMA9"] and bar["Close"] > t.entry_price:
                        # cleared target earlier, now lost the 9EMA
                        exit_px, reason = bar["Close"], "ema9_trail"
                elif bars_held >= p.max_hold:
                    exit_px, reason = bar["Close"], "time_stop"
                elif bars_held >= 3 and bar["Close"] > t.target and bar["Close"] < bar["EMA9"]:
                    exit_px, reason = bar["Close"], "ema9_trail"
            else:
                if bar["High"] >= t.stop:
                    exit_px, reason = t.stop, "stop"
                elif bar["Low"] <= t.target:
                    exit_px, reason = t.target, "target"
                elif bars_held >= p.max_hold:
                    exit_px, reason = bar["Close"], "time_stop"
            if reason:
                if t.side == 1:
                    proceeds = sell_cash_in(exit_px, t.qty)
                    cost = buy_cash_out(t.entry_price, t.qty)
                    t.pnl_net = proceeds - cost
                else:
                    # short: receive at entry, pay at exit (both net of friction)
                    t.pnl_net = (sell_cash_in(t.entry_price, t.qty)
                                 - buy_cash_out(exit_px, t.qty))
                t.exit_date, t.exit_price, t.exit_reason = today, float(exit_px), reason
                t.r_mult = t.pnl_net / (t.risk_per_share * t.qty) if t.risk_per_share * t.qty > 0 else 0.0
                equity += t.pnl_net
                opens.remove(t)
                closed.append(t)

        # 2) entries: scan armed retests + fresh breakouts
        if len(opens) < p.max_positions:
            for sym, df in data.items():
                if today not in df.index:
                    continue
                if any(o.symbol == sym for o in opens):
                    continue
                loc = df.index.get_loc(today)
                if loc < 30 or loc + 1 >= len(df):
                    continue
                row = df.iloc[loc]
                if not np.isfinite(row["R1"]) or row["ATR14"] <= 0:
                    continue

                # -- fresh breakout arms a pending retest setup
                breakout = armed.get(sym)
                long_break = (
                    row["Close"] > row["R1"]
                    and row["RSI14"] > p.rsi_long
                    and row["RSI14"] > df.iloc[loc - 1]["RSI14"]
                    and (not p.use_adx or row["ADX14"] > p.adx_min)
                    and (not p.use_volume or row["Volume"] > p.vol_mult * row["VolMA20"])
                )
                short_break = (
                    p.allow_shorts
                    and row["Close"] < row["S1"]
                    and row["RSI14"] < p.rsi_short
                    and row["RSI14"] < df.iloc[loc - 1]["RSI14"]
                    and (not p.use_adx or row["ADX14"] > p.adx_min)
                    and (not p.use_volume or row["Volume"] > p.vol_mult * row["VolMA20"])
                )
                if breakout is None and (long_break or short_break):
                    side = 1 if long_break else -1
                    lvl = float(row["R1"] if side == 1 else row["S1"])
                    tgt = float(row["R2"] if side == 1 else row["S2"])
                    stop = lvl - side * p.stop_atr_mult * float(row["ATR14"])
                    # sanity: stop must be on the correct side with real risk
                    if side == 1 and stop >= row["Close"]:
                        continue
                    if side == -1 and stop <= row["Close"]:
                        continue
                    if p.require_retest:
                        armed[sym] = {"side": side, "level": lvl, "target": tgt,
                                      "stop": stop, "atr": float(row["ATR14"]),
                                      "breakout_date": today,
                                      "expires": df.index[min(loc + p.retest_window, len(df) - 1)]}
                    else:
                        # ablation: enter next open without retest
                        nxt = df.iloc[loc + 1]
                        entry_px = float(nxt["Open"])
                        risk = abs(entry_px - stop)
                        if risk <= 0:
                            continue
                        qty = min(int(equity * p.risk_pct / risk),
                                  int(equity * 0.25 / entry_px))
                        if qty < 1:
                            continue
                        opens.append(Trade(sym, side, nxt.name, entry_px, qty,
                                           stop, tgt, risk, lvl, today))
                        if len(opens) >= p.max_positions:
                            break
                    continue

                # -- pending retest fill
                if breakout is not None:
                    if today > breakout["expires"]:
                        armed.pop(sym, None)
                        continue
                    side, lvl = breakout["side"], breakout["level"]
                    filled = (row["Low"] <= lvl and row["Close"] > lvl) if side == 1 \
                        else (row["High"] >= lvl and row["Close"] < lvl)
                    if filled:
                        entry_px = float(row["Close"])
                        risk = abs(entry_px - breakout["stop"])
                        if risk <= 0:
                            armed.pop(sym, None)
                            continue
                        qty = min(int(equity * p.risk_pct / risk),
                                  int(equity * 0.25 / entry_px))
                        armed.pop(sym, None)
                        if qty < 1:
                            continue
                        opens.append(Trade(sym, side, today, entry_px, qty,
                                           breakout["stop"], breakout["target"],
                                           risk, lvl, breakout["breakout_date"]))
                        if len(opens) >= p.max_positions:
                            break

        mtm = equity + sum(
            (data[t.symbol].loc[today]["Close"] - t.entry_price) * t.qty * t.side
            for t in opens if today in data[t.symbol].index
        )
        equity_curve.append({"date": today, "equity": mtm, "cash": equity,
                             "open": len(opens)})
    return closed, pd.DataFrame(equity_curve).set_index("date")


def metrics(trades: list[Trade], curve: pd.DataFrame, start_cash: float) -> dict:
    if curve.empty:
        return {"trades": 0}
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gross_w = sum(t.pnl_net for t in wins)
    gross_l = -sum(t.pnl_net for t in losses)
    rets = curve["equity"].pct_change().dropna()
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    cagr = (curve["equity"].iloc[-1] / start_cash) ** (1 / years) - 1
    dd = (curve["equity"] / curve["equity"].cummax() - 1).min()
    r = [t.r_mult for t in trades]
    return {
        "trades": len(trades),
        "win_rate_%": 100 * len(wins) / len(trades) if trades else 0.0,
        "avg_R": float(np.mean(r)) if r else 0.0,
        "expectancy_Rs/trade": float(np.mean(r)) if r else 0.0,
        "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf") if gross_w > 0 else 0.0,
        "net_pnl": float(sum(t.pnl_net for t in trades)),
        "CAGR_%": 100 * cagr,
        "maxDD_%": 100 * dd,
        "sharpe_daily_ann": float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0,
        "end_equity": float(curve["equity"].iloc[-1]),
    }


def walk_forward(data: dict[str, pd.DataFrame], p: Params, folds: int,
                 start_cash: float) -> None:
    all_dates = sorted({d for df in data.values() for d in df.index})
    chunks = np.array_split(all_dates, folds)
    print(f"\n===== WALK-FORWARD ({folds} folds, identical params — no re-optimisation) =====")
    for i, chunk in enumerate(chunks, 1):
        sub = {s: df.loc[df.index.intersection(chunk)] for s, df in data.items()}
        sub = {s: df for s, df in sub.items() if len(df) > 60}
        if not sub:
            print(f"fold {i}: no data")
            continue
        trades, curve = run_portfolio(sub, p, start_cash)
        m = metrics(trades, curve, start_cash)
        print(f"fold {i} [{chunk[0].date()} → {chunk[-1].date()}] "
              f"trades={m['trades']} win={m['win_rate_%']:.1f}% avgR={m['avg_R']:.2f} "
              f"PF={m['profit_factor']:.2f} CAGR={m['CAGR_%']:.1f}% maxDD={m['maxDD_%']:.1f}%")


def ablation(data: dict[str, pd.DataFrame], base: Params, start_cash: float) -> None:
    import dataclasses
    variants = {
        "BASE (ADX + volume + retest)": base,
        "no-ADX regime filter": dataclasses.replace(base, use_adx=False),
        "no-volume filter": dataclasses.replace(base, use_volume=False),
        "no-retest (chase breakout)": dataclasses.replace(base, require_retest=False),
    }
    print("\n===== ABLATION (each filter removed in isolation) =====")
    for name, prm in variants.items():
        trades, curve = run_portfolio(data, prm, start_cash)
        m = metrics(trades, curve, start_cash)
        print(f"{name:32s} trades={m['trades']:4d} win={m['win_rate_%']:5.1f}% "
              f"avgR={m['avg_R']:+.2f} PF={m['profit_factor']:.2f} "
              f"net=₹{m['net_pnl']:,.0f} maxDD={m['maxDD_%']:.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="Pivot+momentum breakout-retest backtest (NSE daily)")
    ap.add_argument("--symbols", nargs="+",
                    default=["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
                             "TATAMOTORS", "SBIN", "TITAN", "ASIANPAINT", "LT"],
                    help="NSE symbols (without .NS suffix)")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--cash", type=float, default=100_000.0)
    ap.add_argument("--no-shorts", action="store_true", help="long-only run")
    args = ap.parse_args()

    print(f"Downloading {len(args.symbols)} NSE symbols {args.start} → {args.end} ...")
    data = download(args.symbols, args.start, args.end)
    if not data:
        print("No data — check network/symbols.")
        sys.exit(1)

    params = Params(allow_shorts=not args.no_shorts)
    trades, curve = run_portfolio(data, params, args.cash)
    print("\n===== FULL-SAMPLE RESULT =====")
    for k, v in metrics(trades, curve, args.cash).items():
        print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")

    longs = [t for t in trades if t.side == 1]
    shorts = [t for t in trades if t.side == -1]
    for label, leg in (("LONG", longs), ("SHORT", shorts)):
        if leg:
            w = sum(1 for t in leg if t.pnl_net > 0) / len(leg) * 100
            print(f"  {label}: n={len(leg)} win={w:.1f}% "
                  f"avgR={np.mean([t.r_mult for t in leg]):+.2f} "
                  f"net=₹{sum(t.pnl_net for t in leg):,.0f}")

    reasons: dict[str, list[float]] = {}
    for t in trades:
        reasons.setdefault(t.exit_reason, []).append(t.pnl_net)
    print("  exits:", {k: f"n={len(v)} net=₹{sum(v):,.0f}" for k, v in reasons.items()})

    walk_forward(data, params, args.folds, args.cash)
    ablation(data, params, args.cash)

    out = Path("scratch") / "pivot_momentum_trades.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([t.__dict__ for t in trades]).to_csv(out, index=False)
    print(f"\nTrade log → {out} ({len(trades)} trades)")


if __name__ == "__main__":
    main()
