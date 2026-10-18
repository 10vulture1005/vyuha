import re
from decimal import Decimal
from typing import Dict, Any, List
from loguru import logger
from db.session import get_session
from db.models import PortfolioHolding, CapitalLedger, HoldingStatus

def escape_markdown_v2(text: str) -> str:
    """Escapes special characters required by Telegram MarkdownV2 spec."""
    escape_chars = r"_*[]()~`>#+-=|{}.!\\"
    return re.sub(f"([{''.join(re.escape(c) for c in escape_chars)}])", r"\\\1", str(text))

def get_portfolio_snapshot(session) -> tuple[List[dict], Decimal]:
    """Queries current open holdings and latest ledger cash balance."""
    holdings = session.query(PortfolioHolding).filter(PortfolioHolding.status == HoldingStatus.OPEN.value).all()
    h_list = [
        {"symbol": h.symbol, "qty": h.qty, "avg_price": float(h.avg_buy_price), "stop": float(h.trailing_stop_price)}
        for h in holdings
    ]
    last_txn = session.query(CapitalLedger).order_by(CapitalLedger.id.desc()).first()
    cash = last_txn.running_balance if last_txn else Decimal("0")
    return h_list, cash

def build_daily_digest(decision: Dict[str, Any]) -> str:
    """Constructs a comprehensive Telegram MarkdownV2 executive summary."""
    action = decision.get("action", "HOLD_CASH")
    rationale = decision.get("rationale", "No explanation provided.")
    
    with get_session() as session:
        holdings, cash_balance = get_portfolio_snapshot(session)
        
    # --- Status Header ---
    if action == "BUY":
        header = "🟢 *VYUHA EXECUTIVE DIGEST: BUY EXECUTION*"
        symbol = decision.get("symbol", "UNKNOWN")
        qty = decision.get("qty", 0)
        price = decision.get("price", 0.0)
        action_line = f"🛒 *Action:* Bought {qty}x `{escape_markdown_v2(symbol)}` @ ₹{price:,.2f}"
    elif action == "SELL":
        header = "🔴 *VYUHA EXECUTIVE DIGEST: DEFENSIVE SELL*"
        symbol = decision.get("symbol", "UNKNOWN")
        qty = decision.get("qty", 0)
        price = decision.get("price", 0.0)
        net = decision.get("net_proceeds", 0.0)
        action_line = f"🚨 *Action:* Sold {qty}x `{escape_markdown_v2(symbol)}` @ ₹{price:,.2f}\n💵 *Net Proceeds:* ₹{net:,.2f} \\(Net of ₹15 DP charge\\)"
    else:
        header = "🔵 *VYUHA EXECUTIVE DIGEST: CASH ACCUMULATION*"
        action_line = "⏸️ *Action:* HOLD CASH \\(No trade executed today\\)"

    # --- Rationale Block ---
    rat_escaped = escape_markdown_v2(rationale)
    rationale_block = f"📝 *Quantitative Rationale:*\n_{rat_escaped}_"
    
    # --- Portfolio Snapshot Block ---
    h_lines = []
    if holdings:
        for h in holdings:
            sym_esc = escape_markdown_v2(h["symbol"])
            h_lines.append(f"• `{sym_esc}`: {h['qty']}x @ ₹{h['avg_price']:,.2f} \\(Stop: ₹{h['stop']:,.2f}\\)")
        holdings_str = "\n".join(h_lines)
    else:
        holdings_str = "_No open equity positions\\._"
        
    snapshot_block = f"💼 *Current Open Holdings:*\n{holdings_str}\n\n💰 *Ledger Cash Balance:* ₹{escape_markdown_v2(f'{cash_balance:,.2f}')}"
    
    # --- Assemble Message ---
    full_message = f"{header}\n\n{action_line}\n\n{rationale_block}\n\n{snapshot_block}\n\n🤖 _System Mode: Paper / Advisory_"
    return full_message

def send_daily_digest_execution(decision: Dict[str, Any]) -> bool:
    """Main execution wrapper called at the tail end of daily cron run."""
    logger.info("Building and broadcasting Telegram daily digest...")
    try:
        msg_text = build_daily_digest(decision)
        from notifications.telegram_bot import send_message
        return send_message(msg_text, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Failed to generate or send daily digest: {e}")
        return False


def build_investor_digest(run: Dict[str, Any]) -> str:
    """Investor Mode digest: regime, exposure, target weights and the order list.

    Separate from `build_daily_digest` because the two modes share almost no
    vocabulary — a swing digest reporting "Bought 6x SCHNEIDER" next to an
    investor book of 25 target weights and a monthly rebalance is confusing
    rather than concise.
    """
    if hasattr(run, "as_dict"):
        run = run.as_dict()

    regime = run.get("regime", "?")
    exposure = run.get("exposure", 0.0)
    equity = run.get("equity", 0.0)
    cash = run.get("cash", 0.0)
    targets = run.get("targets") or {}
    orders = run.get("orders") or []
    filled = run.get("filled") or []
    news = run.get("news") or []
    as_of = run.get("as_of", "")

    icon = {"RISK_ON": "🟢", "MIXED": "🟡", "RISK_OFF": "🔴"}.get(regime, "⚪")

    head = f"{icon} *VYUHA INVESTOR MODE*"
    line_date = f"📅 _{escape_markdown_v2(str(as_of)[:10])}_"

    exposure_line = (
        f"🌡️ *Regime:* `{escape_markdown_v2(str(regime))}` "
        f"\\(equity target {exposure:.0%}\\)"
    )
    book_line = (
        f"💼 *Book:* ₹{equity:,.0f} total · ₹{cash:,.0f} cash · "
        f"{len(run.get('holdings') or {})} holdings"
    )

    blocks = [head, line_date, exposure_line, book_line]

    if run.get("is_rebalance"):
        blocks.append("🔄 *Monthly rebalance day*")

    top = sorted(targets.items(), key=lambda kv: -kv[1])[:8]
    if top:
        tg = "\n".join(
            f"• `{escape_markdown_v2(s)}` {w:.1%}" for s, w in top
        )
        blocks.append(f"🎯 *Top targets* \\({len(targets)} total\\):\n{tg}")

    if filled:
        fl = "\n".join(
            f"• `{escape_markdown_v2(str(f['symbol']))}` "
            f"{escape_markdown_v2(str(f['side']))} {f['qty']} @ ₹{f['price']:,.1f}"
            for f in filled[:12]
        )
        blocks.append(f"✅ *Paper fills today:*\n{fl}")

    if orders:
        ol = "\n".join(
            f"• `{escape_markdown_v2(str(o['symbol']))}` "
            f"*{escape_markdown_v2(str(o['side']))}* {o['qty']} "
            f"@ ~₹{o['limit_hint']:,.1f}\n  _{escape_markdown_v2(str(o['layer']))}: "
            f"{escape_markdown_v2(str(o['reason'])[:90])}_"
            for o in orders[:10]
        )
        blocks.append(f"📋 *Orders for next open* \\({len(orders)}\\):\n{ol}")
    else:
        blocks.append("📋 *Orders:* none")

    hard = [n for n in news if n.get("event_type") == "HARD_NEGATIVE"]
    if hard:
        hl = "\n".join(
            f"• `{escape_markdown_v2(str(n.get('symbol')))}` "
            f"{escape_markdown_v2(str(n.get('subtype') or ''))}"
            for n in hard[:5]
        )
        blocks.append(f"🚨 *Hard negatives* \\({len(hard)}\\):\n{hl}")

    blocks.append("🤖 _System Mode: Paper / Advisory — no live orders_")
    return "\n\n".join(blocks)


def send_investor_digest(message: str) -> bool:
    """Sends a pre-built Investor Mode digest."""
    try:
        from notifications.telegram_bot import send_message
        return send_message(message, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Failed to send investor digest: {e}")
        return False
