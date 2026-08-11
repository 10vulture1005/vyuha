# scripts/credit_sip.py
import sys
from loguru import logger
from core.capital_allocator import credit_monthly_sip_execution

if __name__ == "__main__":
    try:
        new_bal = credit_monthly_sip_execution()
        logger.info(f"Monthly SIP credit successful. New balance: ₹{new_bal}")
    except Exception as e:
        logger.exception(f"Fatal error during SIP credit: {e}")
        sys.exit(1)
