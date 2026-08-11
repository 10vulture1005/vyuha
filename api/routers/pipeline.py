import json
from fastapi import APIRouter, BackgroundTasks, HTTPException
from crew.flow_runner import run_daily_flow
from api.schemas import PipelineRunResponse

router = APIRouter(prefix="/pipeline", tags=["Pipeline Execution"])

@router.post("/run", response_model=PipelineRunResponse)
async def trigger_pipeline_run(background_tasks: BackgroundTasks, sync: bool = True):
    """
    Triggers the quantitative CrewAI Flow.
    If sync=True, blocks until execution completes and returns the executive decision.
    If sync=False, spawns background task and returns immediate acknowledgment.
    """
    if not sync:
        def bg_run():
            run_daily_flow(is_weekly=False)
        background_tasks.add_task(bg_run)
        return PipelineRunResponse(
            status="ACCEPTED", execution_mode="BACKGROUND",
            decision_summary={"message": "Pipeline spawned in background task."}
        )
        
    try:
        decision = run_daily_flow(is_weekly=False)
        
        # Ensure decision is a dict
        if isinstance(decision, str):
            try:
                decision = json.loads(decision)
            except json.JSONDecodeError:
                decision = {"action": "UNKNOWN", "rationale": decision}
                
        return PipelineRunResponse(
            status="SUCCESS", execution_mode="SYNCHRONOUS", decision_summary=decision
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline run failed: {e}")
