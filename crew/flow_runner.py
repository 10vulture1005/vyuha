# crew/flow_runner.py
import datetime
from loguru import logger
from crew.crew_definition import create_crew
from agents.llm_crosscheck_agent import run_cross_check_execution
import litellm

# Monkey-patch litellm to strip non-standard keys and to fail over to the
# configured backup provider.
_original_completion = litellm.completion
_original_acompletion = litellm.acompletion

import time
import asyncio
from litellm.exceptions import RateLimitError, BadRequestError
from config.llm_routing import fallback_route

# Resolve the fallback once at import: routing is static for the life of a
# process, and re-reading it per call would hit pydantic on every completion.
# ``None`` means "no usable fallback — run primary only".
_FALLBACK = fallback_route()


def _sanitize_request(kwargs: dict) -> None:
    """Normalise a chat-completions payload in place.

    Two provider-compatibility fixes, both needed regardless of which
    provider is active:

    * ``cache_breakpoint`` is injected by CrewAI's caching layer but is not
      part of the OpenAI schema; several providers reject the whole request
      when they see it.
    * Some providers demand that ``required`` list every key in
      ``properties``, and reject tool definitions with an empty
      ``properties`` object. CrewAI's no-arg tools hit both cases.
    """
    if "messages" in kwargs:
        for msg in kwargs["messages"]:
            if isinstance(msg, dict) and "cache_breakpoint" in msg:
                del msg["cache_breakpoint"]

    if kwargs.get("tools"):
        for tool in kwargs["tools"]:
            if "function" in tool and "parameters" in tool["function"]:
                params = tool["function"]["parameters"]
                params.setdefault("type", "object")
                # If properties is missing or empty, inject a real param the LLM can fill
                if not params.get("properties"):
                    params["properties"] = {"trigger": {"type": "string", "description": "Pass any value like 'run' to execute."}}
                # `required` must list every key in `properties`
                params["required"] = list(params["properties"].keys())


def _retarget_to_fallback(kwargs: dict) -> dict:
    """Return a copy of ``kwargs`` pointed at the fallback provider.

    Rewrites the three fields that identify the destination — ``model``,
    ``api_key`` and ``base_url`` — and leaves everything else (messages,
    tools, temperature, response format) untouched, so the retry is the
    *same* request just served by the backup.
    """
    retargeted = dict(kwargs)
    retargeted["model"] = f"openai/{_FALLBACK.model}"
    retargeted["api_key"] = _FALLBACK.api_key or ""
    retargeted["base_url"] = _FALLBACK.base_url
    return retargeted


def _log_failover(exc: BaseException | None) -> None:
    """Emit the single warning shown when the primary provider gives up."""
    detail = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
    logger.warning(
        f"Primary LLM call failed after retries{detail}; failing over to "
        f"{_FALLBACK.provider}/{_FALLBACK.model}."
    )


def _patched_completion(*args, **kwargs):
    """Retry the primary provider, then fail over to the fallback.

    Control flow is a single loop that only exits on success or on a
    condition worth stopping for:

    * ``RateLimitError``  — retried in place up to ``retries`` times with a
      15s backoff (preserves the original behaviour).
    * ``tool_use_failed`` — malformed tool-call XML, non-deterministic, so
      retrying in place helps.
    * anything else       (5xx, timeout, connection error, auth, model
      unavailable on the primary) — stop retrying the primary immediately and
      spend the remaining attempts on the fallback instead. Retrying a dead
      provider three times with a 15s sleep only delays the failover.
    """
    _sanitize_request(kwargs)

    retries = 3
    last_exc: BaseException | None = None

    for attempt in range(retries):
        try:
            return _original_completion(*args, **kwargs)
        except RateLimitError as e:
            last_exc = e
            if attempt < retries - 1:
                logger.warning(f"RateLimitError hit (attempt {attempt + 1}/{retries}). Sleeping 15s...")
                time.sleep(15)
        except BadRequestError as e:
            if "tool_use_failed" in str(e) and attempt < retries - 1:
                logger.warning(f"tool_use_failed (attempt {attempt + 1}/{retries}). Retrying in 2s...")
                time.sleep(2)
                continue
            last_exc = e
            break
        except Exception as e:
            last_exc = e
            break

    if _FALLBACK is not None:
        _log_failover(last_exc)
        return _original_completion(*args, **_retarget_to_fallback(kwargs))

    raise last_exc


async def _patched_acompletion(*args, **kwargs):
    """Async twin of :func:`_patched_completion` — same policy, awaitable."""
    _sanitize_request(kwargs)

    retries = 3
    last_exc: BaseException | None = None

    for attempt in range(retries):
        try:
            return await _original_acompletion(*args, **kwargs)
        except RateLimitError as e:
            last_exc = e
            if attempt < retries - 1:
                logger.warning(f"RateLimitError hit (attempt {attempt + 1}/{retries}). Sleeping 15s...")
                await asyncio.sleep(15)
        except BadRequestError as e:
            if "tool_use_failed" in str(e) and attempt < retries - 1:
                logger.warning(f"tool_use_failed (attempt {attempt + 1}/{retries}). Retrying in 2s...")
                await asyncio.sleep(2)
                continue
            last_exc = e
            break
        except Exception as e:
            last_exc = e
            break

    if _FALLBACK is not None:
        _log_failover(last_exc)
        return await _original_acompletion(*args, **_retarget_to_fallback(kwargs))

    raise last_exc


litellm.completion = _patched_completion
litellm.acompletion = _patched_acompletion


def run_daily_flow(is_weekly: bool = False):
    """Executes the CrewAI orchestration pipeline directly without the modern Flow wrapper."""
    logger.info(f"Starting VYUHA Execution. Mode: {'WEEKLY + DAILY' if is_weekly else 'DAILY ONLY'}")

    crew = create_crew(is_weekly=is_weekly)
    result = crew.kickoff()

    logger.info(f"Crew execution finished. Final Output: {result}")

    # Phase 7 — optional Vyuha Agent LLM cross-check. Runs after the
    # rule-based pipeline so we have fresh TechnicalSignal rows in the
    # DB to cross-reference. Never raises — failures are logged so the
    # rule-based pipeline's output remains intact.
    try:
        confirmed = run_cross_check_execution()
        logger.info(f"Phase 7 LLM cross-check confirmed symbols: {confirmed}")
    except Exception as exc:
        logger.exception(f"Phase 7 LLM cross-check failed: {exc}")

    return result