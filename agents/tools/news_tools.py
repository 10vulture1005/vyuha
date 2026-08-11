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
        ..., description="'HIGH', 'MEDIUM', 'LOW', or 'NONE'"
    )
    reason: str = Field(
        ..., description="Concise explanation of the red flag."
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
    """Scrapes Google News RSS for company-specific mentions over the trailing lookback window.

    Args:
        symbol: Stock ticker symbol (e.g., "RELIANCE")
        company_name: Full company name for broader search coverage
        lookback_days: Number of days to look back for news (default 30)

    Returns:
        List of validated HeadlineSchema objects within the lookback window.
    """
    query = f"{company_name} OR {symbol} NSE BSE"
    encoded_query = urllib.parse.quote(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded_query}"
        f"&hl=en-IN&gl=IN&ceid=IN:en"
    )

    from agents.tools.scraping_tools import ResilientScraper
    scraper = ResilientScraper(use_adaptive=False)  # RSS is standard XML
    adaptor = scraper.fetch_page(rss_url)
    if not adaptor:
        return []

    headlines = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    try:
        # Standard RSS item extraction
        items = adaptor.css("item")
        for item in items[:15]:  # Process top 15 most recent articles
            title = item.css("title").text()
            link = item.css("link").text()
            pub_date_str = item.css("pubDate").text()

            try:
                # Example: Tue, 10 Jun 2026 14:00:00 GMT
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
        )

    # --- Tier 1: FinBERT NLP Pass ---
    try:
        finbert = get_finbert()
        for hl in headlines:
            result = finbert(hl.title)[0]
            if result["label"] == "negative" and result["score"] > 0.85:
                reason_msg = (
                    f"Tier 1 FinBERT Match [Confidence: {result['score']:.2f}]: "
                    f"'{hl.title}'"
                )
                logger.warning(f"[{symbol}] {reason_msg}")
                return ClassificationResult(
                    is_red_flag=True,
                    severity="HIGH",
                    reason=reason_msg,
                    source_url=hl.url,
                )
    except Exception as e:
        logger.error(f"[{symbol}] FinBERT inference failed: {e}")

    # --- Tier 2: LLM Contextual Pass (Grok / xAI) ---
    if not settings.XAI_API_KEY:
        logger.debug(
            f"[{symbol}] No xAI API key; skipping Tier 2 Grok pass."
        )
        return ClassificationResult(
            is_red_flag=False,
            severity="NONE",
            reason="Passed Tier 1; Tier 2 disabled.",
        )

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.XAI_API_KEY.get_secret_value(),
            base_url="https://api.x.ai/v1",
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

        response = client.chat.completions.create(
            model="grok-beta",
            messages=[
                {"role": "system", "content": "You are a JSON-only bot. Output strict JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )

        raw_json = response.choices[0].message.content.strip()
        data = json.loads(raw_json)
        result = ClassificationResult(
            is_red_flag=data.get("is_red_flag", False),
            severity=data.get("severity", "NONE"),
            reason=f"Tier 2 Grok: {data.get('reason', 'Clean')}",
            source_url=(
                headlines[0].url if data.get("is_red_flag") else None
            ),
        )
        if result.is_red_flag and result.severity in ("HIGH", "MEDIUM"):
            logger.warning(f"[{symbol}] {result.reason}")
        return result
    except Exception as e:
        logger.error(
            f"[{symbol}] Tier 2 Grok classification failed: {e}. "
            f"Defaulting to safe clean verdict."
        )
        return ClassificationResult(
            is_red_flag=False,
            severity="NONE",
            reason="Passed Tier 1; Tier 2 error.",
        )
