# agents/tools/ledger_tools.py
from crewai.tools import tool
from core.capital_allocator import select_and_execute_buy_candidate
from agents.risk_exit_agent import RiskExitAgent
from agents.technical_agent import TechnicalAgent
from agents.fundamental_agent import FundamentalAgent
from agents.sentiment_agent import run_sentiment_pass_execution

@tool("Run Capital Allocator")
def run_capital_allocator(trigger: str) -> str:
    """Evaluates today's technical signals against cash and priority rules to buy or hold cash. Pass any value like 'run' to trigger execution."""
    decision = select_and_execute_buy_candidate()
    return decision.model_dump_json()

@tool("Run Risk Review")
def run_risk_review(trigger: str) -> str:
    """Scans all OPEN holdings for trailing stop breaches or fundamental degradation. Pass any value like 'run' to trigger execution."""
    agent = RiskExitAgent()
    sells = agent.evaluate_exits()
    return str(sells)

@tool("Run Technical Scan")
def run_technical_scan(trigger: str) -> str:
    """Scans active watchlist for W-Bottom or BB Squeeze setups and writes to database. Pass any value like 'run' to trigger execution."""
    agent = TechnicalAgent()
    signals = agent.run_technical_scan_execution()
    return str(signals)

@tool("Run Fundamental Scan")
def run_fundamental_scan(trigger: str) -> str:
    """Scans seed universe for fundamentally strong stocks and generates an active watchlist. Pass any value like 'run' to trigger execution."""
    agent = FundamentalAgent()
    watchlist = agent.generate_watchlist_execution()
    return str(watchlist)

@tool("Run Sentiment Veto Scan")
def run_sentiment_scan(trigger: str) -> str:
    """Screens active watchlist against corporate governance red flags in recent news. Pass any value like 'run' to trigger execution."""
    survivors = run_sentiment_pass_execution()
    return str(survivors)
