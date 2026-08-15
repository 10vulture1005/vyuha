# scripts/run_daily_pipeline.py
import sys
import argparse
from datetime import date
from loguru import logger
from crew.flow_runner import run_daily_flow
from notifications.digest_builder import send_daily_digest_execution
from core.capital_allocator import credit_monthly_sip_execution
from config.settings import settings
from core.paper_engine import ForwardTestEngine

def main():
    parser = argparse.ArgumentParser(description="Run VYUHA Daily Execution Pipeline.")
    parser.add_argument("--weekly", action="store_true", help="Run the full weekly fundamental/sentiment refresh before daily tasks.")
    args = parser.parse_args()
    
    logger.info(f"Triggering VYUHA Pipeline. Weekly Refresh: {args.weekly}")
    try:
        today = date.today()
        if today.day == 1:
            credit_monthly_sip_execution(settings.MONTHLY_SIP_AMOUNT)
            logger.info(f"Credited monthly SIP: ₹{settings.MONTHLY_SIP_AMOUNT}")
            
        result = run_daily_flow(is_weekly=args.weekly)
        logger.info("Pipeline executed successfully.")
        
        # Record daily valuation for stats
        try:
            engine = ForwardTestEngine()
            engine._record_daily_valuation()
        except RuntimeError as e:
            # LIVE_TRADING_ENABLED might be True, in which case ForwardTestEngine aborts
            logger.debug(f"Skipping valuation record: {e}")
            
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
