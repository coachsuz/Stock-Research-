"""
Stock Research Pipeline — My Portfolio Manager
================================================
Tracks your real portfolio (stocks you actually own) separately
from the paper tracking system. Shows:
- Current holdings with live P&L
- Buy/Sell/Trim signals based on quality score + valuation + thesis
- Alerts when action is needed
- Performance vs S&P 500

Add to your existing portfolio:
    python3 my_portfolio.py --add AAPL 150.00 50        # ticker, price, shares
    python3 my_portfolio.py --trim AAPL 180.00 25       # sell partial position
    python3 my_portfolio.py --sell AAPL 180.00          # full exit
    python3 my_portfolio.py                             # show full report
"""

import db_adapter as sqlite3
import yfinance as yf
import argparse
import time
from datetime import datetime

DB_PATH = "research.db"


# ── Database setup ────────────────────────────────────────────────────────────

def init_my_portfolio_db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS my_portfolio (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            shares          REAL NOT NULL,
            avg_cost        REAL NOT NULL,
            added_date      TEXT NOT NULL,
            notes           TEXT,
            status          TEXT DEFAULT 'open'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS my_transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            date            TEXT NOT NULL,
            action          TEXT NOT NULL,
            shares          REAL NOT NULL,
            price           REAL NOT NULL,
            notes           TEXT
        )
    """)

    conn.commit()
    conn.close()


# ── Position management ───────────────────────────────────────────────────────

def add_position(ticker, price, shares, notes=""):
    """Add a new holding or add to existing position."""
    ticker = ticker.upper()
    conn   = sqlite3.connect(DB_PATH)

    # Check if position exists
    existing = conn.execute("""
        SELECT id, shares, avg_cost FROM my_portfolio
        WHERE ticker = ? AND status = 'open'
    """, (ticker,)).fetchone()

    if existing:
        # Average down/up
        pos_id, old_shares, old_cost = existing
        new_shares   = old_shares + shares
        new_avg_cost = ((old_shares * old_cost) + (shares * price)) / new_shares
        conn.execute("""
            UPDATE my_portfolio SET shares=?, avg_cost=?
            WHERE id=?
        """, (new_shares, round(new_avg_cost, 4), pos_id))
        print(f"✅ Added {shares} shares of {ticker} — new avg cost: ${new_avg_cost:.2f}")
    else:
        conn.execute("""
            INSERT INTO my_portfolio (ticker, shares, avg_cost, added_date, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (ticker, shares, price, datetime.now().strftime("%Y-%m-%d"), notes))
        print(f"✅ Added {ticker} — {shares} shares at ${price:.2f}")

    # Log transaction
    conn.execute("""
        INSERT INTO my_transactions (ticker, date, action, shares, price, notes)
        VALUES (?, ?, 'buy', ?, ?, ?)
    """, (ticker, datetime.now().strftime("%Y-%m-%d"), shares, price, notes))

    conn.commit()
    conn.close()


def trim_position(ticker, price, shares, notes=""):
    """Sell a partial position."""
    ticker = ticker.upper()
    conn   = sqlite3.connect(DB_PATH)

    existing = conn.execute("""
        SELECT id, shares, avg_cost FROM my_portfolio
        WHERE ticker = ? AND status = 'open'
    """, (ticker,)).fetchone()

    if not existing:
        print(f"No open position found for {ticker}")
        conn.close()
        return

    pos_id, current_shares, avg_cost = existing
    if shares >= current_shares:
        print(f"Trim shares ({shares}) >= position size ({current_shares}). Use --sell to exit fully.")
        conn.close()
        return

    new_shares = current_shares - shares
    ret        = (price - avg_cost) / avg_cost * 100

    conn.execute("UPDATE my_portfolio SET shares=? WHERE id=?", (new_shares, pos_id))
    conn.execute("""
        INSERT INTO my_transactions (ticker, date, action, shares, price, notes)
        VALUES (?, ?, 'trim', ?, ?, ?)
    """, (ticker, datetime.now().strftime("%Y-%m-%d"), shares, price, notes))

    conn.commit()
    conn.close()
    print(f"{'✅' if ret > 0 else '❌'} Trimmed {shares} shares of {ticker} at ${price:.2f} — {ret:+.1f}% gain")
    print(f"   Remaining: {new_shares} shares")


def sell_position(ticker, price, notes=""):
    """Exit a full position."""
    ticker = ticker.upper()
    conn   = sqlite3.connect(DB_PATH)

    existing = conn.execute("""
        SELECT id, shares, avg_cost FROM my_portfolio
        WHERE ticker = ? AND status = 'open'
    """, (ticker,)).fetchone()

    if not existing:
        print(f"No open position found for {ticker}")
        conn.close()
        return

    pos_id, shares, avg_cost = existing
    ret = (price - avg_cost) / avg_cost * 100

    conn.execute("UPDATE my_portfolio SET status='closed' WHERE id=?", (pos_id,))
    conn.execute("""
        INSERT INTO my_transactions (ticker, date, action, shares, price, notes)
        VALUES (?, ?, 'sell', ?, ?, ?)
    """, (ticker, datetime.now().strftime("%Y-%m-%d"), shares, price, notes))

    conn.commit()
    conn.close()
    icon = "✅" if ret > 0 else "❌"
    print(f"{icon} Sold {ticker} — {shares} shares at ${price:.2f} — {ret:+.1f}% return")


# ── Signal generator ──────────────────────────────────────────────────────────

def get_signal(ticker, current_price, avg_cost, quality_score,
               margin_of_safety, rs_3m, days_until_earnings):
    """Generate action signal for an existing holding."""
    ret = (current_price - avg_cost) / avg_cost * 100 if avg_cost else 0

    signals  = []
    action   = "hold"

    # SELL signals
    if quality_score and quality_score <= 4:
        action = "sell"
        signals.append(f"Quality score dropped to {quality_score}/10 — thesis likely broken")
    elif ret < -20 and quality_score and quality_score < 6:
        action = "sell"
        signals.append(f"Down {ret:.1f}% with deteriorating quality — cut losses")

    # TRIM signals
    elif ret > 50:
        action = "trim"
        signals.append(f"Up {ret:+.1f}% — take some profit, let rest run")
    elif ret > 30 and margin_of_safety and margin_of_safety < 0:
        action = "trim"
        signals.append(f"Up {ret:+.1f}% and now above base target — trim into strength")
    elif rs_3m and rs_3m > 25:
        action = "trim"
        signals.append(f"Outperforming SPY by {rs_3m:.1f}% in 3 months — trim and rebalance")

    # ADD signals
    elif ret < -10 and quality_score and quality_score >= 7:
        action = "add"
        signals.append(f"Down {ret:.1f}% — thesis intact, consider adding")
    elif margin_of_safety and margin_of_safety > 30 and quality_score and quality_score >= 7:
        action = "add"
        signals.append(f"Still {margin_of_safety:.0f}% below base target — room to add")

    # WATCH before earnings
    elif days_until_earnings and days_until_earnings <= 14:
        action  = "watch"
        signals.append(f"Earnings in {days_until_earnings} days — hold and prepare")

    # HOLD
    else:
        signals.append("Thesis intact — hold")

    return action, signals


# ── Portfolio report ──────────────────────────────────────────────────────────

def run_report():
    conn = sqlite3.connect(DB_PATH)

    positions = conn.execute("""
        SELECT p.ticker, p.shares, p.avg_cost, p.added_date, p.notes,
               f.quality_score, f.name,
               v.base_target, v.margin_of_safety,
               r.rs_3m, r.rs_6m,
               e.days_until
        FROM my_portfolio p
        LEFT JOIN fundamentals f ON p.ticker = f.ticker
        LEFT JOIN valuations v ON p.ticker = v.ticker
        LEFT JOIN relative_strength r ON p.ticker = r.ticker
        LEFT JOIN earnings_dates e ON p.ticker = e.ticker
        WHERE p.status = 'open'
        ORDER BY p.added_date
    """).fetchall()

    conn.close()

    if not positions:
        print("\nNo positions yet. Use --add TICKER PRICE SHARES to add holdings.")
        return

    print(f"\n{'='*65}")
    print("MY PORTFOLIO")
    print(f"{'='*65}")

    total_value     = 0
    total_cost      = 0
    action_needed   = []

    for row in positions:
        (ticker, shares, avg_cost, date, notes,
         score, name, base_target, mos,
         rs_3m, rs_6m, days_earn) = row

        # Get live price
        try:
            t             = yf.Ticker(ticker)
            current_price = t.info.get("regularMarketPrice") or avg_cost
        except Exception:
            current_price = avg_cost

        market_value  = current_price * shares
        cost_basis    = avg_cost * shares
        ret           = (current_price - avg_cost) / avg_cost * 100
        gain_loss     = market_value - cost_basis

        total_value += market_value
        total_cost  += cost_basis

        action, signals = get_signal(
            ticker, current_price, avg_cost,
            score, mos, rs_3m, days_earn
        )

        action_icons = {
            "sell":  "🔴 SELL",
            "trim":  "🟡 TRIM",
            "add":   "💚 ADD",
            "watch": "👀 WATCH",
            "hold":  "⏸  HOLD",
        }

        ret_icon = "📈" if ret >= 0 else "📉"
        print(f"\n  {ret_icon} {ticker:<6} {name or ''}")
        print(f"     {shares:.0f} shares  ·  avg cost: ${avg_cost:.2f}  ·  current: ${current_price:.2f}")
        print(f"     Return: {ret:+.1f}%  ·  Gain/Loss: ${gain_loss:+,.0f}  ·  Value: ${market_value:,.0f}")
        if score:
            print(f"     Quality: {score}/10  ·  Base target: ${base_target:.2f}" if base_target else f"     Quality: {score}/10")
        print(f"     Signal: {action_icons.get(action, action)}")
        for s in signals:
            print(f"       • {s}")

        if action in ["sell", "trim"]:
            action_needed.append((ticker, action, signals[0]))

        time.sleep(0.2)

    # Summary
    total_ret = (total_value - total_cost) / total_cost * 100 if total_cost else 0
    print(f"\n{'='*65}")
    print(f"  Total value:     ${total_value:>10,.0f}")
    print(f"  Total cost:      ${total_cost:>10,.0f}")
    print(f"  Total gain/loss: ${total_value-total_cost:>+10,.0f}  ({total_ret:+.1f}%)")
    print(f"{'='*65}")

    if action_needed:
        print(f"\n⚠️  ACTION REQUIRED:")
        for ticker, action, reason in action_needed:
            print(f"  {ticker}: {action.upper()} — {reason}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_my_portfolio_db()

    p = argparse.ArgumentParser(description="My real portfolio tracker")
    p.add_argument("--add",  nargs="+", metavar=("TICKER", "PRICE"),
                   help="Add position: --add TICKER PRICE SHARES")
    p.add_argument("--trim", nargs="+", metavar=("TICKER", "PRICE"),
                   help="Trim position: --trim TICKER PRICE SHARES")
    p.add_argument("--sell", nargs=2, metavar=("TICKER", "PRICE"),
                   help="Exit position: --sell TICKER PRICE")
    args = p.parse_args()

    if args.add:
        ticker = args.add[0]
        price  = float(args.add[1])
        shares = float(args.add[2]) if len(args.add) > 2 else 100
        notes  = args.add[3] if len(args.add) > 3 else ""
        add_position(ticker, price, shares, notes)
    elif args.trim:
        ticker = args.trim[0]
        price  = float(args.trim[1])
        shares = float(args.trim[2]) if len(args.trim) > 2 else 0
        add_position(ticker, price, -shares)
        trim_position(ticker, price, shares)
    elif args.sell:
        ticker, price = args.sell
        sell_position(ticker, float(price))
    else:
        run_report()
