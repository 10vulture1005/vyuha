import sys
from pathlib import Path
from decimal import Decimal
from loguru import logger
import re

# Ensure imports work from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.session import get_session
from db.models import PortfolioValueHistory, PortfolioHolding, TradeLog, HoldingStatus
from config.settings import settings

def create_progress_bar(pct: float, width: int = 15) -> str:
    """Creates a visual progress bar like ████░░░░░░"""
    pct = max(0.0, min(100.0, pct))
    filled_chars = int(round((pct / 100.0) * width))
    empty_chars = width - filled_chars
    return "█" * filled_chars + "░" * empty_chars

def generate_markdown() -> str:
    with get_session() as session:
        latest_val = session.query(PortfolioValueHistory).order_by(PortfolioValueHistory.date.desc()).first()
        if not latest_val:
            return "*No forward test data available yet.*"
            
        open_holdings = session.query(PortfolioHolding).filter(PortfolioHolding.status == HoldingStatus.OPEN.value).all()
        trades = session.query(TradeLog).filter(TradeLog.txn_type == "SELL").all()
        
        # Metrics
        total_val = float(latest_val.total_value)
        cash = float(latest_val.cash_balance)
        invested = float(latest_val.invested_value)
        
        from db.models import CapitalLedger
        sip_credits = session.query(CapitalLedger).filter(CapitalLedger.txn_type == "SIP_CREDIT").all()
        total_principal = float(sum(c.amount for c in sip_credits))
        if total_principal == 0:
            total_principal = settings.INITIAL_CAPITAL
            
        pnl = total_val - total_principal
        pnl_pct = (pnl / total_principal) * 100 if total_principal > 0 else 0
        
        win_rate = 0.0
        if trades:
            wins = sum(1 for t in trades if t.realized_pnl and t.realized_pnl > 0)
            win_rate = (wins / len(trades)) * 100
            
        heat_pct = 0.0
        if total_val > 0:
            heat_pct = sum([float((h.qty * h.initial_risk) / Decimal(str(total_val))) for h in open_holdings if h.initial_risk > 0]) * 100
            
        # Drawdown bar (0 to 20%)
        dd_pct = float(latest_val.drawdown_pct)
        dd_bar = create_progress_bar(min(100, (-dd_pct / 20.0) * 100) if dd_pct < 0 else 0)
        
        # Heat bar (0 to MAX_PORTFOLIO_HEAT)
        heat_bar_pct = min(100, (heat_pct / (settings.MAX_PORTFOLIO_HEAT_PCT * 100)) * 100)
        heat_bar = create_progress_bar(heat_bar_pct)
        
        md = f"""
> **Last Updated:** `{latest_val.date.isoformat()}` | **Total Value:** `₹{total_val:,.2f}` | **Cash:** `₹{cash:,.2f}`

<div align="center">

| Metric | Value | Graphic |
|---|---|---|
| **Net PnL** | {'🟢' if pnl >= 0 else '🔴'} ₹{pnl:,.2f} ({pnl_pct:+.2f}%) | - |
| **Win Rate** | {win_rate:.1f}% | {create_progress_bar(win_rate)} |
| **Max Drawdown** | {dd_pct:.2f}% | {dd_bar} |
| **Portfolio Heat** | {heat_pct:.2f}% / {settings.MAX_PORTFOLIO_HEAT_PCT * 100:.2f}% | {heat_bar} |

</div>

### 💼 Current Open Positions ({len(open_holdings)} / {settings.MAX_POSITIONS})

"""
        if not open_holdings:
            md += "*Holding 100% Cash.*"
        else:
            md += "| Symbol | Qty | Avg Buy | Trailing Stop | Risk/Share | Pattern |\n"
            md += "|---|---|---|---|---|---|\n"
            for h in open_holdings:
                md += f"| **{h.symbol}** | {h.qty} | ₹{h.avg_buy_price:,.2f} | ₹{h.trailing_stop_price:,.2f} | ₹{h.initial_risk:,.2f} | `{h.entry_pattern}` |\n"
                
        return md.strip()

def main():
    logger.info("Generating live stats for README.md...")
    try:
        new_stats_md = generate_markdown()
    except Exception as e:
        logger.error(f"Failed to generate stats: {e}")
        return

    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    if not readme_path.exists():
        logger.error("README.md not found!")
        return

    content = readme_path.read_text(encoding="utf-8")
    
    # Replace content between tags
    pattern = r"(<!-- LIVE_STATS_START -->\n).*?(\n<!-- LIVE_STATS_END -->)"
    
    if re.search(pattern, content, re.DOTALL):
        new_content = re.sub(pattern, rf"\1{new_stats_md}\2", content, flags=re.DOTALL)
        readme_path.write_text(new_content, encoding="utf-8")
        logger.info("Successfully injected live stats into README.md")
    else:
        logger.warning("Could not find LIVE_STATS tags in README.md")

if __name__ == "__main__":
    main()
