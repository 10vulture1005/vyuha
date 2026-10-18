# crew/llm_failover.py
"""Retry-then-failover wrapper for CrewAI LLM calls.

Why this module exists
----------------------
``crew/flow_runner.py`` used to monkey-patch ``litellm.completion`` /
``litellm.acompletion`` to add retry + provider-failover. That patch became
**silently inert** when CrewAI moved to native provider SDKs (1.15+):
``LLM(model="openai/...")`` now constructs
``crewai.llms.providers.openai.completion.OpenAICompletion``, which talks to
the OpenAI SDK directly and never calls ``litellm.completion``. The dead patch
meant a dead primary provider took the whole pipeline down instead of failing
over — which is exactly what happened in CI on 2026-10-16
(``ValueError: OPENAI_API_KEY is required``).

This module hooks the layer CrewAI *does* call: ``BaseLLM.call`` /
``BaseLLM.acall``, installed on the concrete provider instance rather than the
abstract base (the base is never invoked once a native provider is in play).

Retry policy (unchanged from the old patch)
-------------------------------------------
* **Rate limit** — retried in place up to ``MAX_ATTEMPTS`` with a fixed backoff.
  A rate limit is transient and the primary is usually the better model.
* **``tool_use_failed``** — retried in place with a short backoff; malformed
  tool-call XML is non-deterministic, so a retry often succeeds.
* **Anything else** (5xx, timeout, connection error, auth, model unavailable)
  — stop retrying the primary *immediately* and spend the remaining attempts
  on the fallback. Retrying a dead provider three times with a 15s sleep only
  delays the failover.

With no usable fallback configured, the last exception is re-raised unchanged,
so a primary-only deployment still fails loudly rather than silently.
"""
from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, Optional

from loguru import logger

from config.llm_routing import Route, fallback_route

# Total attempts against the primary, including the first.
MAX_ATTEMPTS = 3
RATE_LIMIT_BACKOFF_S = 15.0
TOOL_USE_FAILED_BACKOFF_S = 2.0

# Exception class names treated as transient. Matched by name rather than by
# import so this works whether CrewAI surfaces the native
# ``openai.RateLimitError`` or a LiteLLM-wrapped equivalent.
_RATE_LIMIT_NAMES = frozenset(
    {"RateLimitError", "APIConnectionError", "APITimeoutError", "Timeout"}
)
_BAD_REQUEST_NAMES = frozenset(
    {"BadRequestError", "UnprocessableEntityError", "APIStatusError"}
)


def _classify(exc: BaseException) -> str:
    """Bucket an exception into a retry/failover decision.

    Returns ``"rate_limit"``, ``"tool_use_failed"`` or ``"fatal"``. The
    ``__cause__``/``__context__`` chain is walked because both the OpenAI SDK
    and CrewAI wrap transport failures several layers deep.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        status = getattr(current, "status_code", None) or getattr(
            current, "http_status", None
        )

        if name in _RATE_LIMIT_NAMES or status == 429:
            return "rate_limit"

        if name in _BAD_REQUEST_NAMES or status == 400:
            # Only a malformed tool call is worth an in-place retry; any other
            # 400 is a permanent request-shape problem.
            if "tool_use_failed" in str(current):
                return "tool_use_failed"
            return "fatal"

        current = current.__cause__ or current.__context__

    return "fatal"


def _log_failover(route: Route, exc: BaseException | None) -> None:
    """Emit the single warning shown when the primary gives up."""
    detail = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
    logger.warning(
        f"Primary LLM call failed after retries{detail}; failing over to "
        f"{route.provider}/{route.model}."
    )


def _construction_route(instance: Any) -> Optional[Route]:
    """The route this instance was built for, captured at attach time.

    Restoring from *per-call* state is not enough: once a failover has landed,
    the instance is sitting on the fallback, so a later call would snapshot the
    fallback and "restore" to it — permanently pinning the process off the
    primary. The construction-time values are therefore captured once, up
    front, from the instance itself (rather than re-derived from settings,
    which may have changed since).
    """
    model = getattr(instance, "model", None)
    if not model:
        return None
    return Route(
        provider="configured",
        model=model,
        api_key=getattr(instance, "api_key", None),
        base_url=getattr(instance, "base_url", None),
    )


def _restore_primary(instance: Any, configured: Optional[Route]) -> None:
    """Reset an instance back to the route it was constructed with.

    Silently a no-op when the construction-time route is unknown — the caller
    still has a working route to fall back to.
    """
    if configured is None:
        return
    _apply_route(instance, configured)


def _apply_route(instance: Any, route: Route) -> None:
    """Point an existing LLM instance at a route, in place.

    Mutating the instance rather than building a second one keeps CrewAI's
    ``isinstance(self.llm, BaseLLM)`` checks and agent↔LLM identity intact, and
    avoids leaking an extra SDK client per call.
    """
    instance.model = route.model
    instance.base_url = route.base_url
    instance.api_key = route.api_key
    _drop_clients(instance)


def _retarget(instance: Any, route: Route) -> None:
    """Apply ``route`` and log the switch (the visible failover event)."""
    _apply_route(instance, route)
    logger.info(f"Failover engaged: routing to {route.provider}/{route.model}.")


def _drop_clients(instance: Any) -> None:
    """Clear memoised SDK clients so the next call rebuilds them.

    Native providers cache their OpenAI client on a pydantic ``PrivateAttr``.
    Plain assignment is required here — ``object.__setattr__`` writes an
    instance-dict entry that *shadows* the private attr, so the provider's
    ``if self._client is None: rebuild`` check would read the shadowing value
    and hand back ``None`` instead of a client. With plain assignment the
    rebuild happens against the new base_url/api_key.
    """
    for attr in ("_client", "_async_client"):
        if hasattr(instance, attr):
            try:
                setattr(instance, attr, None)
            except Exception:  # pragma: no cover - provider-specific layout
                pass


def _resolve_fallback() -> Optional[Route]:
    """Look up the fallback route at call time, not at patch time.

    Resolving lazily means a key added to the environment after import is
    still honoured.
    """
    try:
        return fallback_route()
    except Exception:  # pragma: no cover - defensive
        return None


def attach_failover(instance: Any) -> Any:
    """Wrap ``instance.call``/``instance.acall`` with retry + failover.

    Returns the same instance, mutated. Idempotent: a second call is a no-op so
    wrappers cannot stack.
    """
    # getattr, not attribute access: a non-pydantic stand-in (as used in the
    # tests) may not have the flag on its class.
    if instance.__dict__.get("_vyuha_failover_attached"):
        return instance

    # Bound methods: they already carry the instance, so the wrappers below
    # must call them without re-passing `self`.
    original_call = instance.call
    original_acall = instance.acall

    # Captured once: every failover must start from the configured primary, not
    # from wherever the previous failover left the instance.
    configured = _construction_route(instance)

    def _sync_call(self: Any, *args: Any, **kwargs: Any) -> Any:
        last_exc: list[BaseException | None] = [None]

        # Every logical call starts from the configured primary. Without this,
        # a second call after a failover would be served by the fallback and
        # the primary would never be retried — a transient outage would
        # permanently pin the pipeline to the backup provider.
        _restore_primary(self, configured)

        def primary() -> Any:
            try:
                return original_call(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last_exc[0] = exc
                raise

        def fallback() -> Any:
            route = _resolve_fallback()
            if route is None:
                # Fallback disappeared between decision and dispatch; surface
                # the primary's error rather than a confusing KeyError.
                if last_exc[0] is not None:
                    raise last_exc[0]
                raise RuntimeError("LLM fallback became unavailable mid-flight")
            _retarget(self, route)
            return original_call(*args, **kwargs)

        route = _resolve_fallback()
        return _drive(primary, fallback if route is not None else None, route, time.sleep)

    async def _async_call(self: Any, *args: Any, **kwargs: Any) -> Any:
        last_exc: list[BaseException | None] = [None]

        _restore_primary(self, configured)

        async def primary() -> Any:
            try:
                return await original_acall(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc[0] = exc
                raise

        async def fallback() -> Any:
            route = _resolve_fallback()
            if route is None:
                if last_exc[0] is not None:
                    raise last_exc[0]
                raise RuntimeError("LLM fallback became unavailable mid-flight")
            _retarget(self, route)
            return await original_acall(*args, **kwargs)

        route = _resolve_fallback()
        return await _drive_async(
            primary, fallback if route is not None else None, route, asyncio.sleep
        )

    instance.call = functools.partial(_sync_call, instance)
    instance.acall = functools.partial(_async_call, instance)
    instance._vyuha_failover_attached = True
    return instance


def _drive(
    primary: Callable[[], Any],
    fallback: Optional[Callable[[], Any]],
    route: Optional[Route],
    sleep: Callable[[float], Any],
) -> Any:
    """Retry loop for the sync path. ``sleep`` is injected so tests need no real delay."""
    last_exc: BaseException | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            return primary()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            kind = _classify(exc)
            has_budget = attempt < MAX_ATTEMPTS - 1

            if kind == "rate_limit" and has_budget:
                logger.warning(
                    f"Rate limit hit (attempt {attempt + 1}/{MAX_ATTEMPTS}). "
                    f"Sleeping {RATE_LIMIT_BACKOFF_S:.0f}s..."
                )
                sleep(RATE_LIMIT_BACKOFF_S)
                continue

            if kind == "tool_use_failed" and has_budget:
                logger.warning(
                    f"tool_use_failed (attempt {attempt + 1}/{MAX_ATTEMPTS}). "
                    f"Retrying in {TOOL_USE_FAILED_BACKOFF_S:.0f}s..."
                )
                sleep(TOOL_USE_FAILED_BACKOFF_S)
                continue

            # Fatal, or out of budget: stop hammering the primary.
            break

    if fallback is not None and route is not None:
        _log_failover(route, last_exc)
        return fallback()

    raise last_exc  # type: ignore[misc]


async def _drive_async(
    primary: Callable[[], Any],
    fallback: Optional[Callable[[], Any]],
    route: Optional[Route],
    sleep: Callable[[float], Any],
) -> Any:
    """Async twin of :func:`_drive`."""
    last_exc: BaseException | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            return await primary()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            kind = _classify(exc)
            has_budget = attempt < MAX_ATTEMPTS - 1

            if kind == "rate_limit" and has_budget:
                logger.warning(
                    f"Rate limit hit (attempt {attempt + 1}/{MAX_ATTEMPTS}). "
                    f"Sleeping {RATE_LIMIT_BACKOFF_S:.0f}s..."
                )
                await sleep(RATE_LIMIT_BACKOFF_S)
                continue

            if kind == "tool_use_failed" and has_budget:
                logger.warning(
                    f"tool_use_failed (attempt {attempt + 1}/{MAX_ATTEMPTS}). "
                    f"Retrying in {TOOL_USE_FAILED_BACKOFF_S:.0f}s..."
                )
                await sleep(TOOL_USE_FAILED_BACKOFF_S)
                continue

            break

    if fallback is not None and route is not None:
        _log_failover(route, last_exc)
        return await fallback()

    raise last_exc  # type: ignore[misc]