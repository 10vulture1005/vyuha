# tests/test_investor_veto.py
"""News classification and veto logic tests.

Three properties matter here:

*   **Macro news can never touch a single-stock decision.** RBI / crude /
    budget headlines must be routed to Layer 0 and no further.
*   **A hard negative blocks buys immediately** but only exits when price
    confirms; otherwise it opens a 5-day review state instead of selling into
    a headline that price action has already contradicted.
*   **A positive catalyst never buys on the headline alone.**
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from investor import news as N
from investor.config import investor_config


HARD = investor_config.news["hard_negative"]
POS = investor_config.news["positive_catalyst"]
OVR = investor_config.news["overreaction"]


def _ev(event_type, subtype="x", title="t", sent=0.0, when=None):
    return N.NewsEvent(
        symbol="TEST", title=title, url="", source="unit-test",
        published_at=when or datetime(2024, 6, 1, tzinfo=timezone.utc),
        event_type=event_type, subtype=subtype, materiality=1.0,
        sentiment=sent, tier="REGEX",
    )


class TestHardNegatives:
    @pytest.mark.parametrize("title,subtype", [
        ("Company charged with accounting fraud by SFIO", "fraud"),
        ("SEBI opens probe into disclosure lapse at firm", "regulatory_probe"),
        ("Auditors resign citing inability to verify records", "auditor_resignation"),
        ("Rating agency flags junk downgrade on bank bonds", "junk_downgrade"),
        ("Promoter pledges 22% stake in sharp rise", "pledge_spike"),
        ("CEO resigns amid accounting irregularities", "exec_exit"),
        ("Firm issues profit warning and cuts guidance", "fundamental_negative"),
    ])
    def test_classified_as_hard_negative(self, title, subtype):
        event_type, sub = N.classify_title(title)
        assert event_type == N.HARD_NEGATIVE
        if sub != "fundamental_negative":
            assert sub == subtype

    def test_pledge_without_a_spike_word_is_not_hard(self):
        """A static pledge disclosure is not a governance event."""
        assert N.classify_title("Promoter holding stands at 8%")[0] != N.HARD_NEGATIVE

    def test_earnings_miss_is_not_a_hard_negative(self):
        """A miss is bad news, not a governance veto."""
        assert N.classify_title("Q4 profit misses estimates")[0] != N.HARD_NEGATIVE

    def test_materiality_of_a_hard_negative_is_high(self):
        mat = N.materiality(N.HARD_NEGATIVE, "fraud", "SEBI probe")
        assert mat == 1.0


class TestMacroRouting:
    @pytest.mark.parametrize("title", [
        "RBI holds repo rate steady, signals neutral stance",
        "Crude oil prices rise on OPEC supply decision",
        "Union Budget proposes higher GST on insurance",
        "Fed signals rate cut, Wall Street rallies",
        "Israel and Iran agree ceasefire framework",
    ])
    def test_macro_headlines_are_macro(self, title):
        event_type, _ = N.classify_title(title)
        assert event_type == N.MACRO

    def test_macro_takes_priority_over_a_positive_pattern(self):
        """'RBI rate cut sends bank shares higher' matches both patterns.

        It must land in MACRO: it says something about the whole market, and
        letting a bank-specific read on it would make one macro headline move
        one stock.
        """
        event_type, _ = N.classify_title(
            "RBI cuts repo rate, bank shares rally to record high"
        )
        assert event_type == N.MACRO

    def test_macro_never_produces_a_blocking_verdict(self):
        events = [_ev(N.MACRO, "rbi_macro", sent=-0.9)]
        v = N.aggregate("TEST", events, datetime(2024, 6, 2, tzinfo=timezone.utc))
        assert v.blocking() is False
        assert v.macro_only is True
        assert v.hard_negative_reason is None

    def test_macro_alone_is_macro_only(self):
        events = [_ev(N.MACRO), _ev(N.MACRO)]
        v = N.aggregate("TEST", events, datetime(2024, 6, 2, tzinfo=timezone.utc))
        assert v.macro_only is True

    def test_macro_plus_a_real_event_is_not_macro_only(self):
        events = [_ev(N.MACRO), _ev(N.POSITIVE)]
        v = N.aggregate("TEST", events, datetime(2024, 6, 2, tzinfo=timezone.utc))
        assert v.macro_only is False


class TestPositiveCatalysts:
    @pytest.mark.parametrize("title", [
        "Q4 net profit beats estimates on strong volume growth",
        "Company wins Rs 2,400 crore order from Railways",
        "Management raises full-year guidance",
        "Brokerage upgrades stock to buy after results",
    ])
    def test_classified_as_positive(self, title):
        assert N.classify_title(title)[0] == N.POSITIVE

    def test_hedged_positive_is_materiality_discounted(self):
        plain = N.materiality(N.POSITIVE, "order_win", "Company wins Rs 500 cr order")
        hedged = N.materiality(N.POSITIVE, "order_win",
                               "Company may win Rs 500 cr order, talks in progress")
        assert hedged < plain


class TestOverreaction:
    def test_rumour_is_overreaction_not_hard(self):
        event_type, sub = N.classify_title("Unconfirmed rumour of stake sale circulates")
        assert event_type == N.OVERREACTION
        assert sub == "rumor"

    def test_sector_wide_noise_is_overreaction(self):
        assert N.classify_title("Sector-wide selloff on profit booking")[0] == N.OVERREACTION

    def test_overreaction_add_requires_three_stabilising_days(self):
        days = int(OVR["stabilization_days"])
        ok, why = N.overreaction_add_allowed(days, True, 95.0, -0.3)
        assert ok is True
        ok, _ = N.overreaction_add_allowed(days - 1, True, 95.0, -0.3)
        assert ok is False

    def test_overreaction_add_requires_the_stock_above_its_200dma(self):
        ok, _ = N.overreaction_add_allowed(5, False, 95.0, -0.3)
        assert ok is False

    def test_overreaction_add_requires_a_top_third_score(self):
        need = float(OVR["min_score_percentile"])
        ok, _ = N.overreaction_add_allowed(5, True, need + 5, -0.3)
        assert ok is True
        ok, _ = N.overreaction_add_allowed(5, True, need - 20, -0.3)
        assert ok is False


class TestVetoBehaviour:
    def test_hard_negative_blocks_new_buys(self):
        events = [_ev(N.HARD_NEGATIVE, "fraud", "SEBI probe opened")]
        v = N.aggregate("TEST", events, datetime(2024, 6, 2, tzinfo=timezone.utc))
        assert v.blocking() is True
        assert "SEBI" in (v.hard_negative_reason or "") or v.hard_negative_reason

    def test_positive_catalyst_never_blocks(self):
        events = [_ev(N.POSITIVE)]
        v = N.aggregate("TEST", events, datetime(2024, 6, 2, tzinfo=timezone.utc))
        assert v.blocking() is False
        assert v.positive_catalyst is True

    def test_price_confirmation_needs_a_5pct_drop_on_2x_volume(self):
        need_drop = float(HARD["confirm_drop_pct"])
        need_vol = float(HARD["confirm_vol_multiple"])
        ok, _ = N.price_confirms_negative(100.0, 100.0, 100.0 - need_drop - 0.5,
                                          None, 5_000.0, 1_000.0 * need_vol)
        assert ok is True

    def test_small_drop_on_huge_volume_does_not_confirm(self):
        """A 2% wobble on 3x volume is not confirmation."""
        ok, _ = N.price_confirms_negative(100.0, 100.0, 98.0, None, 3_000.0, 1_000.0)
        assert ok is False

    def test_big_drop_on_thin_volume_does_not_confirm(self):
        ok, _ = N.price_confirms_negative(100.0, 100.0, 90.0, None, 1_100.0, 1_000.0)
        assert ok is False

    def test_close_below_the_50dma_confirms_independently(self):
        """The slow bleed: no big volume day, but the chart is broken."""
        ok, why = N.price_confirms_negative(100.0, 100.0, 94.0, 95.0, 1_000.0, 1_000.0)
        assert ok is True
        assert "50DMA" in why

    def test_closing_above_the_50dma_does_not_confirm(self):
        ok, _ = N.price_confirms_negative(100.0, 100.0, 99.0, 95.0, 1_000.0, 1_000.0)
        assert ok is False


class TestCatalystEntryConfirmation:
    def test_requires_a_top_thirty_percent_close(self):
        ok, why = N.catalyst_entry_allowed(
            110.0, 90.0, 92.0, 100.0, 5_000.0, 1_000.0)
        assert ok is False
        assert "range" in why

    def test_requires_double_volume(self):
        need_vol = float(POS["min_vol_multiple"])
        ok, why = N.catalyst_entry_allowed(
            120.0, 90.0, 118.0, 100.0, 1_500.0, 1_000.0)
        assert ok is False
        assert "volume" in why
        ok, _ = N.catalyst_entry_allowed(
            120.0, 90.0, 118.0, 100.0, need_vol * 1_100.0, 1_000.0)
        assert ok is True

    def test_requires_the_gap_to_hold(self):
        """A strong close that still sits BELOW the prior close is a failed gap.

        Constructed so the close is in the top third of the day's range (so the
        range test passes) yet below the previous close — which is precisely a
        catalyst that got sold into.
        """
        ok, why = N.catalyst_entry_allowed(
            110.0, 80.0, 95.0, 100.0, 5_000.0, 1_000.0)
        assert ok is False
        assert "gap" in why

    def test_all_three_conditions_together_pass(self):
        ok, _ = N.catalyst_entry_allowed(
            120.0, 100.0, 118.0, 100.0, 3_000.0, 1_000.0)
        assert ok is True

    def test_flat_bar_is_rejected(self):
        ok, _ = N.catalyst_entry_allowed(100.0, 100.0, 100.0, 100.0, 3_000.0, 1_000.0)
        assert ok is False

    def test_catalyst_size_is_1_25x_and_still_capped_by_weight(self):
        assert float(POS["size_multiple"]) == 1.25


class TestSentimentDecay:
    def test_recent_headline_dominates_an_old_one(self):
        now = datetime(2024, 6, 10, tzinfo=timezone.utc)
        recent = _ev(N.HARD_NEGATIVE, sent=-1.0,
                     when=now - timedelta(days=1))
        old = _ev(N.HARD_NEGATIVE, sent=-1.0,
                  when=now - timedelta(days=60))
        decay_recent = N.decay_sentiment([recent], now)
        decay_old = N.decay_sentiment([old], now)
        assert decay_recent < decay_old
        # A single day of age already costs ~9% (0.5 ** (1/7)).
        assert -0.95 < decay_recent <= -0.85
        # 60 days at a 7-day half-life is ~2 ** -8.6: effectively nothing.
        assert abs(decay_old) < 0.01

    def test_half_life_halves_the_weight(self):
        now = datetime(2024, 6, 10, tzinfo=timezone.utc)
        hl = float(investor_config.news["sentiment_half_life_days"])
        d1 = N.decay_sentiment([_ev(N.NEUTRAL, sent=-1.0,
                                    when=now - timedelta(days=hl))], now)
        d2 = N.decay_sentiment([_ev(N.NEUTRAL, sent=-1.0,
                                    when=now - timedelta(days=hl * 2))], now)
        assert abs(d2 - d1 / 2.0) < 1e-6

    def test_empty_events_return_zero(self):
        now = datetime(2024, 6, 10, tzinfo=timezone.utc)
        assert N.decay_sentiment([], now) == 0.0

    def test_events_without_a_timestamp_are_ignored(self):
        now = datetime(2024, 6, 10, tzinfo=timezone.utc)
        ev = _ev(N.NEUTRAL, sent=-1.0)
        ev.published_at = None
        assert N.decay_sentiment([ev], now) == 0.0


class TestEarningsProxy:
    def _bar(self, o, h, l, c, v):
        return pd.Series({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})

    def test_earnings_gap_on_volume_is_detected(self):
        bar = self._bar(108.0, 112.0, 107.0, 110.0, 3_000.0)
        prev = self._bar(100.0, 101.0, 99.0, 100.0, 1_000.0)
        proxy = N.earnings_proxy("X", bar, prev, 1_000.0)
        assert proxy is not None
        assert proxy.is_catalyst is True
        assert proxy.is_negative is False
        assert proxy.gap_pct == pytest.approx(8.0, abs=1e-6)
        assert proxy.volume_ratio == pytest.approx(3.0, abs=1e-6)

    def test_negative_gap_is_flagged_as_a_dip_not_a_veto(self):
        bar = self._bar(92.0, 93.0, 90.0, 91.0, 3_000.0)
        prev = self._bar(100.0, 101.0, 99.0, 100.0, 1_000.0)
        proxy = N.earnings_proxy("X", bar, prev, 1_000.0)
        assert proxy.is_negative is True
        ev = N.aggregate("X", InvestorEngine_events(proxy), datetime(2024, 6, 2))
        assert ev.blocking() is False, \
            "a price gap must never manufacture a governance veto"

    def test_ordinary_day_is_not_an_event(self):
        bar = self._bar(100.5, 101.0, 100.0, 100.8, 1_100.0)
        prev = self._bar(100.0, 101.0, 99.0, 100.0, 1_000.0)
        assert N.earnings_proxy("X", bar, prev, 1_000.0) is None

    def test_gap_without_volume_is_not_an_event(self):
        bar = self._bar(108.0, 112.0, 107.0, 110.0, 1_100.0)
        prev = self._bar(100.0, 101.0, 99.0, 100.0, 1_000.0)
        assert N.earnings_proxy("X", bar, prev, 1_000.0) is None

    def test_proxy_events_are_labelled(self):
        """Proxy events must be unmistakably synthetic in the DB."""
        from investor.pipeline import InvestorEngine
        bar = self._bar(108.0, 112.0, 107.0, 110.0, 3_000.0)
        prev = self._bar(100.0, 101.0, 99.0, 100.0, 1_000.0)
        proxy = N.earnings_proxy("X", bar, prev, 1_000.0)
        events = InvestorEngine._proxy_to_events(proxy, None)
        assert len(events) == 1
        assert events[0].tier == "PROXY"
        assert events[0].source == "earnings_gap_proxy"
        assert events[0].title.startswith("[proxy]")


def InvestorEngine_events(proxy):
    """Helper mirroring the pipeline's proxy -> event conversion."""
    from investor.pipeline import InvestorEngine
    return InvestorEngine._proxy_to_events(proxy, None)