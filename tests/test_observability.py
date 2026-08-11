# tests/test_observability.py
"""Unit tests for Phase 11 observability, logging, and failure recovery.

Tests verify:
    - Clean function execution writes a SUCCESS row to agent_run_log
    - Returning empty results when min_expected > 0 raises EmptyResultAlarm
    - Exception trapping writes FAILED row with error message
    - Latency measurement is non-negative
"""
import pytest
from unittest.mock import patch, MagicMock
from core.observability import log_agent_run, EmptyResultAlarm
from db.models import AgentRunLog, Base
from db.session import get_session, sync_engine


@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    """Creates all tables in the test DB and tears them down after."""
    Base.metadata.create_all(bind=sync_engine)
    yield
    Base.metadata.drop_all(bind=sync_engine)


# ── Successful Execution Tests ──────────────────────────────────────────────


def test_successful_agent_logging():
    """Verify clean function execution writes a SUCCESS row to agent_run_log."""

    @log_agent_run("TestSuccessAgent", min_expected_results=1)
    def dummy_success():
        return ["RELIANCE", "TCS"]

    res = dummy_success()
    assert len(res) == 2

    with get_session() as session:
        log = (
            session.query(AgentRunLog)
            .filter(AgentRunLog.agent_name == "TestSuccessAgent")
            .order_by(AgentRunLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.status == "SUCCESS"
        assert log.duration_ms >= 0
        assert log.error_msg is None


def test_successful_agent_no_min_expected():
    """Verify that min_expected_results=0 allows empty returns without alarm."""

    @log_agent_run("TestNoMinAgent", min_expected_results=0)
    def dummy_empty_ok():
        return []

    res = dummy_empty_ok()
    assert res == []

    with get_session() as session:
        log = (
            session.query(AgentRunLog)
            .filter(AgentRunLog.agent_name == "TestNoMinAgent")
            .order_by(AgentRunLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.status == "SUCCESS"


# ── Empty-Result Alarm Tests ────────────────────────────────────────────────


@patch("core.observability.trigger_soft_failure_alarm")
def test_empty_result_alarm_trigger(mock_alarm):
    """Verify returning zero results when min_expected > 0 raises alarm and logs SOFT_FAIL."""

    @log_agent_run("TestBrokenScraperAgent", min_expected_results=5)
    def dummy_broken_scrape():
        return []  # Returns empty list due to simulated markup change

    with pytest.raises(EmptyResultAlarm):
        dummy_broken_scrape()

    mock_alarm.assert_called_once()

    with get_session() as session:
        log = (
            session.query(AgentRunLog)
            .filter(AgentRunLog.agent_name == "TestBrokenScraperAgent")
            .order_by(AgentRunLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.status == "SOFT_FAIL"
        assert "Empty-Result Alarm" in log.error_msg


@patch("core.observability.trigger_soft_failure_alarm")
def test_below_threshold_triggers_alarm(mock_alarm):
    """Verify that returning fewer results than min_expected triggers alarm."""

    @log_agent_run("TestPartialAgent", min_expected_results=10)
    def dummy_partial():
        return ["RELIANCE", "TCS", "INFY"]  # Only 3, but 10 expected

    with pytest.raises(EmptyResultAlarm):
        dummy_partial()

    mock_alarm.assert_called_once()

    with get_session() as session:
        log = (
            session.query(AgentRunLog)
            .filter(AgentRunLog.agent_name == "TestPartialAgent")
            .order_by(AgentRunLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.status == "SOFT_FAIL"
        assert "Expected >= 10" in log.error_msg


# ── Exception Trapping Tests ───────────────────────────────────────────────


def test_exception_trapping_writes_failed_row():
    """Verify that unhandled exceptions write a FAILED row with the error message."""

    @log_agent_run("TestCrashAgent", min_expected_results=0)
    def dummy_crash():
        raise ValueError("Simulated screener.in connection timeout")

    with pytest.raises(ValueError, match="connection timeout"):
        dummy_crash()

    with get_session() as session:
        log = (
            session.query(AgentRunLog)
            .filter(AgentRunLog.agent_name == "TestCrashAgent")
            .order_by(AgentRunLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.status == "FAILED"
        assert "connection timeout" in log.error_msg
        assert log.duration_ms >= 0


# ── Latency Measurement Tests ──────────────────────────────────────────────


def test_latency_is_measured():
    """Verify that duration_ms is captured and non-negative."""
    import time

    @log_agent_run("TestLatencyAgent", min_expected_results=0)
    def dummy_slow():
        time.sleep(0.05)  # 50ms sleep
        return ["OK"]

    dummy_slow()

    with get_session() as session:
        log = (
            session.query(AgentRunLog)
            .filter(AgentRunLog.agent_name == "TestLatencyAgent")
            .order_by(AgentRunLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.duration_ms >= 40  # Should be at least ~50ms
