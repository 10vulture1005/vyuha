"""Tests for the batched sentiment path: FinBERT gate + chunked Tier 2 LLM."""
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from agents.tools import news_tools as nt  # noqa: E402
from agents.tools.news_tools import (  # noqa: E402
    ClassificationResult,
    HeadlineSchema,
    classify_symbols_batch,
    finbert_negative_scores,
)

NEGATIVE, CLEAN = 0.95, 0.05


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path):
    """Keep the persistent verdict cache out of these tests."""
    monkeypatch.setattr(nt, "_tier2_cache_dir", lambda: tmp_path / "t2cache")


def _hl(*titles):
    return [HeadlineSchema(title=t, published_date=datetime.now(timezone.utc),
                           source="test", url="u") for t in titles]


def _stub_finbert(monkeypatch, scores):
    """Replace the model with a lookup keyed by headline title."""
    def fake(titles, batch_size=32):
        return [scores.get(t, 0.5) for t in titles]
    monkeypatch.setattr(nt, "finbert_negative_scores", fake)


def _stub_llm(monkeypatch, flag_symbols):
    """Record which symbols reached the LLM; veto only the flagged ones."""
    calls = []

    def fake_chunk(chunk):
        symbols = [s for s, _ in chunk]
        calls.append(symbols)
        return {s: ClassificationResult(
            is_red_flag=s in flag_symbols, severity="HIGH" if s in flag_symbols else "NONE",
            reason=f"stub verdict for {s}", outcome="RED_FLAG" if s in flag_symbols else "CLEAN")
            for s in symbols}

    monkeypatch.setattr(nt, "_tier2_classify_chunk", fake_chunk)
    monkeypatch.setattr(nt.settings, "SENTIMENT_TIER2_ENABLED", True)
    return calls


def test_only_negative_headlines_reach_the_llm(monkeypatch):
    # Headlines chosen to be SILENT to the Tier 1 regex — that is the whole
    # point of the gate. A "SEBI + investigation" headline would be vetoed by
    # the regex before FinBERT ever ran and would not test anything.
    _stub_finbert(monkeypatch, {
        "shareholders approve routine share transfer": CLEAN,
        "board meeting scheduled for dividend declaration": CLEAN,
        "Independent director raises concerns over related-party transactions": NEGATIVE,
    })
    calls = _stub_llm(monkeypatch, flag_symbols={"BADCO"})
    items = [
        ("GOODCO", _hl("shareholders approve routine share transfer",
                       "board meeting scheduled for dividend declaration")),
        ("BADCO", _hl("Independent director raises concerns over related-party transactions")),
    ]
    results = classify_symbols_batch(items)

    assert set(results) == {"GOODCO", "BADCO"}
    # Only the FinBERT-negative symbol is escalated.
    assert [s for chunk in calls for s in chunk] == ["BADCO"]
    assert "FinBERT" in results["GOODCO"].reason
    assert results["BADCO"].is_red_flag is True


def test_regex_veto_skips_finbert_and_llm_entirely(monkeypatch):
    """A Tier 1 veto must not consume FinBERT inference or an LLM call."""
    def explode(*a, **k):
        raise AssertionError("regex veto should short-circuit the model")

    monkeypatch.setattr(nt, "finbert_negative_scores", explode)
    monkeypatch.setattr(nt, "_tier2_classify_chunk", explode)
    results = classify_symbols_batch([("FRAUDCO", _hl("Kajaria fraud disclosure raises concerns"))])
    assert results["FRAUDCO"].is_red_flag is True
    assert "Tier 1" in results["FRAUDCO"].reason


def test_chunking_splits_escalations(monkeypatch, settings=None):
    monkeypatch.setattr(nt.settings, "SENTIMENT_TIER2_BATCH_SIZE", 3)
    _stub_finbert(monkeypatch, {f"bad news {i}": NEGATIVE for i in range(7)})
    calls = _stub_llm(monkeypatch, flag_symbols=set())
    items = [(f"SYM{i}", _hl(f"bad news {i}")) for i in range(7)]
    results = classify_symbols_batch(items)

    assert len(calls) == 3, f"expected 7 symbols in chunks of 3 -> 3 calls, got {len(calls)}"
    assert [len(c) for c in calls] == [3, 3, 1]
    assert len(results) == 7


def test_missing_finbert_escalates_rather_than_suppresses(monkeypatch):
    """A dead model must fail toward MORE scrutiny, not less."""
    monkeypatch.setattr(nt, "finbert_negative_scores", lambda t, b=32: [0.5] * len(t))
    calls = _stub_llm(monkeypatch, flag_symbols=set())
    classify_symbols_batch([("AMBER", _hl("routine headline about operations"))])
    # threshold 0.5 is inclusive, so a 0.5 "no opinion" still escalates.
    assert [s for c in calls for s in c] == ["AMBER"]


def test_chunk_omission_is_not_read_as_clean(monkeypatch):
    """A model that drops a symbol must produce SCREENING_FAILED, not CLEAN."""
    monkeypatch.setattr(nt.settings, "SENTIMENT_TIER2_ENABLED", True)

    class FakeCompletions:
        @staticmethod
        def create(**kw):
            # Model echoes back only the first symbol, dropping the second.
            content = '{"A": {"is_red_flag": false, "severity": "NONE", "reason": "ok"}}'

            class Msg:
                def __init__(self):
                    self.content = content

            class Choice:
                def __init__(self):
                    self.message = Msg()

            class Resp:
                def __init__(self):
                    self.choices = [Choice()]

            return Resp()

    class FakeClient:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": FakeCompletions})()

    monkeypatch.setattr("openai.OpenAI", FakeClient)
    monkeypatch.setattr(nt, "_tier2_routes",
                        lambda: [("stub", "k", "http://stub", "m")])
    # `_tier2_classify_chunk` short-circuits under pytest so tests never touch
    # the network. Shadow `sys` to reach the real parsing logic with a fake client.
    monkeypatch.setattr(nt, "sys", types.SimpleNamespace(modules={}))

    out = nt._tier2_classify_chunk([("A", _hl("x")), ("B", _hl("y"))])
    assert out["A"].outcome == "CLEAN"
    assert out["B"].outcome == "SCREENING_FAILED", \
        "an omitted symbol must not be recorded as clean"


def test_finbert_score_mapping_handles_all_three_labels(monkeypatch):
    class FakePipe:
        def __call__(self, titles, **kw):
            labels = [("negative", 0.8), ("positive", 0.7), ("neutral", 0.9)]
            return [{"label": lbl, "score": sc} for lbl, sc in labels]

    monkeypatch.setattr(nt.settings, "SENTIMENT_FINBERT_ENABLED", True)
    monkeypatch.setattr(nt, "get_finbert", lambda: FakePipe())
    scores = finbert_negative_scores(["a", "b", "c"])
    assert scores[0] == 0.8          # P(negative) taken directly
    assert abs(scores[1] - 0.3) < 1e-6  # P(positive) inverted
    assert scores[2] == 0.5          # neutral -> no opinion