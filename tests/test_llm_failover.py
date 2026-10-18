# tests/test_llm_failover.py
"""Tests for the retry/failover wrapper in :mod:`crew.llm_failover`.

These exist because the previous implementation was a ``litellm.completion``
monkey-patch that went silently inert when CrewAI moved to native provider
SDKs — it had no tests, so nothing noticed. The cases below pin the behaviour
that actually matters: which errors retry, which fail over immediately, and
that a fallback-less deployment still raises.
"""
import asyncio

import pytest

from crew import llm_failover as fo
from config.llm_routing import Route


def _route(provider: str = "groq", model: str = "openai/gpt-oss-20b") -> Route:
    return Route(
        provider=provider,
        model=model,
        api_key="fallback-key",
        base_url="https://api.groq.com/openai/v1",
    )


class FakeRateLimit(Exception):
    status_code = 429


class FakeBadRequest(Exception):
    status_code = 400


class FakeTimeout(Exception):
    pass


# ── _classify ────────────────────────────────────────────────────────────

def test_classify_rate_limit_by_status():
    assert fo._classify(FakeRateLimit("429 too many")) == "rate_limit"


def test_classify_rate_limit_by_name():
    assert fo._classify(Exception("x")) == "fatal"
    assert fo._classify(type("RateLimitError", (Exception,), {})("x")) == "rate_limit"


def test_classify_tool_use_failed():
    exc = FakeBadRequest("tool_use_failed: malformed tool call")
    assert fo._classify(exc) == "tool_use_failed"


def test_classify_other_bad_request_is_fatal():
    assert fo._classify(FakeBadRequest("unknown parameter")) == "fatal"


def test_classify_walks_exception_chain():
    """A 429 wrapped by a provider SDK must still be seen as a rate limit."""
    inner = FakeRateLimit("slow down")
    outer = RuntimeError("provider call failed")
    outer.__cause__ = inner
    assert fo._classify(outer) == "rate_limit"


def test_classify_survives_self_referential_chain():
    """A cycle in __cause__ must not hang the classifier."""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert fo._classify(a) == "fatal"


# ── retry / failover policy ───────────────────────────────────────────────

def test_success_on_first_try_does_not_sleep_or_failover():
    slept = []
    calls = []

    def primary():
        calls.append(1)
        return "ok"

    def fallback():
        calls.append("fb")
        return "fb"

    out = fo._drive(primary, fallback, _route(), slept.append)
    assert out == "ok"
    assert calls == [1]
    assert slept == []


def test_rate_limit_retries_in_place_then_succeeds():
    slept = []
    attempts = []

    def primary():
        attempts.append(1)
        if len(attempts) < 3:
            raise FakeRateLimit("429")
        return "recovered"

    out = fo._drive(primary, None, None, slept.append)
    assert out == "recovered"
    assert len(attempts) == 3
    assert slept == [fo.RATE_LIMIT_BACKOFF_S] * 2


def test_tool_use_failed_retries_with_short_backoff():
    slept = []
    attempts = []

    def primary():
        attempts.append(1)
        if len(attempts) < 2:
            raise FakeBadRequest("tool_use_failed")
        return "ok"

    assert fo._drive(primary, None, None, slept.append) == "ok"
    assert slept == [fo.TOOL_USE_FAILED_BACKOFF_S]


def test_fatal_error_fails_over_immediately_without_retrying():
    """A dead provider must not be hammered three times before failover."""
    slept = []
    attempts = []

    def primary():
        attempts.append(1)
        raise FakeBadRequest("model not found")

    def fallback():
        return "fallback-ok"

    assert fo._drive(primary, fallback, _route(), slept.append) == "fallback-ok"
    assert len(attempts) == 1, "fatal error should not retry the primary"
    assert slept == []


def test_rate_limit_exhausting_budget_fails_over():
    slept = []
    attempts = []

    def primary():
        attempts.append(1)
        raise FakeRateLimit("429")

    assert fo._drive(primary, lambda: "fb", _route(), slept.append) == "fb"
    assert len(attempts) == fo.MAX_ATTEMPTS
    assert len(slept) == fo.MAX_ATTEMPTS - 1


def test_no_fallback_reraises_original_error():
    """A primary-only deployment must still fail loudly."""
    def primary():
        raise FakeBadRequest("model not found")

    with pytest.raises(FakeBadRequest):
        fo._drive(primary, None, None, lambda _: None)


def test_no_fallback_after_exhausting_retries_reraises_last_error():
    def primary():
        raise FakeRateLimit("429")

    with pytest.raises(FakeRateLimit):
        fo._drive(primary, None, None, lambda _: None)


def test_fallback_error_propagates():
    def primary():
        raise FakeBadRequest("dead")

    def fallback():
        raise ConnectionError("fallback also dead")

    with pytest.raises(ConnectionError):
        fo._drive(primary, fallback, _route(), lambda _: None)


# ── async path ───────────────────────────────────────────────────────────

def test_async_rate_limit_then_success():
    slept = []
    attempts = []

    async def primary():
        attempts.append(1)
        if len(attempts) < 2:
            raise FakeRateLimit("429")
        return "ok"

    async def sleep(_s):
        slept.append(1)

    assert asyncio.run(fo._drive_async(primary, None, None, sleep)) == "ok"
    assert len(attempts) == 2


def test_async_fatal_fails_over():
    attempts = []

    async def primary():
        attempts.append(1)
        raise FakeBadRequest("dead")

    async def fallback():
        return "fb"

    async def sleep(_s):
        return None

    assert asyncio.run(fo._drive_async(primary, fallback, _route(), sleep)) == "fb"
    assert len(attempts) == 1


# ── instance wrapping ────────────────────────────────────────────────────

class _FakeLLM:
    """Minimal stand-in exposing the attributes the wrapper touches."""

    def __init__(self, behaviour=None):
        self.model = "primary/model"
        self.base_url = "https://primary.test/v1"
        self.api_key = "primary-key"
        self._client = object()
        self._async_client = object()
        self._behaviour = behaviour or (lambda **kw: "primary-ok")
        self.seen = []

    def call(self, **kwargs):
        self.seen.append((self.model, self.base_url, self.api_key))
        return self._behaviour(model=self.model, api_key=self.api_key, **kwargs)

    async def acall(self, **kwargs):
        self.seen.append((self.model, self.base_url, self.api_key))
        return self._behaviour(model=self.model, api_key=self.api_key, **kwargs)


def test_attach_failover_is_idempotent():
    llm = _FakeLLM()
    fo.attach_failover(llm)
    first = llm.call
    fo.attach_failover(llm)
    assert llm.call is first


def test_passthrough_when_fallback_unavailable(monkeypatch):
    monkeypatch.setattr(fo, "_resolve_fallback", lambda: None)
    llm = _FakeLLM()
    fo.attach_failover(llm)
    assert llm.call(messages="hi") == "primary-ok"
    assert llm.model == "primary/model"


def test_failover_retargets_instance(monkeypatch):
    """The dead-provider case from CI: primary unusable, fallback must serve."""
    route = _route()
    monkeypatch.setattr(fo, "_resolve_fallback", lambda: route)
    monkeypatch.setattr(fo.time, "sleep", lambda _s: None)

    def behaviour(model=None, api_key=None, **kwargs):
        if api_key == "primary-key":
            raise FakeBadRequest("OPENAI_API_KEY is required")
        return f"served-by-{model}"

    llm = _FakeLLM(behaviour)
    fo.attach_failover(llm)
    assert llm.call(messages="hi") == "served-by-openai/gpt-oss-20b"
    # The cached client must be dropped so the next call uses the new endpoint.
    assert llm._client is None and llm._async_client is None


def test_failover_restores_primary_before_a_second_failover(monkeypatch):
    """A flapping primary must not permanently pin the instance to fallback."""
    route = _route()
    monkeypatch.setattr(fo, "_resolve_fallback", lambda: route)
    monkeypatch.setattr(fo.time, "sleep", lambda _s: None)

    def behaviour(model=None, api_key=None, **kwargs):
        if api_key == "primary-key":
            raise FakeBadRequest("primary down")
        return f"served-by-{model}"

    llm = _FakeLLM(behaviour)
    fo.attach_failover(llm)
    llm.call(messages="a")
    assert llm.model == "openai/gpt-oss-20b"
    # Next call must start from the configured primary again.
    llm.call(messages="b")
    assert [c[0] for c in llm.seen] == ["primary/model", "openai/gpt-oss-20b"] * 2


def test_failover_reraises_primary_error_if_fallback_vanishes(monkeypatch):
    """If the key disappears mid-flight, surface the primary's error."""
    calls = {"n": 0}

    def resolve():
        calls["n"] += 1
        return _route() if calls["n"] == 1 else None

    monkeypatch.setattr(fo, "_resolve_fallback", resolve)
    monkeypatch.setattr(fo.time, "sleep", lambda _s: None)

    llm = _FakeLLM(lambda **kw: (_ for _ in ()).throw(FakeBadRequest("primary dead")))
    fo.attach_failover(llm)
    with pytest.raises(FakeBadRequest):
        llm.call(messages="hi")


def test_async_failover_retargets(monkeypatch):
    route = _route()
    monkeypatch.setattr(fo, "_resolve_fallback", lambda: route)
    monkeypatch.setattr(fo.asyncio, "sleep", _noop_sleep)

    def behaviour(model=None, api_key=None, **kwargs):
        if api_key == "primary-key":
            raise FakeRateLimit("429")
        return f"async-{model}"

    llm = _FakeLLM(behaviour)
    fo.attach_failover(llm)
    assert asyncio.run(llm.acall(messages="hi")) == "async-openai/gpt-oss-20b"


async def _noop_sleep(_seconds):
    return None


# ── integration: real CrewAI native provider ─────────────────────────────

def test_client_rebuild_uses_the_new_endpoint_after_failover():
    """Regression: clearing a pydantic PrivateAttr must actually rebuild.

    Using ``object.__setattr__`` here writes an instance-dict entry that
    shadows the private attr. CrewAI's ``_get_sync_client`` then reads the
    shadowing ``None``, skips the rebuild, and hands back ``None`` — the next
    call dies with ``'NoneType' object has no attribute 'chat'``. This pins
    the plain-assignment behaviour the wrapper depends on.
    """
    pytest.importorskip("crewai")
    from crewai import LLM

    llm = LLM(model="openai/gpt-4o", api_key="k1", base_url="https://old.test/v1")
    llm.base_url = "https://new.test/v1"
    llm.api_key = "k2"

    fo._drop_clients(llm)
    assert llm._client is None, "client should be cleared"
    client = llm._get_sync_client()
    assert client is not None, "provider must rebuild the client, not return None"
    assert "new.test" in str(client.base_url)