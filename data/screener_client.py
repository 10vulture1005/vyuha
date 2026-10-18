"""Polite Screener transport and lossless table extraction. No browser bypass."""
from __future__ import annotations

import random
import re
import json
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


class HostLimiter:
    """One-token bucket: no bursts, including retries and robots requests."""
    def __init__(self, rate=0.4, clock=time.monotonic, sleep=time.sleep, jitter=None):
        self.interval = 1 / rate
        self.clock, self.sleep = clock, sleep
        self.jitter = jitter or (lambda: random.uniform(0, 0.5))
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            self.sleep(max(0, self.next_at - self.clock()))
            self.next_at = self.clock() + self.interval + self.jitter()

    def defer(self, seconds):
        with self.lock:
            self.next_at = max(self.next_at, self.clock() + seconds)


def retry_after(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


_LIMITER = HostLimiter()


class ScreenerClient:
    HOST = 'https://www.screener.in'
    UA = 'VyuhaFundamentals/1.0 (research statement ingestion; Python requests)'

    def __init__(self, cache_dir=None, session=None, limiter=None, retries=4):
        self.cache_dir = Path(cache_dir or Path(__file__).parent / 'cache' / 'screener')
        self.session = session or requests.Session()
        self.limiter = limiter or _LIMITER
        self.retries = retries
        self.robots = None

    def _get(self, url):
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            response = self.session.get(url, headers={'User-Agent': self.UA}, timeout=30,
                                        allow_redirects=False)
            if response.status_code == 429:
                self.limiter.defer(max(retry_after(response.headers.get('Retry-After')), 5 * 2 ** attempt))
                if attempt < self.retries:
                    continue
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError(f'Unexpected response {response.status_code}: {url}')
            return response
        raise RuntimeError('Retry budget exhausted')

    def fetch(self, symbol, basis='consolidated', refresh=False):
        symbol = symbol.upper()
        if not re.fullmatch(r'[A-Z0-9&_-]+', symbol) or basis not in ('consolidated', 'standalone'):
            raise ValueError('Invalid symbol or basis')
        directory = self.cache_dir / symbol / basis
        cached = sorted(directory.glob('*.html'))
        if cached and not refresh:
            newest = cached[-1]
            # Cache the PARSED page, not just the HTML. parse_page is a full
            # BeautifulSoup walk over a large screener.in document (~2.7s each);
            # replaying 209 cached pages re-did all of that work on every run
            # for a result that could not change. The .json sibling is only
            # trusted when it is at least as new as the .html it came from.
            parsed_path = newest.with_suffix('.json')
            if parsed_path.exists() and parsed_path.stat().st_mtime >= newest.stat().st_mtime:
                try:
                    return json.loads(parsed_path.read_text())
                except Exception:
                    pass  # corrupt or partial; fall through and re-parse
            page = parse_page(newest.read_text(), symbol)
            _write_parsed_cache(parsed_path, page)
            return page
        url = f'{self.HOST}/company/{symbol}/' + ('consolidated/' if basis == 'consolidated' else '')
        if self.robots is None:
            robot = RobotFileParser()
            robot.parse(self._get(f'{self.HOST}/robots.txt').text.splitlines())
            self.robots = robot
        if not self.robots.can_fetch(self.UA, url):
            raise PermissionError(f'robots.txt disallows {url}')
        try:
            response = self._get(url)
        except requests.HTTPError as exc:
            if basis == 'consolidated' and exc.response is not None and exc.response.status_code == 404:
                return self.fetch(symbol, basis='standalone', refresh=refresh)
            raise
        # Persist before parsing: a parser failure must not force another request.
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{datetime.now(timezone.utc).date()}.html'
        temp = path.with_suffix('.tmp')
        temp.write_text(response.text)
        temp.replace(path)
        page = parse_page(response.text, symbol)
        html_path = path
        if page['basis'] != basis:
            actual_dir = self.cache_dir / symbol / page['basis']
            actual_dir.mkdir(parents=True, exist_ok=True)
            html_path = path.replace(actual_dir / path.name)
        # Derive the .json from where the HTML actually ended up. Writing it
        # from the pre-move `path` orphaned the parsed cache in the wrong
        # basis directory, so it was never found again and every run silently
        # refetched over the network.
        _write_parsed_cache(html_path.with_suffix('.json'), page)
        return page


def _write_parsed_cache(path, page):
    """Persist the parsed page so repeat runs skip BeautifulSoup entirely."""
    try:
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(page))
        temp.replace(path)
    except Exception:
        pass  # the parsed cache is an optimisation, never a correctness input


def parse_page(html, symbol):
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"]')
    href = canonical.get('href', '') if canonical else ''
    if f'/company/{symbol}/' not in href:
        raise ValueError('Missing or mismatched canonical company URL (challenge or invalid page)')
    basis = 'consolidated' if '/consolidated/' in href else 'standalone'
    tables = []
    for table in soup.find_all('table'):
        section = table.find_parent('section')
        rows = [[cell.get_text(' ', strip=True) for cell in tr.find_all(['th', 'td'])]
                for tr in table.find_all('tr')]
        tables.append({'section': section.get('id', '') if section else '', 'rows': rows})
    if not any(t['section'] == 'profit-loss' for t in tables):
        raise ValueError('No annual statements in response')
    top = {}
    for item in soup.select('#top-ratios li'):
        name, value = item.select_one('.name'), item.select_one('.value')
        if name and value:
            top[name.get_text(' ', strip=True)] = value.get_text(' ', strip=True)
    return {'symbol': symbol, 'basis': basis, 'tables': tables, 'top_ratios': top}
