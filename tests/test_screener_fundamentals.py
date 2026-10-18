from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from data.screener_client import HostLimiter, ScreenerClient, parse_page
from data.screener_fundamentals import normalize, merge_fundamentals
from investor.data import PointInTimeFundamentals
from agents.tools.fundamental_tools import snapshot_from_records

HTML = (Path(__file__).parent / 'fixtures/screener_minimal.html').read_text()


def test_normalize_no_forward_fill_or_current_ratios(tmp_path):
    page = parse_page(HTML, 'TEST')
    assert page['basis'] == 'consolidated'
    assert page['top_ratios']['ROCE'] == '99%'
    rows = normalize(page)
    assert {r['period_end'] for r in rows} == {'2020-03-31', '2023-03-31'}
    assert all('roce' not in r and 'roe' not in r for r in rows)
    annual = [r for r in rows if r['frequency'] == 'yearly']
    assert annual[-1]['revenue'] == 200e7
    assert annual[-1]['equity'] == 100e7
    assert 'promoter_pledge_pct' not in annual[0]
    path = tmp_path / 'fund.csv'
    pd.DataFrame(rows).to_csv(path, index=False)
    store = PointInTimeFundamentals(path)
    assert store.as_of('TEST', '2020-05-29') is None
    assert store.as_of('TEST', '2020-05-30') is not None
    assert len(store.annual('TEST', '2022-12-31')) == 1
    snap = snapshot_from_records('TEST', rows, '2023-06-01')
    assert snap.roe == 20  # PAT / equity, NOT page ROCE
    assert snap.eps_growth_3y == pytest.approx(100 * (2 ** (1 / 3) - 1))


def test_merge_yfinance_wins_gaps_only():
    rows = normalize(parse_page(HTML, 'TEST'))
    yf = dict(rows[0], revenue=123, data_source='yfinance')
    yf.pop('equity')
    merged = merge_fundamentals(pd.DataFrame([yf]), pd.DataFrame(rows))
    row = merged.iloc[0]
    assert row.revenue == 123
    assert row.equity == 50e7
    assert row.data_source == 'yfinance+screener'
    assert len(merged) == len(rows)


def test_limiter_and_retry_after_cache(tmp_path):
    now = [0.0]
    def sleep(seconds):
        now[0] += seconds
    limiter = HostLimiter(clock=lambda: now[0], sleep=sleep, jitter=lambda: 0)
    session = Mock()
    def response(status, text='', headers=None):
        result = Mock(status_code=status, text=text, headers=headers or {})
        if status >= 400:
            result.raise_for_status.side_effect = requests.HTTPError(str(status))
        return result
    session.get.side_effect = [response(200, 'User-agent: *\nAllow: /company/'),
                               response(429, headers={'Retry-After': '20'}), response(200, HTML)]
    client = ScreenerClient(tmp_path, session, limiter)
    assert client.fetch('TEST')['symbol'] == 'TEST'
    assert now[0] >= 22.5
    assert len(list(tmp_path.rglob('*.html'))) == 1
    client.fetch('TEST')
    assert session.get.call_count == 3


def test_robots_deny(tmp_path):
    session = Mock()
    session.get.return_value = Mock(status_code=200, text='User-agent: *\nDisallow: /', headers={})
    client = ScreenerClient(tmp_path, session, Mock())
    with pytest.raises(PermissionError):
        client.fetch('TEST')
    assert session.get.call_count == 1


@pytest.mark.parametrize('poisoned_store', [False, True])
def test_no_hash_data_can_reach_db(monkeypatch, tmp_path, poisoned_store):
    import agents.fundamental_agent as agent
    import importlib
    settings_module = importlib.import_module('config.settings')
    from data.fundamentals_provider import FundamentalsProvider
    generator = Mock(side_effect=AssertionError('Synthetic provider must never be called'))
    monkeypatch.setattr(FundamentalsProvider, '_generate_stylized_metrics', generator)
    monkeypatch.setattr(settings_module, 'BASE_DIR', tmp_path)
    if poisoned_store:
        path = tmp_path / 'data/raw/fundamentals_investor.csv'
        path.parent.mkdir(parents=True)
        rows = normalize(parse_page(HTML, 'TEST'))
        for row in rows:
            row['data_source'] = 'stylized'
        pd.DataFrame(rows).to_csv(path, index=False)
    monkeypatch.setattr(agent, 'PointInTimeUniverse', lambda: Mock(get_midcap_universe=lambda *a, **kw: ['TEST']))
    session = Mock()
    @contextmanager
    def get_session():
        yield session
    monkeypatch.setattr(agent, 'get_session', get_session)
    assert agent.FundamentalAgent().generate_watchlist_execution() == []
    session.add.assert_not_called()
    session.query.assert_not_called()
    generator.assert_not_called()


def test_unverified_records_rejected():
    rows = normalize(parse_page(HTML, 'TEST'))
    for row in rows:
        row['data_source'] = 'stylized'
    with pytest.raises(ValueError, match='no verified'):
        snapshot_from_records('TEST', rows)


def test_parser_failure_keeps_raw_html(tmp_path):
    session = Mock()
    session.get.side_effect = [
        Mock(status_code=200, text='User-agent: *\nAllow: /', headers={}),
        Mock(status_code=200, text='<html>unrecognized markup</html>', headers={}),
    ]
    client = ScreenerClient(tmp_path, session, Mock())
    with pytest.raises(ValueError):
        client.fetch('TEST')
    assert len(list(tmp_path.rglob('*.html'))) == 1
    with pytest.raises(ValueError):
        client.fetch('TEST')
    assert session.get.call_count == 2


def test_generic_scraper_never_escalates_429(monkeypatch):
    import agents.tools.scraping_tools as scraping
    fetcher, browser, selector = Mock(), Mock(), Mock()
    fetcher.return_value.get.return_value = Mock(status_code=429, headers={'Retry-After': '10'})
    monkeypatch.setattr(scraping, '_get_scrapling', lambda: (fetcher, browser, selector))
    monkeypatch.setattr(scraping, '_rate_limit', lambda: None)
    sleep = Mock()
    monkeypatch.setattr(scraping.time, 'sleep', sleep)
    assert scraping.ResilientScraper().fetch_page('https://example.com') is None
    browser.assert_not_called()
    assert fetcher.return_value.get.call_count == 5
    assert sleep.call_args_list[0].args[0] == 10
