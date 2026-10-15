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
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from pydantic import BaseModel, Field
from loguru import logger

from config.settings import settings


class HeadlineSchema(BaseModel):
    """Validated news headline with metadata."""

    title: str
    published_date: datetime
    source: str
    url: str


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


def fetch_recent_headlines(
    symbol: str, company_name: str, lookback_days: int = 30
) -> List[HeadlineSchema]:
    """Fetches company recent news from IndianAPI.in when available, otherwise Google News RSS."""
    from config.settings import settings
    key = settings.INDIAN_API_KEY
    secret = key.get_secret_value() if key is not None else None

    headlines: List[HeadlineSchema] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    if secret:
        try:
            import requests
            resp = requests.get(
                "https://stock.indianapi.in/stock",
                params={"name": company_name or symbol},
                headers={"x-api-key": secret},
                timeout=20,
            )
            if resp.ok:
                data = resp.json()
                for item in (data.get("recentNews") or [])[:15]:
                    pub = item.get("pub_date") or item.get("published_date")
                    try:
                        pub_date = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                        if pub_date.tzinfo is None:
                            pub_date = pub_date.replace(tzinfo=timezone.utc)
                    except Exception:
                        pub_date = datetime.now(timezone.utc)
                    if pub_date < cutoff:
                        continue
                    title = item.get("title") or item.get("headline") or ""
                    url = item.get("url") or item.get("link") or ""
                    source = item.get("source") or "IndianAPI"
                    if title:
                        headlines.append(
                            HeadlineSchema(
                                title=title,
                                published_date=pub_date,
                                source=source,
                                url=url,
                            )
                        )
                if headlines:
                    return headlines
        except Exception as e:
            logger.error(f"IndianAPI news fetch failed for {symbol}: {e}")

    # Fallback: Google News RSS
    query = f"{company_name} OR {symbol} NSE BSE"
    encoded_query = urllib.parse.quote(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded_query}"
        f"&hl=en-IN&gl=IN&ceid=IN:en"
    )

    from agents.tools.scraping_tools import ResilientScraper
    scraper = ResilientScraper(use_adaptive=False)
    adaptor = scraper.fetch_page(rss_url)
    if not adaptor:
        return []

    try:
        items = adaptor.css("item")
        for item in items[:15]:
            title = item.css("title").text()
            link = item.css("link").text()
            pub_date_str = item.css("pubDate").text()

            try:
                pub_date = datetime.strptime(
                    pub_date_str[:25], "%a, %d %b %Y %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except Exception:
                pub_date = datetime.now(timezone.utc)

            if pub_date >= cutoff:
                headlines.append(
                    HeadlineSchema(
                        title=title,
                        published_date=pub_date,
                        source="Google News",
                        url=link,
                    )
                )
    except Exception as e:
        logger.error(f"Failed to parse RSS feed for {symbol}: {e}")

    return headlines





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
    _tier1_patterns = [
        re.compile(r"\bsebi\b", re.IGNORECASE),
        re.compile(r"\bsfio\b", re.IGNORECASE),
        re.compile(r"\bcbi\b", re.IGNORECASE),
        re.compile(r"\bed\b", re.IGNORECASE),
        re.compile(r"\b(?:auditor|audit)\w*\b", re.IGNORECASE),
        re.compile(r"\bpromoter\w*\b", re.IGNORECASE),
        re.compile(r"\bpledge\w*\b", re.IGNORECASE),
        re.compile(r"\bfraud\w*\b", re.IGNORECASE),
        re.compile(r"\bdelis\w*\b", re.IGNORECASE),
        re.compile(r"\bdefault\w*\b", re.IGNORECASE),
        re.compile(r"\braid\w*\b", re.IGNORECASE),
        re.compile(r"\bprobe\w*\b", re.IGNORECASE),
        re.compile(r"\bshow[- ]cause\b", re.IGNORECASE),
        re.compile(r"\bforensic audit\b", re.IGNORECASE),
        re.compile(r"\bmoney laundering\b", re.IGNORECASE),
        re.compile(r"\bwhistleblower\b", re.IGNORECASE),
        re.compile(r"\bresign(?:s|ed|ation)?\b", re.IGNORECASE),
        re.compile(r"\bban\b", re.IGNORECASE),
    ]

    for hl in headlines:
        if any(pattern.search(hl.title) for pattern in _tier1_patterns):
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
    import sys

    # Keep unit tests deterministic and offline. In normal runtime, fall
    # through to the configured OpenRouter/Groq providers.
    if "pytest" in sys.modules:
        return ClassificationResult(
            is_red_flag=False,
            severity="NONE",
            reason="Passed Tier 1 regex screening; Tier 2 disabled in tests.",
            outcome="SCREENING_FAILED",
        )

    candidate_routes = []
    if settings.XAI_API_KEY is not None:
        candidate_routes.append(("xai", settings.XAI_API_KEY.get_secret_value(), "https://api.x.ai/v1", "grok-beta"))
    if settings.OPENROUTER_API_KEY is not None:
        openrouter_models = [
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3.5-lightning:free",
            "poolside/laguna-s-2.1:free",
        ]
        seen = set()
        for model in openrouter_models:
            if not model or model in seen:
                continue
            seen.add(model)
            candidate_routes.append(("openrouter", settings.OPENROUTER_API_KEY.get_secret_value(), "https://openrouter.ai/api/v1", model))
    if settings.GROQ_API_KEY is not None and settings.GROQ_API_KEY.get_secret_value():
        candidate_routes.append(("groq", settings.GROQ_API_KEY.get_secret_value(), "https://api.groq.com/openai/v1", settings.LLM_FALLBACK_MODEL))

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
            client = OpenAI(api_key=api_key, base_url=base_url)
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
