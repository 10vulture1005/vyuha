# scripts/cli_ui.py
"""Interactive prompt helpers for run_tradingagents.py.

Centralises every questionary call so the CLI can offer a guided
setup-and-run experience even when the user doesn't pass any flags.
The module never raises on user cancel — a Ctrl-C / empty input
falls back to a sensible default or exits cleanly with code 0, so a
cron-driven caller that mistakenly invokes the CLI without args
doesn't get an ugly traceback.

Public surface
--------------
  run_first_time_wizard()       full guided flow: provider -> model
                                -> key -> save -> offer to run
  prompt_main_menu()            loop: pick an action, run it, repeat
  prompt_single_args()          ticker / date / full -> argparse.Namespace
  prompt_yes_no(question, default)
  prompt_press_any_key(message)
  print_banner()
  print_section(title)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

# Lazy imports — keep questionary optional so the CLI still works (with
# the --no-setup flag) on minimal installs that don't have it.
def _q():
    import questionary
    from questionary import Choice
    return questionary, Choice


_STYLE = [
    ("qmark", "fg:#00ffcc bold"),
    ("question", "fg:#ffffff bold"),
    ("selected", "fg:#00ffcc bold noinherit"),
    ("highlighted", "fg:#00ffcc bold noinherit"),
    ("pointer", "fg:#00ffcc bold"),
    ("text", "fg:#cccccc"),
    ("answer", "fg:#00ffcc bold"),
]


def _style_kwargs():
    return {"style": _STYLE}


# ─── Output helpers ──────────────────────────────────────────────────────────


def print_banner() -> None:
    """Print the VYUHA × TradingAgents ASCII banner."""
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║                                                                      ║")
    print("║   🔱  VYUHA × TradingAgents  —  LLM multi-agent research              ║")
    print("║                                                                      ║")
    print("║   Multi-agent LangGraph pipeline for Indian mid-cap equities.         ║")
    print("║   4 analysts → bull/bear debate → trader → risk → portfolio manager.  ║")
    print("║                                                                      ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()


def print_section(title: str) -> None:
    bar = "─" * 70
    print()
    print(bar)
    print(f"  {title}")
    print(bar)


def print_info(msg: str) -> None:
    print(f"  ℹ  {msg}")


def print_success(msg: str) -> None:
    print(f"  ✓  {msg}")


def print_warning(msg: str) -> None:
    print(f"  ⚠  {msg}")


def print_error(msg: str) -> None:
    print(f"  ✗  {msg}", file=sys.stderr)


def prompt_press_any_key(message: str = "Press Enter to continue…") -> None:
    questionary, _ = _q()
    try:
        questionary.press_any_key_to_continue(
            message=message,
        ).ask()
    except Exception:
        # Fall back to plain input if press_any_key isn't available.
        try:
            input(f"\n{message}")
        except EOFError:
            pass


def prompt_yes_no(question: str, default: bool = True) -> bool:
    questionary, _ = _q()
    try:
        ans = questionary.confirm(
            question,
            default=default,
            **_style_kwargs(),
        ).ask()
    except Exception:
        return default
    if ans is None:
        return default
    return bool(ans)


# ─── Interactive single-ticker prompts ───────────────────────────────────────


def prompt_ticker(question: str = "Ticker symbol (e.g. RELIANCE, TATASTEEL.BO):") -> Optional[str]:
    """Prompt for a ticker with light validation.

    Bare symbols get ``.NS`` appended by the bridge layer; this prompt
    just accepts whatever the user types. Empty input / Ctrl-C returns
    None so the caller can bail.
    """
    questionary, _ = _q()
    raw = questionary.text(
        question,
        validate=lambda x: (
            len(x.strip()) > 0 or "Please enter a ticker (e.g. RELIANCE)."
        ),
        **_style_kwargs(),
    ).ask()
    if raw is None:
        return None
    return raw.strip().upper()


def prompt_date(question: str = "Analysis date (YYYY-MM-DD, blank = today):") -> Optional[str]:
    """Prompt for an ISO date. Blank means today (returned as '')."""
    questionary, _ = _q()
    raw = questionary.text(
        question,
        default="",
        validate=lambda x: (
            x.strip() == "" or _is_iso_date(x.strip()) or
            "Please use YYYY-MM-DD or leave blank for today."
        ),
        **_style_kwargs(),
    ).ask()
    if raw is None:
        return None
    return raw.strip()


def _is_iso_date(s: str) -> bool:
    from datetime import datetime
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def prompt_yes_no_default(question: str, default_yes: bool = True) -> bool:
    """Same as prompt_yes_no — explicit name for clarity at call sites."""
    return prompt_yes_no(question, default=default_yes)


def prompt_single_args() -> Optional[argparse.Namespace]:
    """Walk the user through a `single <ticker>` invocation.

    Returns a fully-formed ``argparse.Namespace`` ready to feed into
    ``cmd_single``, or ``None`` if the user cancelled.
    """
    print_section("Run pipeline for a single ticker")
    ticker = prompt_ticker()
    if ticker is None:
        return None
    iso_date = prompt_date()
    if iso_date is None:
        return None
    full = prompt_yes_no("Print every agent's full report?", default=False)

    args = argparse.Namespace(
        ticker=ticker,
        date=iso_date,
        full=full,
        no_setup=True,
        setup=False,
    )
    return args


def prompt_portfolio_args() -> Optional[argparse.Namespace]:
    """Walk the user through a `portfolio` invocation."""
    print_section("Cross-check every watchlist symbol")
    iso_date = prompt_date("Analysis date (blank = today):")
    if iso_date is None:
        return None
    only_today = prompt_yes_no(
        "Only symbols with fresh signals on the chosen date?",
        default=False,
    )

    args = argparse.Namespace(
        date=iso_date,
        trade_date_only=only_today,
        no_setup=True,
        setup=False,
    )
    return args


# ─── Main menu loop ──────────────────────────────────────────────────────────


def prompt_main_menu() -> Optional[str]:
    """Show the main menu, return the user's choice or None on cancel.

    Choices:
      single     run the pipeline for one ticker (interactive prompts)
      portfolio  cross-check every watchlist symbol
      resolve    backfill realised outcomes
      setup      rotate provider / re-paste API key
      cli        launch upstream rich interactive UI
      exit       leave the program
    """
    questionary, Choice = _q()
    choices = [
        Choice("🔍  Run for a single ticker", value="single"),
        Choice("📊  Cross-check my watchlist (portfolio)", value="portfolio"),
        Choice("📈  Backfill realised outcomes (resolve)", value="resolve"),
        Choice("⚙   Change provider / API key (setup)", value="setup"),
        Choice("🎨  Launch upstream rich CLI (cli)", value="cli"),
        Choice("🚪  Exit", value="exit"),
    ]
    try:
        choice = questionary.select(
            "What would you like to do?",
            choices=choices,
            qmark="▸",
            **_style_kwargs(),
        ).ask()
    except Exception:
        return None
    return choice


def run_main_loop() -> int:
    """Top-level interactive loop. Returns process exit code.

    Assumes the user already has a working setup (or has just finished
    ``run_setup``). Shows the dashboard + main menu, executes whatever
    the user picks, then asks whether to run another action.
    """
    from scripts.setup_wizard import (
        run_setup,
    )
    from config import settings

    print_banner()
    _print_dashboard()
    print_success("Ready.")
    prompt_press_any_key("Press Enter to open the main menu…")

    while True:
        print()
        choice = prompt_main_menu()
        if choice is None or choice == "exit":
            print()
            print_info("Goodbye.")
            return 0
        try:
            if choice == "setup":
                run_setup()
                _print_dashboard()
            elif choice == "single":
                args = prompt_single_args()
                if args is None:
                    continue
                from scripts.run_tradingagents import cmd_single
                cmd_single(args)
            elif choice == "portfolio":
                args = prompt_portfolio_args()
                if args is None:
                    continue
                from scripts.run_tradingagents import cmd_portfolio
                cmd_portfolio(args)
            elif choice == "resolve":
                from scripts.run_tradingagents import cmd_resolve
                args = argparse.Namespace(holding_days=None, no_setup=True, setup=False)
                cmd_resolve(args)
            elif choice == "cli":
                from scripts.run_tradingagents import cmd_cli_ux
                args = argparse.Namespace(no_setup=True, setup=False)
                cmd_cli_ux(args)
        except KeyboardInterrupt:
            print()
            print_warning("Interrupted. Returning to menu.")
            continue
        except Exception as exc:
            print_error(f"Action failed: {exc}")
            if not prompt_yes_no("Try another action?", default=True):
                return 1
            continue

        if not prompt_yes_no("\nRun another action?", default=True):
            return 0


def _print_dashboard() -> None:
    """Render the at-a-glance status block shown before the menu."""
    from config import settings
    from scripts.setup_wizard import needs_api_key

    print_info(f"Project root:    {Path.cwd()}")
    print_info(f"Database:        {settings.DATABASE_URL}")
    print_info(f"TradingAgents:   {'enabled' if settings.TRADINGAGENTS_ENABLED else 'disabled'}")
    if settings.TRADINGAGENTS_ENABLED:
        provider = settings.TRADINGAGENTS_LLM_PROVIDER
        env_key_set = not needs_api_key(provider)
        marker = "✓" if env_key_set else "✗"
        print_info(f"LLM provider:    {provider} (key {marker})")
    print()


def run_first_time_wizard() -> int:
    """Entry point for users who've never run the CLI before.

    Same as ``run_main_loop`` but the welcome message is tuned for
    first-timers. The setup wizard is offered only when the provider
    is unconfigured — if the user already has a working provider/key,
    the welcome screen just says so and drops straight into the menu.
    """
    from scripts.setup_wizard import (
        maybe_run_setup,
        needs_api_key,
        run_setup,
    )
    from config import settings

    print_banner()
    print_info("Welcome! Let's get you set up to run the LLM research pipeline.")
    print()

    # Case A: TRADINGAGENTS is off entirely — offer to turn it on and
    # configure together.
    if not settings.TRADINGAGENTS_ENABLED:
        print_warning("TradingAgents is disabled in your .env (TRADINGAGENTS_ENABLED=false).")
        if prompt_yes_no("Enable it and run the setup wizard now?", default=True):
            from scripts.setup_wizard import _set_env_var, _find_env_path
            env_path = _find_env_path()
            _set_env_var(env_path, "TRADINGAGENTS_ENABLED", "true")
            os.environ["TRADINGAGENTS_ENABLED"] = "true"
            from config.settings import Settings as SettingsClass
            import config
            fresh = SettingsClass()
            for field in fresh.model_fields:
                object.__setattr__(config.settings, field, getattr(fresh, field))
            run_setup()
        else:
            print_info(
                "Skipped. Re-run with --setup whenever you're ready."
            )
            return 0

    # Case B: TRADINGAGENTS is on but no real key is configured.
    elif needs_api_key(settings.TRADINGAGENTS_LLM_PROVIDER):
        print_warning(
            f"No API key set for {settings.TRADINGAGENTS_LLM_PROVIDER}."
        )
        if prompt_yes_no("Run the setup wizard now?", default=True):
            run_setup()
        else:
            print_info("Skipped. Re-run with --setup to configure later.")
            return 0

    # Case C: Already configured — just announce and continue.
    else:
        print_success(
            f"You're already configured for {settings.TRADINGAGENTS_LLM_PROVIDER}."
        )

    if prompt_yes_no(
        "Open the main menu (run a single ticker, cross-check watchlist, …)?",
        default=True,
    ):
        return run_main_loop()
    return 0