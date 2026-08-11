# scripts/run_daily_pipeline.py
import sys
import argparse
from loguru import logger
from crew.flow_runner import run_daily_flow
from notifications.digest_builder import send_daily_digest_execution

def main():
    parser = argparse.ArgumentParser(description="Run VYUHA Daily Execution Pipeline.")
    parser.add_argument("--weekly", action="store_true", help="Run the full weekly fundamental/sentiment refresh before daily tasks.")
    args = parser.parse_args()
    
    logger.info(f"Triggering VYUHA Pipeline. Weekly Refresh: {args.weekly}")
    try:
        result = run_daily_flow(is_weekly=args.weekly)
        logger.info("Pipeline executed successfully.")
        logger.info(f"Final Decision Rationale: {result}")
        import json
        try:
            parsed_result = json.loads(result) if isinstance(result, str) else result
        except json.JSONDecodeError:
            parsed_result = {"action": "UNKNOWN", "rationale": str(result)}
        
        send_daily_digest_execution(parsed_result)
    except Exception as e:
        logger.exception(f"Fatal error during pipeline execution: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
