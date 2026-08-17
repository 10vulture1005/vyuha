import litellm
litellm.drop_params = True  # Drops unsupported parameters like cache_breakpoint before sending to Groq

# Optional: Monkey-patch CrewAI's cache_breakpoint directly to prevent the injection
import crewai.llm as _crewai_llm
if hasattr(_crewai_llm, "cache"):
    _crewai_llm.cache.mark_cache_breakpoint = lambda msg: msg

from crewai import Agent, Crew, Process, LLM
from config.settings import settings
from agents.tools.ledger_tools import (
    run_capital_allocator,
    run_risk_review,
    run_technical_scan,
    run_fundamental_scan,
    run_sentiment_scan,
)
from crew.tasks import get_tasks

def create_crew(is_weekly: bool = False) -> Crew:
    """Creates the CrewAI orchestration pipeline.
    
    If is_weekly is True, the full fundamental -> sentiment -> technical -> risk -> decision chain runs.
    If False, it skips fundamental and sentiment, running only daily timing and risk scans.
    """
    
    groq_key = settings.GROQ_API_KEY.get_secret_value() if settings.GROQ_API_KEY else ""
    
    # Use CrewAI's native LLM wrapper (powered by LiteLLM)
    groq_llm = LLM(
        model="groq/openai/gpt-oss-20b",
        api_key=groq_key,
    )
    
    # 1. Fundamental Analyst
    fundamental_analyst = Agent(
        role="Fundamental Equity Analyst",
        goal="Filter the Nifty 500 universe for high ROE, low debt, and strong EPS growth.",
        backstory="You are a quantitative value investor who insists on business quality over hype.",
        tools=[run_fundamental_scan],
        verbose=True,
        allow_delegation=False,
        llm=groq_llm,
    )
    
    # 2. Sentiment Analyst
    sentiment_analyst = Agent(
        role="Corporate Governance Sentinel",
        goal="Veto fundamentally strong stocks that have severe regulatory or governance red flags in the news.",
        backstory="You are a skeptical forensic auditor who protects capital from frauds and scandals.",
        tools=[run_sentiment_scan],
        verbose=True,
        allow_delegation=False,
        llm=groq_llm,
    )
    
    # 3. Technical Analyst
    technical_analyst = Agent(
        role="Technical Timing Specialist",
        goal="Scan active watchlists for W-Bottoms and Volatility Squeezes to time entries perfectly.",
        backstory="You are a market technician who only acts on verified exhaustion or compression patterns.",
        tools=[run_technical_scan],
        verbose=True,
        allow_delegation=False,
        llm=groq_llm,
    )
    
    # 4. Risk Manager
    risk_manager = Agent(
        role="Risk and Exit Controller",
        goal="Ruthlessly protect capital by enforcing ratcheting trailing stops and fundamental degradation exits.",
        backstory="You are a strict risk manager who cuts losers immediately according to math, without emotion.",
        tools=[run_risk_review],
        verbose=True,
        allow_delegation=False,
        llm=groq_llm,
    )
    
    # 5. Portfolio Manager
    portfolio_manager = Agent(
        role="Lead Portfolio Manager",
        goal="Synthesize daily technical setups and risk reviews to make final ₹1,000/month allocation decisions.",
        backstory="You are the lead PM of a concentrated accumulation fund. You follow strict priority rules and never over-allocate.",
        tools=[run_capital_allocator],
        verbose=True,
        allow_delegation=False,
        llm=groq_llm,
    )
    
    agents = [technical_analyst, risk_manager, portfolio_manager]
    if is_weekly:
        agents = [fundamental_analyst, sentiment_analyst] + agents
        
    tasks = get_tasks(
        fundamental_analyst,
        sentiment_analyst,
        technical_analyst,
        risk_manager,
        portfolio_manager,
        is_weekly
    )
    
    return Crew(
        agents=agents,
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
    )
