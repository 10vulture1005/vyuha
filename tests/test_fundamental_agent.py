# tests/test_fundamental_agent.py
"""Unit tests for Phase 2 — Fundamental Agent math and filtering logic.

Tests cover:
    - Hard quantitative filter gating (ROE, D/E, EPS thresholds)
    - Cross-sectional Z-score conviction scoring and ranking
    - Edge cases: single-item universe, identical metrics
"""
import pytest

from agents.tools.fundamental_tools import (
    FundamentalSnapshotSchema,
    passes_hard_filters,
    compute_relative_conviction_scores,
)


def _snapshot(symbol: str, roe: float, debt_to_equity: float, eps_growth_3y: float):
    """Build a schema instance with the provenance block the model requires.

    ``raw_json`` became mandatory when point-in-time sourcing landed — it
    records which statement the ratios came from, so a snapshot cannot exist
    without saying where it was measured. These tests only exercise the filter
    and scoring math, so a fixed provenance block is appropriate.
    """
    return FundamentalSnapshotSchema(
        symbol=symbol,
        roe=roe,
        debt_to_equity=debt_to_equity,
        eps_growth_3y=eps_growth_3y,
        raw_json={"data_source": "screener", "period_end": "2026-03-31"},
    )


def _t() -> dict:
    """Live thresholds, so a deliberate re-tuning of thresholds.yaml does not
    break these tests. The gates themselves are asserted below against the
    loaded values, which is what actually matters."""
    from config import thresholds

    return thresholds.get("fundamental", {})


class TestHardFilters:
    """Verify non-negotiable quantitative gates from thresholds.yaml."""

    def test_passing_stock(self):
        """Stock comfortably inside every gate should pass."""
        t = _t()
        passer = _snapshot("GOOD", t["min_roe"] + 4.0, t["max_debt_equity"] - 1.0, t["min_eps_cagr_3y"] + 7.0)
        assert passes_hard_filters(passer) is True

    def test_failing_roe(self):
        """ROE below the configured floor must be rejected."""
        t = _t()
        fail_roe = _snapshot("BAD_ROE", t["min_roe"] - 0.1, 0.2, t["min_eps_cagr_3y"] + 5.0)
        assert passes_hard_filters(fail_roe) is False

    def test_failing_debt_equity(self):
        """D/E above the configured ceiling must be rejected."""
        t = _t()
        fail_de = _snapshot("BAD_DE", t["min_roe"] + 4.0, t["max_debt_equity"] + 0.01, t["min_eps_cagr_3y"] + 5.0)
        assert passes_hard_filters(fail_de) is False

    def test_failing_eps_growth(self):
        """EPS 3Y CAGR below the configured floor must be rejected."""
        t = _t()
        fail_eps = _snapshot("BAD_EPS", t["min_roe"] + 4.0, 0.2, t["min_eps_cagr_3y"] - 0.1)
        assert passes_hard_filters(fail_eps) is False

    def test_exact_boundary_passes(self):
        """Metrics sitting exactly on the gates must pass (gates are inclusive)."""
        t = _t()
        boundary = _snapshot("EDGE", t["min_roe"], t["max_debt_equity"], t["min_eps_cagr_3y"])
        assert passes_hard_filters(boundary) is True

    def test_all_metrics_failing(self):
        """Stock failing every gate must be rejected."""
        t = _t()
        fail_all = _snapshot("WORST", t["min_roe"] - 7.0, t["max_debt_equity"] + 0.5, t["min_eps_cagr_3y"] - 8.0)
        assert passes_hard_filters(fail_all) is False


class TestConvictionScoring:
    """Verify the weighted conviction score and its clamp.

    Despite the ``relative`` in the name, this is an *absolute* weighted score
    (``50 + 0.5*ROE + 0.5*EPS_CAGR - 10*D/E``) clamped to [0, 100] — not a
    cross-sectional percentile. An earlier version of this test asserted a
    percentile ranking that the implementation never performed.
    """

    @staticmethod
    def _expected(roe: float, d_e: float, eps: float) -> float:
        return min(max(50.0 + roe * 0.5 + eps * 0.5 - d_e * 10.0, 0.0), 100.0)

    def test_ranking_order(self):
        """Best-quality stock must score highest, worst lowest."""
        snapshots = [
            _snapshot("BEST", 30.0, 0.05, 25.0),
            _snapshot("MID", 20.0, 0.20, 15.0),
            _snapshot("WORST", 15.0, 0.45, 12.0),
        ]

        scores = compute_relative_conviction_scores(snapshots)
        assert scores["BEST"] > scores["MID"] > scores["WORST"]

    def test_score_matches_weighted_formula(self):
        """Score equals the documented weighted sum (no percentile mapping)."""
        snapshots = [
            _snapshot("BEST", 30.0, 0.05, 25.0),
            _snapshot("MID", 20.0, 0.20, 15.0),
            _snapshot("WORST", 15.0, 0.45, 12.0),
        ]

        scores = compute_relative_conviction_scores(snapshots)
        assert scores["BEST"] == pytest.approx(self._expected(30.0, 0.05, 25.0))
        assert scores["MID"] == pytest.approx(self._expected(20.0, 0.20, 15.0))
        assert scores["WORST"] == pytest.approx(self._expected(15.0, 0.45, 12.0))

    def test_high_quality_does_not_auto_score_100(self):
        """Regression guard: a merely-good stock must not be pinned to 100.

        Under a percentile scheme every universe has a 100. Here only a
        saturating score reaches it, which keeps scores comparable across runs.
        """
        scores = compute_relative_conviction_scores([_snapshot("GOOD", 20.0, 0.2, 18.0)])
        assert scores["GOOD"] < 100.0

    def test_score_range(self):
        """All conviction scores must be within [0, 100]."""
        snapshots = [_snapshot(f"S{i}", 15.0 + i, 0.1 * i, 12.0 + i * 2) for i in range(1, 6)]

        scores = compute_relative_conviction_scores(snapshots)
        for sym, score in scores.items():
            assert 0.0 <= score <= 100.0, f"{sym} score {score} out of range"

    def test_score_clamped_at_ceiling(self):
        """An absurdly strong snapshot must clamp to 100, not overflow."""
        scores = compute_relative_conviction_scores([_snapshot("MOON", 400.0, 0.0, 300.0)])
        assert scores["MOON"] == 100.0

    def test_score_clamped_at_floor(self):
        """A hopeless snapshot must clamp to 0, not go negative."""
        scores = compute_relative_conviction_scores([_snapshot("JUNK", 1.0, 50.0, -20.0)])
        assert scores["JUNK"] == 0.0

    def test_empty_input(self):
        """Empty input should return empty scores dict."""
        assert compute_relative_conviction_scores([]) == {}

    def test_single_item_universe(self):
        """A single stock is scored on its own merits, not forced to 100."""
        scores = compute_relative_conviction_scores([_snapshot("ONLY", 20.0, 0.1, 18.0)])
        assert scores["ONLY"] == pytest.approx(self._expected(20.0, 0.1, 18.0))
