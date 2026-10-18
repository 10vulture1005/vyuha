# investor/news.py
"""Layer 3 — event-driven news overlay.

Pipeline for every headline on every holding and watchlist name:

    fetch (Google News RSS)
      -> Tier 1 FinBERT sentiment        (agents/tools/news_tools.py)
      -> Tier 2 LLM event classification (same module, optional)
      -> event_type + materiality score
      -> 7-day exponentially decayed sentiment

Event types and what they are allowed to do:

    HARD_NEGATIVE   fraud / SEBI-ED probe / auditor resignation or qualification /
                    default or junk downgrade / pledge spike / CEO-CFO exit under a
                    cloud. Blocks buys immediately; exits at the next open IF price
                    confirms, otherwise 5-day review state.
    POSITIVE        earnings beat / order win / guidance raise / upgrade. NEVER buys
                    on the headline. A post-event-drift entry is allowed only with
                    full technical confirmation, at 1.25x tranche 1.
    OVERREACTION    negative but non-fundamental (rumor, sector or market noise).
                    Permits an add on a top-30% name above its 200DMA after three
                    stabilization days.
    MACRO           RBI / crude / geopolitics / budget. Feeds Layer 0 ONLY; these
                    are dropped before they can touch a single-stock decision.

Every classification is returned as a `NewsEvent` so the caller can persist the
full daily history. That persistence is what makes a real news backtest
possible later; without it, this layer stays forward-test only forever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from loguru import logger

from investor.config import investor_config

# ── Event taxonomy ────────────────────────────────────────────────────────────

HARD_NEGATIVE = "HARD_NEGATIVE"
POSITIVE = "POSITIVE"
OVERREACTION = "OVERREACTION"
MACRO = "MACRO"
NEUTRAL = "NEUTRAL"

#: Phrases that map to a hard negative. Deliberately explicit rather than
#: model-driven for this tier: a governance veto that fires on a vague
#: embedding is worse than one that misses a synonym.
_HARD_NEGATIVE_PATTERNS: List[Tuple[str, str]] = [
    (r"\bseo\s*fraud\b|\baccounting fraud\b|\bfraud(ulent)? (case|charge|allegation|investigation)\b", "fraud"),
    (r"\b(sebi|sfio|cbi|ed)\b.{0,40}\b(probe|investigation|raid|notice|show cause)\b", "regulatory_probe"),
    (r"\b(probe|investigation|search|raid)\b.{0,40}\b(sebi|sfio|cbi|ed|police)\b", "regulatory_probe"),
    (r"\bauditors?\b.{0,30}\b(resigns?|quits?|disqualif\w*|qualified opinion|adverse opinion)\b", "auditor_resignation"),
    (r"\b(qualified|adverse|disclaimer)\s+opinion\b", "auditor_qualification"),
    (r"\b(defaults?|defaults\b|missed payment|payment default)\b", "default"),
    (r"\bjunk\b|\bdowngrad\w+ to\b", "junk_downgrade"),
    (r"\brating (downgrade|cut)\b|\b(crisil|icra|india ratings)\b.{0,20}downgrad", "rating_downgrade"),
    (r"\bpledge\w*\b.{0,40}\b(spike|surge|jump|sharp (rise|increase))\b", "pledge_spike"),
    (r"\b(ceo|cfo|md|chairman|managing director)\b.{0,30}\b(resigns?|resignation|quits?|exit|steps? down|step down)\b", "exec_exit"),
]

#: Phrases that map to a positive catalyst.
_POSITIVE_PATTERNS: List[Tuple[str, str]] = [
    (r"\b(earnings|profit|pat|net profit|results?)\b.{0,40}\b(beat|beats|above estimate|surpass)\b", "earnings_beat"),
    (r"\b(raises?|raised|lifts?|lifts up)\b.{0,25}\b(guidance|forecast|outlook|target)\b", "guidance_raise"),
    (r"\b(guidance|outlook)\b.{0,25}\b(raises?|raised|lifted|upgrade[sd]?)\b", "guidance_raise"),
    (r"\b(order|win|contract|mandate)\b.{0,30}\b(wins?|awarded|secured|bags?)\b", "order_win"),
    (r"\b(bag|secures?|wins?)\b.{0,25}\b(₹\s?[\d,.]+\s?(cr|crore|lakh)|order|contract)\b", "order_win"),
    (r"\b(upgrad(e|ed|es)|reiterat(e|ed) buy|raised to buy)\b", "upgrade"),
]

#: Non-fundamental negatives: real, but not a reason to change the thesis.
_OVERREACTION_PATTERNS: List[Tuple[str, str]] = [
    (r"\b(rumou?r|speculation|unconfirmed|rumour)\b", "rumor"),
    (r"\b(sector[- ]wide|peer stocks|broader market|market[- ]wide)\b", "market_noise"),
    (r"\b(dip|falls?|slips?|drops?)\b.{0,30}\b(on profit booking|amid broad)\b", "market_noise"),
]

#: Macro news never reaches a single-stock decision.
_MACRO_PATTERNS: List[Tuple[str, str]] = [
    (r"\b(rbi|reserve bank|monetary policy|repo rate|mpc|crude|oil price|brent)\b", "rbi_macro"),
    (r"\b(geopolit\w+|war|ceasefire|tariff|sanction)\b", "geopolitics"),
    (r"\b(budget|union budget|gst (rate|hike|cut)|fiscal)\b", "fiscal"),
    (r"\b(fed|fomc|nasdaq|wall street)\b", "global_macro"),
]

#: Finance terms that make a negative headline *fundamental* rather than noise.
_FUNDAMENTAL_NEGATIVE = re.compile(
    r"\b(guidance cut|profit warning|dilution|qip|qip|fund rais|stake sale|"
    r"promoter (sell|sell-down)|demat|freeze)\b",
    re.IGNORECASE,
)


@dataclass
class NewsEvent:
    """One classified headline."""

    symbol: Optional[str]
    title: str
    url: str
    source: str
    published_at: Optional[datetime]
    event_type: str = NEUTRAL
    subtype: Optional[str] = None
    materiality: float = 0.0
    sentiment: float = 0.0        # -1 .. +1
    finbert_score: Optional[float] = None
    tier: str = "TIER1"           # TIER1 | TIER2 | REGEX
    is_macro: bool = False
    raw_text: str = ""

    def as_row(self, trade_date: date) -> Dict:
        """Row shape for the `news_events` table."""
        return {
            "trade_date": trade_date,
            "symbol": self.symbol,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "event_type": self.event_type,
            "subtype": self.subtype,
            "materiality": round(float(self.materiality), 4),
            "sentiment": round(float(self.sentiment), 4),
            "finbert_score": self.finbert_score,
            "tier": self.tier,
            "is_macro": self.is_macro,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────

def _match(patterns: Sequence[Tuple[str, str]], text: str) -> Optional[str]:
    for pat, label in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return label
    return None


def is_macro(title: str) -> Optional[str]:
    return _match(_MACRO_PATTERNS, title)


def classify_title(title: str) -> Tuple[str, Optional[str]]:
    """Regex tier: (event_type, subtype).

    Macro is checked FIRST and unconditionally. A headline like "RBI rate cut
    sends bank shares higher" matches both the macro and the positive patterns;
    it must land in MACRO so it can only ever move Layer 0.
    """
    if is_macro(title):
        return MACRO, is_macro(title)
    hard = _match(_HARD_NEGATIVE_PATTERNS, title)
    if hard:
        return HARD_NEGATIVE, hard
    pos = _match(_POSITIVE_PATTERNS, title)
    if pos:
        return POSITIVE, pos
    # Rumour/noise is checked BEFORE the fundamental-negative list. "Unconfirmed
    # rumour of stake sale" contains "stake sale", which the fundamental list
    # would claim — but a rumour is explicitly NOT fundamental, and treating
    # hedged speculation as a governance event is how a dip-buying veto gets
    # wired to nothing.
    ovr = _match(_OVERREACTION_PATTERNS, title)
    if ovr:
        return OVERREACTION, ovr
    if _FUNDAMENTAL_NEGATIVE.search(title):
        return HARD_NEGATIVE, "fundamental_negative"
    return NEUTRAL, None


def materiality(event_type: str, subtype: Optional[str], text: str = "") -> float:
    """0..1 score used to gate the trade rules.

    Hard negatives are structurally high materiality (a regulator showing up is
    always material). Everything else is discounted by how hedged the headline
    is — "unconfirmed", "may", "rumour" — because those words are exactly what
    separates a story from a fact.
    """
    cfg = investor_config.news
    bands = cfg.get("materiality_bands", {})
    low = float(bands.get("low", 0.34))
    medium = float(bands.get("medium", 0.67))

    if event_type == HARD_NEGATIVE:
        base = 0.95
    elif event_type == POSITIVE:
        base = 0.55
    elif event_type == OVERREACTION:
        base = 0.30
    elif event_type == MACRO:
        base = 0.40
    else:
        base = 0.10

    # Hedges mark the difference between a story and a fact. Each one discounts
    # materiality; the bands below are spaced so a single hedge actually moves
    # the score — with the old 0.34/0.67 bands a hedged positive (0.35) and an
    # unhedged one (0.55) both collapsed onto the same 0.5 band and the hedge
    # had no effect at all.
    hedge_words = 0
    for w in (r"\brumou?r\b", r"\bspeculat\w*\b", r"\bunconfirmed\b", r"\bmay\b",
              r"\bmight\b", r"\bin talks\b", r"\bproposed\b", r"\bexpected\b"):
        if re.search(w, text, re.IGNORECASE):
            hedge_words += 1
    base -= 0.15 * hedge_words

    if base >= medium:
        return 1.0
    if base >= low:
        return 0.5
    return 0.25


def finbert_sentiment(title: str) -> Optional[float]:
    """Tier 1 sentiment in [-1, 1] using the repo's existing FinBERT pass.

    Returns None when the model is unavailable so callers can fall back to the
    regex verdict instead of treating a missing model as neutral sentiment.
    """
    try:
        from agents.tools.news_tools import get_finbert

        result = get_finbert()(title)[0]
        label = str(result.get("label", "")).lower()
        score = float(result.get("score", 0.0))
        if label == "positive":
            return score
        if label == "negative":
            return -score
        return 0.0
    except Exception as exc:
        logger.debug(f"FinBERT unavailable for '{title[:40]}': {exc}")
        return None


def llm_classify(symbol: Optional[str], title: str) -> Optional[Dict]:
    """Tier 2 LLM pass. Returns None when disabled or unavailable."""
    try:
        import json
        from config.settings import settings
        from openai import OpenAI

        if not settings.XAI_API_KEY:
            return None
        client = OpenAI(
            api_key=settings.XAI_API_KEY.get_secret_value(),
            base_url="https://api.x.ai/v1",
        )
        prompt = (
            f"Classify this Indian equity headline about {symbol or 'the Indian market'}.\n"
            f"Headline: {title}\n\n"
            "Return JSON with keys: event_type (one of HARD_NEGATIVE, POSITIVE, "
            "OVERREACTION, MACRO, NEUTRAL), subtype (short slug), "
            "materiality (0 to 1), sentiment (-1 to 1). "
            "Mark MACRO for RBI, crude, geopolitics or budget news."
        )
        resp = client.chat.completions.create(
            model="grok-beta",
            messages=[
                {"role": "system", "content": "JSON-only bot. Output strict JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        return json.loads(resp.choices[0].message.content.strip())
    except Exception as exc:
        logger.debug(f"Tier 2 LLM classification unavailable: {exc}")
        return None


def classify_headline(
    symbol: Optional[str],
    headline,
    use_finbert: bool = True,
    use_llm: bool = True,
) -> NewsEvent:
    """Full two-tier classification of one headline."""
    title = getattr(headline, "title", None) or (
        headline.get("title") if isinstance(headline, dict) else str(headline)
    )
    url = getattr(headline, "url", "") or ""
    source = getattr(headline, "source", "") or ""
    published = getattr(headline, "published_date", None)

    event_type, subtype = classify_title(title)
    tier = "REGEX"
    sentiment = 0.0
    fb_score: Optional[float] = None

    if use_finbert:
        fb = finbert_sentiment(title)
        if fb is not None:
            fb_score = abs(fb)
            sentiment = fb
            tier = "TIER1"
            # FinBERT sentiment can promote an otherwise neutral headline into
            # a positive catalyst, but it can NEVER downgrade a hard negative —
            # the governance veto runs on the rule list, not the model.
            if event_type == NEUTRAL and fb > float(investor_config.news.get("finbert_threshold", 0.85)):
                event_type, subtype, tier = POSITIVE, "finbert_positive", "TIER1"

    if use_llm and event_type == NEUTRAL:
        res = llm_classify(symbol, title)
        if res:
            tier = "TIER2"
            event_type = str(res.get("event_type", event_type)).upper()
            subtype = res.get("subtype", subtype)
            try:
                sentiment = float(res.get("sentiment", sentiment))
            except (TypeError, ValueError):
                pass

    mat = materiality(event_type, subtype, title)

    return NewsEvent(
        symbol=symbol,
        title=title,
        url=url,
        source=source,
        published_at=published,
        event_type=event_type,
        subtype=subtype,
        materiality=mat,
        sentiment=sentiment,
        finbert_score=fb_score,
        tier=tier,
        is_macro=(event_type == MACRO),
        raw_text=title,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation + decision rules
# ─────────────────────────────────────────────────────────────────────────────

def decay_sentiment(
    events: Sequence[NewsEvent],
    as_of: datetime,
    half_life_days: Optional[float] = None,
) -> float:
    """Exponentially decayed sentiment with a 7-day half-life.

    Returns the decayed SUM, not a weighted average. An average normalises by
    the total weight, which makes a single month-old scandal score exactly as
    strongly as one from this morning — the age cancels out entirely and the
    signal never fades. Summing instead lets the magnitude decay: one event at
    -1.0 is -1.0 today, -0.5 after 7 days and -0.003 after 60.

    A week-old scandal still counts; a month-old one is nearly noise.
    """
    hl = float(half_life_days if half_life_days is not None
               else investor_config.news.get("sentiment_half_life_days", 7.0))
    if hl <= 0 or not events:
        return 0.0
    total = 0.0
    for ev in events:
        ts = ev.published_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (as_of - ts).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / hl)
        total += weight * float(ev.sentiment)
    return total


@dataclass
class NewsVerdict:
    """Per-symbol aggregate of the day's headlines."""

    symbol: str
    events: List[NewsEvent] = field(default_factory=list)
    hard_negative: bool = False
    hard_negative_reason: Optional[str] = None
    positive_catalyst: bool = False
    overreaction: bool = False
    macro_only: bool = False
    sentiment: float = 0.0

    def blocking(self) -> bool:
        """True when new buys must be blocked immediately."""
        return self.hard_negative

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "hard_negative": self.hard_negative,
            "hard_negative_reason": self.hard_negative_reason,
            "positive_catalyst": self.positive_catalyst,
            "overreaction": self.overreaction,
            "macro_only": self.macro_only,
            "sentiment": round(self.sentiment, 4),
            "n_events": len(self.events),
        }


def aggregate(symbol: str, events: Sequence[NewsEvent], as_of: datetime) -> NewsVerdict:
    """Folds a symbol's headlines into a single verdict.

    Macro headlines are recorded but never influence the verdict — they are
    routed to Layer 0 by the caller.
    """
    v = NewsVerdict(symbol=symbol, events=list(events))
    non_macro = 0
    for ev in events:
        if ev.event_type == MACRO:
            continue
        non_macro += 1
        if ev.event_type == HARD_NEGATIVE:
            v.hard_negative = True
            v.hard_negative_reason = f"{ev.subtype}: {ev.title}"
        elif ev.event_type == POSITIVE:
            v.positive_catalyst = True
        elif ev.event_type == OVERREACTION:
            v.overreaction = True
    # "macro only" means every headline was macro — i.e. nothing here says
    # anything about this particular company.
    v.macro_only = (non_macro == 0)
    v.sentiment = decay_sentiment(v.events, as_of)
    return v


def price_confirms_negative(
    day_open: float,
    prev_close: float,
    close: float,
    sma_fast: Optional[float],
    volume: float,
    avg_volume: float,
) -> Tuple[bool, str]:
    """Does price confirm a hard negative? Two independent ways to say yes.

    Confirms on (a) a >= 5% drop on >= 2x volume, OR (b) a close below the
    50DMA. Condition (b) catches the slow bleed that never prints a big volume
    day but is still a broken chart.
    """
    cfg = investor_config.news.get("hard_negative", {})
    drop_pct = (prev_close - close) / prev_close * 100.0 if prev_close > 0 else 0.0
    need_drop = float(cfg.get("confirm_drop_pct", 5.0))
    need_vol = float(cfg.get("confirm_vol_multiple", 2.0))

    vol_mult = (volume / avg_volume) if avg_volume and avg_volume > 0 else 0.0
    if drop_pct >= need_drop and vol_mult >= need_vol:
        return True, f"drop {drop_pct:.1f}% on {vol_mult:.1f}x volume"

    ma_level = float(cfg.get("or_close_below_ma", 50))
    if sma_fast is not None and close < sma_fast and ma_level == 50:
        return True, f"close {close:.1f} below 50DMA {sma_fast:.1f}"
    return False, ""


def catalyst_entry_allowed(
    day_high: float,
    day_low: float,
    day_close: float,
    prev_close: float,
    volume: float,
    avg_volume: float,
) -> Tuple[bool, str]:
    """Post-event-drift confirmation for a positive catalyst.

    Requires: the gap was held, the close landed in the top 30% of the day's
    range, and volume >= 2x average. All three — a gap that faded, or a strong
    close on no volume, is exactly what a failed catalyst looks like.
    """
    cfg = investor_config.news.get("positive_catalyst", {})
    need_pct = float(cfg.get("min_close_range_pct", 0.30))
    need_vol = float(cfg.get("min_vol_multiple", 2.0))

    rng = day_high - day_low
    if rng <= 0:
        return False, "no intraday range"
    close_pos = (day_close - day_low) / rng
    if close_pos < need_pct:
        return False, f"closed at {close_pos:.0%} of range (< {need_pct:.0%})"
    if prev_close > 0 and day_close < prev_close:
        return False, "gap not held (close below prior close)"
    vol_mult = (volume / avg_volume) if avg_volume and avg_volume > 0 else 0.0
    if vol_mult < need_vol:
        return False, f"volume only {vol_mult:.1f}x"
    return True, f"closed {close_pos:.0%} of range on {vol_mult:.1f}x volume"


def overreaction_add_allowed(
    days_since_event: int,
    above_sma_slow: bool,
    score_percentile: float,
    sentiment: float,
) -> Tuple[bool, str]:
    """Allows an add on an overreaction dip after three stabilization days."""
    cfg = investor_config.news.get("overreaction", {})
    need_days = int(cfg.get("stabilization_days", 3))
    need_pct = float(cfg.get("min_score_percentile", 70.0))
    if days_since_event < need_days:
        return False, f"only {days_since_event} stabilization days"
    if not above_sma_slow:
        return False, "below 200DMA"
    if score_percentile < need_pct:
        return False, f"score percentile {score_percentile:.0f} < {need_pct:.0f}"
    return True, "overreaction dip confirmed"


# ─────────────────────────────────────────────────────────────────────────────
# Backtest proxy: the earnings-day gap/volume signature
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NewsProxy:
    """The backtestable stand-in for news, used when `news=proxy`.

    No historical headline archive exists for Indian equities, so the backtest
    cannot know what was printed. What it CAN know is the price/volume
    signature an event leaves behind: the gap on the day a quarter is reported
    and the volume ratio against the 20-day average. Those two numbers are
    logged for every earnings date in history and used to gate the same rules
    the live classifier would fire.
    """

    symbol: str
    date: Optional[pd.Timestamp] = None
    gap_pct: float = 0.0
    volume_ratio: float = 0.0
    gap_held: bool = False
    close_range_position: float = 0.0
    is_negative: bool = False
    is_catalyst: bool = False

    def as_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "gap_pct": round(self.gap_pct, 4),
            "volume_ratio": round(self.volume_ratio, 4),
            "gap_held": self.gap_held,
            "close_range_position": round(self.close_range_position, 4),
            "is_negative": self.is_negative,
            "is_catalyst": self.is_catalyst,
        }


def earnings_proxy(
    symbol: str,
    bar: pd.Series,
    prev_bar: pd.Series,
    avg_volume: float,
    gap_pct_threshold: float = 3.0,
    vol_threshold: float = 1.5,
) -> Optional[NewsProxy]:
    """Classifies one bar as an earnings-event proxy, or None if it isn't one.

    An "event day" is a >= 3% gap on >= 1.5x volume — the signature that
    distinguishes a scheduled-results reaction from ordinary price noise.
    """
    prev_close = float(prev_bar["Close"])
    close = float(bar["Close"])
    if prev_close <= 0:
        return None
    gap_pct = (float(bar["Open"]) - prev_close) / prev_close * 100.0
    vol_mult = (float(bar["Volume"]) / avg_volume) if avg_volume and avg_volume > 0 else 0.0

    is_event = abs(gap_pct) >= gap_pct_threshold and vol_mult >= vol_threshold
    if not is_event:
        return None

    rng = float(bar["High"]) - float(bar["Low"])
    close_pos = ((close - float(bar["Low"])) / rng) if rng > 0 else 0.5
    return NewsProxy(
        symbol=symbol,
        gap_pct=gap_pct,
        volume_ratio=vol_mult,
        gap_held=close >= prev_close,
        close_range_position=close_pos,
        is_negative=(gap_pct <= -gap_pct_threshold),
        is_catalyst=(gap_pct >= gap_pct_threshold),
    )


__all__ = [
    "HARD_NEGATIVE", "POSITIVE", "OVERREACTION", "MACRO", "NEUTRAL",
    "NewsEvent", "NewsVerdict", "NewsProxy",
    "classify_title", "classify_headline", "materiality", "is_macro",
    "finbert_sentiment", "llm_classify", "decay_sentiment", "aggregate",
    "price_confirms_negative", "catalyst_entry_allowed", "overreaction_add_allowed",
    "earnings_proxy",
]