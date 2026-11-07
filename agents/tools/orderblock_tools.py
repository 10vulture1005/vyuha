# agents/tools/orderblock_tools.py
"""Order-Block detector — vendored from nse-orderblock (`src/nse_ob/`).

Replaces the W-Bottom / BB-Squeeze patterns as the Vyuha entry signal
(Phase 4). Rules are identical to nse-orderblock DetectorConfig defaults
unless overridden via thresholds.yaml `order_block:` section:

- Disclosure: bullish confirmation bar with body >= disp_atr x ATR(14)
  and close in top 25% of range; last bearish origin within 5 bars;
  zone = full origin high-low; entry_mode A (limit at zone top, first
  touch only, eligible next bar); expiry 60 bars.
- Risk (Experiment E winner): initial stop = entry - 2.0 x ATR(14 lagged),
  20-bar time exit — wired in `detect_orderblock` / portfolio sizing.

The detector is causal: a block detected on bar j is only touchable
from bar j+1 onward.

Reference: /home/vulture/codes/nse-orderblock/src/nse_ob/detector.py
  (bullish order-block: last bearish candle before displacement).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from agents.tools.ta_tools import PatternType, TechnicalSignalResult
from config import thresholds

ATR_MULT = 2.0  # Experiment E winner stop multiplier


@dataclass(frozen=True)
class OBConfig:
    atr_period: int = 14
    disp_atr: float = 1.0
    top_fraction: float = 0.25
    use_rvol: bool = False
    rvol_period: int = 20
    min_rvol: float = 1.5
    lookback: int = 5
    zone_mode: str = "full"
    entry_mode: str = "A"
    expiry: int = 60

    @property
    def lifetime(self) -> int:
        return self.expiry


def _ob_config_from_thresholds() -> OBConfig:
    t = thresholds.get("order_block", {}) or {}
    return OBConfig(
        atr_period=int(t.get("atr_period", 14)),
        disp_atr=float(t.get("disp_atr", 1.0)),
        top_fraction=float(t.get("top_fraction", 0.25)),
        use_rvol=bool(t.get("use_rvol", False)),
        rvol_period=int(t.get("rvol_period", 20)),
        min_rvol=float(t.get("min_rvol", 1.5)),
        lookback=int(t.get("lookback", 5)),
        zone_mode=str(t.get("zone_mode", "full")),
        entry_mode=str(t.get("entry_mode", "A")),
        expiry=int(t.get("expiry", 60)),
    )


def compute_lagged_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR lagged 1 bar (available before entry) — nse-orderblock."""
    prev = frame.close.shift()
    tr = pd.concat(
        [
            frame.high - frame.low,
            (frame.high - prev).abs(),
            (frame.low - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = pd.Series(float("nan"), index=frame.index)
    n = period
    if len(frame) >= n:
        atr.iloc[n - 1] = tr.iloc[:n].mean()
        for i in range(n, len(frame)):
            atr.iloc[i] = (atr.iloc[i - 1] * (n - 1) + tr.iloc[i]) / n
    return atr.shift(1)


def _to_ob_frame(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Normalise yfinance Capitalised cols -> nse_ob lowercase cols.

    Drops today's forming bar (same _closed_bars rule as W/BB) so the
    latest bar is a confirmed close.
    """
    if df is None or df.empty:
        return None
    frame = df.rename(columns={c: c.lower() for c in df.columns})[
        ["open", "high", "low", "close", "volume"]
    ].copy()
    frame = frame.dropna()
    if frame.empty:
        return None
    # Drop the forming bar when the last timestamp is today.
    try:
        from datetime import date as _date

        last_ts = frame.index[-1]
        last_date = last_ts.date() if hasattr(last_ts, "date") else None
        if last_date is not None and last_date >= _date.today() and len(frame) > 61:
            frame = frame.iloc[:-1]
    except Exception:
        pass
    # nse_ob.validate requirements: tz-naive normalised DatetimeIndex.
    idx = pd.DatetimeIndex(frame.index).tz_localize(None).normalize() if frame.index.tz is not None else pd.DatetimeIndex(frame.index).normalize()
    frame.index = idx
    frame.index = frame.index[~frame.index.duplicated(keep="last")]
    frame = frame.sort_index()
    # validate
    if (
        not isinstance(frame.index, pd.DatetimeIndex)
        or frame.empty
        or not frame.index.is_monotonic_increasing
        or frame.index.has_duplicates
        or not np.isfinite(frame.to_numpy()).all()
        or (frame[["open", "high", "low", "close"]] <= 0).any().any()
        or (frame.volume < 0).any()
        or ((frame.high < frame[["open", "close", "low"]].max(axis=1)) | (frame.low > frame[["open", "close", "high"]].min(axis=1))).any()
    ):
        return None
    return frame


def _indicators(frame: pd.DataFrame, c: OBConfig) -> pd.DataFrame:
    prev = frame.close.shift()
    tr = pd.concat(
        [frame.high - frame.low, (frame.high - prev).abs(), (frame.low - prev).abs()],
        axis=1,
    ).max(axis=1)
    atr = pd.Series(float("nan"), index=frame.index)
    n = c.atr_period
    if len(frame) >= n:
        atr.iloc[n - 1] = tr.iloc[:n].mean()
        for i in range(n, len(frame)):
            atr.iloc[i] = (atr.iloc[i - 1] * (n - 1) + tr.iloc[i]) / n
    baseline = frame.volume.shift().rolling(c.rvol_period).mean().replace(0, float("nan"))
    return pd.DataFrame({"atr": atr, "rvol": frame.volume / baseline})


def detect_orderblock(df: pd.DataFrame, cfg: Optional[OBConfig] = None) -> Optional[TechnicalSignalResult]:
    """Return the most recent Order-Block entry candidate, or None.

    Scans `df` (yfinance-style OHLCV) with the nse-orderblock Detector
    rules. The returned signal uses:
      entry = first-touch limit price (min(open, zone_top), Mode A)
      stop  = entry - 2.0 x lagged ATR(14)   (Experiment E winner)
      strength graded 6.0-9.5 from RVOL + displacement body/ATR.
    Only the latest `entry_candidate` is returned (today's actionable
    signal); older ones are consumed by design (first touch only).
    """
    frame = _to_ob_frame(df)
    if frame is None or len(frame) < 61:
        return None
    c = cfg or _ob_config_from_thresholds()
    ind = _indicators(frame, c)
    atr_lagged = compute_lagged_atr(frame, c.atr_period)

    blocks: list[dict] = []
    used_origins: set[int] = set()
    # (bar_idx, block_id, price)
    last_candidate: Optional[tuple[int, int, float]] = None

    for i, (date, bar) in enumerate(frame.iterrows()):
        # Existing zones only — today's new OB unavailable until tomorrow.
        for blk in blocks:
            if blk["state"] not in {"active", "waiting", "pending"}:
                continue
            if blk["state"] == "pending":
                last_candidate = (i, blk["id"], float(bar.open))
                blk["state"] = "consumed"
                continue
            if i - blk["confirmed"] > c.lifetime:
                blk["state"] = "expired"
                continue
            touch = bar.low <= blk["top"]
            if blk["state"] == "active" and touch:
                blk["touched"] = True
                if c.entry_mode in {"A", "B"}:
                    limit = blk["top"] if c.entry_mode == "A" else (blk["top"] + blk["low"]) / 2
                    if bar.low <= limit:
                        last_candidate = (i, blk["id"], float(min(bar.open, limit)))
                    blk["state"] = "consumed"
                    continue
                blk["state"] = "waiting"
            if bar.close < blk["low"]:
                blk["state"] = "invalidated"
            elif blk["state"] == "waiting" and bar.close > blk["top"]:
                blk["state"] = "pending"

        atr, rvol = ind.iloc[i]
        span = bar.high - bar.low
        if (
            not math.isfinite(atr) or span <= 0 or bar.close <= bar.open
            or bar.close - bar.open < c.disp_atr * atr
            or bar.close < bar.high - c.top_fraction * span
            or (c.use_rvol and (not math.isfinite(rvol) or rvol < c.min_rvol))
        ):
            continue
        origin = None
        for k in range(i - 1, max(-1, i - c.lookback - 1), -1):
            prior = frame.iloc[k]
            if prior.close < prior.open:
                origin = k
                break
            if prior.close == prior.open:
                break
        if origin is None or origin in used_origins:
            continue
        prior = frame.iloc[origin]
        low, top = (
            (float(prior.low), float(prior.high))
            if c.zone_mode == "full"
            else (float(prior.close), float(prior.open))
        )
        blocks.append(
            {
                "id": len(blocks),
                "origin": origin,
                "confirmed": i,
                "low": low,
                "top": top,
                "candle_low": float(prior.low),
                "atr": float(atr),
                "rvol": float(rvol) if math.isfinite(rvol) else 1.0,
                "state": "active",
                "touched": False,
            }
        )
        used_origins.add(origin)

    if last_candidate is None:
        return None
    bar_idx, blk_id, entry = last_candidate
    # Only surface candidates on/near the latest bar (fresh first touch).
    # Stale candidates (>3 bars old) are reported as None so the engine
    # does not chase yesterday's touch price.
    if len(frame) - 1 - bar_idx > 3:
        return None
    blk = blocks[blk_id]
    entry_bar = frame.index[bar_idx]
    atr_val = float(atr_lagged.iloc[bar_idx]) if bar_idx < len(atr_lagged) else float("nan")
    if not math.isfinite(atr_val) or atr_val <= 0:
        atr_val = float(blk["atr"])
    if not math.isfinite(atr_val) or atr_val <= 0 or entry <= 0:
        return None
    stop = float(entry - ATR_MULT * atr_val)
    if entry <= stop:
        return None
    # Reject if the block is already invalidated post-touch (close < zone low
    # after the entry bar) — mirrors nse-ob invalidation.
    zone_low = float(blk["low"])
    after = frame.iloc[bar_idx + 1 :]
    if len(after) and bool((after.close < zone_low).any()):
        return None

    # Grade strength: base 7.0 + RVOL + displacement quality (6.0-9.5).
    vol_ratio = float(blk["rvol"]) if blk["rvol"] and math.isfinite(blk["rvol"]) else 1.0
    disp_body = float(frame.iloc[blk["confirmed"]].close - frame.iloc[blk["confirmed"]].open)
    disp_atr = disp_body / float(blk["atr"]) if blk["atr"] else 1.0
    strength = 7.0 + min(1.0, max(0.0, vol_ratio - 1.0) * 0.5) + min(1.0, max(0.0, disp_atr - 1.0) * 0.5)
    strength = round(min(9.5, max(6.0, strength)), 2)
    return TechnicalSignalResult(
        pattern_type=PatternType.ORDER_BLOCK,
        atr_14=float(atr_val),
        entry_price=float(entry),
        structural_stop_price=float(stop),
        signal_strength=float(strength),
        vol_ratio=float(vol_ratio),
    )
