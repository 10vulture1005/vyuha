"""Tests for the batched Drishti news provider."""
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from agents.tools import news_tools as nt  # noqa: E402


def _row(symbol="RELIANCE", title="Some headline", link="https://x/1", date=None):
    return {
        "title": title,
        "symbol": symbol,
        "link": link,
        "source": "Test",
        "date": date or datetime.now(timezone.utc).isoformat(),
    }


class FakeClient:
    """Stands in for DrishtiClient; records the symbol batches it was asked for."""

    calls = []
    pages = {}

    def __init__(self, **kw):
        pass

    def get_news(self, symbols, limit=None, page=None):
        FakeClient.calls.append(list(symbols))
        rows = []
        for i, s in enumerate(symbols):
            for j in range(2):
                rows.append(_row(symbol=f"{s}:PEER", link=f"https://x/{s}/{page}/{j}"))
        return {"data": rows}


@pytest.fixture
def fake_sdk(monkeypatch):
    FakeClient.calls = []
    module = types.ModuleType("drishti_sdk")
    module.DrishtiClient = FakeClient
    monkeypatch.setitem(sys.modules, "drishti_sdk", module)
    monkeypatch.setattr(nt.settings, "DRISHTI_API_KEY", types.SimpleNamespace(
        get_secret_value=lambda: "test-key"))
    monkeypatch.setattr(nt.settings, "NEWS_PROVIDER", "auto")
    monkeypatch.setattr(nt.settings, "DRISHTI_BATCH_SIZE", 20)
    monkeypatch.setattr(nt.settings, "DRISHTI_MAX_PAGES", 1)
    monkeypatch.setattr(nt.settings, "DRISHTI_MIN_INTERVAL", 0.0)
    return FakeClient


def test_one_request_covers_the_whole_watchlist(fake_sdk):
    syms = [f"SYM{i}" for i in range(45)]
    res = nt.fetch_drishti_batch(syms, lookback_days=30)
    assert len(res) == 45, "every symbol should be attributed"
    # 45 symbols at 20/request = 3 requests, not 45.
    assert len(fake_sdk.calls) == 3
    assert max(len(c) for c in fake_sdk.calls) <= 20


def test_multi_symbol_article_is_attributed_to_each(fake_sdk):
    res = nt.fetch_drishti_batch(["AAA", "BBB"], lookback_days=30)
    assert set(res) == {"AAA", "BBB"}
    assert len(res["AAA"]) == 2 and len(res["BBB"]) == 2


def test_lookback_filters_old_articles(monkeypatch, fake_sdk):
    old = datetime.now(timezone.utc) - timedelta(days=60)

    class OldClient:
        def __init__(self, **kw):
            pass

        def get_news(self, symbols, limit=None, page=None):
            return {"data": [_row(symbol=sym, link=f"https://x/{sym}",
                                  date=old.isoformat()) for sym in symbols]}

    sys.modules["drishti_sdk"].DrishtiClient = OldClient
    res = nt.fetch_drishti_batch(["AAA"], lookback_days=30)
    assert "AAA" not in res, "articles older than the lookback must be dropped"


def test_recent_articles_survive_lookback(monkeypatch, fake_sdk):
    fresh = datetime.now(timezone.utc) - timedelta(days=2)

    class FreshClient:
        def __init__(self, **kw):
            pass

        def get_news(self, symbols, limit=None, page=None):
            return {"data": [_row(symbol=sym, link=f"https://x/{sym}",
                                  date=fresh.isoformat()) for sym in symbols]}

    sys.modules["drishti_sdk"].DrishtiClient = FreshClient
    res = nt.fetch_drishti_batch(["AAA"], lookback_days=30)
    assert "AAA" in res and len(res["AAA"]) == 1


def test_disabled_provider_returns_nothing(monkeypatch, fake_sdk):
    monkeypatch.setattr(nt.settings, "NEWS_PROVIDER", "marketaux")
    assert nt.fetch_drishti_batch(["AAA"]) == {}, \
        "'marketaux' must reserve the Drishti budget even with a key set"


def test_missing_key_returns_nothing(monkeypatch, fake_sdk):
    monkeypatch.setattr(nt.settings, "DRISHTI_API_KEY", None)
    assert nt.fetch_drishti_batch(["AAA"]) == {}


def test_api_failure_degrades_to_empty_not_crash(monkeypatch, fake_sdk):
    """A provider outage must not take the sentiment stage down."""
    class Boom:
        def __init__(self, **kw):
            pass

        def get_news(self, **kw):
            raise RuntimeError("provider down")

    sys.modules["drishti_sdk"].DrishtiClient = Boom
    assert nt.fetch_drishti_batch(["AAA", "BBB"]) == {}


def test_marketaux_quota_exhaustion_returns_empty(monkeypatch):
    """HTTP 402 (daily limit) must stop cleanly, not raise.

    Regression: the paginated loop broke out of the page loop on 402 while
    the article-processing block sat at the outer indent level, so it read an
    unassigned `articles` and raised UnboundLocalError — which crashed the
    whole sentiment stage.
    """
    import requests as _rq

    def _quota_exhausted(*a, **k):
        resp = _rq.Response()
        resp.status_code = 402
        resp._content = b'{"error":{"code":"usage_limit_reached"}}'
        return resp

    monkeypatch.setattr(_rq, "get", _quota_exhausted)
    monkeypatch.setattr(nt.settings, "MARKETAUX_API_KEY",
                        types.SimpleNamespace(get_secret_value=lambda: "k"))
    monkeypatch.setattr(nt.settings, "NEWS_PROVIDER", "marketaux")
    monkeypatch.setattr(nt.settings, "MARKET_MAX_PAGES", 8)

    assert nt.fetch_marketaux_batch(["AAA", "BBB"]) == {}


@pytest.mark.parametrize("raw,parseable", [
    ("2026-10-16T10:01:49+00:00", True),
    ("2026-10-16T10:01:49Z", True),
    ("2026-10-16", True),
    ("Fri, 16 Oct 2026 10:01:49 +0000", True),
    ("garbage", False),
    ("", False),
    (None, False),
])
def test_date_parsing_always_returns_tz_aware(raw, parseable):
    """Naive inputs are normalised to UTC — mixing the two breaks arithmetic."""
    got = nt._parse_news_date(raw)
    if not parseable:
        assert got is None
    else:
        assert got is not None and got.tzinfo is not None