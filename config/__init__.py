# config/__init__.py
"""VYUHA configuration module.

Exports:
    settings  — pydantic-settings singleton (env vars / .env)
    thresholds — dict parsed from thresholds.yaml (strategy knobs)
    tradingagents_config — dict parsed from tradingagents.yaml (LLM knobs)
    BASE_DIR  — project root Path
"""
import yaml
from pathlib import Path
from typing import Any, Dict

from .settings import settings, BASE_DIR


def load_thresholds() -> Dict[str, Any]:
    """Load and return strategy parameters from thresholds.yaml."""
    yaml_path = BASE_DIR / "config" / "thresholds.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"Strategy parameters not found at {yaml_path}")
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_tradingagents_yaml() -> Dict[str, Any]:
    """Load and return TradingAgents LLM configuration from tradingagents.yaml.

    Returns an empty dict (with sensible defaults applied lazily by the
    bridge) when the file is absent — that way vyuha runs without the
    TradingAgents config file.
    """
    yaml_path = BASE_DIR / "config" / "tradingagents.yaml"
    if not yaml_path.exists():
        return {}
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


thresholds = load_thresholds()
tradingagents_config = load_tradingagents_yaml()

__all__ = ["settings", "thresholds", "tradingagents_config", "BASE_DIR"]
