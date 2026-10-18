# config/llm_routing.py
"""Shared resolution for the primary LLM provider and its fallback.

Vyuha has two independent LLM runtimes:

* the **CrewAI chain** (``crew/crew_definition.py`` → LiteLLM), and
* the **Vyuha Agent** (``agents/vyuha_agent/`` → LangChain).

Both read the same four ``Settings`` keys — ``LLM_PROVIDER``,
``LLM_MODEL``, ``LLM_FALLBACK_PROVIDER``, ``LLM_FALLBACK_MODEL`` — so
switching providers is a ``.env`` edit rather than a code change. This
module is the single place that turns those keys into a resolved route
and answers the one question both runtimes keep asking: *is the
fallback actually usable right now?*

The fallback is **inert when its API key is missing**. That is
deliberate: the key is optional in ``.env``, and a half-configured
fallback must degrade to primary-only rather than raise at import time
or, worse, silently swallow the primary's real error.

Endpoint note: the provider → base-URL table below deliberately mirrors
``tradingagents/llm_clients/openai_client.py::OPENAI_COMPATIBLE_PROVIDERS``.
We pass an explicit ``base_url`` instead of using LiteLLM's
``openrouter/...`` prefix convention, because the model IDs used here
contain a ``:free`` variant suffix that LiteLLM's provider-prefix parser
is not reliably transparent to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .settings import settings

# OpenAI-compatible endpoints for the providers vyuha routes between.
# Anthropic / Google / Bedrock use native APIs and deliberately have no
# entry — they are not reachable through the OpenAI-compatible path.
PROVIDER_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "ollama": "http://localhost:11434/v1",
}

# Provider key → the Settings field holding its API key. Kept as strings
# rather than attribute access so this module never has to import
# ``SecretStr`` or know about pydantic.
PROVIDER_KEY_FIELDS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "ollama": "",  # local runtime, no auth
}


def api_key_for(provider: str) -> Optional[str]:
    """Return the plaintext API key for ``provider``, or None if unset.

    Empty strings are normalised to ``None`` so that a blank
    ``GROQ_API_KEY=`` in ``.env`` reads as "absent" rather than as a key
    that will produce a confusing 401 downstream.
    """
    field_name = PROVIDER_KEY_FIELDS.get(provider.lower())
    if not field_name:
        return None
    secret = getattr(settings, field_name, None)
    if secret is None:
        return None
    # Settings uses SecretStr everywhere; unwrap if present.
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    value = (value or "").strip()
    # HTTP Authorization headers are ASCII. Reject copied terminal masking,
    # ellipses, and whitespace rather than letting httpx fail cryptically.
    if (not value or not value.isascii() or any(ch.isspace() for ch in value)
            or "…" in value or "..." in value or set(value) <= {"*", "•", "x", "X"}):
        return None
    return value


def base_url_for(provider: str) -> Optional[str]:
    """Resolve provider endpoint, including user-configured compatible APIs."""
    provider = provider.lower()
    if provider == "openai_compatible":
        return (settings.OPENAI_COMPATIBLE_BASE_URL or "").strip() or None
    return PROVIDER_BASE_URLS.get(provider)


@dataclass(frozen=True)
class Route:
    """One resolved provider/model pair."""

    provider: str
    model: str
    api_key: Optional[str]
    base_url: Optional[str]

    @property
    def is_usable(self) -> bool:
        """Whether this route has everything it needs to make a call.

        Keyless runtimes (ollama) are usable without a key; anything else
        needs one.
        """
        keyless = self.provider.lower() == "ollama"
        return keyless or bool(self.api_key)


def _build(provider: str, model: str) -> Route:
    provider = (provider or "").strip().lower()
    return Route(
        provider=provider,
        model=(model or "").strip(),
        api_key=api_key_for(provider),
        base_url=base_url_for(provider),
    )


def primary_route() -> Route:
    """The primary provider/model, as configured in ``Settings``."""
    return _build(settings.LLM_PROVIDER, settings.LLM_MODEL)


def fallback_route() -> Optional[Route]:
    """The fallback provider/model, or None when it cannot be used.

    Returns ``None`` — meaning "run primary-only" — when the fallback is
    switched off (empty provider), when no model is named, when the
    provider has no resolvable endpoint, or when its API key is missing.
    Callers should treat ``None`` as normal, not as an error.
    """
    provider = (settings.LLM_FALLBACK_PROVIDER or "").strip().lower()
    if not provider:
        return None

    route = _build(provider, settings.LLM_FALLBACK_MODEL)
    if not route.model or route.base_url is None:
        return None
    if not route.is_usable:
        return None

    # A fallback identical to the primary would just fail twice.
    primary = primary_route()
    if route.provider == primary.provider and route.model == primary.model:
        return None
    return route


def describe() -> str:
    """One-line human-readable routing summary for startup logs."""
    primary = primary_route()
    fallback = fallback_route()
    fb = f"{fallback.provider}/{fallback.model}" if fallback else "disabled"
    return f"primary={primary.provider}/{primary.model} fallback={fb}"