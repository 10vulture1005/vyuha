import pytest
from httpx import AsyncClient, ASGITransport
from api.main import app

@pytest.mark.asyncio
async def test_health_endpoint():
    """Verify /health returns 200 OK with database connection status."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code in (200, 503)
        data = response.json()
        assert "database" in data

@pytest.mark.asyncio
async def test_portfolio_holdings_endpoint():
    """Verify /portfolio/holdings returns valid JSON list."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/portfolio/holdings")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

@pytest.mark.asyncio
async def test_async_pipeline_trigger(monkeypatch):
    """Verify POST /pipeline/run with sync=False returns ACCEPTED.

    ``run_daily_flow`` is stubbed because ASGITransport executes FastAPI
    BackgroundTasks inline, before the response returns. Unstubbed, this test
    booted the full CrewAI crew and made live OpenRouter calls — which is why
    it intermittently failed with a 429 rate-limit error. The endpoint's
    contract is the response shape and the fact that the run is deferred, not
    the LLM's behaviour.
    """
    calls = []

    def fake_run_daily_flow(is_weekly: bool = False):
        calls.append(is_weekly)
        return {"action": "HOLD", "rationale": "stubbed"}

    monkeypatch.setattr("api.routers.pipeline.run_daily_flow", fake_run_daily_flow)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/pipeline/run?sync=false")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACCEPTED"
    assert body["execution_mode"] == "BACKGROUND"
    # The background task did run (inline, under ASGITransport) and used the
    # stub rather than the real crew.
    assert calls == [False]
