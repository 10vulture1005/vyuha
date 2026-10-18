from pydantic import SecretStr


def test_setup_subcommand_dispatches_to_wizard(monkeypatch):
    from scripts import run_vyuha_agent, setup_wizard

    calls = []
    monkeypatch.setattr(run_vyuha_agent.sys, "argv", ["vyuha", "setup"])
    monkeypatch.setattr(setup_wizard, "run_setup", lambda: calls.append("setup"))

    assert run_vyuha_agent.main() == 0
    assert calls == ["setup"]


def test_mistral_provider_auth(monkeypatch):
    from config import llm_routing
    from config.settings import settings

    monkeypatch.setattr(settings, "MISTRAL_API_KEY", SecretStr("mistral-test"), raising=False)
    route = llm_routing._build("mistral", "mistral-large-latest")
    assert route.is_usable
    assert route.base_url == "https://api.mistral.ai/v1"


def test_custom_openai_compatible_endpoint(monkeypatch):
    from config import llm_routing
    from config.settings import settings

    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", SecretStr("custom-test"), raising=False)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "https://llm.example/v1", raising=False)
    route = llm_routing._build("openai_compatible", "my-model")
    assert route.is_usable
    assert route.base_url == "https://llm.example/v1"


def test_provider_cli_does_not_print_secrets(monkeypatch, capsys):
    from config.settings import settings
    from scripts.run_vyuha_agent import build_parser

    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", SecretStr("secret-do-not-print"), raising=False)
    args = build_parser().parse_args(["providers"])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "secret-do-not-print" not in output
    assert "openrouter" in output
