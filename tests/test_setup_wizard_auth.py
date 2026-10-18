from pathlib import Path

import pytest
from dotenv import dotenv_values


def test_key_save_round_trip(monkeypatch, tmp_path):
    from scripts import setup_wizard

    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=old-key\nUNRELATED=keep\n")
    key = "sk-or-v1-test-replacement"
    setup_wizard._set_env_var(env_file, "OPENROUTER_API_KEY", key)

    saved = dotenv_values(env_file)
    assert saved["OPENROUTER_API_KEY"] == key
    assert saved["UNRELATED"] == "keep"


def test_key_save_detects_silent_write_failure(monkeypatch, tmp_path):
    from scripts import setup_wizard

    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=old-key\n")
    monkeypatch.setattr(setup_wizard, "set_key", lambda *args: (True, args[1], args[2]))
    with pytest.raises(RuntimeError, match="Could not verify OPENROUTER_API_KEY"):
        setup_wizard._set_env_var(env_file, "OPENROUTER_API_KEY", "new-secret-key")


def test_needs_api_key_reads_saved_dotenv_without_exposing_value(monkeypatch, tmp_path):
    from scripts import setup_wizard

    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=sk-or-v1-private\n")
    monkeypatch.setattr(setup_wizard, "_find_env_path", lambda: env_file)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert setup_wizard.needs_api_key("openrouter") is False


def test_needs_api_key_reports_missing_or_invalid_dotenv_key(monkeypatch, tmp_path):
    from scripts import setup_wizard

    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=masked…value\n")
    monkeypatch.setattr(setup_wizard, "_find_env_path", lambda: env_file)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert setup_wizard.needs_api_key("openrouter") is True


def test_openrouter_rejects_sk_live_before_writing(monkeypatch, tmp_path):
    from scripts import setup_wizard

    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY=existing\n")
    monkeypatch.setattr(setup_wizard, "_find_env_path", lambda: env_file)
    with pytest.raises(ValueError, match="sk-or-v1-"):
        setup_wizard.non_interactive_setup("openrouter", api_key="sk-live-test-not-real")
    assert env_file.read_text() == "OPENROUTER_API_KEY=existing\n"


def test_openrouter_key_format_validation():
    from scripts.setup_wizard import _validate_provider_api_key

    _validate_provider_api_key("OPENROUTER_API_KEY", "sk-or-v1-test-not-real")
    _validate_provider_api_key("OPENAI_COMPATIBLE_API_KEY", "sk-live-test-not-real")


def test_api_key_validator_rejects_unicode_and_masked_values():
    from scripts.setup_wizard import _is_usable_api_key

    assert not _is_usable_api_key("sk-or-…-key")
    assert not _is_usable_api_key("**************")
    assert _is_usable_api_key("sk-or-v1-validascii123")
