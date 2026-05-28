from typing import Optional, List, Dict
"""
Stock Research Pipeline — AI Summarization Layer
=================================================
Uses Claude API to:
  1. Summarize 10-K filings into investment-relevant bullets
  2. Extract red/green flags from earnings call transcripts
  3. Generate variant perception thesis for qualifying stocks

Install:
    pip install anthropic requests

Set environment variable:
    export ANTHROPIC_API_KEY=sk-ant-...
"""

import os
import json
import time
import requests
import anthropic
from datetime import datetime
from db import save_ai_summary, get_latest_summary, log_alert

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
MODEL  = "claude-sonnet-4-5"


# ── Prompt templates ──────────────────────────────────────────────────────────

PROMPT_10K = """You are an expert equity analyst. Analyze this 10-K filing excerpt and extract investment-relevant insights.

Company: {company} ({ticker})
Fiscal Year: {period}

Filing text:
{text}

Return ONLY valid JSON in this exact format:
{{
  "summary": "3-4 sentence executive summary focusing on business quality and durability",
  "green_flags": ["up to 5 specific positive signals from this filing"],
  "red_flags": ["up to 5 specific concerns or risks from this filing"],
  "key_metrics": {{
    "revenue_growth": "exact figure if mentioned",
    "gross_margin": "exact figure if mentioned",
    "fcf": "exact figure if mentioned",
    "guidance": "any forward guidance mentioned"
  }},
  "management_tone": "one of: confident / cautious / defensive / evasive",
  "sentiment": "one of: positive / neutral / negative",
  "variant_angle": "one sentence: what does this filing reveal that the market might be underweighting?"
}}"""


PROMPT_EARNINGS = """You are an expert equity analyst specializing in earnings call analysis.

Company: {company} ({ticker})
Quarter: {period}

Earnings call transcript:
{text}

Return ONLY valid JSON in this exact format:
{{
  "summary": "3-4 sentences: key takeaways an investor needs to know",
  "green_flags": [
    "specific positive signals — quotes or paraphrases with context"
  ],
  "red_flags": [
    "specific concerns — vague answers, guidance cuts, margin pressure, etc."
  ],
  "guidance_change": "raised / maintained / lowered / withdrawn / none",
  "management_tone": "confident / cautious / defensive / evasive",
  "analyst_pushback": "were analysts pushing back on anything? summarize",
  "what_wasnt_said": "notable omissions or topics management avoided",
  "sentiment": "positive / neutral / negative",
  "follow_up_questions": [
    "2-3 questions you'd want answered before the next quarter"
  ]
}}"""


PROMPT_THESIS = """You are a fundamental equity analyst helping build a variant perception thesis.

Stock: {ticker} — {company}
Sector: {sector}
Quality score: {score}/10

Financial snapshot:
- Revenue growth: {revenue_growth}
- Gross margin: {gross_margin}
- FCF conversion: {fcf_conversion}
- ROIC: {roic}
- Insider ownership: {insider_pct}
- Analyst coverage: {analyst_count} analysts

Known context: {context}

Generate a variant perception thesis — what does a contrarian, well-informed investor believe about this company that the consensus does not?

Return ONLY valid JSON in this exact format:
{{
  "thesis": "One crisp sentence stating the variant view. Must complete: 'Unlike consensus which believes X, we believe Y because Z'",
  "thesis_expanded": "2-3 sentence elaboration of the thesis",
  "what_consensus_misses": "The specific insight or data point the market is underweighting",
  "leading_indicators": [
    {{
      "indicator": "specific metric or event to track",
      "frequency": "quarterly / monthly / weekly",
      "confirms_thesis_if": "what outcome validates the thesis",
      "kills_thesis_if": "what outcome invalidates it"
    }},
    {{
      "indicator": "second indicator",
      "frequency": "quarterly",
      "confirms_thesis_if": "...",
      "kills_thesis_if": "..."
    }},
    {{
      "indicator": "third indicator",
      "frequency": "quarterly",
      "confirms_thesis_if": "...",
      "kills_thesis_if": "..."
    }}
  ],
  "time_horizon": "estimated months for thesis to play out",
  "key_risks": ["2-3 specific risks that could break the thesis"],
  "conviction": "high / medium / low"
}}"""


# ── Core API caller ───────────────────────────────────────────────────────────

def _call_claude(prompt: str, max_tokens: int = 1000) -> dict:
    """Call Claude API and parse JSON response."""
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

        # Strip markdown code fences if present
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        return {"ok": True, "data": json.loads(raw), "raw": raw}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"JSON parse failed: {e}", "raw": raw}
    except Exception as e:
        return {"ok": False, "error": str(e), "raw": ""}


# ── 10-K summarizer ───────────────────────────────────────────────────────────

def summarize_10k(
    ticker:  str,
    company: str,
    period:  str,
    text:    str,
    max_chars: int = 12000,
) -> dict:
    """
    Summarize a 10-K filing excerpt.
    text: raw text from the filing (risk factors + MD&A sections work best)
    max_chars: trim to stay within token limits (~3k tokens)
    """
    print(f"  Summarizing 10-K: {ticker} {period}...")

    prompt = PROMPT_10K.format(
        ticker=ticker,
        company=company,
        period=period,
        text=text[:max_chars],
    )

    result = _call_claude(prompt, max_tokens=1200)

    if result["ok"]:
        data = result["data"]
        save_ai_summary(ticker, "10k", period, {
            "summary":      data.get("summary"),
            "green_flags":  data.get("green_flags", []),
            "red_flags":    data.get("red_flags", []),
            "sentiment":    data.get("sentiment"),
            "raw_response": result["raw"],
        })

        # Alert if red flags found
        if len(data.get("red_flags", [])) >= 3:
            log_alert(ticker, "earnings_flag",
                      f"10-K for {ticker} has {len(data['red_flags'])} red flags",
                      severity="high")
        return data
    else:
        print(f"  Error: {result['error']}")
        return {}


# ── Earnings call analyzer ────────────────────────────────────────────────────

def analyze_earnings(
    ticker:     str,
    company:    str,
    period:     str,
    transcript: str,
    max_chars:  int = 15000,
) -> dict:
    """
    Analyze an earnings call transcript for red/green flags.
    transcript: raw text of the earnings call (Q&A section especially valuable)
    """
    print(f"  Analyzing earnings: {ticker} {period}...")

    prompt = PROMPT_EARNINGS.format(
        ticker=ticker,
        company=company,
        period=period,
        text=transcript[:max_chars],
    )

    result = _call_claude(prompt, max_tokens=1200)

    if result["ok"]:
        data = result["data"]
        save_ai_summary(ticker, "earnings", period, {
            "summary":      data.get("summary"),
            "green_flags":  data.get("green_flags", []),
            "red_flags":    data.get("red_flags", []),
            "sentiment":    data.get("sentiment"),
            "raw_response": result["raw"],
        })

        # Alert on lowered guidance or negative sentiment
        if data.get("guidance_change") == "lowered":
            log_alert(ticker, "earnings_flag",
                      f"{ticker} lowered guidance in {period} earnings call",
                      severity="high")
        if data.get("sentiment") == "negative":
            log_alert(ticker, "earnings_flag",
                      f"{ticker} {period} earnings call: negative management tone",
                      severity="medium")
        return data
    else:
        print(f"  Error: {result['error']}")
        return {}


# ── Thesis generator ──────────────────────────────────────────────────────────

def generate_thesis(
    ticker:  str,
    company: str,
    sector:  str,
    score:   int,
    metrics: dict,
    context: str = "",
) -> dict:
    """
    Generate a variant perception thesis for a stock.
    Call this on stocks that score 7+ on the quality checklist.
    """
    print(f"  Generating thesis: {ticker}...")

    def fmt(val, pct=False):
        if val is None:
            return "N/A"
        return f"{val*100:.1f}%" if pct else str(round(val, 2))

    prompt = PROMPT_THESIS.format(
        ticker=ticker,
        company=company,
        sector=sector,
        score=score,
        revenue_growth=fmt(metrics.get("revenue_growth"), pct=True),
        gross_margin=fmt(metrics.get("gross_margin"), pct=True),
        fcf_conversion=fmt(metrics.get("fcf_conversion")),
        roic=fmt(metrics.get("roic"), pct=True),
        insider_pct=fmt(metrics.get("insider_pct"), pct=True),
        analyst_count=metrics.get("analyst_count", "N/A"),
        context=context or "No additional context provided.",
    )

    result = _call_claude(prompt, max_tokens=1000)

    if result["ok"]:
        data = result["data"]
        save_ai_summary(ticker, "thesis", datetime.utcnow().strftime("%Y-%m"), {
            "summary":      data.get("thesis"),
            "green_flags":  [data.get("what_consensus_misses", "")],
            "red_flags":    data.get("key_risks", []),
            "sentiment":    "positive",
            "raw_response": result["raw"],
        })
        return data
    else:
        print(f"  Error: {result['error']}")
        return {}


# ── Batch runner ──────────────────────────────────────────────────────────────

def run_ai_pipeline(watchlist: List[dict], delay: float = 1.0):
    """
    Run AI summarization across a watchlist.
    watchlist: list of dicts with ticker, name, sector, score, and metric fields
    Skips tickers that already have recent summaries (within 90 days).
    """
    print("\n" + "=" * 60)
    print("AI SUMMARIZATION LAYER")
    print("=" * 60)

    for stock in watchlist:
        ticker  = stock["ticker"]
        company = stock.get("name", ticker)
        sector  = stock.get("sector", "")
        score   = stock.get("quality_score", 0)

        print(f"\n{ticker} — {company} (score: {score}/10)")

        # Only generate thesis for high-quality names (score ≥ 7)
        if score >= 7:
            existing = get_latest_summary(ticker, "thesis")
            if not existing:
                thesis = generate_thesis(
                    ticker=ticker,
                    company=company,
                    sector=sector,
                    score=score,
                    metrics=stock,
                )
                if thesis:
                    print(f"  Thesis: {thesis.get('thesis', '')[:100]}...")
                    print(f"  Conviction: {thesis.get('conviction')}")
                    print(f"  Horizon: {thesis.get('time_horizon')}")
            else:
                print(f"  Thesis already exists (skipping)")
            time.sleep(delay)
        else:
            print(f"  Score {score} < 7 — skipping thesis generation")


# ── Fetch transcript helper ───────────────────────────────────────────────────

def fetch_transcript_motleyfool(ticker: str) -> Optional[str]:
    """
    Attempt to fetch earnings transcript from Motley Fool (public pages).
    In production: use Seeking Alpha API ($150/mo) or Earnings Call Transcript API.
    """
    url = f"https://www.fool.com/quote/{ticker.lower()}/#earnings-transcripts"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            # Basic text extraction — in production use BeautifulSoup
            return r.text[:15000]
    except Exception:
        pass
    return None


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from db import init_db
    init_db()

    # Demo: generate a thesis for a sample high-quality stock
    # (requires ANTHROPIC_API_KEY env var)
    sample = {
        "ticker":         "AXON",
        "name":           "Axon Enterprise",
        "sector":         "Technology",
        "quality_score":  8,
        "revenue_growth": 0.29,
        "gross_margin":   0.62,
        "fcf_conversion": 0.91,
        "roic":           0.17,
        "insider_pct":    0.04,
        "analyst_count":  18,
    }

    print("Generating variant perception thesis for AXON...")
    thesis = generate_thesis(
        ticker  = sample["ticker"],
        company = sample["name"],
        sector  = sample["sector"],
        score   = sample["quality_score"],
        metrics = sample,
        context = "Axon dominates law enforcement with Tasers and body cams. "
                  "Now expanding into enterprise software (AXON Records, Fusus). "
                  "International expansion just starting. Low analyst coverage relative to TAM."
    )

    if thesis:
        print("\n" + "─" * 60)
        print("VARIANT PERCEPTION THESIS")
        print("─" * 60)
        print(f"\n{thesis.get('thesis')}\n")
        print(f"What consensus misses: {thesis.get('what_consensus_misses')}")
        print(f"\nLeading indicators to track:")
        for li in thesis.get("leading_indicators", []):
            print(f"  • {li['indicator']} ({li['frequency']})")
            print(f"    Confirms if: {li['confirms_thesis_if']}")
            print(f"    Breaks if:   {li['kills_thesis_if']}")
        print(f"\nConviction: {thesis.get('conviction')} | Horizon: {thesis.get('time_horizon')}")
        print(f"Key risks: {', '.join(thesis.get('key_risks', []))}")
