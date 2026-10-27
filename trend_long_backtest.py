#!/usr/bin/env python3
"""
trend_long_backtest.py  --  standalone backtest of the long-only `trend_long` strategy.

Reads Vyuha's price archive (data/raw/ohlc/*.csv: Date,Open,High,Low,Close,Volume plus
INDEX_NIFTY500 / INDEX_NIFTY50 / INDEX_INDIAVIX) and prints total return, XIRR, CAGR,
drawdown, costs, post-tax XIRR, benchmarks, an entry funnel and an optional placebo test.

    EVERY NUMBER PRODUCED HERE IS A BACKTEST ON A SELECTED, SURVIVOR-BIASED ARCHIVE.

Usage (from the repo root, with the real archive present):
    python trend_long_backtest.py --ohlc-dir data/raw/ohlc --capital 100000 --sip 1000
    python trend_long_backtest.py --ohlc-dir data/raw/ohlc --capital 1000000 --placebo 30
    python trend_long_backtest.py --window-start 2022-01-01 --capital 1000000   # independent replay

FIXES applied after the exploratory run recorded in docs/trend_long_exploratory/REPORT.md.
Each one removes a defect that report listed under "what I would distrust".

  F1  DATA_END hindsight exit REMOVED. The old engine used the *future* knowledge
      that a symbol's archive data ends later, and sold at that last close as if it
      were a normal exit. New rule: when a symbol reaches its last available bar the
      position cannot be priced afterwards, so it is marked out at that last close
      with a FLAGGED HAIRCUT (extra penalty `data_end_haircut` on top of normal sell
      slippage, because a clean close print is not a price you can assume you get).
      These exits are tagged DATA_END_HAIRCUT and are reported as their own line,
      never mixed into the trading statistics. The haircut is a penalty, not an
      estimate of the look-ahead's value; a valid counterfactual cannot be recovered
      by subtracting whole trade gains.

  F2  VIX staleness. The old code forward-filled INDEX_INDIAVIX with no limit, so a
      feed that stopped 23 sessions before the archive end silently governed every
      later regime decision, and a *missing* VIX file scored +1 (most bullish possible).
      New rule: the VIX series is forward-filled for at most `vix_ffill_limit` (=3)
      sessions. Beyond that the feed is STALE: the VIX leg of the regime score
      contributes 0 (never +1), no NEW entry signal is taken, and an entry already
      queued for the next open is cancelled rather than filled. Exits, stops and
      position management are never blocked by a stale feed.

  F3  Stop-exit proceeds cannot fund same-day opening buys. The old order was
      stops -> discretionary sells -> buys, so a stop that triggered intraday on day t
      handed cash to a buy that had already filled at day t's open, i.e. the trade
      book was funded with money from a price that had not printed yet. New order on
      each session: (1) discretionary closing sells at the open, (2) opening buys,
      (3) protective stops (gap-through at the open, else the day's low touching the
      stop), (4) data-end marks. Only step 1 can fund step 2.
      NOTE: this is stricter than the Phase 1 execution contract in
      docs/trend_long_phase1.md item 4, which allowed an opening gap-stop fill to fund
      opening buys. The stricter rule is used here deliberately; it is not an oversight.

  F4  Terminal liquidation reported separately. Selling the whole book at the last
      close is an artefact of the archive ending, not trading. Results now print both
      the liquidated final value (net of liquidation slippage and fees) and the same
      account marked at the final close with NO liquidation cost, plus the number,
      gross P&L and cost of the END_LIQUIDATION trades.

  F5  Initial stop anchoring, stated exactly:
          initial_stop = fill_price_including_buy_slippage
                         - chandelier_atr * ATR(signal_session)
      where ATR is the ATR known at the signal close (t), never a fill-day ATR, and
      `fill_price` is the actual slipped opening fill on t+1. Therefore realised risk
      per share equals chandelier_atr * ATR(t) EXACTLY, whatever the overnight gap,
      and the position-size formula (qty = risk_pct * E / (chandelier_atr * ATR(t)))
      keeps that risk constant across gaps. A fill whose stop would be <= 0 is
      rejected. This fills the gap between the fill and the stop, so a gap-down open
      widens the stop-to-entry distance instead of being excluded from risk; the
      alternative (carry the absolute close-t stop into a gapped fill) is the Phase 1
      contract and is NOT what this script does.

  F6  (not requested, applied for correctness) the long-term tax boundary is strictly
      more than 12 CALENDAR months, not a flat 365 days. 365 is wrong in leap years.

Execution contract otherwise unchanged: signal at close t -> fill at open t+1; a stop
is the one set at the previous close; if the open gaps through it, fill at the open,
else if the day's low touches it, fill at the stop. All prices are slipped against you.
Pure numpy/pandas, no DB, no repo imports.

Known limits (also printed): the archive is a selected, survivor-biased set; fundamentals
and news layers are NOT used; the liquid/gold sleeve is modelled as cash at a flat yield.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

CFG = dict(
    # universe (monthly admission, computed from prior-day data)
    top_n=300, min_price=50.0, min_tv=5e7, hist_bars=252,
    # rank score S = w_slope*P(slope90*R2) + w_mom*P(mom12_1/vol126) + w_prox*P(close/high252)
    w_slope=0.5, w_mom=0.3, w_prox=0.2, top_pct=0.15,
    # eligibility
    min_prox=0.75, max_ext=1.25,
    # entry triggers
    breakout_n=55, vol_ratio_min=1.2, resume_lookback=7, near_ema=1.08,
    fill_window_days=5, max_new_per_day=2,
    # regime
    exposure={4: 1.0, 3: 0.8, 2: 0.5, 1: 0.25, 0: 0.10}, confirm_days=3,
    breadth_min=0.5, vix_pct_max=0.8, vix_ffill_limit=3, regime_cap_enabled=True,
    # sizing
    risk_pct=0.0075, max_name=0.12, sector_cap=0.25, heat_cap=0.08,
    min_position_inr=12000, inr_per_name=12000, n_min=8, n_max=20,
    # exits
    atr_n=22, chandelier_atr=3.5, breakeven_r=1.5, be_buffer=0.004,
    dead_days=40, dead_r=0.5, dead_rank=0.70, decay_rank=0.50,
    # frictions (Indian delivery equity). Verify against your broker's contract note.
    slip=0.0015, cash_yield=0.06, data_end_haircut=0.02,
    stt=0.001, stamp_buy=0.00015, exch=0.0000345, sebi=0.000001,
    gst=0.18, brokerage_pct=0.0, brokerage_cap=0.0, dp_sell=15.34,
    # tax (FY2026-27 as verified Oct 2026; re-verify before relying on it)
    stcg=0.20 * 1.04, ltcg=0.125 * 1.04, ltcg_exempt=125000.0, lt_months=12,
    # tax-driven deferral of SOFT (discretionary) exits - ablation only
    defer_soft_exits=False, defer_from_days=335,
)

# candidate pool -> first gate that blocked it, in evaluation order
GATES = ("vix_stale", "regime_cap", "no_trigger", "slots_full", "exposure_cap",
         "min_position", "heat_cap", "sector_cap", "per_day_cap", "filled")


# ----------------------------------------------------------------------------- data
def _read(p):
    df = pd.read_csv(p)
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str[:10])   # ignore tz offsets
    df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
    df = df[~df.index.duplicated()].sort_index()
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def load_archive(ohlc_dir, start, end):
    d = Path(ohlc_dir)
    idx = {}
    for name in ("INDEX_NIFTY500", "INDEX_NIFTY50", "INDEX_INDIAVIX"):
        f = d / f"{name}.csv"
        if f.exists():
            idx[name] = _read(f)
    main = idx.get("INDEX_NIFTY500", idx.get("INDEX_NIFTY50"))
    if main is None:
        raise SystemExit("FAIL CLOSED: no INDEX_NIFTY500/INDEX_NIFTY50 csv - regime cannot be computed.")
    cal = main.index
    if start:
        cal = cal[cal >= pd.Timestamp(start)]
    if end:
        cal = cal[cal <= pd.Timestamp(end)]
    stocks = {}
    for f in sorted(d.glob("*.csv")):
        if f.stem.startswith("INDEX_"):
            continue
        df = _read(f).reindex(cal)
        if df["Close"].notna().sum() >= 60:
            stocks[f.stem] = df
    if not stocks:
        raise SystemExit(f"No stock csvs found in {d}")
    return cal, stocks, idx


def sector_map(syms, cfg_dir):
    m = {s: "UNK" for s in syms}
    if cfg_dir and Path(cfg_dir).exists():
        for f in Path(cfg_dir).glob("universe_*.csv"):
            try:
                df = pd.read_csv(f)
                for s, sec in zip(df["symbol"], df["sector"]):
                    if s in m and isinstance(sec, str):
                        m[s] = sec.strip().upper()
            except Exception:
                pass
    return np.array([m[s] for s in syms])


# ----------------------------------------------------------------------------- features
def build(cal, stocks, idx, cfg, sectors_dir):
    syms = list(stocks)
    O = pd.DataFrame({s: stocks[s]["Open"] for s in syms})
    H = pd.DataFrame({s: stocks[s]["High"] for s in syms})
    L = pd.DataFrame({s: stocks[s]["Low"] for s in syms})
    C = pd.DataFrame({s: stocks[s]["Close"] for s in syms})
    V = pd.DataFrame({s: stocks[s]["Volume"] for s in syms})
    Cf, Hf = C.ffill(limit=10), H.ffill(limit=10)
    T, K = C.shape

    sma50 = Cf.rolling(50, min_periods=50).mean()
    sma200 = Cf.rolling(200, min_periods=200).mean()
    ema21 = Cf.ewm(span=21, adjust=False, min_periods=21).mean()
    pc = Cf.shift(1)
    Lf = L.ffill(limit=10)
    tr = np.maximum.reduce([(Hf - Lf).values, (Hf - pc).abs().values, (Lf - pc).abs().values])
    atr = pd.DataFrame(tr, index=cal, columns=syms).ewm(alpha=1.0 / cfg["atr_n"], adjust=False,
                                                         min_periods=cfg["atr_n"]).mean()
    vol_ratio = V / V.shift(1).rolling(20, min_periods=15).mean()
    high_n = Cf.shift(1).rolling(cfg["breakout_n"], min_periods=cfg["breakout_n"]).max()
    high252 = Hf.rolling(252, min_periods=252).max()
    mom = Cf.shift(21) / Cf.shift(252) - 1
    sig126 = Cf.pct_change().rolling(126, min_periods=100).std() * math.sqrt(252)

    # slope * R^2 of log price over 90 bars (Clenow-style), vectorised
    n = 90
    lp = np.log(Cf.values)
    x = np.arange(n) - (n - 1) / 2.0
    sxx = (x ** 2).sum()
    win = np.lib.stride_tricks.sliding_window_view(lp, n, axis=0)           # (T-n+1, K, n)
    slope = np.einsum("tkn,n->tk", win, x) / sxx
    syy = win.var(axis=2) * n
    with np.errstate(all="ignore"):
        r2 = slope ** 2 * sxx / syy
        sr2 = (np.exp(slope * 252) - 1) * r2
    slope_r2 = np.full((T, K), np.nan)
    slope_r2[n - 1:] = sr2

    # monthly universe admission, using only the PRIOR close's data
    tv20 = (C * V).rolling(20, min_periods=15).median()
    bars = C.notna().cumsum()
    months = cal.to_period("M")
    is_first = np.r_[True, months[1:] != months[:-1]]
    starts = list(np.where(is_first)[0]) + [T]
    admitted = np.zeros((T, K), bool)
    for a, b in zip(starts[:-1], starts[1:]):
        if a == 0:
            continue
        r = a - 1
        ok = (bars.iloc[r].values >= cfg["hist_bars"]) & (Cf.iloc[r].values >= cfg["min_price"]) & \
             (tv20.iloc[r].values >= cfg["min_tv"])
        cand = np.where(ok)[0]
        cand = cand[np.argsort(-tv20.iloc[r].values[cand])][: cfg["top_n"]]
        admitted[a:b, cand] = True
    adm = pd.DataFrame(admitted, index=cal, columns=syms)

    def pct(df):
        return df.where(adm).rank(axis=1, pct=True)

    prox = Cf / high252
    feat = pd.DataFrame(slope_r2, index=cal, columns=syms)
    valid = feat.notna() & mom.notna() & sig126.notna() & prox.notna() & atr.notna() & sma200.notna()
    S = cfg["w_slope"] * pct(feat.where(valid)) + cfg["w_mom"] * pct((mom / sig126).where(valid)) \
        + cfg["w_prox"] * pct(prox.where(valid))
    s_pct = S.rank(axis=1, pct=True)

    trend = (Cf > sma200) & (sma50 > sma200)
    elig = trend & (prox >= cfg["min_prox"]) & (Cf <= cfg["max_ext"] * sma50) & adm & valid
    breakout = (Cf > high_n) & (vol_ratio >= cfg["vol_ratio_min"])
    below = (Cf < ema21).astype(float)
    was_below = below.shift(1).rolling(cfg["resume_lookback"], min_periods=1).max() > 0
    resume = was_below & (Cf > ema21) & (Cf > Hf.shift(1)) & (Cf > sma50)
    trig = breakout | resume
    near = Cf <= cfg["near_ema"] * ema21

    # regime
    n5 = idx.get("INDEX_NIFTY500", idx.get("INDEX_NIFTY50"))["Close"].reindex(cal).ffill()
    n_sma200, n_sma50 = n5.rolling(200, min_periods=200).mean(), n5.rolling(50, min_periods=50).mean()
    above200 = ((Cf > sma200) & adm).sum(axis=1)
    breadth = above200 / adm.sum(axis=1).replace(0, np.nan)
    vix_stale = pd.Series(True, index=cal)
    if "INDEX_INDIAVIX" in idx:
        vix = idx["INDEX_INDIAVIX"]["Close"].reindex(cal).ffill(limit=cfg["vix_ffill_limit"])
        vix_stale = vix.isna()                      # F2: stale beyond the ffill cap
        vpct = vix.rolling(252, min_periods=126).apply(lambda a: (a[:-1] < a[-1]).mean(), raw=True)
        c4 = (vpct < cfg["vix_pct_max"]).astype(float)
        c4[vpct.isna()] = 0.0                       # F2: unknown VIX scores 0, never +1
    else:
        c4 = pd.Series(0.0, index=cal)              # F2: absent VIX feed fails CLOSED
    score = (n5 > n_sma200).astype(float) + (n_sma50 > n_sma200).astype(float) + \
            (breadth >= cfg["breadth_min"]).astype(float) + c4
    score[n_sma200.isna() | breadth.isna()] = np.nan
    level, cur, cand, cnt = np.full(T, np.nan), None, None, 0
    for t in range(T):
        raw = score.iloc[t]
        if np.isnan(raw):
            continue
        if cur is None:
            cur = int(raw)
        elif int(raw) != cur:
            cnt = cnt + 1 if cand == int(raw) else 1
            cand = int(raw)
            if cnt >= cfg["confirm_days"]:
                cur, cnt = int(raw), 0
        else:
            cnt = 0
        level[t] = cur
    cap = np.array([cfg["exposure"][int(l)] if not np.isnan(l) else 0.0 for l in level])

    sess = np.zeros(T, int)
    for a, b in zip(starts[:-1], starts[1:]):
        sess[a:b] = np.arange(b - a)
    last_idx = np.array([np.max(np.where(C[s].notna().values)[0]) for s in syms])

    return dict(
        cal=cal, syms=syms, K=K, T=T,
        O=O.values, H=H.values, L=L.values, C=C.values, Cf=Cf.values, atr=atr.values,
        sma50=sma50.values, elig=elig.values, trig=trig.values, near=near.values,
        valid=valid.values, adm=admitted, s_pct=s_pct.values, cap=cap, level=level,
        is_first=is_first, sess=sess, month_id=np.cumsum(is_first) - 1, last_idx=last_idx,
        sector=sector_map(syms, sectors_dir), n5=n5, vix_stale=vix_stale.values,
        vix_present="INDEX_INDIAVIX" in idx,
    )


def slice_window(P, start=None, end=None):
    """Independent replay over [start, end] using features computed on the FULL archive.

    Every feature is a trailing calculation, so nothing after the slice boundary can
    influence it. The book starts empty with fresh capital on the first session of the
    window; the regime level carries over because it too only depends on past closes.
    """
    cal = P["cal"]
    m = np.ones(len(cal), bool)
    if start:
        m &= (cal >= pd.Timestamp(start))
    if end:
        m &= (cal <= pd.Timestamp(end))
    out = dict(P)
    out["cal"] = cal[m]
    out["T"] = int(m.sum())
    for key in ("O", "H", "L", "C", "Cf", "atr", "sma50", "elig", "trig", "near", "valid",
                "s_pct", "level", "is_first", "sess", "month_id", "cap", "vix_stale"):
        out[key] = np.asarray(P[key])[m]
    out["adm"] = np.asarray(P["adm"])[m]
    C = np.asarray(P["C"])[m]
    last = []
    for j in range(out["K"]):
        w = np.where(np.isfinite(C[:, j]))[0]
        last.append(int(w[-1]) if w.size else -1)
    out["last_idx"] = np.array(last)      # archive end *inside this window*
    return out


# ----------------------------------------------------------------------------- engine
def run(P, cfg, capital, sip, seed=None):
    T, K = P["T"], P["K"]
    cal, O, L, Cf, atr, sma50 = P["cal"], P["O"], P["L"], P["Cf"], P["atr"], P["sma50"]
    s_pct, vix_stale = P["s_pct"], P["vix_stale"]
    if seed is not None:            # placebo: same machinery, rank replaced by month-stable noise
        rng = np.random.default_rng(seed)
        R = rng.random((P["month_id"].max() + 1, K))[P["month_id"]]
        Sx = np.where(P["valid"] & P["adm"], R, np.nan)
        s_pct = pd.DataFrame(Sx).rank(axis=1, pct=True).values

    def bcost(v):
        b = min(v * cfg["brokerage_pct"], cfg["brokerage_cap"]) if cfg["brokerage_cap"] else 0.0
        return v * (cfg["stt"] + cfg["stamp_buy"] + cfg["exch"] + cfg["sebi"]) + b + \
            cfg["gst"] * (v * (cfg["exch"] + cfg["sebi"]) + b)

    def scost(v):
        b = min(v * cfg["brokerage_pct"], cfg["brokerage_cap"]) if cfg["brokerage_cap"] else 0.0
        return v * (cfg["stt"] + cfg["exch"] + cfg["sebi"]) + b + \
            cfg["gst"] * (v * (cfg["exch"] + cfg["sebi"]) + b) + cfg["dp_sell"]

    cash, pos, trades = float(capital), {}, []
    pend_buy, pend_sell = [], {}
    eq = np.full(T, np.nan)
    flow = np.zeros(T)
    flow[0] = capital
    n_hold, cash_w, total_costs = np.zeros(T), np.zeros(T), 0.0
    gate = {g: np.zeros(T, int) for g in GATES}
    slots_avail = np.zeros(T, int)
    n_slots = np.zeros(T, int)
    final_mark = np.nan
    deferred = 0

    def sell(k, px, t, reason):
        nonlocal cash, total_costs
        p = pos.pop(k)
        val = p["qty"] * px
        c = scost(val)
        cash += val - c
        total_costs += c
        trades.append(dict(symbol=P["syms"][k], entry=cal[p["t0"]], exit=cal[t], qty=p["qty"],
                           entry_px=p["fill"], exit_px=px, sig_date=cal[p["sig"]],
                           init_atr=p["init_atr"], init_stop=p["stop0"], init_risk=p["risk"],
                           gain=val - c - p["basis"],
                           gross=val - p["basis"], cost=c, basis=p["basis"],
                           ret_pct=100 * (val - c - p["basis"]) / p["basis"],
                           reason=reason, days=(cal[t] - cal[p["t0"]]).days, sessions=p["held"]))

    def soft_exit(k, reason, t):
        """Discretionary (signal) exit: REGIME trims, RANK_DECAY, DEAD_MONEY.

        Returns True when an order was queued. Deferral fires only when the exit would
        realise a SHORT-TERM GAIN while the position is already close to its 12-month
        mark (>= defer_from_days). Such a sale is held until the long-term boundary,
        where the same gain is taxed at 12.5% and shares the Rs 1.25 lakh annual
        exemption. Hard exits (stops, data-end marks, terminal liquidation) are never
        deferred. Deferring a REGIME trim knowingly leaves the book above its cap for
        as long as the deferral lasts; that is the cost of the ablation, not a bug.
        """
        nonlocal deferred
        if not cfg["defer_soft_exits"]:
            pend_sell.setdefault(k, reason)
            return True
        p = pos[k]
        held_days = (cal[t] - cal[p["t0"]]).days
        unrealised = (Cf[t, k] - p["fill"]) * p["qty"]
        long_term_at = cal[p["t0"]] + pd.DateOffset(months=cfg["lt_months"])
        if unrealised > 0 and held_days >= cfg["defer_from_days"] and cal[t] <= long_term_at:
            deferred += 1
            return False
        pend_sell.setdefault(k, reason)
        return True

    for t in range(T):
        if t > 0:
            cash *= (1 + cfg["cash_yield"]) ** ((cal[t] - cal[t - 1]).days / 365.0)
            if sip and P["is_first"][t]:
                cash += sip
                flow[t] = sip
        # 1) discretionary (signal) exits fill at the open -- the only proceeds that
        #    may fund a same-day opening buy
        for k in list(pend_sell):
            if k in pos and not np.isnan(O[t, k]):
                sell(k, O[t, k] * (1 - cfg["slip"]), t, pend_sell[k])
                del pend_sell[k]
            elif k not in pos:
                del pend_sell[k]
        # 2) entries at the open
        e_now = cash + sum(p["qty"] * Cf[t - 1 if t else 0, k] for k, p in pos.items())
        n_cap = int(np.clip(e_now // cfg["inr_per_name"], cfg["n_min"], cfg["n_max"]))
        n_slots[t] = n_cap
        for k, qty, a_sig in pend_buy:
            # F2: a VIX feed that went stale after the signal also cancels the queued
            # entry. Exits and stops are never cancelled by a stale feed.
            if k in pos or len(pos) >= n_cap or np.isnan(O[t, k]) or vix_stale[t]:
                continue
            px = O[t, k] * (1 + cfg["slip"])
            while qty > 0 and qty * px + bcost(qty * px) > cash:
                qty -= max(1, int(0.05 * qty))
            if qty <= 0 or qty * px < 0.6 * cfg["min_position_inr"]:
                continue
            # F5: stop is the actual slipped fill minus a fixed ATR distance measured
            # at the SIGNAL close. Risk per share = chandelier_atr * ATR(t), exactly.
            stop = px - cfg["chandelier_atr"] * a_sig
            if stop <= 0:
                continue
            c = bcost(qty * px)
            cash -= qty * px + c
            total_costs += c
            pos[k] = dict(qty=qty, fill=px, stop=stop, risk=px - stop, hi=px, t0=t, held=0,
                          basis=qty * px + c, sig=t - 1, init_atr=a_sig, stop0=stop)
            if not np.isnan(L[t, k]) and L[t, k] <= stop:
                sell(k, stop * (1 - cfg["slip"]), t, "STOP")
        pend_buy = []
        # 3) protective stops. Their proceeds arrive after the open and therefore
        #    cannot fund anything in step 2 (F3).
        for k in list(pos):
            p, o, lo = pos[k], O[t, k], L[t, k]
            if not (np.isnan(o) or np.isnan(lo)):
                if o <= p["stop"]:
                    sell(k, o * (1 - cfg["slip"]), t, "STOP_GAP")
                elif lo <= p["stop"]:
                    sell(k, p["stop"] * (1 - cfg["slip"]), t, "STOP")
        # 4) F1: the symbol's archive data ends here. Mark out at the last close with a
        #    flagged haircut; flagged separately from trading results.
        for k in list(pos):
            if t == P["last_idx"][k] and t < T - 1 and not np.isnan(Cf[t, k]):
                sell(k, Cf[t, k] * (1 - cfg["slip"] - cfg["data_end_haircut"]), t, "DATA_END_HAIRCUT")
        # 5) mark to market at the close, ratchet stops
        val = sum(p["qty"] * Cf[t, k] for k, p in pos.items())
        E = cash + val
        eq[t] = E
        n_hold[t], cash_w[t] = len(pos), cash / E if E > 0 else 1.0
        for k, p in pos.items():
            p["held"] += 1
            p["hi"] = max(p["hi"], Cf[t, k])
            if not np.isnan(atr[t, k]):
                p["stop"] = max(p["stop"], p["hi"] - cfg["chandelier_atr"] * atr[t, k])
            if Cf[t, k] - p["fill"] >= cfg["breakeven_r"] * p["risk"]:
                p["stop"] = max(p["stop"], p["fill"] * (1 + cfg["be_buffer"]))
        # 6) terminal session: liquidate the book and report it separately (F4)
        if t == T - 1:
            final_mark = cash + val
            for k in list(pos):
                sell(k, Cf[t, k] * (1 - cfg["slip"]), t, "END_LIQUIDATION")
            eq[t] = cash
            break
        # 7) generate tomorrow's orders from today's close, and attribute the funnel
        cap_t = P["cap"][t] if cfg["regime_cap_enabled"] else 1.0
        held_mask = np.zeros(K, bool)
        held_mask[list(pos)] = True
        invested = val
        if cfg["regime_cap_enabled"] and invested > (cap_t + 0.05) * E:
            for k in sorted(pos, key=lambda j: (np.nan_to_num(s_pct[t, j], nan=-1))):
                if invested <= cap_t * E:
                    break
                if soft_exit(k, "REGIME", t):
                    invested -= pos[k]["qty"] * Cf[t, k]
        for k, p in list(pos.items()):
            sp = s_pct[t, k]
            if P["is_first"][t] and not np.isnan(sp) and sp < cfg["decay_rank"] and Cf[t, k] < sma50[t, k]:
                soft_exit(k, "RANK_DECAY", t)
            r_now = (Cf[t, k] - p["fill"]) / p["risk"]
            if p["held"] >= cfg["dead_days"] and r_now < cfg["dead_r"] and (np.isnan(sp) or sp < cfg["dead_rank"]):
                soft_exit(k, "DEAD_MONEY", t)
        # entries
        keep = [k for k in pos if k not in pend_sell]
        invested_after = sum(pos[k]["qty"] * Cf[t, k] for k in keep)
        slots = n_cap - len(keep)
        slots_avail[t] = max(slots, 0)
        heat = sum(pos[k]["qty"] * max(Cf[t, k] - pos[k]["stop"], 0) for k in keep)
        sec_val = {}
        for k in keep:
            sec_val[P["sector"][k]] = sec_val.get(P["sector"][k], 0) + pos[k]["qty"] * Cf[t, k]
        in_window = P["sess"][t] < cfg["fill_window_days"]
        # candidate pool: admitted, technically eligible, in the top rank band, not held.
        # `gate` attributes each candidate to the FIRST gate that stopped it.
        pool = P["elig"][t] & ~held_mask & (s_pct[t] >= 1 - cfg["top_pct"])
        cands = np.where(pool)[0]
        if cands.size:
            cands = cands[np.argsort(-np.nan_to_num(s_pct[t, cands], nan=-1))]
        added = 0
        for k in cands:
            if vix_stale[t]:                                   # F2
                gate["vix_stale"][t] += 1
                continue
            if cap_t <= 0:
                gate["regime_cap"][t] += 1
                continue
            if not (P["trig"][t, k] or (in_window and P["near"][t, k])):
                gate["no_trigger"][t] += 1
                continue
            if slots <= 0:
                gate["slots_full"][t] += 1
                continue
            a = atr[t, k]
            rps = cfg["chandelier_atr"] * a
            if not (rps > 0):
                gate["min_position"][t] += 1
                continue
            qty = int(cfg["risk_pct"] * E // rps)
            qty = min(qty, int(cfg["max_name"] * E // Cf[t, k]))
            notional = qty * Cf[t, k]
            if qty < 1 or notional < cfg["min_position_inr"]:
                gate["min_position"][t] += 1
                continue
            if invested_after + notional > cap_t * E:
                gate["exposure_cap"][t] += 1
                continue
            if heat + qty * rps > cfg["heat_cap"] * E:
                gate["heat_cap"][t] += 1
                continue
            sec = P["sector"][k]
            if sec != "UNK" and sec_val.get(sec, 0) + notional > cfg["sector_cap"] * E:
                gate["sector_cap"][t] += 1
                continue
            if added >= cfg["max_new_per_day"]:
                gate["per_day_cap"][t] += 1
                continue
            pend_buy.append((k, qty, a))
            invested_after += notional
            heat += qty * rps
            sec_val[sec] = sec_val.get(sec, 0) + notional
            slots -= 1
            added += 1
            gate["filled"][t] += 1

    return dict(eq=eq, flow=flow, trades=pd.DataFrame(trades), n_hold=n_hold, cash_w=cash_w,
                costs=total_costs, final_mark=final_mark, deferred_soft_exits=deferred,
                gate=gate, slots_avail=slots_avail, n_slots=n_slots)


# ----------------------------------------------------------------------------- metrics
def xirr(flows):
    """flows: list of (Timestamp, amount); investor view (negative = money in)."""
    t0 = flows[0][0]
    f = lambda r: sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in flows)
    lo, hi = -0.9999, 10.0
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def twr(eq, flow):
    r = np.zeros(len(eq))
    for t in range(1, len(eq)):
        r[t] = (eq[t] - flow[t]) / eq[t - 1] - 1 if eq[t - 1] > 0 else 0.0
    return r


def stats(cal, eq, flow, rf):
    r = twr(eq, flow)
    idx = np.cumprod(1 + r)
    yrs = (cal[-1] - cal[0]).days / 365.25
    dd = idx / np.maximum.accumulate(idx) - 1
    ex = r[1:] - ((1 + rf) ** (1 / 252) - 1)
    inflow = flow.sum()
    flows = [(cal[t], -flow[t]) for t in range(len(cal)) if flow[t] > 0] + [(cal[-1], eq[-1])]
    return dict(
        invested=inflow, final=eq[-1], total_return_pct=100 * (eq[-1] / inflow - 1),
        xirr_pct=None if (x := xirr(flows)) is None else 100 * x,
        twr_cagr_pct=100 * (idx[-1] ** (1 / yrs) - 1) if yrs > 0 else np.nan,
        max_dd_pct=100 * dd.min(), sharpe=ex.mean() / ex.std() * math.sqrt(252) if ex.std() > 0 else np.nan,
        years=yrs)


def yearly(cal, eq, flow):
    r = pd.Series(twr(eq, flow), index=cal)
    return ((1 + r).groupby(r.index.year).prod() - 1) * 100


def post_tax(trades, cfg):
    """Simplified end-charged FY model. F6: long-term = held strictly > 12 calendar
    months (the old flat 365-day test is wrong in leap years)."""
    if trades.empty:
        return 0.0
    tr = trades.copy()
    tr["fy"] = [d.year if d.month >= 4 else d.year - 1 for d in tr["exit"]]
    total = 0.0
    for _, g in tr.groupby("fy"):
        lt = g.loc[g["exit"] > g["entry"] + pd.DateOffset(months=cfg["lt_months"]), "gain"].sum()
        st = g.loc[g["exit"] <= g["entry"] + pd.DateOffset(months=cfg["lt_months"]), "gain"].sum()
        if st < 0:
            lt, st = lt + st, 0.0        # short-term loss offsets both
        total += cfg["stcg"] * max(st, 0) + cfg["ltcg"] * max(lt - cfg["ltcg_exempt"], 0)
    return total


def benchmark(cal, price, flow):
    px = price.reindex(cal).ffill().values
    units, flows = 0.0, []
    for t in range(len(cal)):
        if flow[t] > 0:
            units += flow[t] / px[t]
            flows.append((cal[t], -flow[t]))
    final = units * px[-1]
    flows.append((cal[-1], final))
    eq_idx = px / px[0]
    dd = (eq_idx / np.maximum.accumulate(eq_idx) - 1).min()
    x = xirr(flows)
    return dict(final=final, xirr_pct=None if x is None else 100 * x, max_dd_pct=100 * dd)


def xirr_with_terminal(P, flow, terminal_value):
    cal = P["cal"]
    flows = [(cal[t], -flow[t]) for t in range(len(cal)) if flow[t] > 0] + [(cal[-1], terminal_value)]
    x = xirr(flows)
    return None if x is None else 100 * x


def top_trade_robustness(P, res, cfg):
    """ACCEPTANCE: is the result driven by the five best trades?

    Two counterfactuals, both applied to the terminal value only:
      idle  - the five trades simply never happen; that capital earns nothing.
      cash  - the five trades never happen; that capital sits in the modelled 6% cash
              sleeve for each trade's own holding period.
    """
    tr = res["trades"]
    if tr.empty or len(tr) <= 5:
        return dict(top5_gain=0.0, n=0, xirr_idle=None, xirr_cash=None)
    top = tr.nlargest(5, "gain")
    g = float(top["gain"].sum())
    idle = res["eq"][-1] - g
    cash = res["eq"][-1] - sum(
        top.apply(lambda r: r["entry_px"] * r["qty"] * ((1 + cfg["cash_yield"]) ** (r["days"] / 365.0) - 1),
                  axis=1))
    return dict(top5_gain=g, n=len(tr), xirr_idle=xirr_with_terminal(P, res["flow"], idle),
                xirr_cash=xirr_with_terminal(P, res["flow"], cash))


def funnel_table(P, res):
    """Per-year slot accounting and the gate that blocks entry candidates."""
    cal = P["cal"]
    df = pd.DataFrame(res["gate"], index=cal)
    df["slots_available"] = res["slots_avail"]
    df["n_slots"] = res["n_slots"]
    rows = []
    for year, g in df.groupby(df.index.year):
        pool = int(sum(g[x].sum() for x in GATES))
        row = {"year": int(year), "decision_days": len(g),
               "mean_target_slots_N": round(g["n_slots"].mean(), 2),
               "slot_opportunities": int(g["slots_available"].sum()),
               "entries_filled": int(g["filled"].sum()),
               "candidates_seen": pool}
        row["fill_rate_pct"] = 100 * row["entries_filled"] / row["slot_opportunities"] \
            if row["slot_opportunities"] else float("nan")
        for gate in GATES:
            row[gate] = int(g[gate].sum())
            row[gate + "_pct"] = 100 * row[gate] / pool if pool else float("nan")
        rows.append(row)
    return pd.DataFrame(rows)


def report(P, cfg, res, capital, sip, oos_from):
    cal, eq, flow, tr = P["cal"], res["eq"], res["flow"], res["trades"]
    s = stats(cal, eq, flow, cfg["cash_yield"])
    tax = post_tax(tr, cfg)
    flows = [(cal[t], -flow[t]) for t in range(len(cal)) if flow[t] > 0] + [(cal[-1], eq[-1] - tax)]
    px_xirr = xirr(flows)
    inv_days = 100 * (res["n_hold"] > 0).mean()
    n_vix_stale = int(P["vix_stale"].sum())
    print("=" * 78)
    print("BACKTEST ON A SELECTED, SURVIVOR-BIASED ARCHIVE - NOT LIVE RESULTS")
    print(f"TREND_LONG   {cal[0].date()} -> {cal[-1].date()}   ({s['years']:.1f}y)   "
          f"universe on disk: {P['K']} names")
    print(f"capital Rs {capital:,.0f}" + (f" + Rs {sip:,.0f}/month SIP" if sip else " lump sum"))
    print("-" * 78)
    print(f"Money invested            : Rs {s['invested']:,.0f}")
    print(f"Final value (net costs)   : Rs {s['final']:,.0f}")
    print(f"TOTAL RETURN              : {s['total_return_pct']:+.1f}%")
    print(f"XIRR (money-weighted)     : {s['xirr_pct']:.2f}%" if s["xirr_pct"] is not None else "XIRR: n/a")
    print(f"TWR CAGR (flow-adjusted)  : {s['twr_cagr_pct']:.2f}%")
    if not sip:
        print(f"Plain CAGR (lump sum)     : {(s['final'] / s['invested']) ** (1 / s['years']) * 100 - 100:.2f}%")
    print(f"Max drawdown (TWR)        : {s['max_dd_pct']:.1f}%      Sharpe (rf {cfg['cash_yield']:.0%}): {s['sharpe']:.2f}")
    print(f"Estimated tax             : Rs {tax:,.0f}   -> post-tax XIRR "
          f"{'n/a' if px_xirr is None else f'{100 * px_xirr:.2f}%'}  (tax charged at end; slightly flattering)")
    print(f"Costs paid                : Rs {res['costs']:,.0f}   ({100 * res['costs'] / s['invested']:.1f}% of money invested)")
    print(f"Days with any holding     : {inv_days:.0f}%   avg holdings {res['n_hold'].mean():.1f}   "
          f"mean cash weight {100 * res['cash_w'].mean():.0f}%")
    if not tr.empty:
        print(f"Trades {len(tr)}   win rate {100 * (tr['gain'] > 0).mean():.0f}%   "
              f"avg hold {tr['days'].mean():.0f}d   exits: " +
              ", ".join(f"{k} {v}" for k, v in tr["reason"].value_counts().items()))
    print("-" * 78)
    print("SEPARATELY REPORTED (F1/F4) - these are archive artefacts, not trading")
    end = tr[tr["reason"] == "END_LIQUIDATION"] if not tr.empty else tr
    de = tr[tr["reason"] == "DATA_END_HAIRCUT"] if not tr.empty else tr
    if len(end):
        print(f"Terminal liquidation      : {len(end)} names, net P&L Rs {end['gain'].sum():,.0f} "
              f"(gross Rs {end['gross'].sum():,.0f}, costs Rs {end['cost'].sum():,.0f})")
    print(f"Final value if NOT sold   : Rs {res['final_mark']:,.0f}  (marked at last close, no liquidation cost)")
    print(f"   -> terminal liquidation costs the account Rs {res['final_mark'] - s['final']:,.0f}")
    if len(de):
        print(f"Data-end haircut exits    : {len(de)} names, {sorted(de['symbol'])}")
        print(f"   flagged P&L Rs {de['gain'].sum():,.0f} on Rs {de['basis'].sum():,.0f} of "
              f"{100 * de['qty'].mul(de['entry_px']).sum():,.0f} cost basis "
              f"({100 * (de['gain'] / de['basis'].clip(lower=1e-9)).mean():+.2f}% mean)")
    else:
        print("Data-end haircut exits    : none")
    print(f"VIX stale sessions (F2)   : {n_vix_stale} of {P['T']} "
          f"({100 * n_vix_stale / P['T']:.1f}%) - entries blocked, exits still allowed")
    if res["deferred_soft_exits"]:
        print(f"Soft exits deferred (tax) : {res['deferred_soft_exits']}")
    print("-" * 78)
    print("Calendar-year TWR returns (%): " + "  ".join(f"{y}:{v:+.0f}" for y, v in yearly(cal, eq, flow).items()))
    if oos_from:
        m = cal >= pd.Timestamp(oos_from)
        if m.sum() > 60:
            e2, f2 = eq[m].copy(), np.zeros(m.sum())
            f2[1:] = flow[m][1:]
            f2[0] = e2[0]
            so = stats(cal[m], e2, f2, cfg["cash_yield"])
            print(f"CONTINUING-BOOK slice from {oos_from}: TWR CAGR {so['twr_cagr_pct']:.2f}%  "
                  f"XIRR {so['xirr_pct'] if so['xirr_pct'] is None else round(so['xirr_pct'], 2)}%  "
                  f"MaxDD {so['max_dd_pct']:.1f}%  (a slice of this book, NOT an independent replay)")
    print("-" * 78)
    b = benchmark(cal, P["n5"], flow)
    cash_b = benchmark(cal, pd.Series((1 + cfg["cash_yield"]) ** (np.arange(len(cal)) / 252), index=cal), flow)
    print(f"Benchmark Nifty500 same flows : final Rs {b['final']:,.0f}  XIRR {b['xirr_pct']:.2f}%  MaxDD {b['max_dd_pct']:.1f}%")
    print(f"Benchmark cash @{cfg['cash_yield']:.0%}           : final Rs {cash_b['final']:,.0f}  XIRR {cash_b['xirr_pct']:.2f}%")
    print("NOTE: check whether INDEX_NIFTY500.csv is price- or total-return; if price-return the comparison")
    print("      flatters the strategy (stocks here are dividend-adjusted). The archive is selected and")
    print("      survivor-biased, so absolute results are optimistic. Not investment advice.")
    print("-" * 78)
    print("ENTRY FUNNEL (FUNNEL): slots filled vs N, and the gate that blocks most")
    ft = funnel_table(P, res)
    print(ft[["year", "decision_days", "mean_target_slots_N", "slot_opportunities", "entries_filled",
              "fill_rate_pct", "candidates_seen"]].round(1).to_string(index=False))
    gate_cols = [g for g in GATES if g != "filled"]
    print("Candidates by blocking gate (count / % of candidates):")
    print(ft[["year"] + gate_cols].to_string(index=False))
    tot = ft[gate_cols].sum()
    print("WHOLE WINDOW blocking counts: " +
          ", ".join(f"{g} {int(v)} ({100 * v / tot.sum():.0f}%)" for g, v in tot.items()))
    print(f"  -> the gate that blocks the most candidates is: {tot.idxmax()}")
    return s, tr, ft


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ohlc-dir", default="data/raw/ohlc")
    ap.add_argument("--config-dir", default=None, help="folder with universe_*.csv for sectors (default: <repo>/config)")
    ap.add_argument("--capital", type=float, default=100000)
    ap.add_argument("--sip", type=float, default=0.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--window-start", default=None,
                    help="independent replay: start this window with fresh capital (features still use full history)")
    ap.add_argument("--oos-from", default="2022-01-01")
    ap.add_argument("--placebo", type=int, default=0, help="number of random-rank placebo runs")
    ap.add_argument("--placebo-seeds-from", type=int, default=1000)
    ap.add_argument("--out", default="trend_long_out")
    a = ap.parse_args()

    cfg_dir = a.config_dir or str(Path(a.ohlc_dir).resolve().parents[2] / "config")
    cal, stocks, idx = load_archive(a.ohlc_dir, a.start, a.end)
    P = build(cal, stocks, idx, CFG, cfg_dir)
    if a.window_start:
        P = slice_window(P, start=a.window_start)
    if not P["vix_present"]:
        print("FAIL CLOSED WARNING: INDEX_INDIAVIX.csv missing -> VIX leg scores 0 and NO new entries are taken.")
    res = run(P, CFG, a.capital, a.sip)
    s, tr, ft = report(P, CFG, res, a.capital, a.sip, a.oos_from)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": P["cal"], "equity": res["eq"], "n_holdings": res["n_hold"]}).to_csv(out / "equity.csv", index=False)
    res["trades"].to_csv(out / "trades.csv", index=False)
    ft.to_csv(out / "funnel.csv", index=False)
    json.dump({k: (None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v))
               for k, v in s.items()}, open(out / "summary.json", "w"), indent=2)

    if a.placebo:
        print("-" * 78)
        print(f"PLACEBO: {a.placebo} runs, random month-stable rank, same triggers/sizing/exits/regime")
        xs = []
        for i in range(a.placebo):
            r = run(P, CFG, a.capital, a.sip, seed=a.placebo_seeds_from + i)
            sp = stats(P["cal"], r["eq"], r["flow"], CFG["cash_yield"])
            xs.append(sp["xirr_pct"] if sp["xirr_pct"] is not None else np.nan)
        xs = np.array(xs, float)
        real = s["xirr_pct"]
        print(f"placebo XIRR %: median {np.nanmedian(xs):.2f}  p10 {np.nanpercentile(xs, 10):.2f}  "
              f"p90 {np.nanpercentile(xs, 90):.2f}   real {real:.2f}")
        print(f"real strategy beats {100 * np.nanmean(xs < real):.0f}% of placebo runs "
              f"(need >= 90% to claim the ranker has edge)")
    print(f"\nWrote {out}/equity.csv, trades.csv, funnel.csv, summary.json")


if __name__ == "__main__":
    main()