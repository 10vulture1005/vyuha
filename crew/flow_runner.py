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
from litellm.exceptions import RateLimitError, BadRequestError

def _patched_completion(*args, **kwargs):
    if "messages" in kwargs:
        for msg in kwargs["messages"]:
            if "cache_breakpoint" in msg:
                del msg["cache_breakpoint"]
                
    if "tools" in kwargs and kwargs["tools"]:
        for tool in kwargs["tools"]:
            if "function" in tool and "parameters" in tool["function"]:
                params = tool["function"]["parameters"]
                params.setdefault("type", "object")
                # If properties is missing or empty, inject a real param the LLM can fill
                if not params.get("properties"):
                    params["properties"] = {"trigger": {"type": "string", "description": "Pass any value like 'run' to execute."}}
                # Groq requires `required` to list every key in `properties`
                params["required"] = list(params["properties"].keys())
    
    retries = 3
    for attempt in range(retries):
        try:
            return _original_completion(*args, **kwargs)
        except RateLimitError as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"Groq RateLimitError hit (attempt {attempt + 1}/{retries}). Sleeping 15s...")
            time.sleep(15)
        except BadRequestError as e:
            # Groq tool_use_failed = LLM generated malformed tool call XML (non-deterministic, retry helps)
            if "tool_use_failed" in str(e) and attempt < retries - 1:
                logger.warning(f"Groq tool_use_failed (attempt {attempt + 1}/{retries}). Retrying in 2s...")
                time.sleep(2)
                continue
            raise

async def _patched_acompletion(*args, **kwargs):
    if "messages" in kwargs:
        for msg in kwargs["messages"]:
            if "cache_breakpoint" in msg:
                del msg["cache_breakpoint"]
                
    if "tools" in kwargs and kwargs["tools"]:
        for tool in kwargs["tools"]:
            if "function" in tool and "parameters" in tool["function"]:
                params = tool["function"]["parameters"]
                params.setdefault("type", "object")
                # If properties is missing or empty, inject a real param the LLM can fill
                if not params.get("properties"):
                    params["properties"] = {"trigger": {"type": "string", "description": "Pass any value like 'run' to execute."}}
                # Groq requires `required` to list every key in `properties`
                params["required"] = list(params["properties"].keys())
                
    retries = 3
    for attempt in range(retries):
        try:
            return await _original_acompletion(*args, **kwargs)
        except RateLimitError as e:
            if attempt == retries - 1:
                raise
            logger.warning(f"Groq RateLimitError hit (attempt {attempt + 1}/{retries}). Sleeping 15s...")
            await asyncio.sleep(15)
        except BadRequestError as e:
            # Groq tool_use_failed = LLM generated malformed tool call XML (non-deterministic, retry helps)
            if "tool_use_failed" in str(e) and attempt < retries - 1:
                logger.warning(f"Groq tool_use_failed (attempt {attempt + 1}/{retries}). Retrying in 2s...")
                await asyncio.sleep(2)
                continue
            raise

litellm.completion = _patched_completion
litellm.acompletion = _patched_acompletion

def run_daily_flow(is_weekly: bool = False):
    """Executes the CrewAI orchestration pipeline directly without the modern Flow wrapper."""
    logger.info(f"Starting VYUHA Execution. Mode: {'WEEKLY + DAILY' if is_weekly else 'DAILY ONLY'}")
    
    crew = create_crew(is_weekly=is_weekly)
    result = crew.kickoff()
    
    logger.info(f"Crew execution finished. Final Output: {result}")
    return result
