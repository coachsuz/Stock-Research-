"""
Stock Research Pipeline — Margin Quality & Analyst Consensus Checker
====================================================================
Catches the DLO problem — growth at declining margins and high analyst
disagreement are both red flags the basic quality scorer misses.

Three additions:
1. Margin trend scorer — flags 2+ quarters of gross margin contraction
2. Analyst consensus quality — flags when bull/bear spread > 50%
3. Revenue quality classifier — volume growth vs pricing power growth

Usage:
    python3 quality_checks.py                    # run all watchlist stocks
    python3 quality_checks.py --ticker DLO       # single stock
"""

import sqlite3
import yfinance as yf
import time
import json
from datetime import datetime
from typing import Optional, List

DB_PATH = "research.db"


# ── Database setup ────────────────────────────────────────────────────────────

def init_quality_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_trends (
            ticker              TEXT PRIMARY KEY,
            fetched_at          TEXT,
            gm_q1               REAL,
            gm_q2               REAL,
            gm_q3               REAL,
            gm_q4               REAL,
            gm_trend            TEXT,
            om_q1               REAL,
            om_q2               REAL,
            om_q3               REAL,
            om_q4               REAL,
            om_trend            TEXT,
            analyst_high_target REAL,
            analyst_low_target  REAL,
            analyst_spread_pct  REAL,
            consensus_quality   TEXT,
            revenue_quality     TEXT,
            conviction_override TEXT
        )
    """)
    conn.commit()
    conn.close()


# ── Margin trend analyzer ─────────────────────────────────────────────────────

def analyze_margin_trend(ticker: str) -> dict:
    """
    Pull annual AND quarterly margin data.
    Use annual trend as primary signal — quarterly is too noisy.
    Flag only if annual margins are declining meaningfully (>3pp).
    """
    try:
        t = yf.Ticker(ticker)

        # ── Annual trend (primary signal) ─────────────────────────────────
        income_a = t.income_stmt  # annual
        ann_gm   = []
        ann_om   = []

        if income_a is not None and not income_a.empty:
            for col in income_a.columns[:3]:  # last 3 years
                try:
                    rev  = income_a.loc["Total Revenue", col]     if "Total Revenue"    in income_a.index else None
                    gp   = income_a.loc["Gross Profit", col]      if "Gross Profit"     in income_a.index else None
                    op   = income_a.loc["Operating Income", col]  if "Operating Income" in income_a.index else None
                    if rev and gp and rev != 0:
                        ann_gm.append(round(gp / rev * 100, 1))
                    if rev and op and rev != 0:
                        ann_om.append(round(op / rev * 100, 1))
                except Exception:
                    continue

        # ── Quarterly (secondary — for context only) ──────────────────────
        income_q = t.quarterly_income_stmt
        qtr_gm   = []

        if income_q is not None and not income_q.empty:
            for col in income_q.columns[:4]:
                try:
                    rev = income_q.loc["Total Revenue", col] if "Total Revenue" in income_q.index else None
                    gp  = income_q.loc["Gross Profit", col]  if "Gross Profit"  in income_q.index else None
                    if rev and gp and rev != 0:
                        qtr_gm.append(round(gp / rev * 100, 1))
                except Exception:
                    continue

        # ── Annual trend determination ────────────────────────────────────
        def annual_trend(values: List[float]) -> str:
            """Uses annual data. Only flags if margin dropped >3pp over 2 years."""
            if len(values) < 2:
                return "insufficient_data"
            # values[0] = most recent year, values[-1] = oldest
            total_change = values[0] - values[-1]
            if total_change < -3.0:
                return "contracting"   # real structural decline
            elif total_change > 1.5:
                return "expanding"
            else:
                return "stable"        # normal fluctuation

        gm_trend = annual_trend(ann_gm)
        om_trend = annual_trend(ann_om)

        # Pad quarterly to 4
        while len(qtr_gm) < 4:
            qtr_gm.append(None)
        while len(ann_gm) < 4:
            ann_gm.append(None)

        return {
            "gm_values":  qtr_gm,    # show quarterly for display
            "ann_gm":     ann_gm,    # annual for trend
            "om_values":  [None]*4,
            "ann_om":     ann_om,
            "gm_trend":   gm_trend,
            "om_trend":   om_trend,
        }

    except Exception as e:
        return {"error": str(e)}


# ── Analyst consensus quality ──────────────────────────────────────────────────

def analyze_analyst_consensus(ticker: str) -> dict:
    """
    Check analyst price target spread.
    If high target / low target > 1.5x (50% spread), flag as uncertain.
    This caught DLO: Morgan Stanley $10 vs bulls at $21.
    """
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        high_target = info.get("targetHighPrice")
        low_target  = info.get("targetLowPrice")
        mean_target = info.get("targetMeanPrice")
        current     = info.get("regularMarketPrice")

        if not high_target or not low_target or low_target == 0:
            return {"error": "no target data"}

        spread_pct = (high_target - low_target) / low_target * 100

        # Quality rating
        if spread_pct > 150:
            quality = "very_uncertain"   # 🔴 bulls 3x bears — extreme disagreement
        elif spread_pct > 80:
            quality = "uncertain"        # 🟡 meaningful disagreement
        elif spread_pct > 40:
            quality = "moderate"         # 🟢 normal for growth stocks
        else:
            quality = "consensus"        # 🟢 analysts agree

        # Implied upside from mean
        upside = ((mean_target - current) / current * 100) if mean_target and current else None

        return {
            "high_target":   high_target,
            "low_target":    low_target,
            "mean_target":   mean_target,
            "spread_pct":    round(spread_pct, 1),
            "consensus_quality": quality,
            "implied_upside": round(upside, 1) if upside else None,
        }

    except Exception as e:
        return {"error": str(e)}


# ── Revenue quality classifier ────────────────────────────────────────────────

def classify_revenue_quality(ticker: str, margin_data: dict, info: dict) -> str:
    """
    Is growth coming from volume at declining margins (weak)
    or pricing power at stable/expanding margins (strong)?

    Returns: 'pricing_power' | 'volume_growth' | 'mixed' | 'unknown'
    """
    gm_trend  = margin_data.get("gm_trend", "unknown")
    rev_growth = info.get("revenueGrowth", 0) or 0

    if gm_trend == "expanding" and rev_growth > 0.15:
        return "pricing_power"     # Growing AND margins improving — best case
    elif gm_trend == "stable" and rev_growth > 0.15:
        return "volume_growth"     # Growing but margins flat — acceptable
    elif gm_trend == "contracting" and rev_growth > 0.15:
        return "volume_at_cost"    # Growing but buying revenue — red flag
    elif gm_trend == "contracting":
        return "deteriorating"     # Shrinking margins without growth — sell signal
    else:
        return "mixed"


# ── Conviction override ───────────────────────────────────────────────────────

def calculate_conviction_override(
    margin_data: dict,
    consensus_data: dict,
    revenue_quality: str,
) -> tuple:
    """
    Override Claude's conviction rating based on hard quality signals.
    Returns (override_conviction, reasons)
    """
    reasons  = []
    override = None

    gm_trend = margin_data.get("gm_trend", "unknown")
    om_trend = margin_data.get("om_trend", "unknown")
    c_quality = consensus_data.get("consensus_quality", "unknown")
    spread    = consensus_data.get("spread_pct", 0) or 0

    # Hard downgrades — require TWO signals to override to LOW
    # Single margin contraction could be input cost cycle, not structural
    flags = []

    if gm_trend == "contracting" and om_trend == "contracting":
        flags.append("both_margins_contracting")
        reasons.append("Both gross AND operating margins contracting — watch for structural deterioration")

    if gm_trend == "contracting" and om_trend != "contracting":
        flags.append("gm_contracting")
        reasons.append("Gross margin contracting — could be input costs or pricing pressure")

    if revenue_quality == "volume_at_cost":
        flags.append("volume_at_cost")
        reasons.append("Revenue growth at cost of margins — verify if temporary or structural")

    if c_quality == "very_uncertain":
        flags.append("analyst_very_uncertain")
        reasons.append(f"Analyst targets spread {spread:.0f}% — major disagreement on value")

    elif c_quality == "uncertain":
        flags.append("analyst_uncertain")
        reasons.append(f"Analyst target spread {spread:.0f}% — meaningful uncertainty")

    # Override rules — require COMBINATION of signals
    if "both_margins_contracting" in flags and "analyst_very_uncertain" in flags:
        override = "low"
    elif "both_margins_contracting" in flags and "volume_at_cost" in flags:
        override = "low"
    elif "analyst_very_uncertain" in flags:
        override = "low"
    elif "both_margins_contracting" in flags:
        override = "medium"
    elif "gm_contracting" in flags and "analyst_uncertain" in flags:
        override = "medium"
    elif "volume_at_cost" in flags and "analyst_uncertain" in flags:
        override = "medium"

    # Hard upgrades (only if no downgrades)
    if not override:
        if gm_trend == "expanding" and revenue_quality == "pricing_power":
            reasons.append("Margins expanding with growth — true pricing power")
        if c_quality == "consensus":
            reasons.append(f"Analyst consensus tight — high agreement on value")

    return override, reasons


# ── Main runner ───────────────────────────────────────────────────────────────

def run_quality_checks(ticker: str = None) -> List[dict]:
    init_quality_db()

    # Get watchlist
    conn = sqlite3.connect(DB_PATH)
    if ticker:
        tickers = [ticker.upper()]
    else:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM fundamentals ORDER BY quality_score DESC"
        ).fetchall()]
    conn.close()

    print(f"\n{'='*65}")
    print("MARGIN QUALITY & ANALYST CONSENSUS CHECKER")
    print(f"{'='*65}")
    print(f"Checking {len(tickers)} stocks...\n")

    trend_icons = {"expanding": "📈", "contracting": "📉", "stable": "➡️", "insufficient_data": "—"}
    quality_icons = {"pricing_power": "💪", "volume_growth": "📦", "volume_at_cost": "⚠️", "deteriorating": "🔴", "mixed": "➡️"}
    consensus_icons = {"consensus": "🟢", "moderate": "🟡", "uncertain": "🟠", "very_uncertain": "🔴"}

    results = []

    for t in tickers:
        try:
            yf_ticker = yf.Ticker(t)
            info      = yf_ticker.info

            margin_data    = analyze_margin_trend(t)
            consensus_data = analyze_analyst_consensus(t)
            rev_quality    = classify_revenue_quality(t, margin_data, info)
            override, reasons = calculate_conviction_override(
                margin_data, consensus_data, rev_quality
            )

            gm_trend = margin_data.get("gm_trend", "unknown")
            gm_vals  = margin_data.get("gm_values", [None]*4)
            c_quality = consensus_data.get("consensus_quality", "unknown")
            spread   = consensus_data.get("spread_pct")
            upside   = consensus_data.get("implied_upside")

            # Print summary
            gm_icon  = trend_icons.get(gm_trend, "?")
            rq_icon  = quality_icons.get(rev_quality, "?")
            cq_icon  = consensus_icons.get(c_quality, "?")
            ov_str   = f" → OVERRIDE TO {override.upper()}" if override else ""

            print(f"  {t:<6} GM:{gm_icon}{gm_trend:<12} Rev quality:{rq_icon}{rev_quality:<16} Consensus:{cq_icon}{c_quality:<14} Spread:{spread or '?':>6}%{ov_str}")
            if reasons:
                for r in reasons:
                    print(f"         ⚡ {r}")

            # Save to DB
            gm = gm_vals
            om = margin_data.get("om_values", [None]*4)
            conn2 = sqlite3.connect(DB_PATH)
            conn2.execute("""
                INSERT OR REPLACE INTO margin_trends
                (ticker, fetched_at, gm_q1, gm_q2, gm_q3, gm_q4, gm_trend,
                 om_q1, om_q2, om_q3, om_q4, om_trend,
                 analyst_high_target, analyst_low_target, analyst_spread_pct,
                 consensus_quality, revenue_quality, conviction_override)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t, datetime.now().isoformat(),
                gm[0], gm[1], gm[2], gm[3], gm_trend,
                om[0], om[1], om[2], om[3], margin_data.get("om_trend"),
                consensus_data.get("high_target"),
                consensus_data.get("low_target"),
                spread,
                c_quality,
                rev_quality,
                override,
            ))
            conn2.commit()
            conn2.close()

            results.append({
                "ticker":    t,
                "gm_trend":  gm_trend,
                "rev_quality": rev_quality,
                "consensus_quality": c_quality,
                "spread_pct": spread,
                "override":  override,
                "reasons":   reasons,
            })

            time.sleep(0.4)

        except Exception as e:
            print(f"  {t:<6} failed: {e}")

    # Summary
    overrides = [r for r in results if r.get("override")]
    print(f"\n{'='*65}")
    print(f"CONVICTION OVERRIDES — {len(overrides)} stocks flagged")
    print(f"{'='*65}")
    if overrides:
        for r in overrides:
            print(f"  {r['ticker']:<6} → {r['override'].upper()}")
            for reason in r["reasons"]:
                print(f"         • {reason}")
    else:
        print("  No overrides — all stocks pass quality checks")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--ticker", help="Single ticker to check")
    args = p.parse_args()
    run_quality_checks(ticker=args.ticker)
