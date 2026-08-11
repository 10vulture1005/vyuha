import requests
from typing import Optional
from loguru import logger
from config.settings import settings

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

def send_message(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """Sends a formatted message to the configured Telegram chat ID."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("Telegram Bot Token or Chat ID not configured. Skipping notification.")
        return False
        
    url = TELEGRAM_API_URL.format(token=settings.TELEGRAM_BOT_TOKEN.get_secret_value())
    payload = {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("Telegram notification delivered successfully.")
            return True
        else:
            logger.error(f"Telegram API returned {response.status_code}: {response.text}")
            # Retry once with basic text if Markdown parsing failed
            if "can't parse entities" in response.text.lower() and parse_mode != "HTML":
                logger.info("Retrying delivery with parse_mode=None...")
                payload["parse_mode"] = None
                res_retry = requests.post(url, json=payload, timeout=10)
                return res_retry.status_code == 200
            return False
    except Exception as e:
        logger.error(f"Failed to connect to Telegram API: {e}")
        return False
