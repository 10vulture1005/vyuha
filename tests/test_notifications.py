import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from notifications.digest_builder import escape_markdown_v2, build_daily_digest

def test_markdown_v2_escaping():
    """Verify special symbols are properly escaped with backslashes."""
    raw = "RELIANCE (NSE) is +5% & [strong]!"
    escaped = escape_markdown_v2(raw)
    assert "\\(" in escaped
    assert "\\[" in escaped
    assert "\\+" in escaped
    assert "\\!" in escaped

@patch("notifications.digest_builder.get_portfolio_snapshot")
def test_digest_builder_hold_cash(mock_snap):
    """Verify HOLD_CASH decision generates clean Markdown summary."""
    mock_snap.return_value = ([], Decimal("2500.00"))
    decision = {
        "action": "HOLD_CASH",
        "rationale": "No technical signal on watchlist."
    }
    msg = build_daily_digest(decision)
    assert "CASH ACCUMULATION" in msg
    assert "₹2,500\\.00" in msg
    assert "No technical signal" in msg

@patch("requests.post")
def test_send_message_retry_on_parse_error(mock_post):
    """Verify bot retries with unformatted text if MarkdownV2 parsing fails."""
    # First call fails with entity parsing error, second succeeds
    mock_res_fail = MagicMock()
    mock_res_fail.status_code = 400
    mock_res_fail.text = "Bad Request: can't parse entities"
    
    mock_res_ok = MagicMock()
    mock_res_ok.status_code = 200
    
    mock_post.side_effect = [mock_res_fail, mock_res_ok]
    
    from notifications.telegram_bot import send_message
    with patch("config.settings.settings.TELEGRAM_BOT_TOKEN", MagicMock(get_secret_value=lambda: "token")), \
         patch("config.settings.settings.TELEGRAM_CHAT_ID", "12345"):
        res = send_message("Test message")
        assert res is True
        assert mock_post.call_count == 2
