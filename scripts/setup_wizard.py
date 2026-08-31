# scripts/setup_wizard.py
"""Interactive provider + API-key setup for the TradingAgents CLI.

The wizard is launched automatically by ``scripts/run_tradingagents.py``
when ``TRADINGAGENTS_ENABLED=true`` but no provider key is present in the
environment. It can also be invoked explicitly via
``python scripts/run_tradingagents.py setup``.

What it does
------------
1. Asks the user to pick an LLM provider from the canonical
   TradingAgents registry (openai / anthropic / google / xai / groq /
   deepseek / openrouter / qwen / glm / mistral / kimi / ollama /
   openai_compatible).
2. Asks the matching ``deep_think_llm`` and ``quick_think_llm`` model
   names — with sensible defaults so a single Enter confirms both.
3. For providers that require a key, prompts for the key (hidden
   input), validates it is non-empty, then writes everything to the
   project's ``.env`` file using ``python-dotenv``'s ``set_key`` so
   existing keys are preserved.
4. Reloads ``Settings`` in-process so the rest of the CLI run sees
   the new keys without requiring a re-launch.

Exposed as :func:`run_setup` so any entrypoint (the ``setup``
subcommand, the auto-trigger inside ``run_tradingagents.py main``,
or an explicit call from another script) gets the same flow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Resolve repo root (parent of scripts/) so .env loads regardless of CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import find_dotenv, set_key
except ImportError:
    find_dotenv = None  # type: ignore[assignment]
    set_key = None  # type: ignore[assignment]


# ─── Provider catalogue ───────────────────────────────────────────────────────
# Curated list: each entry is (provider_key, display_name, default_deep,
# default_quick, needs_key, help_url). `help_url` is shown so users know
# where to grab a key.
PROVIDERS: list[dict] = [
    {
        "key": "anthropic",
        "name": "Anthropic (Claude)",
        "deep": "claude-sonnet-4-6",
        "quick": "claude-haiku-4-5",
        "needs_key": True,
        "help": "https://console.anthropic.com/",
    },
    {
        "key": "openai",
        "name": "OpenAI (GPT)",
        "deep": "gpt-5.6",
        "quick": "gpt-5.6-luna",
        "needs_key": True,
        "help": "https://platform.openai.com/api-keys",
    },
    {
        "key": "openrouter",
        "name": "OpenRouter (any model, one key)",
        "deep": "anthropic/claude-sonnet-4-6",
        "quick": "anthropic/claude-haiku-4-5",
        "needs_key": True,
        "help": "https://openrouter.ai/keys",
    },
    {
        "key": "groq",
        "name": "Groq (fast LPU inference)",
        "deep": "llama-3.3-70b-versatile",
        "quick": "llama-3.1-8b-instant",
        "needs_key": True,
        "help": "https://console.groq.com/keys",
    },
    {
        "key": "google",
        "name": "Google (Gemini)",
        "deep": "gemini-3.1-pro",
        "quick": "gemini-3.1-flash",
        "needs_key": True,
        "help": "https://aistudio.google.com/apikey",
    },
    {
        "key": "xai",
        "name": "xAI (Grok)",
        "deep": "grok-4-latest",
        "quick": "grok-4-mini",
        "needs_key": True,
        "help": "https://console.x.ai/",
    },
    {
        "key": "deepseek",
        "name": "DeepSeek",
        "deep": "deepseek-chat",
        "quick": "deepseek-chat",
        "needs_key": True,
        "help": "https://platform.deepseek.com/api_keys",
    },
    {
        "key": "mistral",
        "name": "Mistral",
        "deep": "mistral-large-latest",
        "quick": "mistral-small-latest",
        "needs_key": True,
        "help": "https://console.mistral.ai/api-keys/",
    },
    {
        "key": "ollama",
        "name": "Ollama (local, no key)",
        "deep": "llama3.3",
        "quick": "llama3.3",
        "needs_key": False,
        "help": "https://ollama.com/",
    },
    {
        "key": "openai_compatible",
        "name": "OpenAI-compatible (vLLM / LM Studio / llama.cpp)",
        "deep": "custom-model",
        "quick": "custom-model",
        "needs_key": True,
        "help": "Set OPENAI_COMPATIBLE_API_KEY if your endpoint requires one",
    },
]


# Mapping of provider key → env-var name. Mirrors TradingAgents'
# ``llm_clients.api_key_env.PROVIDER_API_KEY_ENV`` so the keys we
# write are exactly what the framework looks for at runtime.
PROVIDER_ENV_VAR: dict[str, Optional[str]] = {
    "openai":     "OPENAI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "xai":        "XAI_API_KEY",
    "deepseek":   "DEEPSEEK_API_KEY",
    "groq":       "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral":    "MISTRAL_API_KEY",
    "kimi":       "MOONSHOT_API_KEY",
    "qwen":       "DASHSCOPE_API_KEY",
    "qwen-cn":    "DASHSCOPE_CN_API_KEY",
    "glm":        "ZHIPU_API_KEY",
    "glm-cn":     "ZHIPU_CN_API_KEY",
    "minimax":    "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "nvidia":     "NVIDIA_API_KEY",
    "azure":      "AZURE_OPENAI_API_KEY",
    "bedrock":    None,  # AWS credential chain
    "ollama":     None,
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}


# ─── Non-interactive helpers (for tests + automation) ─────────────────────────


def _find_env_path() -> Path:
    """Locate the project .env file, creating it if absent."""
    if find_dotenv is not None:
        existing = find_dotenv(usecwd=True)
        if existing:
            return Path(existing)
    env_path = REPO_ROOT / ".env"
    env_path.touch(exist_ok=True)
    return env_path


def _set_env_var(env_path: Path, key: str, value: str) -> None:
    """Write a single key=value to .env, preserving everything else."""
    if set_key is None:
        # Fallback: manual write (no dotenv dep). Preserves existing
        # lines by replacing just the matching ``KEY=...`` line.
        text = env_path.read_text() if env_path.exists() else ""
        lines = text.splitlines()
        prefix = f"{key}="
        found = False
        new_lines = []
        for line in lines:
            if line.startswith(prefix):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        env_path.write_text("\n".join(new_lines) + "\n")
    else:
        set_key(str(env_path), key, value)


def _reload_settings() -> None:
    """Force the ``config.settings.settings`` singleton to re-read .env.

    Pydantic-settings caches loaded values on the singleton instance;
    after writing to .env we need to copy the fresh values back onto
    the same instance so the rest of the CLI run sees the new keys
    without requiring a re-launch.

    Note: ``config.settings`` is the *instance* (named ``settings``),
    shadowing the module of the same name; the class is reachable via
    ``type(config.settings)``. Pydantic v2 disallows bare ``setattr``
    on BaseModel instances, so we use ``object.__setattr__`` to bypass
    the validator.
    """
    import config  # noqa: F401 — ensures the singleton is built

    from config.settings import Settings as SettingsClass

    fresh = SettingsClass()
    instance = config.settings
    for field in fresh.model_fields:
        object.__setattr__(instance, field, getattr(fresh, field))


# ─── Interactive wizard ──────────────────────────────────────────────────────


def _prompt_provider() -> dict:
    """Pick a provider from the catalogue (interactive)."""
    import questionary

    choices = [
        questionary.Choice(
            title=(
                f"{p['name']}"
                + ("" if p['needs_key'] else "  (no key needed)")
            ),
            value=p,
        )
        for p in PROVIDERS
    ]
    selected = questionary.select(
        "Pick your LLM provider:",
        choices=choices,
        style=questionary.Style([
            ("selected", "fg:green noinherit"),
            ("highlighted", "fg:green noinherit"),
            ("pointer", "fg:green noinherit"),
        ]),
        qmark="?",
    ).ask()
    if selected is None:
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(1)
    return selected


def _prompt_model(prompt_text: str, default: str) -> str:
    """Prompt for a model id with a sensible default."""
    import questionary

    raw = questionary.text(
        f"{prompt_text} [{default}]:",
        default=default,
        style=questionary.Style([("text", "fg:cyan")]),
    ).ask()
    if raw is None or not raw.strip():
        print(f"\nSetup cancelled.", file=sys.stderr)
        sys.exit(1)
    return raw.strip()


def _prompt_api_key(env_var: str) -> str:
    """Prompt for an API key (hidden input)."""
    import questionary

    key = questionary.password(
        f"Paste your {env_var}:",
        style=questionary.Style([
            ("text", "fg:cyan"),
            ("highlighted", "noinherit"),
        ]),
        validate=lambda x: len(x.strip()) > 0 or f"{env_var} cannot be empty",
    ).ask()
    if key is None or not key.strip():
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(1)
    return key.strip()


def _print_section(title: str) -> None:
    print()
    print("─" * 70)
    print(f"  {title}")
    print("─" * 70)


# ─── Public entry points ─────────────────────────────────────────────────────


def non_interactive_setup(
    provider: str,
    api_key: Optional[str] = None,
    deep_model: Optional[str] = None,
    quick_model: Optional[str] = None,
) -> dict:
    """Write provider / key / model settings to .env without any prompts.

    Returns the dict that was persisted. Useful for CI / tests /
    scripts that already know the provider and just want to plumb the
    key in without going through the interactive wizard.
    """
    provider_entry = next((p for p in PROVIDERS if p["key"] == provider), None)
    if provider_entry is None:
        raise ValueError(
            f"Unknown provider {provider!r}. Known: "
            + ", ".join(p["key"] for p in PROVIDERS)
        )

    env_path = _find_env_path()
    env_var = PROVIDER_ENV_VAR.get(provider)

    deep_model = deep_model or provider_entry["deep"]
    quick_model = quick_model or provider_entry["quick"]

    _set_env_var(env_path, "TRADINGAGENTS_ENABLED", "true")
    _set_env_var(env_path, "TRADINGAGENTS_LLM_PROVIDER", provider)
    _set_env_var(env_path, "TRADINGAGENTS_DEEP_THINK_LLM", deep_model)
    _set_env_var(env_path, "TRADINGAGENTS_QUICK_THINK_LLM", quick_model)

    if env_var and api_key:
        _set_env_var(env_path, env_var, api_key)
        os.environ[env_var] = api_key

    _reload_settings()

    return {
        "provider": provider,
        "env_var": env_var,
        "deep_model": deep_model,
        "quick_model": quick_model,
        "wrote_key": bool(env_var and api_key),
        "env_path": str(env_path),
    }


def run_setup() -> dict:
    """Run the interactive wizard end-to-end. Returns the persisted config."""
    _print_section("VYUHA × TradingAgents — LLM provider setup")

    provider = _prompt_provider()
    print(f"\nSelected: {provider['name']}")
    if provider["help"] and provider["needs_key"]:
        print(f"  Get a key at: {provider['help']}")

    print()
    deep = _prompt_model("Deep reasoning model", provider["deep"])
    quick = _prompt_model("Fast model", provider["quick"])

    env_var = PROVIDER_ENV_VAR.get(provider["key"])
    api_key: Optional[str] = None
    if env_var is not None and provider["needs_key"]:
        api_key = _prompt_api_key(env_var)

    env_path = _find_env_path()
    _set_env_var(env_path, "TRADINGAGENTS_ENABLED", "true")
    _set_env_var(env_path, "TRADINGAGENTS_LLM_PROVIDER", provider["key"])
    _set_env_var(env_path, "TRADINGAGENTS_DEEP_THINK_LLM", deep)
    _set_env_var(env_path, "TRADINGAGENTS_QUICK_THINK_LLM", quick)
    if env_var and api_key:
        _set_env_var(env_path, env_var, api_key)
        os.environ[env_var] = api_key

    _reload_settings()

    _print_section("Saved")
    print(f"  .env file:      {env_path}")
    print(f"  Provider:       {provider['name']}")
    print(f"  Deep model:     {deep}")
    print(f"  Quick model:    {quick}")
    if env_var and api_key:
        print(f"  API key:        {env_var} ({'*' * 8}…{api_key[-4:]})")
    elif env_var is None:
        print(f"  API key:        not required for {provider['name']}")
    print()
    print("  Run `python scripts/run_tradingagents.py single RELIANCE --full` to try it.")
    print()

    return {
        "provider": provider["key"],
        "env_var": env_var,
        "deep_model": deep,
        "quick_model": quick,
        "wrote_key": bool(env_var and api_key),
        "env_path": str(env_path),
    }


# ─── Auto-trigger helper ─────────────────────────────────────────────────────


def needs_api_key(provider: str) -> bool:
    """Whether ``provider`` requires an API key AND we don't have one set."""
    env_var = PROVIDER_ENV_VAR.get(provider)
    if env_var is None:
        return False
    existing = os.environ.get(env_var) or ""
    if existing and not existing.startswith(("sk-", "test-", "your-", "sk-ant-")) and "placeholder" not in existing:
        return False
    # Treat obvious placeholder values from .env.example as missing.
    if existing in ("", "sk-your-key-here", "sk-ant-your-key-here"):
        return True
    return not existing or "your-" in existing or "placeholder" in existing


def maybe_run_setup(force: bool = False) -> Optional[dict]:
    """Run :func:`run_setup` iff the chosen provider is missing a key.

    Returns the persisted config on a successful run, ``None`` when
    no setup was needed. Bails out silently (with a stderr hint) when
    stdin is not a TTY — that way ``cron`` jobs, CI, and pipe-driven
    callers don't crash on the auto-launch.
    """
    from config import settings

    if not settings.TRADINGAGENTS_ENABLED:
        return None
    provider = settings.TRADINGAGENTS_LLM_PROVIDER
    if not force and not needs_api_key(provider):
        return None
    # Auto-launch requires an interactive terminal. Detect it up front
    # so cron / CI / piped callers don't crash on questionary's
    # terminal probe.
    if not sys.stdin.isatty():
        env_var = PROVIDER_ENV_VAR.get(provider) or "<unknown>"
        print(
            f"\n[!] TRADINGAGENTS is enabled but {env_var} is not set, and stdin is\n"
            f"    not a TTY — cannot launch the interactive setup wizard.\n"
            f"    Either:\n"
            f"      • set {env_var}=... in .env and re-run, OR\n"
            f"      • run `python scripts/run_tradingagents.py setup` from a terminal.",
            file=sys.stderr,
        )
        return None
    return run_setup()


if __name__ == "__main__":
    run_setup()