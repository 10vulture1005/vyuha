# agents/tools/ledger_tools.py
from langchain.tools import tool
from core.capital_allocator import select_and_execute_buy_candidate
from agents.risk_exit_agent import run_risk_exit_review_execution
from agents.technical_agent import run_technical_scan_execution
from agents.fundamental_agent import generate_watchlist_execution
from agents.sentiment_agent import run_sentiment_pass_execution

@tool("Run Capital Allocator")
def run_capital_allocator() -> str:
    """Evaluates today's technical signals against cash and priority rules to buy or hold cash."""
    decision = select_and_execute_buy_candidate()
    return decision.model_dump_json()

@tool("Run Risk Review")
def run_risk_review() -> str:
    """Scans all OPEN holdings for trailing stop breaches or fundamental degradation."""
    sells = run_risk_exit_review_execution()
    return str(sells)

@tool("Run Technical Scan")
def run_technical_scan() -> str:
    """Scans active watchlist for W-Bottom or BB Squeeze setups and writes to database."""
    signals = run_technical_scan_execution()
    return str(signals)

@tool("Run Fundamental Scan")
def run_fundamental_scan() -> str:
    """Scans seed universe for fundamentally strong stocks and generates an active watchlist."""
    watchlist = generate_watchlist_execution()
    return str(watchlist)

@tool("Run Sentiment Veto Scan")
def run_sentiment_scan() -> str:
    """Screens active watchlist against corporate governance red flags in recent news."""
    survivors = run_sentiment_pass_execution()
    return str(survivors)
