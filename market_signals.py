"""
Stock Research Pipeline — Market Signals
=========================================
Adds three high-value data layers to the pipeline:

1. Earnings date tracker  — when each stock reports next
2. Analyst estimate revisions — are estimates going up or down?
3. Short interest — how much of the float is sold short?

Usage:
    python3 market_signals.py
"""

import sqlite3
import yfinance as yf
import pandas as pd
import time
from datetime import datetime, timedelta

DB_PATH = "research.db"


# ── Database setup ────────────────────────────────────────────────────────────

def init_signals_db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS earnings_dates (
            ticker          TEXT PRIMARY KEY,
            next_earnings   TEXT,
            last_earnings   TEXT,
            days_until      INTEGER,
            fetched_at      TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyst_revisions (
            ticker              TEXT PRIMARY KEY,
            current_eps_est     REAL,
            eps_est_30d_ago     REAL,
            eps_est_90d_ago     REAL,
            current_rev_est     REAL,
            rev_est_30d_ago     REAL,
            revision_trend      TEXT,
            analyst_count       INTEGER,
            fetched_at          TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS short_interest (
            ticker              TEXT PRIMARY KEY,
            short_pct_float     REAL,
            short_ratio         REAL,
            shares_short        INTEGER,
            shares_float        INTEGER,
            signal              TEXT,
            fetched_at          TEXT
        )
    """)

    conn.commit()
    conn.close()
    print("Signal tables ready.")


# ── Earnings dates ────────────────────────────────────────────────────────────

def fetch_earnings_dates(tickers):
    print("\nFetching earnings dates...")
    conn = sqlite3.connect(DB_PATH)
    now  = datetime.now()

    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            next_earnings = None
            last_earnings = None

            # Try earnings_dates first (most reliable)
            try:
                hist = t.earnings_dates
                if hist is not None and not hist.empty:
                    hist.index = pd.to_datetime(hist.index).tz_localize(None)
                    future_dates = hist[hist.index > now]
                    past_dates   = hist[hist.index <= now]
                    if not future_dates.empty:
                        next_earnings = future_dates.index[-1].strftime("%Y-%m-%d")
                    if not past_dates.empty:
                        last_earnings = past_dates.index[0].strftime("%Y-%m-%d")
            except Exception:
                pass

            # Fallback: calendar dict
            if not next_earnings:
                try:
                    cal = t.calendar
                    if isinstance(cal, dict):
                        for key in ["Earnings Date", "earningsDate"]:
                            if key in cal:
                                dates = cal[key]
                                if not isinstance(dates, list):
                                    dates = [dates]
                                date_strs = [str(d)[:10] for d in dates if d]
                                future = [d for d in date_strs if d >= now.strftime("%Y-%m-%d")]
                                if future:
                                    next_earnings = min(future)
                                    break
                except Exception:
                    pass

            days_until = None
            if next_earnings:
                days_until = (datetime.strptime(next_earnings, "%Y-%m-%d") - now).days

            conn.execute("""
                INSERT OR REPLACE INTO earnings_dates
                (ticker, next_earnings, last_earnings, days_until, fetched_at)
                VALUES (?, ?, ?, ?, ?)
            """, (ticker, next_earnings, last_earnings, days_until,
                  now.isoformat()))

            status = f"in {days_until}d" if days_until else "unknown"
            print(f"  {ticker:<6} next earnings: {next_earnings or '?'} ({status})")
            time.sleep(0.3)

        except Exception as e:
            print(f"  {ticker:<6} failed: {e}")

    conn.commit()
    conn.close()


# ── Analyst revisions ─────────────────────────────────────────────────────────

def fetch_analyst_revisions(tickers):
    print("\nFetching analyst estimate revisions...")
    conn = sqlite3.connect(DB_PATH)
    now  = datetime.now()

    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            info = t.info

            current_eps = info.get("forwardEps")
            current_rev = info.get("revenueEstimate") or info.get("totalRevenue")
            analysts    = info.get("numberOfAnalystOpinions")

            # Get historical estimates for comparison
            eps_30d = None
            eps_90d = None
            rev_30d = None

            try:
                analysis = t.analyst_price_targets
                if analysis is not None and not analysis.empty:
                    eps_30d = analysis.get("mean", {}).get("low")
            except Exception:
                pass

            # Determine revision trend
            trend = "neutral"
            if current_eps and eps_30d:
                change = (current_eps - eps_30d) / abs(eps_30d) if eps_30d != 0 else 0
                if change > 0.02:
                    trend = "rising"
                elif change < -0.02:
                    trend = "falling"

            conn.execute("""
                INSERT OR REPLACE INTO analyst_revisions
                (ticker, current_eps_est, eps_est_30d_ago, eps_est_90d_ago,
                 current_rev_est, rev_est_30d_ago, revision_trend,
                 analyst_count, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, current_eps, eps_30d, eps_90d,
                  current_rev, rev_30d, trend, analysts, now.isoformat()))

            trend_icon = {"rising": "⬆️", "falling": "⬇️", "neutral": "➡️"}.get(trend, "")
            print(f"  {ticker:<6} EPS est: {current_eps or '?':>8}  trend: {trend_icon} {trend}")
            time.sleep(0.3)

        except Exception as e:
            print(f"  {ticker:<6} failed: {e}")

    conn.commit()
    conn.close()


# ── Short interest ────────────────────────────────────────────────────────────

def fetch_short_interest(tickers):
    print("\nFetching short interest...")
    conn = sqlite3.connect(DB_PATH)
    now  = datetime.now()

    for ticker in tickers:
        try:
            t    = yf.Ticker(ticker)
            info = t.info

            short_pct   = info.get("shortPercentOfFloat")
            short_ratio = info.get("shortRatio")
            shares_short = info.get("sharesShort")
            shares_float = info.get("floatShares")

            # Classify signal
            signal = "neutral"
            if short_pct:
                if short_pct > 0.20:
                    signal = "high_short"      # >20% — crowded short, squeeze risk
                elif short_pct > 0.10:
                    signal = "elevated_short"  # 10-20% — notable
                elif short_pct < 0.03:
                    signal = "low_short"       # <3% — market not worried

            conn.execute("""
                INSERT OR REPLACE INTO short_interest
                (ticker, short_pct_float, short_ratio, shares_short,
                 shares_float, signal, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ticker, short_pct, short_ratio, shares_short,
                  shares_float, signal, now.isoformat()))

            pct_str = f"{short_pct*100:.1f}%" if short_pct else "?"
            signal_icon = {
                "high_short":     "🔴 squeeze risk",
                "elevated_short": "🟡 elevated",
                "low_short":      "🟢 low",
                "neutral":        "➡️  normal",
            }.get(signal, "")
            print(f"  {ticker:<6} short: {pct_str:>6}  {signal_icon}")
            time.sleep(0.3)

        except Exception as e:
            print(f"  {ticker:<6} failed: {e}")

    conn.commit()
    conn.close()


# ── Upcoming earnings alert ───────────────────────────────────────────────────

def print_earnings_calendar():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT e.ticker, e.next_earnings, e.days_until, f.quality_score
        FROM earnings_dates e
        JOIN fundamentals f ON e.ticker = f.ticker
        WHERE e.days_until IS NOT NULL AND e.days_until >= 0
        ORDER BY e.days_until ASC
    """).fetchall()
    conn.close()

    print("\n" + "=" * 50)
    print("UPCOMING EARNINGS — YOUR WATCHLIST")
    print("=" * 50)
    for ticker, date, days, score in rows:
        urgency = "🔴 THIS WEEK" if days <= 7 else "🟡 THIS MONTH" if days <= 30 else "  "
        print(f"  {ticker:<6} {date}  ({days}d)  score:{score}/10  {urgency}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_signals_db()

    # Get current watchlist from DB
    conn = sqlite3.connect(DB_PATH)
    tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM fundamentals"
    ).fetchall()]
    conn.close()

    if not tickers:
        print("No tickers in database. Run discover.py first.")
    else:
        print(f"Fetching signals for {len(tickers)} stocks...")
        fetch_earnings_dates(tickers)
        fetch_analyst_revisions(tickers)
        fetch_short_interest(tickers)
        print_earnings_calendar()
        print("\nDone. Run dashboard to see new signal data.")
