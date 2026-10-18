# agents/tools/news_tools.py
"""News ingestion and two-tier governance/sentiment classifier.

Tier 1 — FinBERT NLP Pass:
    HuggingFace inference using ProsusAI/finbert. Evaluates headlines for
    negative financial sentiment (confidence > 0.85).

Tier 2 — LLM Contextual Pass (xAI Grok):
    For ambiguous headlines, invoke a Grok API classification call
    with structured JSON output. Gracefully skips if no API key is set.
"""
import re
import json
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field
from loguru import logger

from config.settings import settings


class HeadlineSchema(BaseModel):
    """Validated news headline with metadata."""

    title: str
    published_date: datetime
    source: str
    url: str
    # Providers that score sentiment for us (MarketAux returns a per-entity
    # sentiment_score) attach it here. None means nobody has an opinion, which
    # must never be read as neutral-and-therefore-fine.
    sentiment_score: Optional[float] = None


class ClassificationResult(BaseModel):
    """Verdict from the two-tier governance vetting pipeline."""

    is_red_flag: bool = Field(
        ...,
        description="True if severe governance, regulatory, or fraud risk detected.",
    )
    severity: str = Field(
        ..., description="'HIGH', 'MEDIUM', 'LOW', 'NONE', or 'UNKNOWN'"
    )
    reason: str = Field(
        ..., description="Concise explanation of the red flag."
    )
    outcome: str = Field(
        default="evaluated",
        description="'CLEAN', 'RED_FLAG', 'SCREENING_FAILED', or 'UNKNOWN'",
    )
    source_url: Optional[str] = None


_finbert_pipeline = None

def get_finbert():
    """Lazily load the 400MB FinBERT model so we don't block boot-up."""
    global _finbert_pipeline
    if _finbert_pipeline is None:
        from transformers import pipeline
        logger.info("Loading ProsusAI/finbert model weights into memory...")
        _finbert_pipeline = pipeline("sentiment-analysis", model="ProsusAI/finbert")
    return _finbert_pipeline


def finbert_negative_scores(titles: List[str], batch_size: int = 32) -> List[float]:
    """Batched P(negative) for many headlines in a single model pass.

    FinBERT classifies one headline at a time when called through the
    pipeline, which is wasteful at watchlist scale. Passing the whole list
    lets the pipeline batch it: ~175 headlines/s on CPU versus ~1/s called
    per headline.

    Returns P(negative) in [0, 1] per input title, in order. Returns
    ``None`` entries (as 0.5, i.e. "no opinion") when the model cannot be
    loaded, so a missing model degrades to "escalate everything" rather than
    silently suppressing escalation.
    """
    if not titles:
        return []
    if not settings.SENTIMENT_FINBERT_ENABLED:
        return [0.5] * len(titles)
    try:
        pipe = get_finbert()
    except Exception as exc:
        logger.warning(f"FinBERT unavailable, escalating all symbols: {exc}")
        return [0.5] * len(titles)

    try:
        # The pipeline truncates at the model's context; long headlines are
        # cut here rather than raising.
        raw = pipe([t[:512] for t in titles], batch_size=batch_size,
                   truncation=True, max_length=256)
    except Exception as exc:
        logger.warning(f"FinBERT inference failed, escalating all: {exc}")
        return [0.5] * len(titles)

    scores: List[float] = []
    for item in raw:
        label = str(item.get("label", "")).lower()
        score = float(item.get("score", 0.5))
        if "negative" in label:
            scores.append(score)
        elif "positive" in label:
            scores.append(1.0 - score)
        else:  # neutral
            scores.append(0.5)
    return scores


def _slice_state_path() -> Path:
    from config.settings import BASE_DIR
    return BASE_DIR / "data" / "cache" / "marketaux_slice.json"


def read_slice_state() -> dict:
    """Last completed MarketAux scan, so a same-day re-run does not re-spend."""
    try:
        return json.loads(_slice_state_path().read_text())
    except Exception:
        return {}


def write_slice_state(state: dict) -> None:
    path = _slice_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2))
        temp.replace(path)
    except Exception as exc:
        logger.warning(f"Could not persist MarketAux slice state: {exc}")


def due_marketaux_slice(symbols: List[str], today=None) -> List[str]:
    """The slice of the watchlist that should be scanned right now.

    The watchlist is split into slices of ``MARKET_SLICE_SIZE``. Runs alternate
    between them so that a free-tier daily budget covers the whole watchlist
    over two days instead of re-scanning one slice and starving the rest.

    The last completed slice is persisted, which makes the schedule survive
    restarts AND makes a same-day re-run a no-op rather than a second spend of
    the daily quota.

    Returns:
        The symbols to scan now. Empty means today's budget is already spent.
    """
    if not symbols:
        return []
    today = today or date.today()
    size = max(1, settings.MARKET_SLICE_SIZE)
    slices = [symbols[i:i + size] for i in range(0, len(symbols), size)]
    if len(slices) == 1:
        return slices[0]

    state = read_slice_state()
    if state.get("date") == today.isoformat():
        logger.info(
            f"MarketAux slice already scanned today ({state.get('slice_index')}); "
            f"skipping to protect the daily quota."
        )
        return []
    next_index = (int(state.get("slice_index", -1)) + 1) % len(slices)
    write_slice_state({
        "date": today.isoformat(),
        "slice_index": next_index,
        "symbol_count": len(slices[next_index]),
        "total_slices": len(slices),
    })
    logger.info(
        f"MarketAux: scanning slice {next_index + 1}/{len(slices)} "
        f"({len(slices[next_index])} symbols) for {today.isoformat()}"
    )
    return slices[next_index]


def fetch_marketaux_batch(
    symbols: List[str], lookback_days: int = 30
) -> Dict[str, List[HeadlineSchema]]:
    """Fetch headlines + per-entity sentiment from MarketAux.

    The free plan caps every response at 3 articles regardless of how many
    symbols the query names, so the request budget — not the symbol list — is
    the binding constraint. This therefore spends requests over wide symbol
    slices and stops at ``MARKET_MAX_REQUESTS_PER_DAY`` rather than walking
    the watchlist one symbol at a time.

    Every article carries an entity-level ``sentiment_score`` and per-highlight
    sentiment, which is server-computed sentiment we do not have to run a
    400MB model for. It is attached to ``HeadlineSchema.sentiment_score``.

    Returns:
        ``{symbol: [HeadlineSchema]}``; symbols with no recent article are
        absent so the caller can fall back.
    """
    out: Dict[str, List[HeadlineSchema]] = {}
    if not settings.MARKETAUX_API_KEY:
        return out
    if settings.NEWS_PROVIDER == "drishti":
        return out

    import requests

    wanted = [s for s in dict.fromkeys(symbols) if s]
    if not wanted:
        return out

    # MarketAux indexes Indian equities with an exchange suffix. Querying the
    # bare symbol returns spurious matches from other markets (a bare
    # "RELIANCE" resolves to unrelated US/Thai entities and yields nothing
    # useful), so qualify with .NS and map the response back by stripping it.
    def _qualify(sym: str) -> str:
        return sym if "." in sym else f"{sym}.NS"

    def _unqualify(sym: str) -> str:
        for suffix in (".NS", ".BO"):
            if sym.endswith(suffix):
                return sym[: -len(suffix)]
        return sym

    token = settings.MARKETAUX_API_KEY.get_secret_value()
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    per_req = max(1, settings.MARKET_SYMBOLS_PER_REQUEST)
    budget = max(1, settings.MARKET_MAX_REQUESTS_PER_DAY)
    max_pages = max(1, settings.MARKET_MAX_PAGES)

    seen: set = set()
    spent = 0
    for start in range(0, len(wanted), per_req):
        chunk = wanted[start:start + per_req]
        chunk_set = set(chunk)
        # Paginate each symbol group. The free plan caps every response at 3
        # articles, so the ONLY way to spend the 100-requests/day allowance
        # on more coverage is to page: a single 20-symbol group burns 3
        # requests for 9 articles otherwise, while 6 groups x 8 pages spends
        # 48 requests for ~144 articles over the same symbols.
        for page in range(1, max_pages + 1):
            if spent >= budget:
                logger.warning(
                    f"MarketAux daily budget ({budget}) reached; "
                    f"{len(wanted) - start} symbols unscanned this run."
                )
                break
            params = {
                "api_token": token,
                "symbols": ",".join(_qualify(s) for s in chunk),
                "language": "en",
                "filter_entities": "true",
                "limit": 3,  # the free-plan cap; asking for more is ignored
                "page": page,
            }
            spent += 1
            try:
                resp = requests.get(
                    "https://api.marketaux.com/v1/news/all", params=params, timeout=30
                )
            except Exception as exc:
                logger.warning(f"MarketAux group {start} page {page} failed: {exc}")
                break
            if not resp.ok:
                # 402 = daily allowance exhausted; stop cleanly rather than
                # burning the remaining budget on guaranteed failures.
                if resp.status_code == 402:
                    logger.error(
                        "MarketAux daily usage limit reached (HTTP 402). "
                        "Remaining symbols are recorded SCREENING_FAILED."
                    )
                else:
                    logger.warning(
                        f"MarketAux group {start} page {page} failed: "
                        f"HTTP {resp.status_code} {resp.text[:120]}"
                    )
                break
            articles = resp.json().get("data") or []
            if not articles:
                break  # exhausted this group's result set

            for art in articles:
                uuid = art.get("uuid")
                if uuid and uuid in seen:
                    continue
                if uuid:
                    seen.add(uuid)
                title = (art.get("title") or "").strip()
                if not title:
                    continue
                pub_dt = _parse_news_date(art.get("published_at"))
                if pub_dt is not None and pub_dt < cutoff:
                    continue

                for ent in art.get("entities") or []:
                    sym = _unqualify(str(ent.get("symbol") or "").strip())
                    if sym not in chunk_set:
                        continue
                    out.setdefault(sym, []).append(HeadlineSchema(
                        title=title,
                        published_date=pub_dt or datetime.now(timezone.utc),
                        source=art.get("source") or "MarketAux",
                        url=art.get("url") or "",
                        sentiment_score=_coerce_float(ent.get("sentiment_score")),
                    ))

    for sym in out:
        out[sym] = out[sym][:15]
    logger.info(
        f"MarketAux: {spent} request(s) -> {sum(len(v) for v in out.values())} "
        f"headline(s) for {len(out)}/{len(wanted)} symbol(s)"
    )
    return out


def _coerce_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_drishti_batch(
    symbols: List[str], lookback_days: int = 30
) -> Dict[str, List[HeadlineSchema]]:
    """Fetch headlines for many symbols in a handful of batched API calls.

    The per-symbol Google News path issued one HTTP request per name, which
    cost ~84s for a 105-symbol watchlist. Drishti accepts up to 20 symbols per
    request and pages up to 50 articles, so the same watchlist costs ~3s.

    Articles reference several symbols in one colon-delimited field
    ("BSE:GROWW:ANGELONE"), so one fetched article can satisfy several
    watchlist entries at once.

    Args:
        symbols: watchlist symbols to fetch for.
        lookback_days: drop articles older than this.

    Returns:
        ``{symbol: [HeadlineSchema]}``. Symbols Drishti does not cover are
        simply absent from the mapping, so the caller can record them as
    SCREENING_FAILED rather than as clean.
    """
    out: Dict[str, List[HeadlineSchema]] = {}
    if settings.NEWS_PROVIDER == "marketaux" or not settings.DRISHTI_API_KEY:
        # 'marketaux' is an explicit instruction to reserve the Drishti
        # budget, so it must opt out even when a key is configured.
        return out
    try:
        from drishti_sdk import DrishtiClient
    except ImportError:
        logger.warning("drishti-sdk not installed; skipping Drishti news fetch.")
        return out

    wanted = [s for s in dict.fromkeys(symbols) if s]
    if not wanted:
        return out

    batch = settings.DRISHTI_BATCH_SIZE
    page_size = settings.DRISHTI_PAGE_LIMIT
    max_pages = settings.DRISHTI_MAX_PAGES
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    try:
        client = DrishtiClient(
            api_key=settings.DRISHTI_API_KEY.get_secret_value(),
            # 429 is a PER-MINUTE quota, so the SDK's default 0.3s -> 5s
            # backoff never outlasts it. Back off for up to a minute instead.
            retry_max_retries=4,
            retry_initial_delay=8.0,
            retry_max_delay=45.0,
        )
    except Exception as exc:
        logger.warning(f"Drishti client init failed: {exc}")
        return out

    interval = settings.DRISHTI_MIN_INTERVAL
    seen_links: set = set()
    for start in range(0, len(wanted), batch):
        chunk = wanted[start:start + batch]
        chunk_set = set(chunk)
        if start and interval:
            time.sleep(interval)
        for page in range(1, max_pages + 1):
            try:
                resp = client.get_news(
                    symbols=chunk, limit=page_size, page=page
                )
                rows = resp["data"] if isinstance(resp, dict) else resp.data
            except Exception as exc:
                logger.warning(
                    f"Drishti batch {start}-{start + len(chunk)} page {page} "
                    f"failed: {str(exc)[:120]}"
                )
                break
            if not rows:
                break
            new_this_page = 0
            for item in rows:
                link = (item.get("link") or "").strip()
                if link and link in seen_links:
                    continue  # pages overlap; de-duplicate across the batch
                if link:
                    seen_links.add(link)
                new_this_page += 1

                title = (item.get("title") or item.get("specific_title") or "").strip()
                if not title:
                    continue
                pub = (item.get("date") or item.get("published_at")
                       or item.get("published_date"))
                pub_dt = _parse_news_date(pub)
                if pub_dt is not None and pub_dt < cutoff:
                    continue

                # An article can mention several symbols; attribute it to each
                # watchlist member present in the colon-delimited field.
                for sym in str(item.get("symbol") or "").split(":"):
                    sym = sym.strip()
                    if sym not in chunk_set:
                        continue
                    out.setdefault(sym, []).append(HeadlineSchema(
                        title=title,
                        published_date=pub_dt or datetime.now(timezone.utc),
                        source=item.get("source") or "Drishti",
                        url=link,
                    ))
            if new_this_page == 0:
                break  # exhausted
        logger.info(
            f"Drishti: fetched {sum(len(v) for v in out.values())} headline(s) "
            f"for {len(out)}/{len(wanted)} symbol(s)"
        )

    for sym in out:
        out[sym] = out[sym][:15]
    return out


def _parse_news_date(value) -> Optional[datetime]:
    """Parse whatever timestamp shape the provider used, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    for parser in (
        lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
        lambda t: datetime.strptime(t[:25], "%a, %d %b %Y %H:%M:%S"),
    ):
        try:
            dt = parser(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _hf_finbert_classify(text: str):
    """Call hosted FinBERT on Hugging Face. Returns {'label':..., 'score':...} or None."""
    import time
    from config.settings import settings
    token = settings.HF_API_TOKEN
    secret = token.get_secret_value() if token is not None else None
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    url = "https://router.huggingface.co/hf-inference/models/ProsusAI/finbert"
    for attempt in range(3):
        try:
            import requests
            resp = requests.post(url, headers=headers, json={"inputs": text}, timeout=30)
            if resp.status_code == 503:
                time.sleep(5)
                continue
            if not resp.ok:
                logger.error(f"FinBERT HF API error {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            if isinstance(data, list) and data and isinstance(data[0], list):
                data = data[0]
            if isinstance(data, list) and data:
                best = max(data, key=lambda x: float(x.get("score", 0.0)))
                return {"label": best.get("label"), "score": float(best.get("score", 0.0))}
            return None
        except Exception as e:
            logger.error(f"FinBERT HF API exception: {e}")
            return None
    return None



def _sentiment_classify_headline(text: str):
    """Try hosted FinBERT, then an OpenRouter LLM fallback, otherwise None."""
    res = _hf_finbert_classify(text)
    if res is not None:
        return res

    from config.settings import settings
    key = settings.OPENROUTER_API_KEY
    secret = key.get_secret_value() if key is not None else None
    if not secret:
        return None
    try:
        import json, requests
        prompt = (
            "Classify the financial headline below as one of: positive, neutral, negative. "
            "Respond ONLY with a JSON object: {\"label\": \"positive|neutral|negative\", \"score\": 0.0-1.0}. "
            "Headline: " + text
        )
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            json={
                "model": "nvidia/nemotron-3-super-120b-a12b:free",
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            },
            timeout=30,
        )
        if not resp.ok:
            logger.error(f"LLM sentiment fallback error {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return {"label": parsed.get("label"), "score": float(parsed.get("score", 0.0))}
    except Exception as e:
        logger.error(f"LLM sentiment fallback exception: {e}")
        return None

def _tier2_cache_dir() -> Path:
    from config.settings import BASE_DIR
    d = BASE_DIR / "data" / "cache" / "tier2_verdicts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tier2_cache_key(symbol: str, headlines: List[HeadlineSchema]) -> str:
    import hashlib
    payload = json.dumps(
        {"symbol": symbol, "titles": sorted(h.title for h in headlines)},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _tier2_cache_get(key: str) -> Optional[ClassificationResult]:
    path = _tier2_cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return ClassificationResult(**data)
    except Exception:
        return None


def _tier2_cache_put(key: str, result: ClassificationResult) -> None:
    try:
        path = _tier2_cache_dir() / f"{key}.json"
        path.write_text(json.dumps(result.__dict__, default=str))
    except Exception as exc:
        logger.debug(f"Tier 2 cache write failed: {exc}")


# --- Tier 1: Deterministic hard-regex governance screen ---
# Split into two tiers of evidence on purpose.
#
# TIER1_STANDALONE are intrinsically adverse: "fraud" or "money laundering" is
# a red flag whatever it is attached to.
#
# TIER1_CONTEXTUAL are neutral tokens that only mean something alongside an
# adverse action. These previously matched bare, which vetoed large caps on
# routine regulatory news: BSE and MCX were dropped for headlines about SEBI's
# CAS framework, 360ONE for a mutual-fund fee revamp, and MRF for a positive
# feature on its promoter family. \bban\b and \bed\b also matched ordinary
# English ("banned", "red"). Requiring co-occurrence keeps the genuine hits
# (CBI chargesheet, SEBI show-cause, auditor resignation) while letting
# routine regulator/company news through to Tier 2.
TIER1_STANDALONE = [
    re.compile(r"\bfraud\w*\b", re.IGNORECASE),
    re.compile(r"\bmoney[- ]?launder\w*\b", re.IGNORECASE),
    re.compile(r"\bshow[- ]?cause\b", re.IGNORECASE),
    re.compile(r"\bforensic[- ]audit\w*\b", re.IGNORECASE),
    re.compile(r"\bwhistle[- ]?blower\w*\b", re.IGNORECASE),
    re.compile(r"\bdelist\w*\b", re.IGNORECASE),
    re.compile(r"\binsolven\w*\b", re.IGNORECASE),
    re.compile(r"\b(?:default|defaults|defaulted|defaulting)\b", re.IGNORECASE),
    re.compile(r"\bchargesheet\b", re.IGNORECASE),
    re.compile(r"\b(?:arrest|arrested|detained|raid|raided|seiz\w*)\b", re.IGNORECASE),
    re.compile(r"\b(?:auditors?|audit firm)s?\b.{0,40}\bresign\w*\b", re.IGNORECASE),
    re.compile(r"\bresign(?:s|ed|ing|ation)?\b.{0,40}\b(?:auditor|audit|director|md|ceo)\w*\b", re.IGNORECASE),
    # ...and the reverse order: "Why Did Non-Executive Director Resign?"
    re.compile(r"\b(?:auditor|audit|director|md|ceo)\w*\b.{0,40}\bresign(?:s|ed|ing|ation)?\b", re.IGNORECASE),
]
_NEGATIVE = (
    r"probe|investigat\w*|inquiry|enforce\w*|action|ban(?:ned)?|penal\w*|fine[sd]?|"
    r"fined|warn(?:ing|ed)?|charge[sd]?|violat\w*|breach\w*|misappropriat\w*|"
    r"irregularit\w*|red[- ]?flag|defraud\w*|scrutiny|raid\w*|seiz\w*|suspend\w*|"
    r"expel\w*|blacklist\w*|penalise\w*|penalize\w*|order\w*|notice|show[- ]?cause|"
    r"detain\w*|arrest\w*|fraud\w*|convict\w*|summon\w*"
)
# Enforcers act; prices merely move. Magnitude verbs belong to the pledge
# vocabulary only — pairing them with an agency token re-created the original
# false positive ("capital market stocks RISE ... as Sebi revises CAS
# framework"), because a rising share price is not enforcement.
_NEG_PLEDGE = _NEGATIVE + (
    r"|spike[sd]?|spiking|surge[sd]?|jump(?:ed|s)?|soar(?:ed|s)?|topple[sd]?|"
    r"slide[sd]?|plunge[sd]?|slump\w*|ris(?:e|es|ing|en)|climb\w*|mount\w*|"
    r"deteriorat\w*|worsen\w*|edg(?:e|es|ing)"
)
# Wrap the whole alternation in \b(?:...)\b. Without it `action` matches
# inside "reaction" and `fine` matches "define" — which is exactly how
# "shares see mixed reACTION to the new SEBI CAS rules" got vetoed.
_NEG_B = rf"\b(?:{_NEGATIVE})\b"
_NEG_PLEDGE_B = rf"\b(?:{_NEG_PLEDGE})\b"
# Agency / governance tokens that are only a red flag when paired with a
# negative action nearby. Order in the alternation is irrelevant; the 60-char
# window is what stops "SEBI approval" reading as enforcement.
TIER1_CONTEXTUAL = [
    re.compile(rf"\b(?:sebi|sfio|cbi|enforcement director(?:ate)?|\bed)\b.{{0,60}}{_NEG_B}", re.IGNORECASE),
    re.compile(rf"{_NEG_B}.{{0,60}}\b(?:sebi|sfio|cbi|enforcement director(?:ate)?)\b", re.IGNORECASE),
    re.compile(rf"\bpromoter\w*\b.{{0,60}}{_NEG_PLEDGE_B}", re.IGNORECASE),
    re.compile(rf"\bpledge\w*\b.{{0,60}}{_NEG_PLEDGE_B}", re.IGNORECASE),
    re.compile(rf"{_NEG_PLEDGE_B}.{{0,60}}\bpledge\w*\b", re.IGNORECASE),
]


def tier1_match(title: str) -> bool:
    """True when the headline trips the deterministic governance screen."""
    return any(p.search(title) for p in TIER1_STANDALONE + TIER1_CONTEXTUAL)


def classify_symbols_batch(
    items: List[tuple],
) -> Dict[str, ClassificationResult]:
    """Classify many symbols with one FinBERT pass and chunked LLM calls.

    This is the daily-scale replacement for looping :func:`classify_red_flags`.
    The per-symbol LLM round trip was the entire runtime cost of the pipeline
    (~454s per symbol when a provider was unhealthy, ~13.5h for a full
    watchlist). Here the work is partitioned:

    1. **Tier 1 regex** per symbol — free, and unchanged, so no veto that the
       old per-symbol path produced is lost.
    2. **Content-addressed cache** lookup per symbol — unchanged headlines are
       free.
    3. **FinBERT** scores every surviving headline in ONE batched pass
       (~175 headlines/s), then:
         * negative at/over threshold -> escalate to the LLM
         * clean/weak               -> resolve locally, no LLM call at all
       This is an OR-gate: it can only add escalations, never remove a veto.
    4. **Tier 2 LLM** on the escalated symbols only, chunked
       ``SENTIMENT_TIER2_BATCH_SIZE`` per request, one JSON array back.

    Args:
        items: ``(symbol, headlines)`` pairs.

    Returns:
        ``{symbol: ClassificationResult}`` covering every input symbol.
    """
    results: Dict[str, ClassificationResult] = {}
    pending_llm: List[tuple] = []

    # ── Stage 1 + 2: regex veto and cache, both cheap ───────────────────────
    for symbol, headlines in items:
        if not headlines:
            # NOT "CLEAN". With no RSS fallback a symbol can simply have no
            # coverage at all, and recording that as clean would report an
            # unscreened name as a passed one.
            results[symbol] = ClassificationResult(
                is_red_flag=False, severity="NONE",
                reason="No provider headlines for this symbol; not screened.",
                outcome="SCREENING_FAILED")
            continue

        vetoed = next((h for h in headlines if tier1_match(h.title)), None)
        if vetoed is not None:
            logger.warning(f"[{symbol}] Tier 1 regex veto: {vetoed.title[:70]}")
            results[symbol] = ClassificationResult(
                is_red_flag=True, severity="HIGH",
                reason=f"Tier 1 Hard Regex Match: '{vetoed.title}'",
                source_url=vetoed.url, outcome="RED_FLAG")
            continue

        cached = _tier2_cache_get(_tier2_cache_key(symbol, headlines))
        if cached is not None:
            cached.reason = f"{cached.reason} [cached]"
            results[symbol] = cached
            continue

        pending_llm.append((symbol, headlines))

    if not pending_llm:
        return results

    # ── Stage 3: one batched FinBERT pass over every surviving headline ─────
    flat, index = [], []
    for symbol, headlines in pending_llm:
        for h in headlines[:10]:
            flat.append(h.title)
            index.append(symbol)
    threshold = settings.SENTIMENT_FINBERT_THRESHOLD
    logger.info(
        f"FinBERT gate: scoring {len(flat)} headline(s) for "
        f"{len(pending_llm)} symbol(s) at threshold {threshold}"
    )
    scores = finbert_negative_scores(flat)

    # Keep the single worst-scoring headline per symbol so the clear path can
    # cite a real source_url. Comparing HeadlineSchema objects directly
    # (e.g. min() over (score, headline) tuples) raises on score ties.
    worst: Dict[str, tuple] = {}
    pos = 0
    for symbol, headlines in pending_llm:
        best_score, best_hl = 0.0, headlines[0]
        for h in headlines[:10]:
            score = scores[pos]
            pos += 1
            if score > best_score:
                best_score, best_hl = score, h
        worst[symbol] = (best_score, best_hl)

    escalate = []
    for symbol, headlines in pending_llm:
        score, hl = worst[symbol]
        if score >= threshold:
            escalate.append((symbol, headlines))
        else:
            reason = (f"FinBERT negative={score:.2f} < {threshold}; "
                      f"cleared without LLM escalation.")
            logger.info(f"[{symbol}] {reason}")
            result = ClassificationResult(
                is_red_flag=False, severity="NONE", reason=reason,
                source_url=hl.url, outcome="CLEAN")
            _tier2_cache_put(_tier2_cache_key(symbol, headlines), result)
            results[symbol] = result

    # ── Stage 4: chunked LLM on the escalated symbols only ──────────────────
    size = settings.SENTIMENT_TIER2_BATCH_SIZE
    logger.info(
        f"Escalating {len(escalate)}/{len(pending_llm)} symbol(s) to Tier 2 "
        f"LLM in chunks of {size} ({-(-len(escalate) // size)} request(s))"
    )
    for start in range(0, len(escalate), size):
        chunk = escalate[start:start + size]
        for symbol, result in _tier2_classify_chunk(chunk).items():
            _tier2_cache_put(_tier2_cache_key(symbol, dict(escalate)[symbol]), result)
            results[symbol] = result

    # Anything the chunk path failed to return must still have a verdict.
    for symbol, headlines in pending_llm:
        results.setdefault(symbol, ClassificationResult(
            is_red_flag=False, severity="NONE",
            reason="Tier 2 LLM batch did not return a verdict; treated as unscreened.",
            outcome="SCREENING_FAILED"))
    return results


def _tier2_classify_chunk(chunk: List[tuple]) -> Dict[str, ClassificationResult]:
    """One LLM request covering ``chunk``; returns per-symbol verdicts.

    Every symbol in the chunk gets a verdict even if the model omits it or
    returns malformed JSON — a missing entry must not silently read as clean.
    """
    symbols = [s for s, _ in chunk]
    if "pytest" in sys.modules or not settings.SENTIMENT_TIER2_ENABLED:
        reason = ("Passed Tier 1 + FinBERT gate; Tier 2 disabled."
                  if not settings.SENTIMENT_TIER2_ENABLED
                  else "Passed Tier 1 + FinBERT gate; Tier 2 disabled in tests.")
        return {s: ClassificationResult(
            is_red_flag=False, severity="NONE", reason=reason,
            outcome="SCREENING_FAILED") for s in symbols}

    prompt_lines = []
    for symbol, headlines in chunk:
        titles = "; ".join(f'"{h.title}"' for h in headlines[:10])
        prompt_lines.append(f"{symbol}: {titles}")
    prompt = (
        "You are a strict Indian equity governance officer. For each symbol, "
        "decide whether its recent headlines show severe corporate governance "
        "failure, a regulatory investigation (SEBI/SFIO/CBI/ED), unexplained "
        "auditor resignation, or a promoter pledging spike that threatens "
        "equity holders. Ordinary business news, routine regulator rule "
        "changes, price moves and routine SEBI approvals are NOT red flags.\n\n"
        f"{chr(10).join(prompt_lines)}\n\n"
        "Reply with strict JSON only: an object keyed by symbol, each value "
        '{"is_red_flag": bool, "severity": "HIGH|MEDIUM|LOW|NONE", '
        '"reason": "short justification"}. Include EVERY symbol listed.'
    )

    routes = _tier2_routes()
    for provider_name, api_key, base_url, model in routes:
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=api_key, base_url=base_url,
                timeout=float(settings.SENTIMENT_TIER2_TIMEOUT),
                max_retries=0,
            )
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a JSON-only bot. Output strict JSON."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            data = json.loads(response.choices[0].message.content.strip())
            out: Dict[str, ClassificationResult] = {}
            for symbol in symbols:
                entry = data.get(symbol)
                if not isinstance(entry, dict):
                    out[symbol] = ClassificationResult(
                        is_red_flag=False, severity="NONE",
                        reason=f"Tier 2 {provider_name} omitted {symbol}; unscreened.",
                        outcome="SCREENING_FAILED")
                    continue
                is_flag = bool(entry.get("is_red_flag", False))
                out[symbol] = ClassificationResult(
                    is_red_flag=is_flag,
                    severity=str(entry.get("severity", "NONE")).upper(),
                    reason=f"Tier 2 {provider_name}: {entry.get('reason', '')}"[:500],
                    outcome="RED_FLAG" if is_flag else "CLEAN")
            return out
        except Exception as exc:
            logger.warning(
                f"Tier 2 {provider_name} failed for chunk of {len(chunk)}: {exc}; "
                f"trying next provider."
            )
    return {s: ClassificationResult(
        is_red_flag=False, severity="NONE",
        reason="All Tier 2 providers failed for this chunk.",
        outcome="SCREENING_FAILED") for s in symbols}


def _tier2_routes() -> List[tuple]:
    """Candidate (name, key, base_url, model) routes for the Tier 2 LLM."""
    routes = []
    if settings.XAI_API_KEY is not None:
        routes.append(("xai", settings.XAI_API_KEY.get_secret_value(),
                       "https://api.x.ai/v1", "grok-beta"))
    if settings.OPENROUTER_API_KEY is not None:
        for model in ("nvidia/nemotron-3-ultra-550b-a55b:free",
                      "nvidia/nemotron-3.5-lightning:free",
                      "poolside/laguna-s-2.1:free"):
            routes.append(("openrouter",
                           settings.OPENROUTER_API_KEY.get_secret_value(),
                           "https://openrouter.ai/api/v1", model))
    if settings.GROQ_API_KEY is not None and settings.GROQ_API_KEY.get_secret_value():
        routes.append(("groq", settings.GROQ_API_KEY.get_secret_value(),
                       "https://api.groq.com/openai/v1",
                       settings.LLM_FALLBACK_MODEL))
    return routes


def classify_red_flags(
    symbol: str, headlines: List[HeadlineSchema]
) -> ClassificationResult:
    """Executes Two-Tier governance vetting: regex screening followed by LLM contextual inference.

    Args:
        symbol: Stock ticker for logging context
        headlines: List of recent headlines to classify

    Returns:
        ClassificationResult with verdict, severity, and reasoning.
    """
    if not headlines:
        return ClassificationResult(
            is_red_flag=False,
            severity="NONE",
            reason="No recent news found.",
            outcome="CLEAN",
        )

    # --- Tier 1: Deterministic hard-regex governance screen ---
    for hl in headlines:
        matched = tier1_match(hl.title)
        if matched:
            reason_msg = f"Tier 1 Hard Regex Match: '{hl.title}'"
            logger.warning(f"[{symbol}] {reason_msg}")
            return ClassificationResult(
                is_red_flag=True,
                severity="HIGH",
                reason=reason_msg,
                source_url=hl.url,
                outcome="RED_FLAG",
            )

    # --- Tier 2: LLM Contextual Pass (OpenRouter / Groq / xAI) ---
    # ── Content-addressed verdict cache ──────────────────────────────────────
    # Checked before the provider guards below because it is a pure local
    # read: Tier 2 is by far the most expensive call in the pipeline (minutes
    # per symbol when a provider is unhealthy). The 30-day news lookback means
    # the SAME headlines recur on every run until new ones arrive, so the
    # verdict for an unchanged headline set cannot change. Keying on the exact
    # titles makes this safe: any new headline yields a new key and a fresh call.
    cache_key = _tier2_cache_key(symbol, headlines)
    cached = _tier2_cache_get(cache_key)
    if cached is not None:
        cached.reason = f"{cached.reason} [cached]"
        logger.info(f"[{symbol}] Tier 2 cache hit {cache_key[:12]}")
        return cached

    # Keep unit tests deterministic and offline. In normal runtime, fall
    # through to the configured OpenRouter/Groq providers.
    if "pytest" in sys.modules:
        return ClassificationResult(
            is_red_flag=False,
            severity="NONE",
            reason="Passed Tier 1 regex screening; Tier 2 disabled in tests.",
            outcome="SCREENING_FAILED",
        )

    if not settings.SENTIMENT_TIER2_ENABLED:
        logger.info(f"[{symbol}] Tier 2 disabled by SENTIMENT_TIER2_ENABLED; "
                    "Tier 1 regex screening only.")
        return ClassificationResult(
            is_red_flag=False,
            severity="NONE",
            reason="Passed Tier 1 regex screening; Tier 2 disabled by config.",
            outcome="SCREENING_FAILED",
        )

    candidate_routes = _tier2_routes()

    if not candidate_routes:
        logger.debug(f"[{symbol}] No LLM provider key available; skipping Tier 2 pass.")
        return ClassificationResult(
            is_red_flag=False,
            severity="NONE",
            reason="Passed Tier 1 regex screening; Tier 2 disabled.",
            outcome="SCREENING_FAILED",
        )

    titles_bulleted = "\n".join(
        [f"- {hl.title} (URL: {hl.url})" for hl in headlines[:10]]
    )
    prompt = (
        f"You are a strict Indian equity risk and corporate governance "
        f"officer. Analyze the following news headlines for {symbol}:\n"
        f"{titles_bulleted}\n\n"
        f"Evaluate if there is any severe corporate governance failure, "
        f"regulatory investigation (SEBI, SFIO, CBI, ED), unexplained "
        f"auditor resignation, or sudden promoter share pledging spike "
        f"that threatens equity holders. Normal business headwinds or "
        f"earnings misses are NOT red flags.\n\n"
        f"Respond ONLY with a valid JSON object matching this schema:\n"
        f'{{\n'
        f'  "is_red_flag": boolean,\n'
        f'  "severity": "HIGH" | "MEDIUM" | "LOW" | "NONE",\n'
        f'  "reason": "short explanation of the governance risk, '
        f"or 'Clean' if none\"\n"
        f"}}"
    )

    from openai import OpenAI

    for provider_name, api_key, base_url, model in candidate_routes:
        try:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                # Without these the SDK defaults to a 600s timeout with 2
                # retries PER PROVIDER — up to ~48 minutes for one symbol
                # across the four candidate routes before failover.
                timeout=float(settings.SENTIMENT_TIER2_TIMEOUT),
                max_retries=0,
            )
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a JSON-only bot. Output strict JSON."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            raw_json = response.choices[0].message.content.strip()
            data = json.loads(raw_json)
            result = ClassificationResult(
                is_red_flag=data.get("is_red_flag", False),
                severity=data.get("severity", "NONE"),
                reason=f"Tier 2 {provider_name}: {data.get('reason', 'Clean')}",
                source_url=(headlines[0].url if data.get("is_red_flag") else None),
                outcome="RED_FLAG" if data.get("is_red_flag") else "CLEAN",
            )
            if result.is_red_flag and result.severity in ("HIGH", "MEDIUM"):
                logger.warning(f"[{symbol}] {result.reason}")
            _tier2_cache_put(cache_key, result)
            return result
        except Exception as e:
            logger.warning(f"[{symbol}] Tier 2 {provider_name} failed: {e}; trying next provider.")

    logger.error(f"[{symbol}] All Tier 2 providers failed.")
    return ClassificationResult(
        is_red_flag=False,
        severity="NONE",
        reason="Passed Tier 1 regex screening; Tier 2 error.",
        outcome="SCREENING_FAILED",
    )
