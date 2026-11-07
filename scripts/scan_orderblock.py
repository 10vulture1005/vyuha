# scripts/scan_orderblock.py
"""Scan ACTIVE watchlist (and optional MidCap150) for Order-Block entries.

Pulls current charts via yfinance (2y daily, auto-adjusted), falling back
to data/raw/ohlc/<SYM>.csv when the network fails. Runs the vendored
nse-orderblock detector (agents/tools/orderblock_tools.py).

Usage:
    /home/vulture/codes/nse-orderblock/.venv/bin/python scripts/scan_orderblock.py
    /home/vulture/codes/nse-orderblock/.venv/bin/python scripts/scan_orderblock.py --universe midcap150
    ... --offline   # skip yfinance, use local CSVs only
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.tools.ta_tools import load_ohlc_df as _vyuha_load
from agents.tools.orderblock_tools import detect_orderblock
from agents.tools.regime_tools import determine_market_regime, is_vix_extreme

BASE = Path(__file__).resolve().parent.parent
OHLC_DIR = BASE / "data" / "raw" / "ohlc"

def load_local(symbol: str) -> pd.DataFrame:
    p = OHLC_DIR / f"{symbol}.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["Date"], index_col="Date")
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

def load_chart(symbol: str, offline: bool, ticker_override: str | None = None) -> pd.DataFrame:
    if offline:
        return load_local(symbol)
    try:
        import yfinance as yf
        ticker = ticker_override or f"{symbol}.NS"
        raw = yf.Ticker(ticker).history(period="2y", auto_adjust=True, actions=False, repair=False)
        if raw is None or raw.empty or len(raw) < 61:
            return load_local(symbol)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        raw.columns = ["Open", "High", "Low", "Close", "Volume"]
        raw.index.name = "Date"
        return raw.dropna()
    except Exception as e:
        print(f"  [warn] yfinance {symbol}: {e} -> local CSV", file=sys.stderr)
        return load_local(symbol)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["watchlist", "midcap150"], default="watchlist")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--write", action="store_true", help="Persist fresh signals to technical_signals (ORDER_BLOCK)")
    ap.add_argument("--equity", type=float, default=100000.0)
    args = ap.parse_args()

    # Regime
    n50 = load_chart("INDEX_NIFTY50", args.offline, "^NSEI")
    n500 = load_chart("INDEX_NIFTY500", args.offline, "^CRSLDX")
    vix = load_chart("INDEX_INDIAVIX", args.offline, "^INDIAVIX")
    if not n50.empty and not n500.empty:
        regime = determine_market_regime(n50, n500)
        vix_ext = bool(is_vix_extreme(vix)) if not vix.empty else False
        print(f"Regime: {regime} | VIX extreme(>80th): {vix_ext} | N50 last {n50.index[-1].date()} close {n50['Close'].iloc[-1]:.1f} | N500 last {n500.index[-1].date()} close {n500['Close'].iloc[-1]:.1f} | VIX last {vix['Close'].iloc[-1]:.2f}" if not vix.empty else f"Regime: {regime} (no VIX)")
    else:
        print("Regime: UNKNOWN (index data missing)")

    # Symbols
    if args.universe == "watchlist":
        db = sqlite3.connect(BASE / "vyuha.db"); db.row_factory = sqlite3.Row
        rows = db.execute("SELECT symbol, conviction_score FROM watchlist WHERE status='ACTIVE' ORDER BY conviction_score DESC").fetchall()
        symbols = [r["symbol"] for r in rows]
        conv = {r["symbol"]: r["conviction_score"] for r in rows}
    else:
        cfg = pd.read_csv(BASE / "config" / "universe_midcap150.csv")
        symbols = cfg["symbol"].tolist()
        conv = {}

    print(f"Scanning {len(symbols)} symbols (universe={args.universe}, offline={args.offline}) ...")
    results = []
    for sym in symbols:
        df = load_chart(sym, args.offline)
        if df.empty or len(df) < 61:
            print(f"  {sym}: NO DATA ({len(df)} bars)")
            continue
        last = df.index[-1].date()
        sig = detect_orderblock(df)
        if sig is None:
            print(f"  {sym}: no fresh OB (last bar {last}, close {df['Close'].iloc[-1]:.1f})")
            continue
        risk = sig.entry_price - sig.structural_stop_price
        qty = int(min(args.equity * 0.01 / risk, args.equity * 0.25 / sig.entry_price)) if risk > 0 else 0
        results.append({
            "symbol": sym, "pattern": sig.pattern_type.value,
            "entry": round(sig.entry_price, 2), "stop": round(sig.structural_stop_price, 2),
            "risk": round(risk, 2), "qty@100k": qty,
            "strength": sig.signal_strength, "vol_ratio": round(sig.vol_ratio, 2),
            "conviction": conv.get(sym), "last_bar": str(last),
        })
        print(f"  SIGNAL {sym}: entry {sig.entry_price:.2f} stop {sig.structural_stop_price:.2f} (risk {risk:.2f}) strength {sig.signal_strength} vol {sig.vol_ratio:.2f} conviction {conv.get(sym)} last_bar {last}")

    print(f"\n=== ACTIONABLE (fresh Order-Block first-touch, last 3 bars): {len(results)} ===")
    for r in sorted(results, key=lambda x: x["strength"], reverse=True):
        print(r)
    if args.write and results:
        from datetime import date as _d
        db = sqlite3.connect(BASE / "vyuha.db")
        today = _d.today().isoformat()
        for r in sorted(results, key=lambda x: x["strength"], reverse=True):
            risk = r["entry"] - r["stop"]
            db.execute(
                "INSERT INTO technical_signals (symbol, signal_date, pattern_type, atr_14, entry_price, structural_stop_price, signal_strength, vol_ratio) VALUES (?,?,?,?,?,?,?,?)",
                (r["symbol"], today, "ORDER_BLOCK", risk / 2.0 if risk else None, r["entry"], r["stop"], r["strength"], r["vol_ratio"]),
            )
        db.commit()
        print(f"Wrote {len(results)} ORDER_BLOCK signals dated {today} to technical_signals.")
    return results

if __name__ == "__main__":
    main()
