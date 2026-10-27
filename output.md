# VYUHA Run Output

## Summary
- **Pipeline final decision:** `HOLD_CASH`
- **Cash:** ₹100,000.00
- **Rationale:** No technical entry signals generated today.
- **Trading mode:** Paper only (`LIVE_TRADING_ENABLED=false`)

---

## 1. Universe / Fundamentals
- Fundamental scraper run: `scripts/fetch_real_fundamentals.py`
- Source: Screener.in annual historical tables
- Snapshots saved: `696` rows (plus CSV header)
- Output file: `data/raw/fundamentals.csv`
- Symbols scraped: 30 large-cap names including RELIANCE, HDFCBANK, INFY, ICICIBANK, HINDUNILVR, ITC, SBIN, BHARTIARTL, KOTAKBANK, LT, AXISBANK, ASIANPAINT, MARUTI, TITAN, SUNPHARMA, BAJFINANCE, WIPRO, HCLTECH, ULTRACEMCO, NESTLEIND, TATAMOTORS (failed), POWERGRID, NTPC, TATASTEEL, TECHM, DRREDDY, CIPLA, COALINDIA, ADANIENT, etc.
- Sample row (RELIANCE):
  - Quarter-end: 2021-03-31
  - Filing date: 2021-05-15
  - ROCE TTM: 8.0
  - EPS growth YoY: 13.23%
  - D/E: 0.4
  - Promoter pledge: 0.0%
  - Revenue growth YoY: 12.0%
  - Margin expansion: 2.0

### Fundamental watchlist output
- `Watchlist` table after run: **8 rows**
- ACTIVE watchlist candidates (sorted by conviction):
  1. `IPCALAB` — conviction 74.8
  2. `COFORGE` — conviction 71.7
  3. `IDEA` — conviction 67.1
  4. `NAM-INDIA` — conviction 64.8
  5. `FEDERALBNK` — conviction 63.0
  6. `IDFCFIRSTB` — conviction 60.0
  7. `AUROPHARMA` — conviction 59.6
  8. `AIIL` — conviction 54.5
- `FundamentalSnapshot` table after run: **30 rows**
- The crew’s LLM layer did not perform sentiment/technical scans successfully because OpenRouter free daily quota was exhausted, but the direct deterministic scans above show the real fundamental watchlist survivors.

---

## 2. News / Governance Screening
- `SentimentFlag` table after run: **0 rows**
- FinBERT screening is currently unavailable because `transformers` is not installed.
- Tier 2 (Grok/xAI) is disabled because `XAI_API_KEY` is not configured.
- Current behavior is now explicit failure state: `SCREENING_FAILED` / `UNKNOWN`, rather than falsely reporting a clean result.

---

## 3. Technical Scan
- `TechnicalSignal` table after run: **0 rows**
- Technical patterns considered: W-bottom, Bollinger-band squeeze
- Direct technical scan result: **RED market regime** (both indices below 200 DMA), so **all new setups were blocked**
- Therefore no technical entry signals were generated even for the 8 active watchlist candidates.

---

## 4. Risk / Exits
- Monthly drawdown breaker: evaluated during pipeline
- Portfolio heat, cash, max positions, and trailing stops: evaluated against empty book
- New exposure blocked? Effectively yes: no valid buy signals and empty watchlist led to no allocation.
- Current book: 0 open positions

---

## 5. Portfolio Manager Decision
- Final action: `HOLD_CASH`
- Cash allocated: ₹0.00
- Reason: no eligible technical entry signals today

---

## 6. Scrapers / Data Sources
- Real fundamental scraper: `scripts/fetch_real_fundamentals.py` (Screener.in)
- Output: `data/raw/fundamentals.csv` (696 rows)
- Failed scrape symbol during run: `TATAMOTORS`
- No technical/sentiment scraper output was persisted for this run.
