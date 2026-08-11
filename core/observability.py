# core/observability.py
"""Enterprise-grade observability, logging, and failure recovery for VYUHA.

Provides:
    - setup_logging(): Configures Loguru for stdout + daily rotating compressed disk logs
    - EmptyResultAlarm: Exception for silent scraper breakages
    - trigger_soft_failure_alarm(): Dispatches Telegram emergency alerts
    - @log_agent_run: Universal decorator for agent entrypoints that measures
      latency, traps exceptions, validates result counts, and writes atomic
      audit records to the agent_run_log table
"""
import sys
import time
from functools import wraps
from datetime import datetime, timezone
from typing import Callable, Any
from loguru import logger
from config.settings import settings, BASE_DIR

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging():
    """Configures Loguru for stdout formatting and daily rotating compressed disk logs."""
    logger.remove()

    # 1. Console Output — colored, structured format
    logger.add(
        sys.stdout,
        level=settings.LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )

    # 2. Disk Rotating Logs (Daily rotation, 30-day retention, zip compression)
    logger.add(
        LOG_DIR / "vyuha_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        compression="zip",
        level="DEBUG",
        encoding="utf-8",
    )
    logger.info("Structured Loguru logging framework initialized.")


class EmptyResultAlarm(Exception):
    """Raised when an agent returns an impossibly empty result, indicating scraper breakage.

    This exception prevents downstream pipeline stages from operating on
    falsely empty data (e.g., an empty watchlist causing the allocator to
    silently hold cash indefinitely).
    """

    pass


def trigger_soft_failure_alarm(agent_name: str, reason: str):
    """Sends an emergency Telegram alert and logs a critical operational warning.

    Called automatically by @log_agent_run when result length falls below
    min_expected_results.
    """
    alert_msg = (
        f"⚠️ *VYUHA OPERATIONAL ALARM: SOFT FAILURE*\n\n"
        f"🤖 *Agent:* `{agent_name}`\n"
        f"🚨 *Reason:* {reason}\n\n"
        f"⏸️ *Action Taken:* Halting downstream database mutations for this "
        f"pipeline stage. Retaining previous valid state to prevent false "
        f"cash-holding or blind buying."
    )
    logger.critical(f"[{agent_name}] {reason}")
    try:
        from notifications.telegram_bot import send_message

        send_message(alert_msg, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Failed to dispatch Telegram soft-failure alarm: {e}")


def log_agent_run(agent_name: str, min_expected_results: int = 0):
    """Universal decorator wrapping agent entrypoints for observability.

    Capabilities:
        1. Measures execution latency (ms) with high-precision timer
        2. Traps unhandled exceptions and writes atomic records to agent_run_log
        3. Evaluates output length against min_expected_results to catch
           silent scraper breakages (the "Empty-Result Alarm")

    Args:
        agent_name: Human-readable identifier for the agent (e.g., "FundamentalAgent")
        min_expected_results: Minimum acceptable result count. If the decorated
            function returns fewer items, an EmptyResultAlarm is raised.

    Usage:
        @log_agent_run("FundamentalAgent", min_expected_results=5)
        def generate_watchlist_execution() -> list[str]:
            ...
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start_time = time.perf_counter()
            logger.info(f"[{agent_name}] Execution started...")
            status = "SUCCESS"
            error_msg = None
            result = None

            try:
                result = func(*args, **kwargs)

                # ── The Empty-Result Alarm Check ──
                if min_expected_results > 0:
                    res_len = len(result) if result is not None else 0
                    if res_len < min_expected_results:
                        status = "SOFT_FAIL"
                        error_msg = (
                            f"Empty-Result Alarm: Expected >= {min_expected_results} "
                            f"results, got {res_len}. Possible scraper "
                            f"redesign/block."
                        )
                        trigger_soft_failure_alarm(agent_name, error_msg)
                        raise EmptyResultAlarm(error_msg)

            except EmptyResultAlarm:
                # Re-raise alarm after logging to prevent downstream state corruption
                raise
            except Exception as e:
                status = "FAILED"
                error_msg = str(e)
                logger.exception(
                    f"[{agent_name}] Fatal exception during execution: {e}"
                )
                raise
            finally:
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                _write_agent_run_log(agent_name, status, duration_ms, error_msg)

            logger.info(
                f"[{agent_name}] Execution completed successfully in {duration_ms}ms."
            )
            return result

        return wrapper

    return decorator


def _write_agent_run_log(
    agent_name: str, status: str, duration_ms: int, error_msg: str | None
):
    """Writes an atomic execution record to the agent_run_log table.

    Isolated in a separate function so DB failures during logging never
    mask the original agent exception.
    """
    try:
        from db.session import get_session
        from db.models import AgentRunLog

        with get_session() as session:
            log_entry = AgentRunLog(
                run_date=datetime.now(timezone.utc),
                agent_name=agent_name,
                status=status,
                duration_ms=duration_ms,
                error_msg=error_msg,
            )
            session.add(log_entry)
        logger.debug(
            f"[{agent_name}] Recorded execution log ({status} in {duration_ms}ms)."
        )
    except Exception as db_err:
        logger.error(
            f"[{agent_name}] Failed to write to agent_run_log table: {db_err}"
        )
