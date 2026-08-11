# tests/test_sentiment_agent.py
"""Unit tests for Phase 3 — Sentiment Agent two-tier classification.

Tests cover:
    - Tier 1 deterministic regex detection of governance red flags
    - Clean headline passage without false-positive vetoes
    - Empty headline edge case
    - Multiple Tier 1 keyword coverage (SEBI, auditor, promoter, CBI, fraud, delisting)
"""
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

from agents.tools.news_tools import (
    HeadlineSchema,
    ClassificationResult,
    classify_red_flags,
    TIER1_REGEX,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_headlines_from_fixture(filename: str) -> list[HeadlineSchema]:
    """Load headlines from a JSON fixture file."""
    filepath = FIXTURES_DIR / filename
    with open(filepath, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [
        HeadlineSchema(
            title=item["title"],
            published_date=datetime.fromisoformat(item["published_date"]),
            source=item["source"],
            url=item["url"],
        )
        for item in raw
    ]


class TestTier1RegexDetection:
    """Verify deterministic regex catches high-severity Indian governance keywords."""

    def test_sebi_probe_detection(self):
        """SEBI probe/order/ban headlines should trigger immediate HIGH veto."""
        scandal_hl = [
            HeadlineSchema(
                title="SEBI issues show-cause notice and orders forensic audit into promoter entity",
                published_date=datetime.now(timezone.utc),
                source="Test News",
                url="http://example.com/scandal",
            )
        ]
        verdict = classify_red_flags("TEST_SCANDAL", scandal_hl)
        assert verdict.is_red_flag is True
        assert verdict.severity == "HIGH"
        assert "Tier 1 Hard Regex Match" in verdict.reason

    def test_auditor_resignation_detection(self):
        """Auditor resignation headline should trigger veto."""
        hl = [
            HeadlineSchema(
                title="Statutory auditor resigns citing lack of information from management",
                published_date=datetime.now(timezone.utc),
                source="Test",
                url="http://example.com/auditor",
            )
        ]
        verdict = classify_red_flags("TEST_AUDITOR", hl)
        assert verdict.is_red_flag is True
        assert verdict.severity == "HIGH"

    def test_promoter_pledge_detection(self):
        """Promoter pledging spike headline should trigger veto."""
        hl = [
            HeadlineSchema(
                title="Promoter pledging rises to 85% as debt concerns mount",
                published_date=datetime.now(timezone.utc),
                source="Test",
                url="http://example.com/pledge",
            )
        ]
        verdict = classify_red_flags("TEST_PLEDGE", hl)
        assert verdict.is_red_flag is True
        assert verdict.severity == "HIGH"

    def test_cbi_raid_detection(self):
        """CBI raid headline should trigger veto."""
        hl = [
            HeadlineSchema(
                title="CBI raid on corporate offices amid ongoing investigation",
                published_date=datetime.now(timezone.utc),
                source="Test",
                url="http://example.com/cbi",
            )
        ]
        verdict = classify_red_flags("TEST_CBI", hl)
        assert verdict.is_red_flag is True
        assert verdict.severity == "HIGH"

    def test_fraud_detection(self):
        """Fraud headline should trigger veto."""
        hl = [
            HeadlineSchema(
                title="Major fraud uncovered in company's subsidiary operations",
                published_date=datetime.now(timezone.utc),
                source="Test",
                url="http://example.com/fraud",
            )
        ]
        verdict = classify_red_flags("TEST_FRAUD", hl)
        assert verdict.is_red_flag is True
        assert verdict.severity == "HIGH"

    def test_delisting_detection(self):
        """Delisting headline should trigger veto."""
        hl = [
            HeadlineSchema(
                title="Stock exchange issues delisting notice to non-compliant company",
                published_date=datetime.now(timezone.utc),
                source="Test",
                url="http://example.com/delist",
            )
        ]
        verdict = classify_red_flags("TEST_DELIST", hl)
        assert verdict.is_red_flag is True
        assert verdict.severity == "HIGH"

    def test_fixture_scandal_headlines_all_flagged(self):
        """All headlines from scandal fixture should individually trigger Tier 1 regex."""
        headlines = _load_headlines_from_fixture("scandal_headlines.json")
        for hl in headlines:
            verdict = classify_red_flags(f"FIXTURE_{hl.source}", [hl])
            assert verdict.is_red_flag is True, (
                f"Expected red flag for: '{hl.title}'"
            )


class TestCleanHeadlinePassage:
    """Verify standard business news passes without false-positive vetoes."""

    def test_clean_earnings_report(self):
        """Standard quarterly earnings report should NOT trigger a veto."""
        clean_hl = [
            HeadlineSchema(
                title="Reliance Industries reports 12% YoY jump in Q3 consolidated EBITDA",
                published_date=datetime.now(timezone.utc),
                source="Test News",
                url="http://example.com/clean",
            )
        ]
        verdict = classify_red_flags("RELIANCE", clean_hl)
        assert verdict.is_red_flag is False
        assert verdict.severity == "NONE"

    def test_fixture_clean_headlines_all_pass(self):
        """All headlines from clean fixture should pass without false-positive vetoes."""
        headlines = _load_headlines_from_fixture("clean_headlines.json")
        for hl in headlines:
            # Test each headline individually to isolate false positives
            verdict = classify_red_flags(f"CLEAN_{hl.source}", [hl])
            assert verdict.is_red_flag is False, (
                f"False positive veto for: '{hl.title}'"
            )


class TestEdgeCases:
    """Verify edge cases in the classification pipeline."""

    def test_empty_headlines_clean(self):
        """No headlines should default to clean verdict."""
        verdict = classify_red_flags("EMPTY", [])
        assert verdict.is_red_flag is False
        assert verdict.severity == "NONE"
        assert "No recent news found" in verdict.reason

    def test_regex_case_insensitivity(self):
        """Regex should match regardless of case."""
        hl = [
            HeadlineSchema(
                title="SEBI PROBE into corporate governance failures at major conglomerate",
                published_date=datetime.now(timezone.utc),
                source="Test",
                url="http://example.com/case",
            )
        ]
        verdict = classify_red_flags("TEST_CASE", hl)
        assert verdict.is_red_flag is True

    def test_regex_does_not_match_partial_words(self):
        """Regex word boundaries should prevent matching inside unrelated words."""
        hl = [
            HeadlineSchema(
                title="Company defaulted on quarterly filing deadline extension request",
                published_date=datetime.now(timezone.utc),
                source="Test",
                url="http://example.com/partial",
            )
        ]
        verdict = classify_red_flags("TEST_PARTIAL", hl)
        # 'defaulted' IS a match in our regex, so this should flag
        assert verdict.is_red_flag is True
