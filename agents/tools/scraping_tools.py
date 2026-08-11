# agents/tools/scraping_tools.py
"""Resilient web scraping engine with automatic fetcher escalation.

Wraps scrapling with a Fetcher -> StealthyFetcher (Camoufox) escalation
strategy. On 403/429 or bot-challenge from the fast TLS-fingerprinted
HTTP scrape, the engine transparently escalates to a headless browser.

Adaptive selectors (auto_save=True, adaptive=True) let scrapling
self-heal when target sites alter their HTML structure.

Note: scrapling imports are lazy to allow tests that only need
fundamental_tools or news_tools to run without the full scraping stack.
"""
import time
from pathlib import Path
from typing import Optional, Any

from loguru import logger
from config.settings import BASE_DIR

CACHE_DIR = BASE_DIR / "data" / "cache" / "scrape_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Rate-limiting: minimum seconds between consecutive requests
_REQUEST_DELAY_SEC = 2.0
_last_request_time: float = 0.0


def _rate_limit():
    """Enforce a minimum delay between consecutive HTTP requests."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _REQUEST_DELAY_SEC:
        time.sleep(_REQUEST_DELAY_SEC - elapsed)
    _last_request_time = time.monotonic()


def _get_scrapling():
    """Lazily import scrapling components to avoid hard dependency in test contexts."""
    from scrapling.fetchers import Fetcher, StealthyFetcher
    from scrapling import Adaptor
    return Fetcher, StealthyFetcher, Adaptor


class ResilientScraper:
    """Wraps scrapling with Fetcher->StealthyFetcher escalation and adaptive selector self-healing."""

    def __init__(self, use_adaptive: bool = True):
        self.use_adaptive = use_adaptive
        self.cache_path = str(CACHE_DIR)

    def fetch_page(self, url: str) -> Optional[Any]:
        """Attempts fast TLS-fingerprinted HTTP scrape; escalates to Camoufox on 403/429 or bot challenge.

        Returns an Adaptor object on success, or None on failure.
        """
        Fetcher, StealthyFetcher, Adaptor = _get_scrapling()

        _rate_limit()
        logger.debug(f"Attempting fast scrape via Fetcher for: {url}")
        try:
            fetcher = Fetcher(auto_match=True)
            response = fetcher.get(url, timeout=15)
            if response.status_code == 200:
                return Adaptor(
                    response.text,
                    url=url,
                    auto_save=self.use_adaptive,
                    adaptive=self.use_adaptive,
                    storage_dir=self.cache_path,
                )
            logger.warning(
                f"Fetcher returned status {response.status_code} for {url}. Escalating..."
            )
        except Exception as e:
            logger.warning(
                f"Fetcher exception for {url}: {e}. Escalating to StealthyFetcher..."
            )

        # Escalation to StealthyFetcher (Camoufox browser automation)
        try:
            _rate_limit()
            logger.info(f"Escalating to StealthyFetcher (Camoufox) for: {url}")
            stealth = StealthyFetcher(headless=True, block_images=True)
            response = stealth.fetch(url, timeout=30)
            if response.status == 200:
                return Adaptor(
                    response.html,
                    url=url,
                    auto_save=self.use_adaptive,
                    adaptive=self.use_adaptive,
                    storage_dir=self.cache_path,
                )
            logger.error(
                f"StealthyFetcher failed with status {response.status} for {url}"
            )
        except Exception as e:
            logger.error(f"StealthyFetcher exception for {url}: {e}")

        return None
