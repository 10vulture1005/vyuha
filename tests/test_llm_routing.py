# tests/test_llm_routing.py
"""Unit tests for provider routing: OpenRouter primary, Groq fallback.

Covers the three behaviours that matter operationally:
  * the resolved primary/fallback routes match what .env asks for,
  * a missing fallback key disables the fallback instead of crashing,
  * the Vyuha Agent graph config carries the fallback through to the client
    factory, and omits it entirely when the fallback is unusable.
"""
import pytest


# ─── Route resolution ─────────────────────────────────────────────────────────


def test_primary_route_defaults_to_openrouter():
    from config.llm_routing import primary_route

    route = primary_route()
    assert route.provider == "openrouter"
    assert route.base_url == "https://openrouter.ai/api/v1"


def test_primary_route_reads_api_key(monkeypatch):
    from config import llm_routing
    from config.settings import settings

    secret = type(settings.OPENROUTER_API_KEY)
    monkeypatch.setattr(
        settings, "OPENROUTER_API_KEY", secret("sk-or-v1-test"), raising=False
    )
    assert llm_routing.api_key_for("openrouter") == "sk-or-v1-test"


def test_api_key_blank_reads_as_absent(monkeypatch):
    """An empty ``GROQ_API_KEY=`` must mean 'absent', not 'an empty key'."""
    from config import llm_routing
    from config.settings import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr(""), raising=False)
    assert llm_routing.api_key_for("groq") is None


def test_api_key_unknown_provider_is_none():
    from config.llm_routing import api_key_for

    assert api_key_for("not-a-real-provider") is None


# ─── Fallback gating ──────────────────────────────────────────────────────────


def test_fallback_disabled_without_key(monkeypatch):
    """No Groq key => no fallback route => pipeline runs primary-only."""
    from config import llm_routing
    from config.settings import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr(""), raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_PROVIDER", "groq", raising=False)
    assert llm_routing.fallback_route() is None


def test_fallback_resolves_with_key(monkeypatch):
    from config import llm_routing
    from config.settings import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr("gsk_test"), raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_PROVIDER", "groq", raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "openai/gpt-oss-20b", raising=False)

    route = llm_routing.fallback_route()
    assert route is not None
    assert route.provider == "groq"
    assert route.model == "openai/gpt-oss-20b"
    assert route.base_url == "https://api.groq.com/openai/v1"
    assert route.api_key == "gsk_test"
    assert route.is_usable


def test_fallback_disabled_when_provider_empty(monkeypatch):
    from config import llm_routing
    from config.settings import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr("gsk_test"), raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_PROVIDER", "", raising=False)
    assert llm_routing.fallback_route() is None


def test_fallback_disabled_when_same_as_primary(monkeypatch):
    """Pointing the fallback at the primary just fails twice; treat as disabled."""
    from config import llm_routing
    from config.settings import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", SecretStr("sk-or-v1-test"), raising=False)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openrouter", raising=False)
    monkeypatch.setattr(settings, "LLM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free", raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_PROVIDER", "openrouter", raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free", raising=False)
    assert llm_routing.fallback_route() is None


# ─── Graph config plumbing ────────────────────────────────────────────────────


def test_build_config_marks_fallback_inert_when_unusable(monkeypatch):
    """No fallback keys -> graph builds unwrapped clients (primary-only)."""
    from agents.vyuha_agent.config_builder import build_vyuha_agent_config
    from config.settings import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr(""), raising=False)

    config = build_vyuha_agent_config()
    assert config["llm_provider"] == "openrouter"
    assert config["deep_think_llm"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert config["fallback_llm_provider"] is None
    assert config["fallback_llm_model"] is None


def test_build_config_carries_fallback_when_usable(monkeypatch):
    from agents.vyuha_agent.config_builder import build_vyuha_agent_config
    from config.settings import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr("gsk_test"), raising=False)

    config = build_vyuha_agent_config()
    assert config["fallback_llm_provider"] == "groq"
    assert config["fallback_llm_model"] == "openai/gpt-oss-20b"
    assert config["fallback_base_url"] == "https://api.groq.com/openai/v1"
    assert config["fallback_api_key"] == "gsk_test"


# ─── Factory wiring ───────────────────────────────────────────────────────────


def test_create_llm_client_without_fallback_is_unwrapped():
    from tradingagents.llm_clients.factory import _FallbackClient, create_llm_client

    client = create_llm_client(provider="openrouter", model="nvidia/nemotron-3-ultra-550b-a55b:free")
    assert not isinstance(client, _FallbackClient)


def test_create_llm_client_with_fallback_wraps(monkeypatch):
    """Both clients are built eagerly and get_llm() memoises the wrapper."""
    from tradingagents.llm_clients import factory
    from tradingagents.llm_clients.factory import _FallbackClient, create_llm_client

    monkeypatch.setattr(
        factory,
        "_build_client",
        lambda provider, model, base_url=None, **kw: _FakeClient(provider, model),
    )

    client = create_llm_client(
        provider="openrouter",
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
        fallback_provider="groq",
        fallback_model="openai/gpt-oss-20b",
    )
    assert isinstance(client, _FallbackClient)
    assert client.primary.provider == "openrouter"
    assert client.fallback.provider == "groq"

    llm = client.get_llm()
    assert isinstance(llm, _FakeRunnable) and llm.name == "wrapped"
    # Memoised: a second call must not rebuild the RunnableWithFallbacks.
    assert client.get_llm() is llm


def test_create_llm_client_forwards_fallback_api_key(monkeypatch):
    from tradingagents.llm_clients import factory
    from tradingagents.llm_clients.factory import create_llm_client

    captured = {}

    def _capture(provider, model, base_url=None, **kw):
        captured[provider] = kw
        return _FakeClient(provider, model)

    monkeypatch.setattr(factory, "_build_client", _capture)

    create_llm_client(
        provider="openrouter",
        model="m-primary",
        fallback_provider="groq",
        fallback_model="openai/gpt-oss-20b",
        fallback_api_key="gsk_explicit",
    )
    assert captured["groq"]["api_key"] == "gsk_explicit"


def test_unsupported_provider_still_raises():
    from tradingagents.llm_clients.factory import create_llm_client

    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        create_llm_client(provider="definitely-not-a-provider", model="x")


# ─── Test doubles ─────────────────────────────────────────────────────────────


class _FakeRunnable:
    def __init__(self, name: str):
        self.name = name
        self.fallbacks = []

    def with_fallbacks(self, fallbacks):
        wrapper = _FakeRunnable("wrapped")
        wrapper.fallbacks = list(fallbacks)
        return wrapper


class _FakeClient:
    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self.base_url = None

    def get_provider_name(self):
        return self.provider

    def get_llm(self):
        return _FakeRunnable(self.provider)

    def validate_model(self):
        return True