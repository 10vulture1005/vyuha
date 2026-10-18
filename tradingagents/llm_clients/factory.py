from .base_client import BaseLLMClient


def _build_client(
    provider: str,
    model: str,
    base_url: str | None = None,
    **kwargs,
) -> BaseLLMClient:
    """Instantiate the native client for one provider/model pair.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    # Native (non-OpenAI) APIs are matched first so their string check doesn't
    # import the OpenAI client. Everything else is OpenAI-compatible and routes
    # through the provider registry (single source of truth).
    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    if provider_lower == "bedrock":
        from .bedrock_client import BedrockClient
        return BedrockClient(model, base_url, **kwargs)

    from .openai_client import OpenAIClient, is_openai_compatible
    if is_openai_compatible(provider_lower):
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")


class _FallbackClient(BaseLLMClient):
    """Primary LLM with a backup LLM behind LangChain's ``with_fallbacks``.

    Any exception from the primary — rate limit, 5xx, timeout, auth error, or
    the model simply being unavailable on that provider — is retried against
    the fallback with identical messages and tools. If the fallback also
    fails, the primary's exception is what surfaces, so the root cause is
    never masked by the backup's failure.

    Both clients are constructed eagerly so a misconfigured fallback surfaces
    at graph-build time rather than mid-debate. The wrapped runnable is built
    lazily and memoised: ``with_fallbacks`` allocates a fresh
    ``RunnableWithFallbacks`` per call, but the graph holds the returned object
    across nodes, so rebuilding it would be wasted work.
    """

    def __init__(self, primary: BaseLLMClient, fallback: BaseLLMClient):
        # Inherit the primary's identity so log lines and any downstream
        # capability checks still describe the model actually in use.
        super().__init__(primary.model, primary.base_url)
        self.primary = primary
        self.fallback = fallback
        self._wrapped = None

    def get_provider_name(self) -> str:
        return self.primary.get_provider_name()

    def get_llm(self):
        if self._wrapped is None:
            self._wrapped = self.primary.get_llm().with_fallbacks(
                [self.fallback.get_llm()]
            )
        return self._wrapped

    def validate_model(self) -> bool:
        # The graph only ever calls get_llm(); validation is the primary's
        # concern and it has already warned at its own build time.
        return True


def create_llm_client(
    provider: str,
    model: str,
    base_url: str | None = None,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
    fallback_base_url: str | None = None,
    fallback_api_key: str | None = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider, with an optional backup.

    When ``fallback_provider``/``fallback_model`` are supplied, the returned
    client's :meth:`get_llm` wraps the primary in LangChain's
    ``with_fallbacks``. Omitting them returns an unwrapped client, which is the
    pre-fallback behaviour.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        fallback_provider: Optional provider to fail over to
        fallback_model: Model identifier on the fallback provider
        fallback_base_url: Optional base URL for the fallback provider
        fallback_api_key: Optional explicit key for the fallback provider.
            Normally left unset — the client resolves its own key from the
            environment. Pass it explicitly when the key is only reachable
            through ``config.settings`` and ``.env`` was never loaded into
            ``os.environ`` in this process.
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    primary = _build_client(provider, model, base_url, **kwargs)

    if not fallback_provider or not fallback_model:
        return primary

    fallback_kwargs = {}
    if fallback_api_key:
        # ``api_key`` is in _PASSTHROUGH_KWARGS, so this wins over the
        # env-var lookup the client would otherwise do.
        fallback_kwargs["api_key"] = fallback_api_key

    fallback = _build_client(
        fallback_provider, fallback_model, fallback_base_url, **fallback_kwargs
    )
    return _FallbackClient(primary, fallback)