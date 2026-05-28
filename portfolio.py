"""
Stock Research Pipeline — Portfolio Tracker & Action Engine
============================================================
Tracks paper portfolio performance and generates Buy/Sell/Hold/Trim/Add
signals based on quality score, thesis status, momentum, and valuation.

Usage:
    python3 portfolio.py                    # show current portfolio + signals
    python3 portfolio.py --add ANET 185.50  # log a new position at price
    python3 portfolio.py --exit ANET 210.00 # log an exit
    python3 portfolio.py --override ANET hold "Waiting for Q2 earnings"
"""

import sqlite3
import yfinance as yf
import argparse
from datetime import datetime, timedelta

DB_PATH = "research.db"


# ── Database setup ────────────────────────────────────────────────────────────

def init_portfolio_db():
    conn = sqlite3.connect(DB_PATH)

    # Portfolio positions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            entry_price     REAL NOT NULL,
            entry_date      TEXT NOT NULL,
            exit_price      REAL,
            exit_date       TEXT,
            shares          REAL DEFAULT 100,
            status          TEXT DEFAULT 'open',
            entry_score     INTEGER,
            entry_thesis    TEXT,
            notes           TEXT
        )
    """)

    # Action log — every signal generated and every override
    conn.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at       TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            action          TEXT NOT NULL,
            reason          TEXT,
            price           REAL,
            score           INTEGER,
            is_override     INTEGER DEFAULT 0
        )
    """)

    # Thesis tracking — quarterly check-ins
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thesis_tracker (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            checked_at      TEXT NOT NULL,
            indicator_1     TEXT,
            indicator_2     TEXT,
            indicator_3     TEXT,
            status_1        TEXT,
            status_2        TEXT,
            status_3        TEXT,
            overall_status  TEXT,
            notes           TEXT
        )
    """)

    conn.commit()
    conn.close()


# ── Action engine ─────────────────────────────────────────────────────────────

def get_buy_price(ticker, current_price):
    """Suggest a buy price based on 52-week range position."""
    if not current_price:
        return None, None
    try:
        t    = yf.Ticker(ticker)
        info = t.info
        low  = info.get("fiftyTwoWeekLow")
        high = info.get("fiftyTwoWeekHigh")
        if not low or not high:
            return current_price, "Buy at market"
        range_pct = (current_price - low) / (high - low) if high != low else 0.5
        if range_pct < 0.35:
            return round(current_price * 1.01, 2), "Near 52w low — buy at market"
        elif range_pct < 0.65:
            suggested = round(current_price * 0.96, 2)
            return suggested, f"Mid-range — ideal entry ~4% lower at ${suggested}"
        else:
            suggested = round(current_price * 0.92, 2)
            return suggested, f"Near 52w high — wait for ~8% pullback to ${suggested}"
    except Exception:
        return current_price, "Buy at market"


def generate_action(ticker, data):
    """
    Generate a Buy/Sell/Hold/Trim/Add/Watch signal based on rules.
    Returns (action, reasons, confidence)
    """
    score      = data.get("quality_score", 0)
    conviction = data.get("conviction", "medium")
    ret_3m     = data.get("return_3m")
    ret_6m     = data.get("return_6m")
    short_pct  = data.get("short_pct_float", 0) or 0
    days_earn  = data.get("days_until_earnings")
    in_port    = data.get("in_portfolio", False)
    rev_trend  = data.get("revision_trend", "neutral")
    thesis_ok  = data.get("thesis_status", "active")

    reasons = []
    score_action = "hold"

    # ── BUY signals ───────────────────────────────────────────────────────────
    if not in_port:
        if score >= 8 and conviction == "high":
            score_action = "buy"
            reasons.append(f"Quality score {score}/10 with high conviction")
        elif score >= 7 and conviction == "high" and rev_trend == "rising":
            score_action = "buy"
            reasons.append(f"Score {score}/10, estimates rising, high conviction")
        elif score >= 8 and short_pct > 0.15:
            score_action = "buy"
            reasons.append(f"Score {score}/10 with {short_pct*100:.0f}% short — squeeze setup")

    # ── ADD signals ───────────────────────────────────────────────────────────
    if in_port:
        if score >= 7 and ret_3m and ret_3m < -0.10 and thesis_ok == "active":
            score_action = "add"
            reasons.append(f"Thesis intact, stock down {abs(ret_3m)*100:.0f}% — add opportunity")

    # ── TRIM signals ──────────────────────────────────────────────────────────
    if in_port:
        if ret_6m and ret_6m > 0.50:
            score_action = "trim"
            reasons.append(f"Up {ret_6m*100:.0f}% in 6 months — take some off the table")
        elif score >= 7 and ret_3m and ret_3m > 0.30:
            score_action = "trim"
            reasons.append(f"Up {ret_3m*100:.0f}% in 3 months — trim into strength")

    # ── SELL signals ──────────────────────────────────────────────────────────
    if in_port:
        if score <= 4:
            score_action = "sell"
            reasons.append(f"Quality score dropped to {score}/10 — thesis broken")
        elif thesis_ok == "broken":
            score_action = "sell"
            reasons.append("Thesis broken — exit position")
        elif score <= 5 and rev_trend == "falling":
            score_action = "sell"
            reasons.append(f"Score {score}/10 and estimates falling — deteriorating")

    # ── WATCH signals ─────────────────────────────────────────────────────────
    if score == 6 and not in_port:
        score_action = "watch"
        reasons.append(f"Score {score}/10 — interesting but needs higher conviction")
    if days_earn and days_earn <= 14 and not in_port:
        score_action = "watch"
        reasons.append(f"Earnings in {days_earn} days — wait for the print")

    # ── HOLD ──────────────────────────────────────────────────────────────────
    if score_action == "hold" and in_port:
        reasons.append("Thesis intact, no action needed")
    elif score_action == "hold" and not in_port:
        score_action = "watch"
        reasons.append(f"Score {score}/10 — monitoring")

    # Confidence
    confidence = "high" if len(reasons) >= 2 else "medium" if reasons else "low"

    return score_action, reasons, confidence


def get_current_price(ticker):
    try:
        t = yf.Ticker(ticker)
        return t.info.get("regularMarketPrice") or t.info.get("currentPrice")
    except Exception:
        return None


# ── Portfolio management ──────────────────────────────────────────────────────

def add_position(ticker, price, shares=100, notes=""):
    conn = sqlite3.connect(DB_PATH)

    # Get current quality score and thesis
    row = conn.execute("""
        SELECT f.quality_score, a.summary
        FROM fundamentals f
        LEFT JOIN ai_summaries a ON f.ticker = a.ticker AND a.summary_type = 'thesis'
        WHERE f.ticker = ?
        ORDER BY f.fetched_at DESC, a.generated_at DESC LIMIT 1
    """, (ticker,)).fetchone()

    score  = row[0] if row else None
    thesis = row[1] if row else None

    conn.execute("""
        INSERT INTO portfolio (ticker, entry_price, entry_date, shares, status, entry_score, entry_thesis, notes)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
    """, (ticker, price, datetime.now().strftime("%Y-%m-%d"), shares, score, thesis, notes))

    conn.execute("""
        INSERT INTO action_log (logged_at, ticker, action, reason, price, score)
        VALUES (?, ?, 'buy', 'Position opened', ?, ?)
    """, (datetime.now().isoformat(), ticker, price, score))

    conn.commit()
    conn.close()
    print(f"✅ Added {ticker} at ${price:.2f} — {shares} shares")


def exit_position(ticker, price, notes=""):
    conn = sqlite3.connect(DB_PATH)

    pos = conn.execute("""
        SELECT id, entry_price, shares FROM portfolio
        WHERE ticker = ? AND status = 'open'
        ORDER BY entry_date DESC LIMIT 1
    """, (ticker,)).fetchone()

    if not pos:
        print(f"No open position found for {ticker}")
        conn.close()
        return

    pos_id, entry_price, shares = pos
    ret = (price - entry_price) / entry_price * 100

    conn.execute("""
        UPDATE portfolio SET exit_price=?, exit_date=?, status='closed'
        WHERE id=?
    """, (price, datetime.now().strftime("%Y-%m-%d"), pos_id))

    conn.execute("""
        INSERT INTO action_log (logged_at, ticker, action, reason, price)
        VALUES (?, ?, 'sell', ?, ?)
    """, (datetime.now().isoformat(), ticker, f"Exit: {ret:+.1f}% return", price))

    conn.commit()
    conn.close()
    print(f"{'✅' if ret > 0 else '❌'} Exited {ticker} at ${price:.2f} — {ret:+.1f}% return")


def log_override(ticker, action, reason):
    conn = sqlite3.connect(DB_PATH)
    price = get_current_price(ticker)
    conn.execute("""
        INSERT INTO action_log (logged_at, ticker, action, reason, price, is_override)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (datetime.now().isoformat(), ticker, action, reason, price))
    conn.commit()
    conn.close()
    print(f"📝 Override logged: {ticker} → {action.upper()} — {reason}")


# ── Performance tracking ──────────────────────────────────────────────────────

def calculate_returns():
    conn  = sqlite3.connect(DB_PATH)
    open_positions = conn.execute("""
        SELECT ticker, entry_price, entry_date, shares, entry_score
        FROM portfolio WHERE status = 'open'
        ORDER BY entry_date
    """).fetchall()
    closed_positions = conn.execute("""
        SELECT ticker, entry_price, exit_price, entry_date, exit_date, shares
        FROM portfolio WHERE status = 'closed'
        ORDER BY exit_date DESC
    """).fetchall()
    conn.close()

    results = []
    for ticker, entry, date, shares, score in open_positions:
        current = get_current_price(ticker)
        if current:
            ret     = (current - entry) / entry * 100
            days    = (datetime.now() - datetime.strptime(date, "%Y-%m-%d")).days
            results.append({
                "ticker": ticker, "entry": entry, "current": current,
                "return_pct": ret, "days_held": days,
                "entry_score": score, "status": "open"
            })

    return results, closed_positions


# ── Main dashboard ────────────────────────────────────────────────────────────

def run_portfolio_report():
    conn = sqlite3.connect(DB_PATH)

    # Get all watchlist stocks with signals data
    stocks = conn.execute("""
        SELECT f.ticker, f.name, f.quality_score,
               s.short_pct_float, s.signal as short_signal,
               e.days_until as days_earn,
               ar.revision_trend,
               a.summary as thesis,
               a.raw_response
        FROM fundamentals f
        INNER JOIN (
            SELECT ticker, MAX(fetched_at) as latest
            FROM fundamentals GROUP BY ticker
        ) l ON f.ticker = l.ticker AND f.fetched_at = l.latest
        LEFT JOIN short_interest s ON f.ticker = s.ticker
        LEFT JOIN earnings_dates e ON f.ticker = e.ticker
        LEFT JOIN analyst_revisions ar ON f.ticker = ar.ticker
        LEFT JOIN ai_summaries a ON f.ticker = a.ticker AND a.summary_type = 'thesis'
        ORDER BY f.quality_score DESC
    """).fetchall()

    open_tickers = {r[0] for r in conn.execute(
        "SELECT ticker FROM portfolio WHERE status='open'"
    ).fetchall()}

    conn.close()

    print("\n" + "=" * 65)
    print("PORTFOLIO ACTION ENGINE")
    print("=" * 65)

    action_groups = {"buy": [], "add": [], "trim": [], "sell": [], "watch": [], "hold": []}

    import json as _json
    for row in stocks:
        (ticker, name, score, short_pct, short_sig,
         days_earn, rev_trend, thesis, raw_resp) = row

        # Extract conviction from Claude's raw response
        conviction = "medium"
        try:
            if raw_resp:
                raw = _json.loads(raw_resp)
                conviction = raw.get("conviction", "medium")
        except Exception:
            pass

        data = {
            "quality_score":       score,
            "conviction":          conviction,
            "short_pct_float":     short_pct,
            "days_until_earnings": days_earn,
            "revision_trend":      rev_trend or "neutral",
            "in_portfolio":        ticker in open_tickers,
            "thesis_status":       "active",
        }

        action, reasons, confidence = generate_action(ticker, data)
        action_groups[action].append((ticker, score, reasons, confidence))

    icons = {
        "buy":   "🟢 BUY",
        "add":   "💚 ADD",
        "trim":  "🟡 TRIM",
        "sell":  "🔴 SELL",
        "watch": "👀 WATCH",
        "hold":  "⏸  HOLD",
    }

    for action in ["buy", "add", "trim", "sell", "watch", "hold"]:
        stocks_in_group = action_groups[action]
        if not stocks_in_group:
            continue
        print(f"\n{icons[action]} ({len(stocks_in_group)} stocks)")
        print("-" * 50)
        for ticker, score, reasons, conf in stocks_in_group:
            print(f"  {ticker:<6} Score:{score}/10  Confidence:{conf}")
            for r in reasons:
                print(f"         • {r}")
            if action in ["buy", "add"]:
                current = get_current_price(ticker)
                if current:
                    buy_price, buy_note = get_buy_price(ticker, current)
                    print(f"         💰 Current: ${current:.2f}  →  {buy_note}")

    # Performance summary
    print("\n" + "=" * 65)
    print("OPEN POSITIONS")
    print("=" * 65)
    returns, closed = calculate_returns()
    if not returns:
        print("  No open positions. Use --add TICKER PRICE to log a position.")
    else:
        total_ret = sum(r["return_pct"] for r in returns) / len(returns)
        for r in sorted(returns, key=lambda x: x["return_pct"], reverse=True):
            icon = "📈" if r["return_pct"] > 0 else "📉"
            print(f"  {icon} {r['ticker']:<6} entry:${r['entry']:.2f}  "
                  f"current:${r['current']:.2f}  "
                  f"return:{r['return_pct']:+.1f}%  "
                  f"({r['days_held']}d held)")
        print(f"\n  Average return: {total_ret:+.1f}%")

    if closed:
        print("\nCLOSED POSITIONS (recent)")
        for ticker, entry, exit_p, entry_d, exit_d, shares in closed[:5]:
            ret = (exit_p - entry) / entry * 100
            icon = "✅" if ret > 0 else "❌"
            print(f"  {icon} {ticker:<6} {entry_d} → {exit_d}  {ret:+.1f}%")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_portfolio_db()

    p = argparse.ArgumentParser(description="Portfolio tracker and action engine")
    p.add_argument("--add",      nargs=2, metavar=("TICKER", "PRICE"), help="Log a new position")
    p.add_argument("--exit",     nargs=2, metavar=("TICKER", "PRICE"), help="Log an exit")
    p.add_argument("--override", nargs=3, metavar=("TICKER", "ACTION", "REASON"), help="Log a manual override")
    args = p.parse_args()

    if args.add:
        ticker, price = args.add
        add_position(ticker.upper(), float(price))
    elif args.exit:
        ticker, price = args.exit
        exit_position(ticker.upper(), float(price))
    elif args.override:
        ticker, action, reason = args.override
        log_override(ticker.upper(), action, reason)
    else:
        run_portfolio_report()
