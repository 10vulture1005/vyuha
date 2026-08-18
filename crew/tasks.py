# crew/tasks.py
from typing import List
from crewai import Task, Agent

def get_tasks(
    fundamental_analyst: Agent,
    sentiment_analyst: Agent,
    technical_analyst: Agent,
    risk_manager: Agent,
    portfolio_manager: Agent,
    is_weekly: bool = False
) -> List[Task]:
    """Generates the sequential task DAG based on execution mode."""
    
    tasks = []
    
    t_fund = None
    t_sent = None
    
    if is_weekly:
        t_fund = Task(
            description="Execute 'Run Fundamental Scan' tool to screen Nifty 500 and update the ACTIVE watchlist.",
            expected_output="A list of ACTIVE watchlist symbols that passed all hard fundamental filters.",
            agent=fundamental_analyst
        )
        
        t_sent = Task(
            description="Execute 'Run Sentiment Veto Scan' tool to check recent news for the ACTIVE watchlist and veto governance risks.",
            expected_output="A finalized list of clean ACTIVE watchlist symbols.",
            agent=sentiment_analyst,
            context=[t_fund]
        )
        tasks.extend([t_fund, t_sent])
        
    t_tech = Task(
        description="Execute 'Run Technical Scan' tool to identify any W-Bottoms or BB Squeezes among ACTIVE watchlist symbols.",
        expected_output="A list of generated actionable technical setups, or an empty list if none exist today.",
        agent=technical_analyst,
        context=[t_sent] if t_sent else []
    )
    
    t_risk = Task(
        description="Execute 'Run Risk Review' tool to check all OPEN portfolio holdings for trailing stop breaches and execute defensive exits.",
        expected_output="A summary of executed defensive sells, or confirmation that no stops were breached.",
        agent=risk_manager
    )
    
    t_decision = Task(
        description=(
            "Execute 'Run Capital Allocator' tool to make the final daily allocation decision based on today's technical signals and cash balance. "
            "Write a clear, concise rationale for the final decision (BUY, SELL, or HOLD_CASH)."
        ),
        expected_output="The EXACT JSON decision object returned by the 'Run Capital Allocator' tool, containing action, symbol, qty, price, and rationale. DO NOT anonymize, modify, or change the symbol to 'XYZ'. Return the true stock symbol exactly as provided by the tool.",
        agent=portfolio_manager,
        context=[t_tech, t_risk]
    )
    
    tasks.extend([t_tech, t_risk, t_decision])
    return tasks
