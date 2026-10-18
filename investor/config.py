# investor/config.py
"""Typed access to the `investor:` block of config/thresholds.yaml.

The YAML block is the single authority for Investor Mode thresholds (per the
design brief). This module wraps it so a missing key surfaces as a clear error
at import time rather than as a silent `None` deep inside a backtest run three
minutes in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from config import load_thresholds, thresholds
from config.settings import settings


def _require(block: Dict[str, Any], path: str) -> Dict[str, Any]:
    """Fetches a nested config block, failing loudly if the key is absent."""
    cur: Any = block
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(
                f"thresholds.yaml is missing required investor config: '{path}' "
                f"(investor mode cannot run without it)"
            )
        cur = cur[part]
    if not isinstance(cur, dict):
        raise TypeError(f"investor config '{path}' must be a mapping, got {type(cur)}")
    return cur


@dataclass(frozen=True)
class InvestorConfig:
    """Immutable view over the investor thresholds block."""

    raw: Dict[str, Any] = field(default_factory=dict)

    # ── section accessors ──────────────────────────────────────────────────
    @property
    def regime(self) -> Dict[str, Any]:
        return self.raw["regime"]

    @property
    def fundamentals(self) -> Dict[str, Any]:
        return self.raw["fundamentals"]

    @property
    def technical(self) -> Dict[str, Any]:
        return self.raw["technical"]

    @property
    def news(self) -> Dict[str, Any]:
        return self.raw["news"]

    @property
    def portfolio(self) -> Dict[str, Any]:
        return self.raw["portfolio"]

    @property
    def costs(self) -> Dict[str, Any]:
        return self.raw["costs"]

    @property
    def tax(self) -> Dict[str, Any]:
        return self.raw["tax"]

    @property
    def backtest(self) -> Dict[str, Any]:
        return self.raw["backtest"]

    # ── frequently used scalars, resolved once ─────────────────────────────
    @property
    def exposure(self) -> Dict[str, float]:
        return self.regime["exposure"]

    @property
    def defensive(self) -> Dict[str, Any]:
        return self.regime["defensive"]

    @property
    def score_weights(self) -> Dict[str, float]:
        return self.fundamentals["score_weights"]

    @property
    def non_financial_filters(self) -> Dict[str, Any]:
        return self.fundamentals["non_financial"]

    @property
    def financial_filters(self) -> Dict[str, Any]:
        return self.fundamentals["financial"]

    @property
    def defensive_symbols(self) -> list[str]:
        raw = settings.INVESTOR_DEFENSIVE_SYMBOLS or ""
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
        return syms

    @property
    def is_investor_mode(self) -> bool:
        return settings.STRATEGY_MODE == "investor"


def _load() -> InvestorConfig:
    block = thresholds.get("investor")
    if not block:
        raise KeyError(
            "config/thresholds.yaml has no `investor:` section. Investor Mode "
            "requires it; re-add the block or run with STRATEGY_MODE=swing."
        )
    return InvestorConfig(raw=block)


block = thresholds.get("investor") or {}
if not block:
    raise KeyError(
        "config/thresholds.yaml has no `investor:` section. Investor Mode "
        "requires it; re-add the block or run with STRATEGY_MODE=swing."
    )


def _validate(cfg: InvestorConfig) -> InvestorConfig:
    """Touches every section so a typo fails here, not mid-backtest."""
    for accessor in (
        "regime", "fundamentals", "technical", "news",
        "portfolio", "costs", "tax", "backtest",
    ):
        getattr(cfg, accessor)
    return cfg


investor_config = _validate(InvestorConfig(raw=block))


def reload_investor_config() -> InvestorConfig:
    """Re-reads thresholds from disk, mutating the live config IN PLACE.

    In-place mutation is deliberate and necessary. Every layer does
    `from investor.config import investor_config`, binding the OBJECT at import
    time; rebinding the module global to a new `InvestorConfig` would leave
    those modules reading the old thresholds forever. That silently turns the
    robustness sweep's +/-20% threshold perturbations into no-ops that report
    a fake "robust" verdict.

    Returns the same object, so callers can chain.
    """
    fresh_all = load_thresholds()
    fresh = fresh_all.get("investor")
    if not fresh:
        raise KeyError(
            "config/thresholds.yaml lost its `investor:` section on reload; "
            "refusing to run with empty Investor Mode thresholds."
        )
    investor_config.raw.clear()
    investor_config.raw.update(fresh)
    return _validate(investor_config)


__all__ = ["InvestorConfig", "investor_config", "reload_investor_config", "_require"]