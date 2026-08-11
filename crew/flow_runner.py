# crew/flow_runner.py
import datetime
from loguru import logger
from crew.crew_definition import create_crew

def run_daily_flow(is_weekly: bool = False):
    """Executes the CrewAI orchestration pipeline directly without the modern Flow wrapper."""
    logger.info(f"Starting VYUHA Execution. Mode: {'WEEKLY + DAILY' if is_weekly else 'DAILY ONLY'}")
    
    crew = create_crew(is_weekly=is_weekly)
    result = crew.kickoff()
    
    logger.info(f"Crew execution finished. Final Output: {result}")
    return result
