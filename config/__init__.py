# config/__init__.py
"""VYUHA configuration module.

Exports:
    settings  — pydantic-settings singleton (env vars / .env)
    thresholds — dict parsed from thresholds.yaml (strategy knobs)
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


thresholds = load_thresholds()

__all__ = ["settings", "thresholds", "BASE_DIR"]
