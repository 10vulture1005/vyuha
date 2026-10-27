# Daily order-block strategy for Nifty-stock investing (research specification)

> **Scope:** A long-only trading strategy for individual NSE-listed stocks, benchmarked against the Nifty 50. It is **not** a strategy for buying the Nifty index, an index fund, or Nifty futures. The illustrative test used 209 locally available equities, **not** a verified historical Nifty 50/200/500 constituent universe. This strategy is research, not investment advice or a live-ready system.

## Idea

A bullish order block is the last bearish daily candle before a short, strong bullish move. The strategy marks that candle's entire high–low range as a potential demand zone. If the price later pulls back into the zone, it attempts a long entry, with a stop at the original bearish candle's low. The hypothesis is that a sharp bullish displacement followed by a pullback may precede a continuation; the backtest alone does not establish that this mechanism is real.

## Signal rules (daily bars)

1. Use adjusted daily open, high, low and close; retain reported volume separately. Compute a 14-bar Wilder ATR, initialized with the mean of the first 14 true ranges. The first true range is high minus low.
2. A confirmation candle must be bullish (`close > open`), have a body at least **1 × its confirmation-day ATR**, and close within the top **25% of its high–low range**.
3. Search backward up to **five bars** from that confirmation candle for the **last bearish candle immediately preceding a consecutive run of bullish candles**. A doji breaks that run. Each bearish origin candle can create at most one block.
4. The order-block zone is the origin candle's **full low–high range**. The block is known only at the confirmation close; it cannot be traded on that confirmation bar.
5. Starting with the next daily bar, the **first touch** of the zone (`low ≤ zone high`) creates a limit-entry candidate at the zone high; if the session opens below that price, the candidate entry is the open. A first touch is used only once, regardless of whether portfolio sizing accepts the trade. A block is eligible for up to **60 subsequent symbol bars**. Relative-volume filtering is **off** in the tested default configuration.
6. The original candle low is the initial stop. Reject an entry priced at or below its stop. This is the **A entry**, not the alternative midpoint or confirmation entries supported by the signal detector.

A signal is **not** a fill: available cash, sizing and execution rules decide whether a candidate becomes a position. The detector is implemented in `/home/vulture/codes/nse-orderblock/src/nse_ob/detector.py`.

## Illustrative portfolio and exit rules

The separate exploratory simulation at `/home/vulture/codes/nse-orderblock/scripts/illustrative_backtest.py` assumes:

- Initial shared fund capital: **₹1 crore**, no deposits or withdrawals, long-only whole shares and no leverage.
- Maximum position size: **10% of pre-entry fund equity**; initial price-to-stop risk: **1% of pre-entry equity**; no more than **10 concurrent stocks**. Candidates on the same date are processed alphabetically; one entry per symbol per day. These are simulation choices, not universal detector rules.
- For an existing position, if the daily low reaches its stop, sell at the stop or, on a gap below it, at the lower opening price. If an entry and stop occur in the same daily candle, assume the stop is hit even though intraday ordering is unknown (conservative ambiguity rule).
- Otherwise sell at the close once **20 trading bars for that stock** have elapsed since entry. Sell any open positions at the end of the test. No trailing stops, benchmark regime filters, or rebalancing rules were used.
- Apply an **assumed 0.10% transaction cost on each side**. This has not been calibrated to actual brokerage, Indian transaction taxes, bid–ask spread, slippage or market impact. Adjusted prices and historical volume do not by themselves establish executable fills.

## Illustrative historical result — not a fund track record

The simulation used the local Vyuha OHLCV files from **2 January 2017 to 11 September 2026**. Results depend materially on the rules and data above.

| Measure | Approximate value |
|---|---:|
| Fund XIRR / CAGR | **19.69%** |
| ₹1 crore ending value | ₹5.71 crore |
| Total fund return | 470.60% |
| Maximum daily-equity drawdown | −36.02% |
| Completed trades | 2,676 |
| Winning trades | 26.94% |
| Average / median net trade return | 0.78% / −2.11% |
| Profit factor | 1.30 |
| Average holding time | 8.6 stock-trading bars |
| Nifty 50 **price-index** CAGR over same dates | 11.46% |

Because there are no intermediate investor contributions or withdrawals, **fund XIRR equals fund CAGR**. Computing one XIRR by pooling overlapping individual trade cash flows would not represent an investor's fund return. The benchmark is a *price* index, not a dividend-inclusive total-return index, so the comparison is imperfect. Daily equity, trades and full assumptions are recorded in `/home/vulture/codes/nse-orderblock/reports/illustrative_equity.csv`, `illustrative_trades.csv` and `illustrative_backtest.md`.

## Before using this for a Nifty investment mandate

- Obtain **effective-dated historical Nifty constituent lists** (choose Nifty 50, 200 or 500 explicitly), including delisted names and listing dates. The available 209-stock convenience sample is not such a universe; using today's survivors historically biases results.
- Validate an official NSE session calendar, individual missing sessions, corporate actions/adjusted OHLCV, zero-volume bars and extreme returns. The source files have not passed those economic checks.
- Re-run with historically correct membership, realistically modeled costs and liquidity, different market regimes and out-of-sample dates; compare against a **dividend-inclusive** benchmark. Evaluate sensitivity to stops, limits, holding times and same-bar ambiguity.
- Implement and test a production-grade shared-cash execution engine and consider capacity, concentration, operational and drawdown limits before any live deployment. A historical 19.69% estimate is **not** a forecast or guaranteed return.
