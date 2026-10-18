# Screener statements: migration and operation

The legacy `fetch_real_fundamentals.py` entrypoint now delegates to
`fetch_screener_snapshots.py`; it no longer writes invented ratios to
`data/raw/fundamentals.csv`. The watchlist agent reads the investor statement
CSV once per scan, rejects unverified sources and incomplete metrics, and
computes ROE as annual PAT / year-end equity (not ROCE), debt/equity and a
same-definition, exactly three-year EPS CAGR. No synthetic provider is used.

## Before enabling the quarterly job

1. Back up the existing CSV and DB. Previously generated synthetic snapshots
   and watchlist scores are **not automatically deleted** by this change.
   Audit/quarantine them before relying on existing scores. There is no reliable
   provenance column in the old snapshot table to identify them automatically.
2. Rebuild the investor CSV with real-source tags:
   `python scripts/fetch_investor_fundamentals.py`.
   The yfinance JSON cache is reused, but legacy pledge backfill is discarded.
   Untagged CSV rows are not retained by this rebuild. Verified Screener rows
   already in the file are retained. Old caches may lack fiscal-year-end
   quarterly records because the previous fetcher deduplicated across frequency;
   remove those caches if a fresh vendor fetch is required.
3. Replay/fetch a small Screener sample:
   `python scripts/fetch_screener_snapshots.py --symbol PREMIERENE --since 2015-01-01`.
   Add `--refresh` to explicitly fetch fresh HTML. Without it, the latest raw
   cached page is replayed; `--since` filters statement periods, not cache dates.
4. Run `python scripts/fetch_screener_snapshots.py` for symbols with price panels.
   The default universe is the available price-panel symbols, excluding INDEX_*,
   not a hardcoded claim of exactly 500 companies.
5. Install the updated host crontab as appropriate for your deployment. The job
   is January/April/July/October **7**, seven days after the preceding quarter
   closes, not March/June/September/December 7. A failed/partial fetch returns
   nonzero and skips the watchlist update; successfully fetched statements are
   still saved. Do not run simultaneous CSV writers.

## GitHub Actions

`.github/workflows/forward_test.yml` fetches fresh Screener statements on Sunday
at 11:00 UTC (16:30 IST), and on manual workflow dispatch, before the weekly
fundamental/sentiment scan. It uses:

```sh
python scripts/fetch_screener_snapshots.py --symbols-file config/universe_nifty500.csv --refresh
```

The CSV universe avoids relying on ignored local OHLC files on a clean runner.
No Screener credentials or browser installation are needed: the client uses
`requests` and `beautifulsoup4` and respects robots rules and host throttling.
Complete the provenance migration above first if the existing statement CSV
contains untagged rows; the workflow will refuse those rows.

Successful weekly runs commit `data/raw/fundamentals_investor.csv` alongside
the database. Raw HTML is **not** committed. The `screener-ingestion-*` artifact
retains HTML, logs, and the resulting CSV for 30 days, including on failures.
A partial/failed scrape fails the job and skips the weekly scan and commit;
partial records can be recovered from that artifact. Runs are serialized to
avoid simultaneous writers. The weekly job has a 90-minute budget for the
rate-limited universe fetch plus the existing sentiment scan.

## Data contracts

- Raw HTML is saved before parsing at
  `data/cache/screener/<symbol>/<basis>/<UTC-date>.html`. Cache files permit
  offline parser debugging even when parsing fails.
- Consolidated is requested first, standalone on HTTP 404 only. The canonical
  company URL verifies identity and actual basis. Robots rules are checked
  before company requests. 403/challenge pages fail closed in this client;
  there is no proxy or browser bypass.
- The shared process-local one-token host limiter permits no bursts and defaults
  to one request per 2.5 seconds plus jitter. 429 responses defer the host by
  the greater of Retry-After (seconds or HTTP date) and exponential backoff.
  Retries are bounded. Run one ingestion process per host.
- Every HTML table and the top-ratio list are extracted. Only dated raw
  statement fields are normalized. TTM/current ratios are never persisted.
- Crore statement amounts become INR; EPS and percentages are not scaled.
  Equity capital plus reserves gives equity. EBIT is PBT plus interest; the
  displayed operating profit is not blindly treated as EBIT.
- No filing date is invented: the existing `period_end + 60 days` guard applies.
  Missing periods/values stay missing. Governance data is imported only when
  an explicit dated pledge row exists; promoter ownership is **not** pledge,
  and an absent pledge row is **not** zero. Collapsed/AJAX-only pledge details
  are not fetched by this HTML-only implementation.
- Merge key is `(symbol, period_end, frequency)`. yfinance non-null fields win;
  Screener fills same-record gaps. Sources are `yfinance`, `screener` or
  `yfinance+screener`. `basis` records the Screener page basis, not a guarantee
  about the basis of yfinance fields in mixed-source rows. Untagged input CSVs
  are refused by the Screener fetcher rather than silently certified.

## Validation

`python -m pytest tests/test_screener_fundamentals.py tests/test_investor_lookahead.py tests/test_investor_pipeline.py -q`

The checked-in HTML fixture is deliberately small and hand-authored. The real
PREMIERENE 13-table HTML capture was not found in the working tree and still
needs to be added for real-page parser validation. No live universe crawl was
performed as part of implementation.
