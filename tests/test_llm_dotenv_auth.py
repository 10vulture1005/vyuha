from pydantic import SecretStr


def test_openai_compatible_client_uses_settings_loaded_dotenv_key(monkeypatch):
    from config.settings import settings
    from tradingagents.llm_clients.openai_client import OpenAIClient

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", SecretStr("sk-or-test-auth-key"), raising=False)

    client = OpenAIClient(
        model="nvidia/nemotron-test",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    ).get_llm()

    # Constructing the client exercises auth resolution without network access.
    assert client.openai_api_key.get_secret_value() == "sk-or-test-auth-key"
