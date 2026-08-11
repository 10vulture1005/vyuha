# agents/tools/__init__.py
"""VYUHA agent tools package.

Exports core tool classes for use by agent orchestration layers.
Imports are explicit to allow selective usage — scraping_tools requires
the full scrapling stack, while fundamental_tools and news_tools
can be used independently for testing.
"""
# Re-export fundamental tools (no heavy dependencies)
from agents.tools.fundamental_tools import (
    FundamentalSnapshotSchema,
    scrape_symbol_fundamentals,
    passes_hard_filters,
    compute_relative_conviction_scores,
)

# Re-export news/sentiment tools (no heavy dependencies beyond anthropic)
from agents.tools.news_tools import (
    HeadlineSchema,
    ClassificationResult,
    fetch_recent_headlines,
    classify_red_flags,
)

from agents.tools.ta_tools import (
    load_ohlc_df,
    detect_w_bottom,
    detect_bb_squeeze,
)

try:
    from agents.tools.ledger_tools import (
        run_capital_allocator,
        run_risk_review,
        run_technical_scan,
        run_fundamental_scan,
        run_sentiment_scan,
    )
except ImportError:
    pass

__all__ = [
    "FundamentalSnapshotSchema",
    "scrape_symbol_fundamentals",
    "passes_hard_filters",
    "compute_relative_conviction_scores",
    "HeadlineSchema",
    "ClassificationResult",
    "fetch_recent_headlines",
    "classify_red_flags",
    "load_ohlc_df",
    "detect_w_bottom",
    "detect_bb_squeeze",
    "run_capital_allocator",
    "run_risk_review",
    "run_technical_scan",
    "run_fundamental_scan",
    "run_sentiment_scan",
]
