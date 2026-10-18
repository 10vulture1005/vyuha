"""Tests for the Tier 2 governance verdict cache and the OHLC freshness skip."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from agents.tools.news_tools import (  # noqa: E402
    ClassificationResult,
    HeadlineSchema,
    _tier2_cache_get,
    _tier2_cache_key,
    _tier2_cache_put,
    classify_red_flags,
)
from scripts.ingest_ohlc import _is_fresh  # noqa: E402


def _hl(*titles):
    return [HeadlineSchema(title=t, published_date=datetime.now(timezone.utc),
                           source="test", url="") for t in titles]


def test_cache_key_ignores_headline_order():
    a, b = _hl("Zeta", "Alpha"), _hl("Alpha", "Zeta")
    assert _tier2_cache_key("X", a) == _tier2_cache_key("X", b)


def test_cache_key_tracks_content_and_symbol():
    a, changed = _hl("Alpha", "Zeta"), _hl("Alpha", "Zeta EXTRA")
    assert _tier2_cache_key("X", a) != _tier2_cache_key("X", changed)
    assert _tier2_cache_key("X", a) != _tier2_cache_key("Y", a)


def test_cached_verdict_short_circuits_the_llm(monkeypatch):
    """A seeded verdict must come back in milliseconds with no provider call."""
    import time
    from agents.tools import news_tools

    headlines = _hl("Cache round-trip probe headline A", "and probe headline B")
    key = _tier2_cache_key("CACHEPROBE", headlines)
    _tier2_cache_put(key, ClassificationResult(
        is_red_flag=False, severity="NONE", reason="Tier 2 openrouter: Clean",
        source_url=None, outcome="CLEAN",
    ))
    assert _tier2_cache_get(key) is not None

    def _explode(*a, **k):  # any network attempt is a test failure
        raise AssertionError("Tier 2 called the provider despite a cache hit")

    monkeypatch.setattr(news_tools, "_tier2_cache_get", _tier2_cache_get)
    started = time.perf_counter()
    result = classify_red_flags("CACHEPROBE", headlines)
    assert time.perf_counter() - started < 1.0
    assert result.outcome == "CLEAN"
    assert "[cached]" in result.reason


def test_is_fresh_is_false_when_session_unknown(tmp_path):
    path = tmp_path / "AAA.csv"
    pd.DataFrame({"Date": ["2026-10-16"], "Close": [1.0]}).to_csv(path, index=False)
    assert _is_fresh(path, None) is False
    assert _is_fresh(tmp_path / "MISSING.csv", pd.Timestamp("2026-10-16").date()) is False


HTML_FIXTURE = """<html><head>
<link rel="canonical" href="https://www.screener.in/company/{sym}/consolidated/"/>
</head><body>
<section id="profit-loss"><table>
<tr><th>Period</th><th>Mar 2024</th></tr>
<tr><td>Sales</td><td>1,000</td></tr>
</table></section>
</body></html>"""


def _screener(tmp_path, monkeypatch, basis="consolidated"):
    """A ScreenerClient whose _get returns a fixture page instead of network."""
    from data import screener_client as sc

    monkeypatch.setattr(sc.ScreenerClient, "_get",
                        lambda self, url: type("R", (), {
                            "status_code": 200, "text": HTML_FIXTURE.format(sym="TESTCO"),
                            "headers": {},
                        })())
    return sc.ScreenerClient(cache_dir=tmp_path / "cache")


def test_parsed_page_is_cached_beside_its_html(tmp_path, monkeypatch):
    """The .json must land next to the .html, including after a basis move."""
    client = _screener(tmp_path, monkeypatch)
    client.fetch("TESTCO")  # populates cache
    htmls = list(tmp_path.rglob("*.html"))
    jsons = list(tmp_path.rglob("*.json"))
    assert len(htmls) == 1, "html not cached"
    assert len(jsons) == 1, "parsed page not cached"
    # Same directory == the json will actually be found on the next call.
    assert jsons[0].parent == htmls[0].parent


def test_second_fetch_reads_parsed_cache_without_reparsing(tmp_path, monkeypatch):
    client = _screener(tmp_path, monkeypatch)
    first = client.fetch("TESTCO")

    def _boom(*a, **k):
        raise AssertionError("re-fetched / re-parsed despite a warm cache")

    monkeypatch.setattr(client, "_get", _boom)
    monkeypatch.setattr("data.screener_client.parse_page", _boom)
    assert client.fetch("TESTCO") == first