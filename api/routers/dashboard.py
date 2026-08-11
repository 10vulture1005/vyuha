# api/routers/dashboard.py
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from db.session import get_session_dependency
from db.models import PortfolioHolding, HoldingStatus, Watchlist, WatchlistStatus, CapitalLedger
from data.live_market_api import LiveMarketAPI

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

@router.get("/", response_class=HTMLResponse)
def get_live_dashboard(request: Request, session: Session = Depends(get_session_dependency)):
    """Renders a simple HTML dashboard with live market prices."""
    
    # 1. Get Portfolio
    holdings = session.query(PortfolioHolding).filter(
        PortfolioHolding.status == HoldingStatus.OPEN.value
    ).all()
    
    # 2. Get Cash
    last_txn = session.query(CapitalLedger).order_by(CapitalLedger.id.desc()).first()
    cash = last_txn.running_balance if last_txn else 0.0
    
    # 3. Get Top 5 Watchlist
    watchlist = session.query(Watchlist).filter(
        Watchlist.status == WatchlistStatus.ACTIVE.value
    ).order_by(Watchlist.conviction_score.desc()).limit(5).all()
    
    # 4. Fetch Live Quotes in Batch
    symbols_to_fetch = [h.symbol for h in holdings] + [w.symbol for w in watchlist]
    symbols_to_fetch = list(set(symbols_to_fetch)) # unique
    
    live_quotes = []
    if symbols_to_fetch:
        # Fetching with units (res=val) for display
        live_quotes = LiveMarketAPI.get_batch_quotes(symbols_to_fetch, with_units=True)
        
    quote_map = {q.get("symbol"): q for q in live_quotes}
    
    # Render HTML (In a real app, use Jinja2 Templates)
    html_content = """
    <html>
        <head>
            <title>VYUHA Live Dashboard</title>
            <style>
                body { font-family: 'Inter', sans-serif; background-color: #0f172a; color: #f8fafc; padding: 2rem; }
                h1, h2 { color: #38bdf8; }
                .card { background-color: #1e293b; padding: 1.5rem; border-radius: 8px; margin-bottom: 2rem; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1); }
                table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
                th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid #334155; }
                th { color: #94a3b8; font-weight: 600; }
                .positive { color: #4ade80; }
                .negative { color: #f87171; }
            </style>
        </head>
        <body>
            <h1>VYUHA Engine | Live Dashboard</h1>
            
            <div class="card">
                <h2>Active Portfolio (Single Slot)</h2>
                <p><strong>Available Cash:</strong> ₹{cash:,.2f}</p>
                <table>
                    <tr>
                        <th>Symbol</th>
                        <th>Qty</th>
                        <th>Avg Buy Price</th>
                        <th>Trailing Stop</th>
                        <th>LIVE PRICE</th>
                        <th>% Change Today</th>
                        <th>Unrealized PnL</th>
                    </tr>
    """
    html_content = html_content.replace("{cash:,.2f}", f"{cash:,.2f}")
    
    if not holdings:
        html_content += "<tr><td colspan='7'>No open positions. Sitting in cash.</td></tr>"
    else:
        for h in holdings:
            q = quote_map.get(h.symbol, {})
            last_price = q.get("last_price", {}).get("value", 0.0)
            pct_change = q.get("percent_change", {}).get("value", 0.0)
            
            color_class = "positive" if pct_change >= 0 else "negative"
            pnl = (float(last_price) - float(h.avg_buy_price)) * float(h.qty) if last_price else 0.0
            pnl_color = "positive" if pnl >= 0 else "negative"
            
            html_content += f"""
                    <tr>
                        <td>{h.symbol}</td>
                        <td>{h.qty}</td>
                        <td>₹{h.avg_buy_price:,.2f}</td>
                        <td>₹{h.trailing_stop_price:,.2f}</td>
                        <td style="font-weight: bold; color: #fbbf24;">₹{last_price:,.2f}</td>
                        <td class="{color_class}">{pct_change}%</td>
                        <td class="{pnl_color}">₹{pnl:,.2f}</td>
                    </tr>
            """
            
    html_content += """
                </table>
            </div>
            
            <div class="card">
                <h2>Top Watchlist (Fundamental Passers)</h2>
                <table>
                    <tr>
                        <th>Symbol</th>
                        <th>FCS Conviction</th>
                        <th>LIVE PRICE</th>
                        <th>% Change Today</th>
                        <th>Volume</th>
                        <th>Market Cap</th>
                    </tr>
    """
    
    if not watchlist:
        html_content += "<tr><td colspan='6'>Watchlist is empty.</td></tr>"
    else:
        for w in watchlist:
            q = quote_map.get(w.symbol, {})
            last_price = q.get("last_price", {}).get("value", 0.0)
            pct_change = q.get("percent_change", {}).get("value", 0.0)
            vol = q.get("volume", {}).get("value", "N/A")
            vol_unit = q.get("volume", {}).get("unit", "")
            mcap = q.get("market_cap", {}).get("value", "N/A")
            mcap_unit = q.get("market_cap", {}).get("unit", "")
            
            color_class = "positive" if pct_change >= 0 else "negative"
            
            html_content += f"""
                    <tr>
                        <td>{w.symbol}</td>
                        <td>{w.conviction_score:.2f}</td>
                        <td>₹{last_price:,.2f}</td>
                        <td class="{color_class}">{pct_change}%</td>
                        <td>{vol} {vol_unit}</td>
                        <td>{mcap} {mcap_unit}</td>
                    </tr>
            """
            
    html_content += """
                </table>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)
