"""
Stock Research Pipeline — Auto Discovery
=========================================
Screens the broad market for quality candidates and uses Claude to
research, rank, and explain each one. Automatically populates your
watchlist — no manual ticker entry needed.

Usage:
    python3 discover.py                    # full discovery run
    python3 discover.py --theme tech       # screen a specific theme
    python3 discover.py --size mid         # filter by market cap

How it works:
    1. Pull a broad universe of stocks (S&P 500 + high-growth candidates)
    2. Apply hard financial filters (growth, margin, FCF)
    3. Score survivors on the 10-metric quality checklist
    4. Ask Claude to research the top candidates and rank by conviction
    5. Save the final list to your watchlist in research.db
"""

import os, json, time, requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from db import init_db, upsert_fundamentals, log_alert

# ── Universe sources ──────────────────────────────────────────────────────────
# We pull from multiple lists so we don't miss under-the-radar names

SP500_URL   = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP400_URL   = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
NASDAQ_URL  = "https://en.wikipedia.org/wiki/Nasdaq-100"

# Manually curated high-growth / under-radar candidates to always include
EXTRA_SEEDS = [
    "AXON","CRWD","MNDY","CAVA","IOT","APP","CELH","ASTS",
    "MELI","NU","TOST","DDOG","HIMS","DUOL","IBKR","MEDP",
    "PODD","TREX","FICO","ROP","MSCI","SPGI","MCO","EFX",
]

# ── Screening thresholds ──────────────────────────────────────────────────────
# These are the hard filters applied before quality scoring.
# Loosening them finds more candidates; tightening finds fewer, higher-quality ones.

SCREEN = {
    "min_revenue_growth":   0.08,   # 8%+ YoY revenue growth
    "min_gross_margin":     0.25,   # 25%+ gross margin
    "max_debt_equity":      4.0,    # not over-leveraged
    "min_market_cap":       500e6,  # $500M+ (avoids micro-cap noise)
    "max_market_cap":       500e9,  # under $500B (room to grow)
    "max_pe":               100,    # not priced to perfection
    "min_quality_score":    5,      # must score 5+ on checklist
}

# ── Themes for focused discovery ──────────────────────────────────────────────
THEMES = {
    "tech":       ["software", "semiconductor", "cloud", "cybersecurity", "ai"],
    "healthcare": ["biotech", "medical devices", "healthcare services", "pharma"],
    "consumer":   ["consumer discretionary", "retail", "restaurants", "e-commerce"],
    "industrial": ["industrials", "aerospace", "defense", "automation"],
    "financial":  ["fintech", "insurance", "asset management", "exchanges"],
}


# ── Step 1: Build universe ────────────────────────────────────────────────────

def build_universe(theme: str = None) -> list:
    """Pull tickers from SEC EDGAR and curated lists."""
    tickers = set(EXTRA_SEEDS)

    print("Building stock universe...")

    # Pull from SEC EDGAR company tickers (free, no auth needed)
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers_exchange.json",
            headers={"User-Agent": "StockResearch research@example.com"},
            timeout=15
        )
        data = r.json().get("data", [])
        # data rows: [cik, name, ticker, exchange]
        # Filter to major exchanges only
        exchanges = {"Nasdaq", "NYSE"}
        sec_tickers = [
            row[2] for row in data
            if len(row) >= 4 and row[3] in exchanges and row[2]
        ]
        tickers.update(sec_tickers[:800])
        print(f"  SEC EDGAR: {len(sec_tickers)} tickers")
    except Exception as e:
        print(f"  SEC EDGAR fetch failed: {e}")

    # Add well-known growth stocks not always in SEC list
    GROWTH_UNIVERSE = [
        "AAPL","MSFT","GOOGL","META","AMZN","NVDA","TSLA","NFLX",
        "CRM","NOW","SNOW","DDOG","NET","CRWD","ZS","PANW","FTNT",
        "HUBS","BILL","GTLB","MDB","ESTC","CFLT","DKNG","RBLX",
        "UBER","LYFT","ABNB","DASH","COIN","SQ","AFRM","UPST",
        "SHOP","ETSY","W","CVNA","CARVANA","CHWY","CHEWY",
        "VEEV","IQVIA","SDGR","RXRX","DNAI","BEAM","CRSP","NTLA",
        "ALGN","ISRG","DXCM","INSP","TMDX","NVST","RVTY",
        "AXON","NVO","ELF","CAVA","WING","BROS","SHAK","LULU",
        "ON","MPWR","ENPH","FSLR","SEDG","ARRY","GTLS","XYL",
        "TREX","AZEK","BLDR","IBP","FIX","CSWI","ROLL","KTOS",
        "MELI","SE","GRAB","GOTO","BABA","JD","PDD","TCOM",
        "NU","STNE","PAGS","DLO","CASH","TPVG","ARCC","MAIN",
    ]
    tickers.update(GROWTH_UNIVERSE)

    result = sorted(list(tickers))
    print(f"  Total universe: {len(result)} tickers\n")
    return result


# ── Step 2: Fast screen ───────────────────────────────────────────────────────

def fast_screen(tickers: list, theme: str = None, batch_size: int = 100) -> list:
    """
    Apply hard financial filters using yfinance.
    Processes in batches to stay under rate limits.
    Returns list of tickers that pass the screen.
    """
    print(f"Screening {len(tickers)} tickers against quality filters...")
    passed = []
    failed_stats = {"no_data": 0, "low_growth": 0, "low_margin": 0,
                    "high_debt": 0, "wrong_cap": 0, "high_pe": 0}

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        print(f"  Batch {i//batch_size + 1}/{(len(tickers)-1)//batch_size + 1} ({len(batch)} tickers)...")

        for ticker in batch:
            try:
                t    = yf.Ticker(ticker)
                info = t.info

                # Skip if no meaningful data
                if not info or not info.get("regularMarketPrice"):
                    failed_stats["no_data"] += 1
                    continue

                mktcap = info.get("marketCap", 0) or 0
                rg     = info.get("revenueGrowth", 0) or 0
                gm     = info.get("grossMargins", 0) or 0
                de     = info.get("debtToEquity", 0) or 0
                pe     = info.get("trailingPE", 0) or 0
                sector = (info.get("sector") or "").lower()
                industry = (info.get("industry") or "").lower()

                # Theme filter
                if theme and theme in THEMES:
                    keywords = THEMES[theme]
                    if not any(kw in sector or kw in industry for kw in keywords):
                        continue

                # Hard filters
                if mktcap < SCREEN["min_market_cap"]:
                    failed_stats["wrong_cap"] += 1; continue
                if mktcap > SCREEN["max_market_cap"]:
                    failed_stats["wrong_cap"] += 1; continue
                if rg < SCREEN["min_revenue_growth"]:
                    failed_stats["low_growth"] += 1; continue
                if gm < SCREEN["min_gross_margin"]:
                    failed_stats["low_margin"] += 1; continue
                if de > SCREEN["max_debt_equity"] * 100:  # yfinance reports as %, not ratio
                    failed_stats["high_debt"] += 1; continue
                if pe and pe > SCREEN["max_pe"]:
                    failed_stats["high_pe"] += 1; continue

                passed.append(ticker)

            except Exception:
                failed_stats["no_data"] += 1
                continue

        time.sleep(0.3)

    print(f"\n  Passed screen: {len(passed)} tickers")
    print(f"  Filtered out: {sum(failed_stats.values())} "
          f"(no data: {failed_stats['no_data']}, "
          f"low growth: {failed_stats['low_growth']}, "
          f"low margin: {failed_stats['low_margin']}, "
          f"high debt: {failed_stats['high_debt']})\n")
    return passed


# ── Step 3: Quality score survivors ──────────────────────────────────────────

def score_candidates(tickers: list) -> list:
    """
    Full quality score on screened candidates.
    Returns sorted list of (ticker, data, score) tuples.
    """
    from data_ingestion import fetch_ticker, score_quality

    print(f"Quality scoring {len(tickers)} candidates...")
    results = []

    for ticker in tickers:
        try:
            data  = fetch_ticker(ticker, use_fmp=False)
            score = score_quality(data)
            if score["score"] >= SCREEN["min_quality_score"]:
                results.append((ticker, data, score))
                print(f"  {ticker}: {score['score']}/10 — {score['verdict']}")
            time.sleep(0.4)
        except Exception as e:
            print(f"  {ticker} failed: {e}")

    results.sort(key=lambda x: x[2]["score"], reverse=True)
    print(f"\n  Qualified candidates: {len(results)}\n")
    return results


# ── Step 4: Claude AI ranking ─────────────────────────────────────────────────

def ai_rank_candidates(candidates: list, top_n: int = 15) -> list:
    """
    Ask Claude to research the top candidates and rank by investment conviction.
    Returns ranked list of tickers with reasoning.
    """
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("No ANTHROPIC_API_KEY — skipping AI ranking, using score ranking.")
        fallback = [{"ticker": t, "rank": i+1, "conviction": "medium",
                     "one_line_thesis": f"Score {s['score']}/10 — {s['verdict']}",
                     "key_edge": "", "watch_for": ""}
                    for i, (t, _, s) in enumerate(candidates[:top_n])]
        return fallback, []

    # Process in batches of 10 to keep JSON response manageable
    all_ranked = []
    batch_size = 10
    for batch_start in range(0, min(top_n, len(candidates)), batch_size):
        batch = candidates[batch_start:batch_start + batch_size]
        print(f"  Ranking batch {batch_start//batch_size + 1}: {[t for t,_,_ in batch]}...")
        batch_ranked, _ = _rank_batch(batch, api_key)
        # Renumber ranks
        for i, item in enumerate(batch_ranked):
            item["rank"] = batch_start + i + 1
        all_ranked.extend(batch_ranked)

    return all_ranked, []


def _rank_batch(candidates, api_key):
    """Rank a single batch of up to 10 candidates."""
    import anthropic
    print(f"Asking Claude to research and rank top {len(candidates)} candidates...")

    # Build a summary of candidates for Claude to evaluate
    candidate_summary = []
    for ticker, data, score in candidates:
        candidate_summary.append({
            "ticker":          ticker,
            "name":            data.get("name", ticker),
            "sector":          data.get("sector", ""),
            "quality_score":   score["score"],
            "revenue_growth":  f"{(data.get('revenue_growth') or 0)*100:.1f}%",
            "gross_margin":    f"{(data.get('gross_margin') or 0)*100:.1f}%",
            "roic":            f"{(data.get('roic') or 0)*100:.1f}%",
            "market_cap_b":    f"${(data.get('market_cap') or 0)/1e9:.1f}B",
            "insider_pct":     f"{(data.get('insider_pct') or 0)*100:.1f}%",
            "analyst_count":   data.get("analyst_count", "?"),
            "passed":          score.get("passed", []),
        })

    prompt = f"""You are an expert equity analyst. Below are {len(candidate_summary)} stocks that passed a quantitative quality screen.

You are an expert investor ranking stocks by investment conviction using TWO frameworks:

FRAMEWORK 1 — QUALITY COMPOUNDERS (applies to all stocks):
High conviction if: quality score 7+, high insider ownership, low analyst coverage, durable competitive advantage, pricing power, reinvesting FCF at high rates.

FRAMEWORK 2 — AI BOTTLENECK FILTER (applies to tech/semi stocks only):
For tech companies, high conviction only if the company owns a bottleneck AI cannot avoid:
1. Core infrastructure: chips, networking, semiconductors
2. Memory/storage: DRAM, HBM, NAND
3. Power/data centers: electricity, cooling
4. Hyperscalers: Google, Amazon, Meta, Microsoft
5. Selective applications: truly differentiated, hard to copy
Watch-outs: good trend bad price, small-cap AI hype, overbuild risk, cyclical shortage profits.

IMPORTANT: A great non-tech business (consumer brand, financial, healthcare) can be HIGH conviction based on Framework 1 alone — do not penalize it for lacking AI exposure. Only apply the AI filter to tech/semiconductor companies.

REVENUE QUALITY TEST (apply to all stocks — this is critical):
Before assigning high conviction, ask: Is growth coming from PRICING POWER (margins stable or expanding) or VOLUME AT COST (margins contracting)?
- Pricing power growth = high conviction supported
- Volume at declining margins = cap at medium conviction maximum
- Margin contraction for 2+ quarters = reduce conviction regardless of growth rate
This test would have caught DLO: 55% revenue growth but declining take rates = volume not pricing power.

Candidates:
{json.dumps(candidate_summary, indent=2)}

Return ONLY valid JSON. Keep every string under 100 characters:
{{
  "ranked_watchlist": [
    {{
      "ticker": "XXXX",
      "rank": 1,
      "conviction": "high",
      "one_line_thesis": "One short sentence — what bottleneck does it own?",
      "key_edge": "One short sentence under 100 chars",
      "watch_for": "One short sentence under 100 chars",
      "ai_bucket": "core_infrastructure/memory/power/hyperscaler/application/none"
    }}
  ],
  "themes_identified": ["theme1", "theme2"],
  "notable_omissions": ["omission1"]
}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model      = "claude-sonnet-4-5",
            max_tokens = 2000,
            messages   = [{"role": "user", "content": prompt}],
        )
        raw  = msg.content[0].text.strip()
        # Strip markdown fences
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()
        data = json.loads(raw)

        ranked = data.get("ranked_watchlist", [])
        themes = data.get("themes_identified", [])
        omissions = data.get("notable_omissions", [])

        print(f"\n  Themes identified: {', '.join(themes)}")
        if omissions:
            print(f"  Notable gaps: {', '.join(omissions)}")
        print()

        return ranked, themes

    except Exception as e:
        print(f"  AI ranking failed: {e}")
        try:
            print(f"  Raw response preview: {raw[:300]}")
        except:
            pass
        print("  Falling back to score ranking")
        fallback = [{"ticker": t, "rank": i+1, "conviction": "medium",
                     "one_line_thesis": f"Score {s['score']}/10 — {s['verdict']}",
                     "key_edge": "", "watch_for": ""}
                    for i, (t, _, s) in enumerate(candidates)]
        return fallback, []


# ── Step 5: Save to watchlist ─────────────────────────────────────────────────

def save_watchlist(candidates: list, ranked: list):
    """Save ranked candidates to the database."""
    import json as _json, sqlite3 as _sq
    from db import save_ai_summary

    # Clean duplicates before saving — prevents daily accumulation
    try:
        conn = _sq.connect("research.db")
        for table in ["fundamentals", "ai_summaries"]:
            conn.execute(f"DELETE FROM {table} WHERE rowid NOT IN (SELECT MAX(rowid) FROM {table} GROUP BY ticker)")
        conn.commit()
        conn.close()
    except Exception:
        pass

    print("Saving watchlist to database...")
    ranked_tickers = [r["ticker"] for r in ranked]

    # Map ticker -> (data, score)
    cand_map = {t: (d, s) for t, d, s in candidates}

    saved = 0
    for item in ranked:
        ticker = item["ticker"]
        if ticker not in cand_map:
            continue
        data, score = cand_map[ticker]

        # Enrich score with AI thesis
        score["ai_thesis"]   = item.get("one_line_thesis", "")
        score["conviction"]  = item.get("conviction", "medium")
        score["key_edge"]    = item.get("key_edge", "")

        upsert_fundamentals(data, score)

        # Save conviction and thesis to ai_summaries so portfolio.py can read it
        save_ai_summary(ticker, "thesis", datetime.now().strftime("%Y-%m"), {
            "summary":      item.get("one_line_thesis", ""),
            "green_flags":  [item.get("key_edge", "")],
            "red_flags":    [],
            "sentiment":    "positive" if item.get("conviction") == "high" else "neutral",
            "raw_response": _json.dumps({
                "conviction":           item.get("conviction", "medium"),
                "one_line_thesis":      item.get("one_line_thesis", ""),
                "key_edge":             item.get("key_edge", ""),
                "watch_for":            item.get("watch_for", ""),
            }),
        })

        if item.get("conviction") == "high":
            log_alert(ticker, "quality_threshold",
                      f"Discovery: {ticker} — {item.get('one_line_thesis', '')}",
                      severity="high")
        saved += 1

    print(f"  Saved {saved} stocks to watchlist.\n")
    return ranked_tickers


# ── Main ──────────────────────────────────────────────────────────────────────

def run_discovery(theme: str = None, size: str = None, top_n: int = 15):
    """Full discovery pipeline. Run this to auto-populate your watchlist."""

    # Adjust market cap filter for size
    if size == "small":
        SCREEN["min_market_cap"] = 500e6
        SCREEN["max_market_cap"] = 5e9
    elif size == "mid":
        SCREEN["min_market_cap"] = 2e9
        SCREEN["max_market_cap"] = 50e9
    elif size == "large":
        SCREEN["min_market_cap"] = 50e9
        SCREEN["max_market_cap"] = 500e9

    init_db()

    print("\n" + "=" * 60)
    print("STOCK DISCOVERY ENGINE")
    if theme:
        print(f"Theme: {theme.upper()}")
    print("=" * 60 + "\n")

    start = time.time()

    # 1. Universe
    universe = build_universe(theme=theme)

    # 2. Screen
    screened = fast_screen(universe, theme=theme)
    if not screened:
        print("No candidates passed the screen. Try loosening the filters in SCREEN dict.")
        return []

    # 3. Score
    scored = score_candidates(screened)
    if not scored:
        print("No candidates scored high enough. Try lowering min_quality_score.")
        return []

    # 4. AI rank
    ranked, themes = ai_rank_candidates(scored, top_n=top_n)

    # 5. Save
    watchlist = save_watchlist(scored, ranked)

    elapsed = round(time.time() - start)
    print("=" * 60)
    print(f"DISCOVERY COMPLETE — {len(watchlist)} stocks added to watchlist")
    print(f"Time: {elapsed}s")
    print("=" * 60)
    print("\nTop picks by conviction:\n")
    for item in ranked[:10]:
        conv_icon = {"high": "🔥", "medium": "👀", "low": "💤"}.get(item.get("conviction"), "")
        print(f"  {item['rank']:>2}. {item['ticker']:<6} {conv_icon} {item.get('one_line_thesis','')}")
    print(f"\nRun `python3 -m streamlit run dashboard.py` to view the full dashboard.")
    return watchlist


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Auto-discover quality stocks")
    p.add_argument("--theme", choices=list(THEMES.keys()), help="Focus on a sector theme")
    p.add_argument("--size",  choices=["small","mid","large"], help="Market cap filter")
    p.add_argument("--top",   type=int, default=15, help="Number of stocks to add (default 15)")
    args = p.parse_args()
    run_discovery(theme=args.theme, size=args.size, top_n=args.top)
