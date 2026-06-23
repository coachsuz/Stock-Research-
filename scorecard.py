"""
Stock Research Pipeline — Thesis Scorecard & Signal Hit Rate Analyzer
======================================================================
Two modules:

1. Thesis Scorecard — quarterly check-in on each thesis
   Track whether leading indicators are confirming or breaking
   Build a record of thesis accuracy over time

2. Signal Hit Rate Analyzer — measures which signals actually work
   Are 8+ quality scores outperforming 6s?
   Is Claude "high conviction" beating "medium"?
   Are 13F cluster buys generating alpha?

Usage:
    python3 scorecard.py                          # show all scorecards
    python3 scorecard.py --checkin ANET           # do a quarterly check-in
    python3 scorecard.py --hitrate                # show signal performance
    python3 scorecard.py --report                 # full report
"""

import db_adapter as sqlite3
import argparse
import json
import yfinance as yf
from datetime import datetime, timedelta

DB_PATH = "research.db"


# ── Database setup ────────────────────────────────────────────────────────────

def init_scorecard_db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS thesis_checkins (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            checkin_date    TEXT NOT NULL,
            indicator_1     TEXT,
            indicator_2     TEXT,
            indicator_3     TEXT,
            status_1        TEXT,
            status_2        TEXT,
            status_3        TEXT,
            overall         TEXT,
            price_at_checkin REAL,
            notes           TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            signal_type     TEXT NOT NULL,
            signal_value    TEXT,
            signal_date     TEXT NOT NULL,
            entry_price     REAL,
            price_30d       REAL,
            price_90d       REAL,
            price_180d      REAL,
            price_365d      REAL,
            return_30d      REAL,
            return_90d      REAL,
            return_180d     REAL,
            return_365d     REAL,
            outcome         TEXT
        )
    """)

    conn.commit()
    conn.close()


# ── Thesis scorecard ──────────────────────────────────────────────────────────

def get_thesis_indicators(ticker):
    """Pull leading indicators from the stored AI thesis."""
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute("""
        SELECT raw_response FROM ai_summaries
        WHERE ticker = ? AND summary_type = 'thesis'
        ORDER BY generated_at DESC LIMIT 1
    """, (ticker,)).fetchone()
    conn.close()

    if not row or not row[0]:
        return []

    try:
        data = json.loads(row[0])
        return data.get("leading_indicators", [])
    except Exception:
        return []


def do_checkin(ticker):
    """Interactive quarterly thesis check-in."""
    indicators = get_thesis_indicators(ticker)

    print(f"\n{'='*55}")
    print(f"QUARTERLY THESIS CHECK-IN: {ticker}")
    print(f"{'='*55}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d')}\n")

    if not indicators:
        print("No leading indicators found for this ticker.")
        print("Run discover.py first to generate a thesis.")
        return

    # Get current price
    current_price = None
    try:
        t = yf.Ticker(ticker)
        current_price = t.info.get("regularMarketPrice")
        print(f"Current price: ${current_price:.2f}\n")
    except Exception:
        pass

    statuses = []
    for i, ind in enumerate(indicators[:3], 1):
        print(f"Indicator {i}: {ind.get('indicator', '?')}")
        print(f"  Confirms if: {ind.get('confirms_thesis_if', '?')}")
        print(f"  Breaks if:   {ind.get('kills_thesis_if', '?')}")
        print()
        print("  Status options:")
        print("    1 = on_track  (confirming the thesis)")
        print("    2 = at_risk   (mixed signals)")
        print("    3 = broken    (thesis invalidated)")
        choice = input("  Your assessment (1/2/3): ").strip()
        status = {"1": "on_track", "2": "at_risk", "3": "broken"}.get(choice, "at_risk")
        statuses.append(status)
        print()

    # Pad to 3 if fewer indicators
    while len(statuses) < 3:
        statuses.append(None)

    # Overall thesis status
    broken_count  = statuses.count("broken")
    ontrack_count = statuses.count("on_track")

    if broken_count >= 2:
        overall = "broken"
    elif broken_count == 1 and ontrack_count == 0:
        overall = "at_risk"
    elif ontrack_count >= 2:
        overall = "on_track"
    else:
        overall = "at_risk"

    notes = input("Any notes for this check-in? (press Enter to skip): ").strip()

    # Save
    conn = sqlite3.connect(DB_PATH)
    inds = [ind.get("indicator", "") for ind in indicators[:3]]
    while len(inds) < 3:
        inds.append(None)

    conn.execute("""
        INSERT INTO thesis_checkins
        (ticker, checkin_date, indicator_1, indicator_2, indicator_3,
         status_1, status_2, status_3, overall, price_at_checkin, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticker, datetime.now().strftime("%Y-%m-%d"),
          inds[0], inds[1], inds[2],
          statuses[0], statuses[1], statuses[2],
          overall, current_price, notes))
    conn.commit()
    conn.close()

    status_icons = {"on_track": "✅", "at_risk": "⚠️", "broken": "❌"}
    print(f"\nCheck-in saved.")
    print(f"Overall thesis: {status_icons.get(overall, '?')} {overall.upper()}")

    if overall == "broken":
        print("⚠️  Consider exiting this position — thesis is broken.")
    elif overall == "at_risk":
        print("👀 Monitor closely — check again next month.")
    else:
        print("✅ Thesis on track — hold or add on weakness.")


def show_all_scorecards():
    """Show thesis check-in history for all tickers."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT t.ticker, t.checkin_date, t.overall,
               t.status_1, t.status_2, t.status_3,
               t.price_at_checkin, t.notes,
               p.entry_price
        FROM thesis_checkins t
        LEFT JOIN portfolio p ON t.ticker = p.ticker AND p.status = 'open'
        ORDER BY t.checkin_date DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("\nNo check-ins yet. Run: python3 scorecard.py --checkin TICKER")
        return

    print(f"\n{'='*55}")
    print("THESIS SCORECARD — ALL CHECK-INS")
    print(f"{'='*55}")

    seen = set()
    for ticker, date, overall, s1, s2, s3, price, notes, entry in rows:
        icons = {"on_track": "✅", "at_risk": "⚠️", "broken": "❌", None: "—"}
        overall_icon = {"on_track": "🟢", "at_risk": "🟡", "broken": "🔴"}.get(overall, "⚪")

        header = f"{ticker} not in seen" if ticker not in seen else ""
        if ticker not in seen:
            print(f"\n  {overall_icon} {ticker}")
            seen.add(ticker)

        ret_str = ""
        if entry and price:
            ret = (price - entry) / entry * 100
            ret_str = f"  return: {ret:+.1f}%"

        print(f"     {date}  {overall_icon} {overall or '?':<10}"
              f"  I1:{icons[s1]} I2:{icons[s2]} I3:{icons[s3]}{ret_str}")
        if notes:
            print(f"     Note: {notes}")


# ── Signal hit rate analyzer ──────────────────────────────────────────────────

def record_signal_outcomes():
    """
    For every closed position, record what signals triggered the buy
    and what the actual return was. Builds the hit rate dataset.
    """
    conn = sqlite3.connect(DB_PATH)

    closed = conn.execute("""
        SELECT p.ticker, p.entry_date, p.entry_price, p.exit_price,
               f.quality_score, a.raw_response,
               fi.fund_count
        FROM portfolio p
        LEFT JOIN fundamentals f ON p.ticker = f.ticker
        LEFT JOIN ai_summaries a ON p.ticker = a.ticker AND a.summary_type = 'thesis'
        LEFT JOIN (
            SELECT ticker, COUNT(DISTINCT fund_cik) as fund_count
            FROM filings_13f WHERE is_new = 1
            GROUP BY ticker
        ) fi ON p.ticker = fi.ticker
        WHERE p.status = 'closed'
    """).fetchall()

    for ticker, entry_date, entry_price, exit_price, score, raw, fund_count in closed:
        if not entry_price or not exit_price:
            continue

        conviction = "medium"
        try:
            if raw:
                conviction = json.loads(raw).get("conviction", "medium")
        except Exception:
            pass

        ret = (exit_price - entry_price) / entry_price * 100
        outcome = "win" if ret > 0 else "loss"

        for signal_type, signal_value in [
            ("quality_score",   str(score)),
            ("conviction",      conviction),
            ("13f_cluster",     str(fund_count or 0)),
        ]:
            conn.execute("""
                INSERT OR IGNORE INTO signal_outcomes
                (ticker, signal_type, signal_value, signal_date,
                 entry_price, return_365d, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ticker, signal_type, signal_value,
                  entry_date, entry_price, ret, outcome))

    conn.commit()
    conn.close()


def show_hit_rate():
    """Analyze which signals are generating the best returns."""
    conn = sqlite3.connect(DB_PATH)

    print(f"\n{'='*55}")
    print("SIGNAL HIT RATE ANALYZER")
    print(f"{'='*55}")

    # Quality score hit rate
    rows = conn.execute("""
        SELECT signal_value,
               COUNT(*) as trades,
               AVG(return_365d) as avg_return,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
        FROM signal_outcomes
        WHERE signal_type = 'quality_score'
        GROUP BY signal_value
        ORDER BY CAST(signal_value AS INTEGER) DESC
    """).fetchall()

    if rows:
        print("\n📊 BY QUALITY SCORE:")
        print(f"  {'Score':<8} {'Trades':<8} {'Avg Return':<14} {'Win Rate'}")
        print(f"  {'-'*45}")
        for score, trades, avg_ret, win_rate in rows:
            print(f"  {score:<8} {trades:<8} {avg_ret:>+.1f}%{'':8} {win_rate:.0f}%")

    # Conviction hit rate
    rows = conn.execute("""
        SELECT signal_value,
               COUNT(*) as trades,
               AVG(return_365d) as avg_return,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
        FROM signal_outcomes
        WHERE signal_type = 'conviction'
        GROUP BY signal_value
        ORDER BY avg_return DESC
    """).fetchall()

    if rows:
        print("\n🎯 BY CONVICTION LEVEL:")
        print(f"  {'Conviction':<12} {'Trades':<8} {'Avg Return':<14} {'Win Rate'}")
        print(f"  {'-'*45}")
        for conv, trades, avg_ret, win_rate in rows:
            icon = {"high": "🔥", "medium": "👀", "low": "💤"}.get(conv, "")
            print(f"  {icon} {conv:<10} {trades:<8} {avg_ret:>+.1f}%{'':8} {win_rate:.0f}%")

    # 13F cluster hit rate
    rows = conn.execute("""
        SELECT
            CASE
                WHEN CAST(signal_value AS INTEGER) >= 3 THEN '3+ funds'
                WHEN CAST(signal_value AS INTEGER) = 2  THEN '2 funds'
                WHEN CAST(signal_value AS INTEGER) = 1  THEN '1 fund'
                ELSE 'no 13F signal'
            END as cluster,
            COUNT(*) as trades,
            AVG(return_365d) as avg_return,
            SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
        FROM signal_outcomes
        WHERE signal_type = '13f_cluster'
        GROUP BY cluster
        ORDER BY avg_return DESC
    """).fetchall()

    if rows:
        print("\n🐋 BY 13F CLUSTER SIZE:")
        print(f"  {'Cluster':<14} {'Trades':<8} {'Avg Return':<14} {'Win Rate'}")
        print(f"  {'-'*45}")
        for cluster, trades, avg_ret, win_rate in rows:
            print(f"  {cluster:<14} {trades:<8} {avg_ret:>+.1f}%{'':8} {win_rate:.0f}%")

    # Overall stats
    total = conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]
    if total == 0:
        print("\n  No closed positions yet to analyze.")
        print("  Use --add and --exit in portfolio.py to build your track record.")
        print("  The more positions you track, the more useful this becomes.")
    else:
        wins = conn.execute(
            "SELECT COUNT(*) FROM signal_outcomes WHERE outcome='win'"
        ).fetchone()[0]
        avg  = conn.execute(
            "SELECT AVG(return_365d) FROM signal_outcomes"
        ).fetchone()[0]
        print(f"\n  Total signals tracked: {total}")
        print(f"  Overall win rate: {wins/total*100:.0f}%")
        print(f"  Average return: {avg:+.1f}%")

    conn.close()


# ── Full report ───────────────────────────────────────────────────────────────

def full_report():
    record_signal_outcomes()
    show_all_scorecards()
    show_hit_rate()

    # Upcoming check-ins needed
    conn = sqlite3.connect(DB_PATH)
    due = conn.execute("""
        SELECT p.ticker, MAX(c.checkin_date) as last_checkin
        FROM portfolio p
        LEFT JOIN thesis_checkins c ON p.ticker = c.ticker
        WHERE p.status = 'open'
        GROUP BY p.ticker
        ORDER BY last_checkin ASC NULLS FIRST
    """).fetchall()
    conn.close()

    if due:
        print(f"\n{'='*55}")
        print("CHECK-INS DUE")
        print(f"{'='*55}")
        for ticker, last in due:
            if not last:
                print(f"  {ticker:<6} Never checked in — do this first")
            else:
                days_ago = (datetime.now() -
                           datetime.strptime(last, "%Y-%m-%d")).days
                icon = "🔴" if days_ago > 90 else "🟡" if days_ago > 45 else "🟢"
                print(f"  {ticker:<6} Last: {last} ({days_ago}d ago) {icon}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_scorecard_db()

    p = argparse.ArgumentParser(description="Thesis scorecard and signal hit rate")
    p.add_argument("--checkin",  metavar="TICKER", help="Do a quarterly thesis check-in")
    p.add_argument("--hitrate",  action="store_true", help="Show signal hit rate analysis")
    p.add_argument("--report",   action="store_true", help="Full scorecard report")
    args = p.parse_args()

    if args.checkin:
        do_checkin(args.checkin.upper())
    elif args.hitrate:
        record_signal_outcomes()
        show_hit_rate()
    elif args.report:
        full_report()
    else:
        show_all_scorecards()
        show_hit_rate()
