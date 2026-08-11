# tests/test_fundamental_agent.py
"""Unit tests for Phase 2 — Fundamental Agent math and filtering logic.

Tests cover:
    - Hard quantitative filter gating (ROE, D/E, EPS thresholds)
    - Cross-sectional Z-score conviction scoring and ranking
    - Edge cases: single-item universe, identical metrics
"""
import pytest
from decimal import Decimal

from agents.tools.fundamental_tools import (
    FundamentalSnapshotSchema,
    passes_hard_filters,
    compute_relative_conviction_scores,
)


class TestHardFilters:
    """Verify non-negotiable quantitative gates from thresholds.yaml."""

    def test_passing_stock(self):
        """Stock with all metrics above thresholds should pass."""
        passer = FundamentalSnapshotSchema(
            symbol="GOOD", roe=16.0, debt_to_equity=0.2, eps_growth_3y=15.0
        )
        assert passes_hard_filters(passer) is True

    def test_failing_roe(self):
        """Stock with ROE below 15% should be rejected."""
        fail_roe = FundamentalSnapshotSchema(
            symbol="BAD_ROE", roe=14.9, debt_to_equity=0.2, eps_growth_3y=15.0
        )
        assert passes_hard_filters(fail_roe) is False

    def test_failing_debt_equity(self):
        """Stock with D/E above 0.5 should be rejected."""
        fail_de = FundamentalSnapshotSchema(
            symbol="BAD_DE", roe=16.0, debt_to_equity=0.51, eps_growth_3y=15.0
        )
        assert passes_hard_filters(fail_de) is False

    def test_failing_eps_growth(self):
        """Stock with EPS 3Y CAGR below 12% should be rejected."""
        fail_eps = FundamentalSnapshotSchema(
            symbol="BAD_EPS", roe=16.0, debt_to_equity=0.2, eps_growth_3y=11.9
        )
        assert passes_hard_filters(fail_eps) is False

    def test_exact_boundary_passes(self):
        """Stock with metrics exactly at threshold boundaries should pass."""
        boundary = FundamentalSnapshotSchema(
            symbol="EDGE", roe=15.0, debt_to_equity=0.50, eps_growth_3y=12.0
        )
        assert passes_hard_filters(boundary) is True

    def test_all_metrics_failing(self):
        """Stock with all metrics below thresholds should be rejected."""
        fail_all = FundamentalSnapshotSchema(
            symbol="WORST", roe=5.0, debt_to_equity=2.0, eps_growth_3y=0.0
        )
        assert passes_hard_filters(fail_all) is False


class TestConvictionScoring:
    """Verify cross-sectional Z-score normalization and percentile ranking."""

    def test_ranking_order(self):
        """Best stock should score highest, worst should score lowest."""
        snapshots = [
            FundamentalSnapshotSchema(
                symbol="BEST", roe=30.0, debt_to_equity=0.05, eps_growth_3y=25.0
            ),
            FundamentalSnapshotSchema(
                symbol="MID", roe=20.0, debt_to_equity=0.20, eps_growth_3y=15.0
            ),
            FundamentalSnapshotSchema(
                symbol="WORST", roe=15.0, debt_to_equity=0.45, eps_growth_3y=12.0
            ),
        ]

        scores = compute_relative_conviction_scores(snapshots)
        assert scores["BEST"] > scores["MID"] > scores["WORST"]

    def test_highest_rank_is_100(self):
        """In a 3-item distribution, the highest rank should be 100.00."""
        snapshots = [
            FundamentalSnapshotSchema(
                symbol="BEST", roe=30.0, debt_to_equity=0.05, eps_growth_3y=25.0
            ),
            FundamentalSnapshotSchema(
                symbol="MID", roe=20.0, debt_to_equity=0.20, eps_growth_3y=15.0
            ),
            FundamentalSnapshotSchema(
                symbol="WORST", roe=15.0, debt_to_equity=0.45, eps_growth_3y=12.0
            ),
        ]

        scores = compute_relative_conviction_scores(snapshots)
        assert scores["BEST"] == Decimal("100.0")

    def test_score_range(self):
        """All conviction scores must be within [0, 100]."""
        snapshots = [
            FundamentalSnapshotSchema(
                symbol=f"S{i}", roe=15.0 + i, debt_to_equity=0.1 * i, eps_growth_3y=12.0 + i * 2
            )
            for i in range(1, 6)
        ]

        scores = compute_relative_conviction_scores(snapshots)
        for sym, score in scores.items():
            assert Decimal("0") <= score <= Decimal("100"), f"{sym} score {score} out of range"

    def test_empty_input(self):
        """Empty input should return empty scores dict."""
        assert compute_relative_conviction_scores([]) == {}

    def test_single_item_universe(self):
        """Single stock in universe should receive 100.00 (sole rank = 1.0 pct)."""
        snapshots = [
            FundamentalSnapshotSchema(
                symbol="ONLY", roe=20.0, debt_to_equity=0.1, eps_growth_3y=18.0
            ),
        ]
        scores = compute_relative_conviction_scores(snapshots)
        assert scores["ONLY"] == Decimal("100.0")
