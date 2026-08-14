import pytest
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, CapitalLedger, TechnicalSignal, LedgerTxnType, TradeLog
from core.capital_allocator import select_and_execute_buy_candidate, HoldCash, BuyDecision

@pytest.fixture
def mock_db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    with TestingSessionLocal() as session:
        yield session

def test_circuit_breaker_active(mock_db_session):
    # Setup initial capital and signal
    initial_cash = Decimal("100000.0")
    mock_db_session.add(CapitalLedger(
        amount=initial_cash,
        txn_type=LedgerTxnType.SIP_CREDIT.value,
        running_balance=initial_cash
    ))
    
    # Add a strong technical signal
    today = date.today()
    mock_db_session.add(TechnicalSignal(
        symbol="TCS",
        signal_date=today,
        pattern_type="W_BOTTOM",
        atr_14=Decimal("20.0"),
        entry_price=Decimal("3000.0"),
        structural_stop_price=Decimal("2900.0"),
        signal_strength=Decimal("80.0")
    ))
    mock_db_session.commit()

    # Patch get_session to use our mock in-memory db session
    with patch("core.capital_allocator.get_session") as mock_get_session:
        mock_get_session.return_value.__enter__.return_value = mock_db_session
        
        # Test 1: Circuit breaker ACTIVE -> should return HoldCash
        decision = select_and_execute_buy_candidate(signal_date=today, circuit_breaker_active=True)
        assert isinstance(decision, HoldCash)
        assert "Monthly Circuit Breaker Active" in decision.rationale
        
        # Verify no trades executed
        trades = mock_db_session.query(TradeLog).all()
        assert len(trades) == 0

        # Test 2: Circuit breaker INACTIVE -> should execute buy
        decision = select_and_execute_buy_candidate(signal_date=today, circuit_breaker_active=False)
        assert isinstance(decision, BuyDecision)
        assert decision.symbol == "TCS"
        
        # Verify trade executed
        trades = mock_db_session.query(TradeLog).all()
        assert len(trades) == 1
        assert trades[0].symbol == "TCS"
        assert trades[0].txn_type == "BUY"
