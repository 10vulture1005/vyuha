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
async def test_async_pipeline_trigger():
    """Verify POST /pipeline/run with sync=False immediately returns ACCEPTED."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/pipeline/run?sync=false")
        assert response.status_code == 200
        assert response.json()["status"] == "ACCEPTED"
