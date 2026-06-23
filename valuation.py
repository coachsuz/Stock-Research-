"""
Stock Research Pipeline — Valuation Model & Relative Strength
=============================================================
Two modules:

1. Valuation Model
   - Reverse DCF: what growth rate does the current price imply?
   - Bull/Base/Bear scenarios with target prices
   - Margin of safety calculator

2. Relative Strength Tracker
   - Stock performance vs S&P 500 over 1, 3, 6 months
   - Sector relative strength
   - Momentum signal (leading or lagging the market?)

Usage:
    python3 valuation.py                    # run all stocks in watchlist
    python3 valuation.py --ticker ANET      # single stock deep dive
"""

import db_adapter as sqlite3
import yfinance as yf
import pandas as pd
import json
import time
from datetime import datetime, timedelta

DB_PATH = "research.db"


# ── Database setup ────────────────────────────────────────────────────────────

def init_valuation_db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS valuations (
            ticker              TEXT PRIMARY KEY,
            calculated_at       TEXT,
            current_price       REAL,
            implied_growth_rate REAL,
            bear_target         REAL,
            base_target         REAL,
            bull_target         REAL,
            bear_return         REAL,
            base_return         REAL,
            bull_return         REAL,
            margin_of_safety    REAL,
            verdict             TEXT,
            fcf_ttm             REAL,
            wacc                REAL,
            terminal_growth     REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS relative_strength (
            ticker          TEXT PRIMARY KEY,
            calculated_at   TEXT,
            current_price   REAL,
            return_1m       REAL,
            return_3m       REAL,
            return_6m       REAL,
            return_12m      REAL,
            spy_return_1m   REAL,
            spy_return_3m   REAL,
            spy_return_6m   REAL,
            spy_return_12m  REAL,
            rs_1m           REAL,
            rs_3m           REAL,
            rs_6m           REAL,
            rs_12m          REAL,
            momentum_signal TEXT
        )
    """)

    conn.commit()
    conn.close()


# ── Reverse DCF ───────────────────────────────────────────────────────────────

def reverse_dcf(current_price, fcf_per_share, wacc=0.10, terminal_growth=0.03, years=10):
    """
    Work backwards from the current price to find what growth rate
    the market is implying. This tells you what has to be true
    for the stock to be fairly valued at today's price.
    """
    if not fcf_per_share or fcf_per_share <= 0 or not current_price:
        return None

    # Binary search for implied growth rate
    low, high = -0.20, 1.00
    for _ in range(50):
        mid = (low + high) / 2
        pv  = _dcf_value(fcf_per_share, mid, wacc, terminal_growth, years)
        if pv > current_price:
            high = mid
        else:
            low = mid
        if abs(high - low) < 0.0001:
            break

    return round(mid, 4)


def _dcf_value(fcf, growth_rate, wacc, terminal_growth, years):
    """Calculate intrinsic value given a growth rate."""
    pv   = 0
    cf   = fcf
    for i in range(1, years + 1):
        cf  *= (1 + growth_rate)
        pv  += cf / (1 + wacc) ** i

    # Terminal value
    terminal_cf  = cf * (1 + terminal_growth)
    terminal_val = terminal_cf / (wacc - terminal_growth)
    pv += terminal_val / (1 + wacc) ** years

    return pv


def run_scenarios(fcf_per_share, current_price, base_growth,
                  wacc=0.10, terminal_growth=0.03, years=10):
    """
    Generate bull/base/bear price targets.
    Bear: base_growth - 8%
    Base: analyst consensus growth
    Bull: base_growth + 8%
    """
    if not fcf_per_share or fcf_per_share <= 0:
        return None

    bear_growth = max(base_growth - 0.08, 0.02)
    bull_growth = min(base_growth + 0.08, 0.60)

    bear_target = _dcf_value(fcf_per_share, bear_growth, wacc, terminal_growth, years)
    base_target = _dcf_value(fcf_per_share, base_growth, wacc, terminal_growth, years)
    bull_target = _dcf_value(fcf_per_share, bull_growth, wacc, terminal_growth, years)

    bear_return = (bear_target - current_price) / current_price * 100
    base_return = (base_target - current_price) / current_price * 100
    bull_return = (bull_target - current_price) / current_price * 100

    # Margin of safety = how far current price is below base target
    margin_of_safety = (base_target - current_price) / base_target * 100

    # Verdict
    if margin_of_safety > 20:
        verdict = "attractive"
    elif margin_of_safety > 5:
        verdict = "fair"
    elif margin_of_safety > -15:
        verdict = "stretched"
    else:
        verdict = "expensive"

    return {
        "bear_target":      round(bear_target, 2),
        "base_target":      round(base_target, 2),
        "bull_target":      round(bull_target, 2),
        "bear_return":      round(bear_return, 1),
        "base_return":      round(base_return, 1),
        "bull_return":      round(bull_return, 1),
        "margin_of_safety": round(margin_of_safety, 1),
        "verdict":          verdict,
        "bear_growth":      bear_growth,
        "base_growth":      base_growth,
        "bull_growth":      bull_growth,
    }


def fetch_valuation(ticker):
    """Fetch data and calculate valuation for a single ticker."""
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        current_price  = info.get("regularMarketPrice") or info.get("currentPrice")
        fcf            = info.get("freeCashflow")
        shares         = info.get("sharesOutstanding")
        revenue_growth = info.get("revenueGrowth") or 0.10
        beta           = info.get("beta") or 1.0

        if not current_price or not fcf or not shares:
            return None

        fcf_per_share = fcf / shares

        # WACC: risk-free rate (4.5%) + beta * equity risk premium (5.5%)
        wacc = 0.045 + beta * 0.055
        wacc = max(0.08, min(wacc, 0.15))  # cap between 8-15%

        # Implied growth rate from current price
        implied_growth = reverse_dcf(current_price, fcf_per_share, wacc)

        # Scenarios based on revenue growth as proxy for FCF growth
        base_growth = revenue_growth * 0.85  # FCF growth slightly below revenue
        scenarios   = run_scenarios(fcf_per_share, current_price, base_growth, wacc)

        if not scenarios:
            return None

        result = {
            "ticker":              ticker,
            "current_price":       current_price,
            "fcf_ttm":             fcf,
            "fcf_per_share":       fcf_per_share,
            "wacc":                round(wacc, 3),
            "terminal_growth":     0.03,
            "implied_growth_rate": implied_growth,
            "base_growth":         base_growth,
            **scenarios,
        }

        # Save to DB
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO valuations
            (ticker, calculated_at, current_price, implied_growth_rate,
             bear_target, base_target, bull_target,
             bear_return, base_return, bull_return,
             margin_of_safety, verdict, fcf_ttm, wacc, terminal_growth)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ticker, datetime.now().isoformat(), current_price,
            implied_growth,
            scenarios["bear_target"], scenarios["base_target"], scenarios["bull_target"],
            scenarios["bear_return"], scenarios["base_return"], scenarios["bull_return"],
            scenarios["margin_of_safety"], scenarios["verdict"],
            fcf, wacc, 0.03
        ))
        conn.commit()
        conn.close()

        return result

    except Exception as e:
        print(f"  {ticker} valuation failed: {e}")
        return None


# ── Relative strength ─────────────────────────────────────────────────────────

def fetch_relative_strength(ticker, spy_prices=None):
    """
    Calculate stock performance vs S&P 500 over 1/3/6/12 months.
    spy_prices: pass in pre-fetched SPY data to avoid repeated downloads.
    """
    try:
        t      = yf.Ticker(ticker)
        hist   = t.history(period="13mo")

        if hist.empty:
            return None

        now    = hist.index[-1]
        price  = hist["Close"].iloc[-1]

        def period_return(days):
            target = now - timedelta(days=days)
            subset = hist[hist.index <= target]
            if subset.empty:
                return None
            old_price = subset["Close"].iloc[-1]
            return round((price - old_price) / old_price * 100, 1)

        r1m  = period_return(30)
        r3m  = period_return(90)
        r6m  = period_return(180)
        r12m = period_return(365)

        # SPY returns for same periods
        spy_r1m = spy_r3m = spy_r6m = spy_r12m = None
        if spy_prices is not None and not spy_prices.empty:
            spy_now   = spy_prices["Close"].iloc[-1]

            def spy_return(days):
                target = spy_prices.index[-1] - timedelta(days=days)
                subset = spy_prices[spy_prices.index <= target]
                if subset.empty:
                    return None
                return round((spy_now - subset["Close"].iloc[-1]) /
                             subset["Close"].iloc[-1] * 100, 1)

            spy_r1m  = spy_return(30)
            spy_r3m  = spy_return(90)
            spy_r6m  = spy_return(180)
            spy_r12m = spy_return(365)

        # Relative strength = stock return - SPY return
        rs_3m  = round(r3m  - spy_r3m,  1) if r3m  and spy_r3m  else None
        rs_6m  = round(r6m  - spy_r6m,  1) if r6m  and spy_r6m  else None
        rs_12m = round(r12m - spy_r12m, 1) if r12m and spy_r12m else None
        rs_1m  = round(r1m  - spy_r1m,  1) if r1m  and spy_r1m  else None

        # Momentum signal
        momentum = "neutral"
        if rs_3m is not None and rs_6m is not None:
            if rs_3m > 5 and rs_6m > 5:
                momentum = "strong_leader"
            elif rs_3m > 2:
                momentum = "outperforming"
            elif rs_3m < -5 and rs_6m < -5:
                momentum = "persistent_laggard"
            elif rs_3m < -2:
                momentum = "underperforming"

        result = {
            "ticker":          ticker,
            "current_price":   round(price, 2),
            "return_1m":       r1m,
            "return_3m":       r3m,
            "return_6m":       r6m,
            "return_12m":      r12m,
            "spy_return_1m":   spy_r1m,
            "spy_return_3m":   spy_r3m,
            "spy_return_6m":   spy_r6m,
            "spy_return_12m":  spy_r12m,
            "rs_1m":           rs_1m,
            "rs_3m":           rs_3m,
            "rs_6m":           rs_6m,
            "rs_12m":          rs_12m,
            "momentum_signal": momentum,
        }

        # Save to DB
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO relative_strength
            (ticker, calculated_at, current_price,
             return_1m, return_3m, return_6m, return_12m,
             spy_return_1m, spy_return_3m, spy_return_6m, spy_return_12m,
             rs_1m, rs_3m, rs_6m, rs_12m, momentum_signal)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ticker, datetime.now().isoformat(), result["current_price"],
            r1m, r3m, r6m, r12m,
            spy_r1m, spy_r3m, spy_r6m, spy_r12m,
            rs_1m, rs_3m, rs_6m, rs_12m, momentum
        ))
        conn.commit()
        conn.close()

        return result

    except Exception as e:
        print(f"  {ticker} relative strength failed: {e}")
        return None


# ── Main runner ───────────────────────────────────────────────────────────────

def run_all(ticker=None):
    init_valuation_db()

    # Get watchlist
    conn = sqlite3.connect(DB_PATH)
    if ticker:
        tickers = [ticker.upper()]
    else:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM fundamentals"
        ).fetchall()]
    conn.close()

    print(f"\nFetching SPY benchmark...")
    try:
        spy      = yf.Ticker("SPY")
        spy_hist = spy.history(period="13mo")
        print(f"  SPY loaded: {len(spy_hist)} days")
    except Exception:
        spy_hist = None
        print("  SPY fetch failed — relative strength vs index unavailable")

    print(f"\nCalculating valuations for {len(tickers)} stocks...")
    val_results = []
    rs_results  = []

    for t in tickers:
        print(f"  {t}...")
        val = fetch_valuation(t)
        rs  = fetch_relative_strength(t, spy_hist)
        if val:
            val_results.append(val)
        if rs:
            rs_results.append(rs)
        time.sleep(0.4)

    # Print summary
    print(f"\n{'='*65}")
    print("VALUATION SUMMARY")
    print(f"{'='*65}")
    print(f"{'Ticker':<7} {'Price':>8} {'Base':>8} {'Upside':>8} {'Implied g':>10} {'Verdict'}")
    print("-" * 65)

    verdicts = {"attractive": "🟢", "fair": "🟡", "stretched": "🟠", "expensive": "🔴"}
    for v in sorted(val_results, key=lambda x: x.get("margin_of_safety", 0), reverse=True):
        icon = verdicts.get(v.get("verdict", ""), "⚪")
        print(f"  {v['ticker']:<6} "
              f"${v['current_price']:>7.2f} "
              f"${v['base_target']:>7.2f} "
              f"{v['base_return']:>+7.1f}% "
              f"{(v['implied_growth_rate'] or 0)*100:>8.1f}%  "
              f"{icon} {v['verdict']}")

    print(f"\n{'='*65}")
    print("RELATIVE STRENGTH VS S&P 500")
    print(f"{'='*65}")
    print(f"{'Ticker':<7} {'1M RS':>7} {'3M RS':>7} {'6M RS':>7} {'Signal'}")
    print("-" * 65)

    momentum_icons = {
        "strong_leader":     "🚀 Strong leader",
        "outperforming":     "📈 Outperforming",
        "neutral":           "➡️  Neutral",
        "underperforming":   "📉 Underperforming",
        "persistent_laggard": "⚠️  Persistent laggard",
    }

    for r in sorted(rs_results,
                    key=lambda x: x.get("rs_3m") or 0, reverse=True):
        rs1  = f"{r['rs_1m']:+.1f}%" if r.get("rs_1m") is not None else "—"
        rs3  = f"{r['rs_3m']:+.1f}%" if r.get("rs_3m") is not None else "—"
        rs6  = f"{r['rs_6m']:+.1f}%" if r.get("rs_6m") is not None else "—"
        sig  = momentum_icons.get(r.get("momentum_signal", ""), "")
        print(f"  {r['ticker']:<6} {rs1:>7} {rs3:>7} {rs6:>7}  {sig}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", help="Single ticker to analyze")
    args = p.parse_args()
    run_all(ticker=args.ticker)
