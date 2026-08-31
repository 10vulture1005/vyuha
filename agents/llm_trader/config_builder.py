# agents/llm_trader/config_builder.py
"""Build a TradingAgents config dict from vyuha's settings + YAML.

The actual TradingAgents default config lives in
``tradingagents.default_config.DEFAULT_CONFIG``. We copy that, overlay
vyuha's ``config/tradingagents.yaml`` file, and finally override with
whatever is in the ``Settings`` pydantic object — env vars win.

Output is a plain dict that can be passed straight into
``TradingAgentsGraph(config=...)``.

Why three layers?
-----------------
1. DEFAULT_CONFIG — vendor defaults (OpenAI, GPT-5.6, US macro queries).
2. tradingagents.yaml — VYUHA-wide knobs (Indian benchmarks, Nifty 50
   benchmark, RBI macro queries, social-media analyst disabled).
3. Settings — operator overrides from .env / shell (specific provider,
   model names, debug flags). These win so a single operator can
   flip the provider without editing YAML.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from config import settings, tradingagents_config
from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


def _coerce_optional_bool(value: Any) -> bool:
    """Coerce YAML / env bool-ish strings to a real bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _apply_yaml_overlay(config: dict[str, Any], yaml_cfg: dict[str, Any]) -> dict[str, Any]:
    """Apply VYUHA's ``tradingagents.yaml`` keys onto a copy of DEFAULT_CONFIG.

    The YAML is namespaced under a ``tradingagents:`` key so we can
    keep unrelated vyuha YAML files in the same directory without
    colliding.
    """
    if not yaml_cfg:
        return config

    ta_yaml = yaml_cfg.get("tradingagents", {}) or {}
    if not ta_yaml:
        return config

    logger.debug("Applying tradingagents.yaml overlay: %s", list(ta_yaml.keys()))

    if "llm_provider" in ta_yaml:
        config["llm_provider"] = ta_yaml["llm_provider"]
    if "deep_think_llm" in ta_yaml:
        config["deep_think_llm"] = ta_yaml["deep_think_llm"]
    if "quick_think_llm" in ta_yaml:
        config["quick_think_llm"] = ta_yaml["quick_think_llm"]
    if "backend_url" in ta_yaml:
        config["backend_url"] = ta_yaml["backend_url"]
    if "temperature" in ta_yaml:
        config["temperature"] = _coerce_optional_float(ta_yaml["temperature"])
    if "max_tokens" in ta_yaml:
        config["max_tokens"] = _coerce_optional_int(ta_yaml["max_tokens"])
    if "llm_max_retries" in ta_yaml:
        config["llm_max_retries"] = _coerce_optional_int(ta_yaml["llm_max_retries"])
    if "output_language" in ta_yaml:
        config["output_language"] = ta_yaml["output_language"]
    if "max_debate_rounds" in ta_yaml:
        config["max_debate_rounds"] = int(ta_yaml["max_debate_rounds"])
    if "max_risk_discuss_rounds" in ta_yaml:
        config["max_risk_discuss_rounds"] = int(ta_yaml["max_risk_discuss_rounds"])
    if "max_recur_limit" in ta_yaml:
        config["max_recur_limit"] = int(ta_yaml["max_recur_limit"])
    if "checkpoint_enabled" in ta_yaml:
        config["checkpoint_enabled"] = _coerce_optional_bool(ta_yaml["checkpoint_enabled"])
    if "news_article_limit" in ta_yaml:
        config["news_article_limit"] = int(ta_yaml["news_article_limit"])
    if "global_news_article_limit" in ta_yaml:
        config["global_news_article_limit"] = int(ta_yaml["global_news_article_limit"])
    if "global_news_lookback_days" in ta_yaml:
        config["global_news_lookback_days"] = int(ta_yaml["global_news_lookback_days"])
    if "global_news_queries" in ta_yaml:
        config["global_news_queries"] = list(ta_yaml["global_news_queries"])

    # Data vendor overrides — narrow to yfinance-only for vyuha.
    if "data_vendors" in ta_yaml:
        merged = dict(config.get("data_vendors") or {})
        merged.update(ta_yaml["data_vendors"])
        # Drop null vendors (VYUHA doesn't run FRED/Polymarket by default).
        merged = {k: v for k, v in merged.items() if v}
        config["data_vendors"] = merged

    if "benchmark_ticker" in ta_yaml:
        config["benchmark_ticker"] = ta_yaml["benchmark_ticker"]
    if "benchmark_map" in ta_yaml:
        merged_bm = dict(config.get("benchmark_map") or {})
        merged_bm.update(ta_yaml["benchmark_map"])
        config["benchmark_map"] = merged_bm

    return config


def _apply_settings_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Apply operator-level overrides from the pydantic Settings object."""
    logger.debug(
        "Applying Settings overrides: provider=%s, deep=%s, quick=%s",
        settings.TRADINGAGENTS_LLM_PROVIDER,
        settings.TRADINGAGENTS_DEEP_THINK_LLM,
        settings.TRADINGAGENTS_QUICK_THINK_LLM,
    )

    config["llm_provider"] = settings.TRADINGAGENTS_LLM_PROVIDER
    config["deep_think_llm"] = settings.TRADINGAGENTS_DEEP_THINK_LLM
    config["quick_think_llm"] = settings.TRADINGAGENTS_QUICK_THINK_LLM
    config["max_debate_rounds"] = settings.TRADINGAGENTS_MAX_DEBATE_ROUNDS
    config["max_risk_discuss_rounds"] = settings.TRADINGAGENTS_MAX_RISK_ROUNDS
    config["output_language"] = settings.TRADINGAGENTS_OUTPUT_LANGUAGE
    config["checkpoint_enabled"] = settings.TRADINGAGENTS_CHECKPOINT_ENABLED

    if settings.TRADINGAGENTS_DATA_VENDORS:
        # Comma-separated string from .env; split into a single fallback
        # chain that yfinance/alpha_vantage vendors can iterate.
        vendors = [v.strip() for v in settings.TRADINGAGENTS_DATA_VENDORS.split(",") if v.strip()]
        # Re-apply across every category (vyuha doesn't need category-level
        # specialisation — pick whatever's first).
        first = vendors[0]
        config["data_vendors"] = {
            "core_stock_apis": ",".join(vendors),
            "technical_indicators": first,
            "fundamental_data": first,
            "news_data": first,
            "macro_data": None,
            "prediction_markets": None,
        }

    return config


def build_tradingagents_config() -> dict[str, Any]:
    """Return a fully-merged config dict ready for ``TradingAgentsGraph``.

    Layering order (later wins):
        DEFAULT_CONFIG (vendor)
      → config/tradingagents.yaml (VYUHA defaults)
      → Settings.TRADINGAGENTS_* env vars (operator overrides)
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    config = _apply_yaml_overlay(config, tradingagents_config)
    config = _apply_settings_overrides(config)

    # Selected analyst team lives outside the TradingAgents config dict
    # (it's passed to TradingAgentsGraph.__init__ directly). Stash the
    # VYUHA default so the bridge can use it without re-reading YAML.
    #
    # NB: the upstream framework keeps the wire key ``social`` for
    # back-compat even though the user-facing label was renamed to
    # "Sentiment Analyst" in v0.2.5. ``sentiment`` is rejected as an
    # unknown key (see ANALYST_NODE_SPECS in
    # tradingagents/graph/analyst_execution.py), so we map any
    # user-friendly YAML alias back to ``social``.
    selected = None
    ta_yaml = (tradingagents_config or {}).get("tradingagents", {}) or {}
    if "selected_analysts" in ta_yaml:
        selected = [
            "social" if a.lower() in ("sentiment", "social") else a
            for a in ta_yaml["selected_analysts"]
        ]
    config["_vyuha_selected_analysts"] = selected or (
        "market", "fundamentals", "news", "social",
    )

    return config