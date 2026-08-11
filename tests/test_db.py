# tests/test_db.py
"""Integration tests for VYUHA database schema, FK constraints, and CRUD.

Uses an in-memory SQLite database for speed — schema is created fresh
per test module and torn down automatically.
"""
import pytest
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import IntegrityError

from db.models import (
    Base,
    Universe,
    FundamentalSnapshot,
    SentimentFlag,
    Watchlist,
    WatchlistStatus,
    TechnicalSignal,
    PatternType,
    CapitalLedger,
    LedgerTxnType,
    PortfolioHolding,
    HoldingStatus,
    TradeLog,
    TradeTxnType,
    PortfolioValueHistory,
    AgentRunLog,
)


# ─── Test Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine():
    """Create an in-memory SQLite engine for testing."""
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture
def session(engine) -> Session:
    """Provide a transactional session that rolls back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    sess = sessionmaker(bind=connection)()
    yield sess
    sess.close()
    transaction.rollback()
    connection.close()


def _seed_universe(session: Session, symbol: str = "TEST_REL") -> Universe:
    """Helper: insert a universe row for FK tests."""
    u = Universe(
        symbol=symbol,
        name="Test Company",
        isin=f"INE{symbol}001",
        sector="Energy",
        exchange="NSE",
    )
    session.add(u)
    session.flush()
    return u


# ─── Schema Creation Test ──────────────────────────────────────────────────────


def test_all_tables_created(engine):
    """Verify all 10 tables exist after create_all."""
    expected_tables = {
        "universe",
        "fundamental_snapshots",
        "sentiment_flags",
        "watchlist",
        "technical_signals",
        "capital_ledger",
        "portfolio_holdings",
        "trade_log",
        "portfolio_value_history",
        "agent_run_log",
    }
    actual = set(Base.metadata.tables.keys())
    assert expected_tables.issubset(actual), f"Missing tables: {expected_tables - actual}"


# ─── Universe CRUD ─────────────────────────────────────────────────────────────


def test_universe_insert_and_read(session):
    """Insert and retrieve a universe record."""
    u = _seed_universe(session, "RELIANCE")
    result = session.query(Universe).filter_by(symbol="RELIANCE").first()
    assert result is not None
    assert result.name == "Test Company"
    assert result.exchange == "NSE"


def test_universe_isin_unique_constraint(session):
    """Duplicate ISINs must be rejected."""
    _seed_universe(session, "SYM_A")
    with pytest.raises(IntegrityError):
        u2 = Universe(
            symbol="SYM_B",
            name="Other Company",
            isin="INESYM_A001",  # same ISIN
            sector="Tech",
            exchange="NSE",
        )
        session.add(u2)
        session.flush()


# ─── Foreign Key Enforcement ──────────────────────────────────────────────────


def test_watchlist_fk_valid(session):
    """Watchlist entry with valid universe FK succeeds."""
    _seed_universe(session, "FK_VALID")
    w = Watchlist(
        symbol="FK_VALID",
        status=WatchlistStatus.ACTIVE.value,
        conviction_score=Decimal("85.50"),
        last_updated=datetime.utcnow(),
    )
    session.add(w)
    session.flush()
    assert session.query(Watchlist).filter_by(symbol="FK_VALID").first() is not None


def test_fundamental_snapshot_fk_valid(session):
    """FundamentalSnapshot with valid universe FK succeeds."""
    _seed_universe(session, "FUND_FK")
    fs = FundamentalSnapshot(
        symbol="FUND_FK",
        snapshot_date=date.today(),
        roe=Decimal("18.5000"),
        debt_to_equity=Decimal("0.3500"),
        eps_growth_3y=Decimal("14.2000"),
        conviction_score=Decimal("78.00"),
    )
    session.add(fs)
    session.flush()
    result = session.query(FundamentalSnapshot).filter_by(symbol="FUND_FK").first()
    assert result.roe == Decimal("18.5000")


# ─── Decimal Precision ─────────────────────────────────────────────────────────


def test_capital_ledger_decimal_precision(session):
    """Verify decimal precision is preserved in capital ledger."""
    txn = CapitalLedger(
        amount=Decimal("1000.0000"),
        txn_type=LedgerTxnType.SIP_CREDIT.value,
        running_balance=Decimal("1000.0000"),
        txn_date=datetime.utcnow(),
    )
    session.add(txn)
    session.flush()

    saved = session.query(CapitalLedger).first()
    assert saved.amount == Decimal("1000.0000")
    assert saved.running_balance == Decimal("1000.0000")
    assert saved.txn_type == LedgerTxnType.SIP_CREDIT.value


# ─── Technical Signal ──────────────────────────────────────────────────────────


def test_technical_signal_insert(session):
    """Insert a technical signal with valid FK."""
    _seed_universe(session, "TECH_SIG")
    sig = TechnicalSignal(
        symbol="TECH_SIG",
        signal_date=date.today(),
        pattern_type=PatternType.W_BOTTOM.value,
        atr_14=Decimal("45.3200"),
        entry_price=Decimal("2150.5000"),
        signal_strength=Decimal("72.50"),
    )
    session.add(sig)
    session.flush()

    result = session.query(TechnicalSignal).filter_by(symbol="TECH_SIG").first()
    assert result.pattern_type == "w_bottom"
    assert result.signal_strength == Decimal("72.50")


# ─── Portfolio Holdings ────────────────────────────────────────────────────────


def test_portfolio_holding_insert(session):
    """Insert a portfolio holding."""
    _seed_universe(session, "HOLD_TEST")
    h = PortfolioHolding(
        symbol="HOLD_TEST",
        qty=2,
        avg_buy_price=Decimal("1500.2500"),
        first_buy_date=date.today(),
        trailing_stop_price=Decimal("1380.0000"),
        status=HoldingStatus.OPEN.value,
    )
    session.add(h)
    session.flush()

    result = session.query(PortfolioHolding).filter_by(symbol="HOLD_TEST").first()
    assert result.qty == 2
    assert result.status == "open"


# ─── Trade Log ─────────────────────────────────────────────────────────────────


def test_trade_log_insert(session):
    """Insert a trade log entry."""
    _seed_universe(session, "TRADE_TEST")
    t = TradeLog(
        symbol="TRADE_TEST",
        txn_type=TradeTxnType.BUY.value,
        qty=1,
        price=Decimal("2500.0000"),
        txn_date=datetime.utcnow(),
        dp_charge=Decimal("0.00"),
        reason="Technical entry: W-bottom confirmed",
        triggered_by="technical_agent",
    )
    session.add(t)
    session.flush()

    result = session.query(TradeLog).filter_by(symbol="TRADE_TEST").first()
    assert result.txn_type == "BUY"
    assert result.dp_charge == Decimal("0.00")


# ─── Portfolio Value History ───────────────────────────────────────────────────


def test_portfolio_value_history(session):
    """Insert a daily portfolio snapshot."""
    pvh = PortfolioValueHistory(
        date=date.today(),
        total_value=Decimal("1000.0000"),
        cash_balance=Decimal("1000.0000"),
        invested_value=Decimal("0.0000"),
        drawdown_pct=Decimal("0.00"),
    )
    session.add(pvh)
    session.flush()

    result = session.query(PortfolioValueHistory).filter_by(date=date.today()).first()
    assert result.total_value == Decimal("1000.0000")


# ─── Agent Run Log ─────────────────────────────────────────────────────────────


def test_agent_run_log(session):
    """Insert an agent run log entry."""
    log = AgentRunLog(
        run_date=datetime.utcnow(),
        agent_name="fundamental_agent",
        status="success",
        duration_ms=4500,
        error_msg=None,
    )
    session.add(log)
    session.flush()

    result = session.query(AgentRunLog).filter_by(agent_name="fundamental_agent").first()
    assert result.status == "success"
    assert result.duration_ms == 4500
    assert result.error_msg is None


# ─── Sentiment Flag ────────────────────────────────────────────────────────────


def test_sentiment_flag_insert(session):
    """Insert a sentiment flag with red_flag=True."""
    _seed_universe(session, "SENT_TEST")
    sf = SentimentFlag(
        symbol="SENT_TEST",
        checked_date=datetime.utcnow(),
        red_flag=True,
        reason="Auditor resignation reported",
        source_url="https://example.com/news/123",
    )
    session.add(sf)
    session.flush()

    result = session.query(SentimentFlag).filter_by(symbol="SENT_TEST").first()
    assert result.red_flag is True
    assert "Auditor" in result.reason
