# crew/flow_runner.py
"""Entry point for the daily / weekly CrewAI pipeline.

Note on LLM retry + failover
----------------------------
This module used to monkey-patch ``litellm.completion`` / ``acompletion`` to
add retry-on-429 and provider failover. That patch is **gone**, and was dead
code besides: CrewAI 1.15 resolves ``openai/...`` to a native provider SDK
(``crewai.llms.providers.openai.completion.OpenAICompletion``) that never
calls into LiteLLM, so the patch never fired. On 2026-10-16 that cost a full
CI run — the primary provider was unusable and nothing failed over.

The behaviour now lives in :mod:`crew.llm_failover`, attached to each ``LLM``
instance at construction time in :func:`crew.crew_definition._build_llm`. It
hooks ``BaseLLM.call``/``acall``, which CrewAI *does* invoke.

The LiteLLM tuning at the top of this module is retained for the other
LiteLLM call sites in the codebase; it has no effect on the native provider
path.
"""
import litellm

# LiteLLM is still used elsewhere (Vyuha Agent tooling), and dropping
# unsupported params keeps its own call sites working.
litellm.drop_params = True

# Optional: Monkey-patch CrewAI's cache_breakpoint directly to prevent the injection
import crewai.llm as _crewai_llm
if hasattr(_crewai_llm, "cache"):
    _crewai_llm.cache.mark_cache_breakpoint = lambda msg: msg

from loguru import logger
from crew.crew_definition import create_crew
from agents.llm_crosscheck_agent import run_cross_check_execution


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