"""Regression tests for the Tier 1 governance veto (agents/tools/news_tools.py).

The first half uses REAL headlines that were wrongly vetoed during a pipeline
run on 2026-10-16 — large caps dropped purely because a neutral agency or
governance token appeared in routine news. The second half pins the genuine
hits that must survive the tightening.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.tools.news_tools import HeadlineSchema, classify_red_flags  # noqa: E402

# (headline, expect_veto)
SHOULD_PASS = [
    "BSE, NSE, Angel One shares see mixed reaction to the new SEBI CAS rules - CNBC TV18",
    "BSE, Angel One Fall Over 3% On SEBI CAS Changes; MCX, Groww, NSE Also Decline - NDTV Profit",
    "NSE, BSE, Groww, other capital market stocks rise up to 3% as Sebi revises CAS framework",
    "How MRF's 102-member promoter family has stayed united for 80 years",
    "Upcoming IPO: Bharat PET receives SEBI approval to raise 760 crore through public issue",
    "HDFC AMC, Nuvama, Nippon, other AMC stocks sink up to 9% on SEBI's mutual fund fee revamp",
    "Sebi's new closing-price mechanism trips up ETFs",
    "GlaxoSmithKline: What Did the Q2 FY27 Share Capital Audit Reveal? - Kalkine India",
    "Adani stocks slip after 1.48 crore SEBI settlement",
    "India to probe Chinese link in solar-cell import surge from Ethiopia",
    "MRF Ltd Share Price Today, MRF Share Price NSE, BSE - Business Today",
    "Reliance Industries, BSE lead mutual fund buys in September",
]

SHOULD_VETO = [
    "SJVN Fined for SEBI LODR Board Compliance Breach - Kalkine India",
    "Six Haryana IAS officers named - what CBI's 3rd chargesheet says",
    "Kajaria Ceramics fraud disclosure raises corporate governance concerns",
    "Oracle Financial Services Software: Why Did Non-Executive Director Yuan Resign?",
    "SEBI launches probe into accounting fraud at the company",
    "Promoter shareholding pledge spikes to 74 percent of equity",
    "Auditor resigns citing disagreement over accounts",
    "Company charged with money laundering by Enforcement Directorate",
]


def _vetoes(title):
    hl = HeadlineSchema(title=title, published_date=datetime.now(timezone.utc),
                        source="test", url="")
    return classify_red_flags("TEST", [hl]).is_red_flag


def test_routine_news_is_not_vetoed():
    bad = [t for t in SHOULD_PASS if _vetoes(t)]
    assert not bad, f"false-positive vetoes on routine news:\n" + "\n".join(bad)


def test_genuine_red_flags_still_vetoed():
    missed = [t for t in SHOULD_VETO if not _vetoes(t)]
    assert not missed, f"missed genuine red flags:\n" + "\n".join(missed)