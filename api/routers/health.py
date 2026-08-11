from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, text
from db.session import get_async_session
from db.models import AgentRunLog
from api.schemas import HealthResponse

router = APIRouter(tags=["Observability"])

@router.get("/health", response_model=HealthResponse)
async def health_check(session: AsyncSession = Depends(get_async_session)):
    """Verifies PostgreSQL connectivity and checks last successful quantitative run freshness."""
    try:
        # Verify DB liveness
        await session.execute(text("SELECT 1;"))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unreachable: {e}")
        
    # Check freshness of last successful pipeline run
    stmt = select(AgentRunLog).where(AgentRunLog.status == "SUCCESS").order_by(desc(AgentRunLog.run_date)).limit(1)
    result = await session.execute(stmt)
    last_run = result.scalar_one_or_none()
    
    if not last_run:
        return HealthResponse(status="degraded", database="connected", last_successful_run=None, seconds_since_last_run=None)
        
    now = datetime.now(timezone.utc)
    # Ensuring both are naive or timezone aware
    if last_run.run_date.tzinfo is None:
        run_date_utc = last_run.run_date.replace(tzinfo=timezone.utc)
    else:
        run_date_utc = last_run.run_date

    delta_secs = int((now - run_date_utc).total_seconds())
    
    # Alert status if no successful run in 48 hours (degraded state)
    status = "healthy" if delta_secs < 172800 else "stale_pipeline"
    return HealthResponse(status=status, database="connected", last_successful_run=run_date_utc, seconds_since_last_run=delta_secs)
