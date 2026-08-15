# crew/flow_runner.py
import datetime
from loguru import logger
from crew.crew_definition import create_crew
import litellm

# Monkey-patch litellm to remove 'cache_breakpoint' which causes Groq API to crash
_original_completion = litellm.completion
_original_acompletion = litellm.acompletion

import time
import asyncio
from litellm.exceptions import RateLimitError

def _patched_completion(*args, **kwargs):
    if "messages" in kwargs:
        for msg in kwargs["messages"]:
            if "cache_breakpoint" in msg:
                del msg["cache_breakpoint"]
                
    if "tools" in kwargs and kwargs["tools"]:
        for tool in kwargs["tools"]:
            if "function" in tool and "parameters" in tool["function"]:
                params = tool["function"]["parameters"]
                # Ensure properties exists (Groq rejects missing properties)
                if "properties" not in params:
                    params["properties"] = {}
                # Groq requires `required` to list every key in `properties`
                params["required"] = list(params["properties"].keys())
    
    retries = 3
    for attempt in range(retries):
        try:
            return _original_completion(*args, **kwargs)
        except RateLimitError as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"Groq RateLimitError hit (attempt {attempt + 1}/{retries}). Sleeping for 15 seconds... Error: {str(e)}")
            time.sleep(15)

async def _patched_acompletion(*args, **kwargs):
    if "messages" in kwargs:
        for msg in kwargs["messages"]:
            if "cache_breakpoint" in msg:
                del msg["cache_breakpoint"]
                
    if "tools" in kwargs and kwargs["tools"]:
        for tool in kwargs["tools"]:
            if "function" in tool and "parameters" in tool["function"]:
                params = tool["function"]["parameters"]
                # Ensure properties exists (Groq rejects missing properties)
                if "properties" not in params:
                    params["properties"] = {}
                # Groq requires `required` to list every key in `properties`
                params["required"] = list(params["properties"].keys())
                
    retries = 3
    for attempt in range(retries):
        try:
            return await _original_acompletion(*args, **kwargs)
        except RateLimitError as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"Groq RateLimitError hit (attempt {attempt + 1}/{retries}). Sleeping for 15 seconds... Error: {str(e)}")
            await asyncio.sleep(15)

litellm.completion = _patched_completion
litellm.acompletion = _patched_acompletion

def run_daily_flow(is_weekly: bool = False):
    """Executes the CrewAI orchestration pipeline directly without the modern Flow wrapper."""
    logger.info(f"Starting VYUHA Execution. Mode: {'WEEKLY + DAILY' if is_weekly else 'DAILY ONLY'}")
    
    crew = create_crew(is_weekly=is_weekly)
    result = crew.kickoff()
    
    logger.info(f"Crew execution finished. Final Output: {result}")
    return result
