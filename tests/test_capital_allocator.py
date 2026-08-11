# tests/test_capital_allocator.py
import pytest
from decimal import Decimal
from datetime import date
from db.models import Base, CapitalLedger, PortfolioHolding, HoldingStatus, TechnicalSignal, PatternType
from db.session import get_session, sync_engine
from core.capital_allocator import credit_monthly_sip_execution, execute_buy, execute_sell, select_and_execute_buy_candidate
from core.stop_loss_engine import compute_new_trailing_stop

@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=sync_engine)
    yield
    Base.metadata.drop_all(bind=sync_engine)

def test_sip_cash_rollover():
    """Verify monthly SIP accumulates cleanly over multiple periods."""
    credit_monthly_sip_execution(1000)
    credit_monthly_sip_execution(1000)
    credit_monthly_sip_execution(1000)
    
    with get_session() as session:
        last = session.query(CapitalLedger).order_by(CapitalLedger.id.desc()).first()
        assert last.running_balance == Decimal("3000.00")

def test_trailing_stop_ratcheting():
    """Verify trailing stop ratchets up on rising prices but NEVER decreases on pullbacks."""
    s1 = compute_new_trailing_stop(Decimal("100.00"), Decimal("4.00"), None) # 100 - 2.5*4 = 90
    assert s1 == Decimal("90.0000")
    
    s2 = compute_new_trailing_stop(Decimal("110.00"), Decimal("4.00"), s1) # 110 - 10 = 100 -> Ratchet up
    assert s2 == Decimal("100.0000")
    
    s3 = compute_new_trailing_stop(Decimal("105.00"), Decimal("4.00"), s2) # 105 - 10 = 95 -> Must hold 100
    assert s3 == Decimal("100.0000")

def test_dp_charge_deduction_on_sell():
    """Verify flat ₹15 DP charge is debited exactly once per sell transaction."""
    with get_session() as session:
        # Buy 1 share @ 2000
        execute_buy(session, "RELIANCE", 1, Decimal("2000.00"), "Test Buy")
        cash_post_buy = session.query(CapitalLedger).order_by(CapitalLedger.id.desc()).first().running_balance
        
        # Sell 1 share @ 2500 -> Gross proceeds 2500, Net proceeds 2485 (2500 - 15)
        sell_dec = execute_sell(session, "RELIANCE", 1, Decimal("2500.00"), "Test Sell")
        assert sell_dec.net_proceeds == 2485.0
        assert sell_dec.dp_charge == 15.0
        
        cash_post_sell = session.query(CapitalLedger).order_by(CapitalLedger.id.desc()).first().running_balance
        assert cash_post_sell == (cash_post_buy + Decimal("2485.00"))
