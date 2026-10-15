import litellm
litellm.drop_params = True  # Drops unsupported parameters like cache_breakpoint before sending to Groq

# Optional: Monkey-patch CrewAI's cache_breakpoint directly to prevent the injection
import crewai.llm as _crewai_llm
if hasattr(_crewai_llm, "cache"):
    _crewai_llm.cache.mark_cache_breakpoint = lambda msg: msg

from crewai import Agent, Crew, Process, LLM
from config.settings import settings
from config.llm_routing import primary_route, fallback_route
from agents.tools.ledger_tools import (
    run_capital_allocator,
    run_risk_review,
    run_technical_scan,
    run_fundamental_scan,
    run_sentiment_scan,
)
try:
    # Vyuha Agent tools require the optional LLM extras. Import
    # lazily so the rule-based-only pipeline keeps working without
    # langgraph/langchain installed.
    from agents.tools.vyuha_agent_tools import (
        run_vyuha_agent_pipeline,
        run_vyuha_agent_cross_check,
        resolve_vyuha_agent_outcomes,
    )
    _TA_TOOLS_AVAILABLE = True
except Exception:
    _TA_TOOLS_AVAILABLE = False

from crew.tasks import get_tasks

def _route_model_string(route) -> str:
    """Render a :class:`~config.llm_routing.Route` as a CrewAI/LiteLLM model string.

    We deliberately use the ``openai/<model>`` form plus an explicit
    ``base_url`` rather than LiteLLM's ``openrouter/<model>`` prefix
    convention: the OpenRouter IDs vyuha uses carry a ``:free`` variant
    suffix, and provider-prefix parsing in LiteLLM is not reliably
    transparent to colons in the model segment. Spelling out the endpoint
    sidesteps that entirely and matches how
    ``tradingagents/llm_clients/openai_client.py`` already talks to
    OpenRouter.
    """
    return f"openai/{route.model}"


def _build_llm(route) -> LLM:
    """Construct a CrewAI ``LLM`` for a resolved route."""
    return LLM(
        model=_route_model_string(route),
        base_url=route.base_url,
        api_key=route.api_key or "",
    )


def create_crew(is_weekly: bool = False) -> Crew:
    """Creates the CrewAI orchestration pipeline.

    If is_weekly is True, the full fundamental -> sentiment -> technical -> risk -> decision chain runs.
    If False, it skips fundamental and sentiment, running only daily timing and risk scans.

    The crew's reasoning LLM comes from ``LLM_PROVIDER``/``LLM_MODEL``
    (OpenRouter by default). ``LLM_FALLBACK_PROVIDER``/``LLM_FALLBACK_MODEL``
    describe the backup used when a primary call fails; the fallback itself is
    wired in at the transport layer by ``crew/flow_runner.py``, since CrewAI
    hands every call to LiteLLM.
    """

    route = primary_route()
    if not route.is_usable:
        print(
            f"[VYUHA] WARNING: no API key for primary provider '{route.provider}'. "
            "Set the matching key in .env — the crew cannot reach the LLM without it."
        )

    primary_llm = _build_llm(route)

    fallback = fallback_route()
    if fallback is not None:
        print(f"[VYUHA] LLM routing: {route.provider}/{route.model} (fallback: {fallback.provider}/{fallback.model})")
    else:
        print(f"[VYUHA] LLM routing: {route.provider}/{route.model} (no usable fallback configured)")
    
    # 1. Fundamental Analyst
    fundamental_analyst = Agent(
        role="Fundamental Equity Analyst",
        goal="Filter the Nifty 500 universe for high ROE, low debt, and strong EPS growth.",
        backstory="You are a quantitative value investor who insists on business quality over hype.",
        tools=[run_fundamental_scan],
        verbose=True,
        allow_delegation=False,
        llm=primary_llm,
    )
    
    # 2. Sentiment Analyst
    sentiment_analyst = Agent(
        role="Corporate Governance Sentinel",
        goal="Veto fundamentally strong stocks that have severe regulatory or governance red flags in the news.",
        backstory="You are a skeptical forensic auditor who protects capital from frauds and scandals.",
        tools=[run_sentiment_scan],
        verbose=True,
        allow_delegation=False,
        llm=primary_llm,
    )
    
    # 3. Technical Analyst
    technical_analyst = Agent(
        role="Technical Timing Specialist",
        goal="Scan active watchlists for W-Bottoms and Volatility Squeezes to time entries perfectly.",
        backstory="You are a market technician who only acts on verified exhaustion or compression patterns.",
        tools=[run_technical_scan],
        verbose=True,
        allow_delegation=False,
        llm=primary_llm,
    )
    
    # 4. Risk Manager
    risk_manager = Agent(
        role="Risk and Exit Controller",
        goal="Ruthlessly protect capital by enforcing ratcheting trailing stops and fundamental degradation exits.",
        backstory="You are a strict risk manager who cuts losers immediately according to math, without emotion.",
        tools=[run_risk_review],
        verbose=True,
        allow_delegation=False,
        llm=primary_llm,
    )
    
    # 5. Portfolio Manager
    pm_tools = [run_capital_allocator]
    if _TA_TOOLS_AVAILABLE and settings.TRADINGAGENTS_ENABLED:
        # Give the PM access to the Vyuha Agent cross-check tool when
        # the LLM layer is enabled. The PM can invoke it as an
        # additional sanity check before allocating.
        pm_tools.append(run_vyuha_agent_cross_check)

    portfolio_manager = Agent(
        role="Lead Portfolio Manager",
        goal="Synthesize daily technical setups and risk reviews to make final ₹1,000/month allocation decisions.",
        backstory="You are the lead PM of a concentrated accumulation fund. You follow strict priority rules and never over-allocate.",
        tools=pm_tools,
        verbose=True,
        allow_delegation=False,
        llm=primary_llm,
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
