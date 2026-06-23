"""
Stock Research Pipeline — Earnings Call Transcript Analyzer
============================================================
Fetches earnings call transcripts and uses Claude to extract:
- Management tone shift (confident/cautious/defensive/evasive)
- Guidance quality (raised/maintained/lowered/withdrawn)
- What management avoided saying
- Take rate, margin, and pricing commentary
- Red and green flags with exact quotes
- Whether the thesis leading indicators are on track or breaking

Transcript sources (in priority order):
1. API Ninjas — free, returns structured data including guidance + risks
2. earningscall library — free tier, 5000+ companies
3. Fallback: fetch from SEC EDGAR 8-K filing text

Setup:
    pip install requests anthropic earningscall
    Get free API Ninjas key at: https://api-ninjas.com (free tier: 50,000 calls/month)
    Set: export API_NINJAS_KEY=your_key_here

Usage:
    python3 transcript_analyzer.py                    # analyze all upcoming earnings
    python3 transcript_analyzer.py --ticker ANET      # single ticker
    python3 transcript_analyzer.py --ticker ANET --quarter 1 --year 2026
"""

import os
import json
import time
import db_adapter as sqlite3
import requests
import anthropic
from datetime import datetime
from typing import Optional, List, Dict

DB_PATH          = "research.db"
API_NINJAS_KEY   = os.environ.get("API_NINJAS_KEY", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")


# ── Database setup ────────────────────────────────────────────────────────────

def init_transcript_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS earnings_transcripts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            quarter         INTEGER,
            year            INTEGER,
            earnings_date   TEXT,
            fetched_at      TEXT,
            source          TEXT,
            raw_transcript  TEXT,
            word_count      INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcript_analysis (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker              TEXT NOT NULL,
            quarter             INTEGER,
            year                INTEGER,
            analyzed_at         TEXT,
            management_tone     TEXT,
            guidance_direction  TEXT,
            guidance_detail     TEXT,
            green_flags         TEXT,
            red_flags           TEXT,
            what_wasnt_said     TEXT,
            take_rate_comment   TEXT,
            margin_comment      TEXT,
            pricing_comment     TEXT,
            thesis_status       TEXT,
            thesis_notes        TEXT,
            analyst_pushback    TEXT,
            follow_up_questions TEXT,
            sentiment_score     REAL,
            overall_verdict     TEXT,
            UNIQUE(ticker, quarter, year)
        )
    """)
    conn.commit()
    conn.close()


# ── Transcript fetchers ───────────────────────────────────────────────────────

def fetch_from_api_ninjas(ticker: str, year: int = None, quarter: int = None) -> Optional[dict]:
    """
    Fetch transcript from API Ninjas.
    Free tier: 50,000 calls/month. Returns transcript + pre-extracted guidance + risks.
    Get key at: https://api-ninjas.com
    """
    if not API_NINJAS_KEY:
        return None

    params = {"ticker": ticker}
    if year and quarter:
        params["year"]    = year
        params["quarter"] = quarter

    try:
        r = requests.get(
            "https://api.api-ninjas.com/v1/earningstranscript",
            headers={"X-Api-Key": API_NINJAS_KEY},
            params=params,
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if data and data.get("transcript"):
                return {
                    "source":       "api_ninjas",
                    "transcript":   data.get("transcript", ""),
                    "summary":      data.get("summary", ""),
                    "guidance":     data.get("guidance", ""),
                    "risk_factors": data.get("risk_factors", ""),
                    "sentiment":    data.get("sentiment", 0),
                    "date":         data.get("date", ""),
                    "quarter":      data.get("quarter"),
                    "year":         data.get("year"),
                }
    except Exception as e:
        print(f"  API Ninjas failed: {e}")
    return None


def fetch_from_earningscall(ticker: str, year: int = None, quarter: int = None) -> Optional[dict]:
    """
    Fetch transcript using the earningscall Python library.
    pip install earningscall
    Free tier available — check earningscall.com for limits.
    """
    try:
        from earningscall import get_company
        company = get_company(ticker)
        if not company:
            return None

        if year and quarter:
            transcript = company.get_transcript(year=year, quarter=quarter)
        else:
            transcript = company.get_transcript()  # latest

        if transcript:
            text = ""
            if hasattr(transcript, "prepared_remarks") and transcript.prepared_remarks:
                text += "=== PREPARED REMARKS ===\n" + transcript.prepared_remarks + "\n\n"
            if hasattr(transcript, "questions_and_answers") and transcript.questions_and_answers:
                text += "=== Q&A ===\n" + transcript.questions_and_answers

            if text:
                return {
                    "source":       "earningscall",
                    "transcript":   text,
                    "summary":      "",
                    "guidance":     "",
                    "risk_factors": "",
                    "sentiment":    0,
                    "date":         str(getattr(transcript, "date", "")),
                    "quarter":      getattr(transcript, "quarter", quarter),
                    "year":         getattr(transcript, "year", year),
                }
    except ImportError:
        pass  # earningscall not installed
    except Exception as e:
        print(f"  earningscall failed: {e}")
    return None


def fetch_from_motley_fool(ticker: str, year: int = None, quarter: int = None) -> Optional[dict]:
    """
    Fetch transcript from Motley Fool via direct URL construction.
    Motley Fool publishes full transcripts for every major earnings call — free.
    """
    import re
    from datetime import datetime, timedelta

    now = datetime.now()
    yr  = year  or now.year
    q   = quarter or 1

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }

    # Quarter to month mapping (end of quarter)
    q_months = {1: ["03", "04", "05"], 2: ["06", "07", "08"],
                 3: ["09", "10", "11"], 4: ["12", "01", "02"]}
    months = q_months.get(q, ["05"])

    # Common URL patterns Motley Fool uses
    ticker_lower = ticker.lower()
    name_guesses = [ticker_lower, f"arista-{ticker_lower}" if ticker == "ANET" else ticker_lower]

    candidate_urls = []
    for month in months:
        adj_yr = yr if month not in ["01", "02"] or q != 4 else yr + 1
        for name in name_guesses:
            # Pattern 1: ticker-name-qN-YYYY format
            candidate_urls.append(
                f"https://www.fool.com/earnings/call-transcripts/{adj_yr}/{adj_yr}-{month}-"
                f"*/{name}-{ticker_lower}-q{q}-{yr}-earnings"
            )
            # Pattern 2: direct known patterns
            candidate_urls.append(
                f"https://www.fool.com/earnings/call-transcripts/{adj_yr}/{adj_yr}-{month}-05/"
                f"{name}-{ticker_lower}-q{q}-{yr}-earnings-call-transcript/"
            )
            candidate_urls.append(
                f"https://www.fool.com/earnings/call-transcripts/{adj_yr}/{adj_yr}-{month}-05/"
                f"{name}-{ticker_lower}-q{q}-{yr}-earnings-transcript/"
            )

    def extract_transcript(html: str, q: int, yr: int) -> Optional[dict]:
        import re
        # Remove scripts and styles
        html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL)
        html = re.sub(r"<style[^>]*>.*?</style>",   " ", html, flags=re.DOTALL)
        html = re.sub(r"<[^>]+>", " ", html)
        html = re.sub(r"\s+", " ", html)

        # Find transcript start
        for marker in ["Full Conference Call Transcript", "Prepared Remarks", "Operator"]:
            idx = html.find(marker)
            if idx > 0:
                end_idx = len(html)
                for end_marker in ["This article represents", "More From The Motley Fool"]:
                    ei = html.find(end_marker, idx)
                    if ei > 0:
                        end_idx = min(end_idx, ei)
                text = html[idx:end_idx].strip()
                if len(text) > 1000:
                    return text
        return None

    # Try known working URLs first (build from search results we already know)
    known_urls = {
        ("ANET", 1, 2026): "https://www.fool.com/earnings/call-transcripts/2026/05/05/arista-anet-q1-2026-earnings-transcript/",
        ("MNST", 1, 2026): "https://www.fool.com/earnings/call-transcripts/2026/05/08/monster-beverage-mnst-q1-2026-earnings-call-trans/",
        ("NVDA", 1, 2026): "https://www.fool.com/earnings/call-transcripts/2026/05/28/nvidia-nvda-q1-2026-earnings-call-transcript/",
    }

    direct_url = known_urls.get((ticker.upper(), q, yr))
    if direct_url:
        try:
            r = requests.get(direct_url, headers=headers, timeout=15)
            if r.status_code == 200:
                text = extract_transcript(r.text, q, yr)
                if text:
                    print(f"  Got transcript from Motley Fool ({len(text)} chars)")
                    return {
                        "source": "motley_fool", "transcript": text[:20000],
                        "summary": "", "guidance": "", "risk_factors": "",
                        "sentiment": 0, "date": "", "quarter": q, "year": yr,
                    }
        except Exception as e:
            print(f"  Direct URL failed: {e}")

    # Try candidate URLs
    for url in candidate_urls[:6]:
        if "*" in url:
            continue
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                text = extract_transcript(r.text, q, yr)
                if text:
                    print(f"  Got transcript from Motley Fool ({len(text)} chars)")
                    return {
                        "source": "motley_fool", "transcript": text[:20000],
                        "summary": "", "guidance": "", "risk_factors": "",
                        "sentiment": 0, "date": "", "quarter": q, "year": yr,
                    }
        except Exception:
            continue

    return None


def fetch_from_sec_8k(ticker: str) -> Optional[dict]:
    """
    Fallback: pull earnings press release text from SEC EDGAR 8-K filings.
    Not a full transcript but contains key numbers and guidance.
    """
    try:
        # Get company CIK
        r = requests.get(
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={ticker}"
            f"&type=8-K&dateb=&owner=include&count=5&search_text=",
            headers={"User-Agent": "StockResearch research@example.com"},
            timeout=10,
        )
        import re
        cik_match = re.search(r"CIK=(\d+)", r.text)
        if not cik_match:
            return None

        cik = cik_match.group(1).zfill(10)
        subs = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": "StockResearch research@example.com"},
            timeout=10,
        ).json()

        filings = subs.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        acc_nos = filings.get("accessionNumber", [])
        dates   = filings.get("filingDate", [])

        # Find most recent 8-K
        for form, acc, date in zip(forms, acc_nos, dates):
            if form == "8-K":
                acc_clean = acc.replace("-", "")
                idx_url   = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{acc}-index.htm"
                idx_r     = requests.get(idx_url, headers={"User-Agent": "StockResearch research@example.com"}, timeout=10)
                # Find the text document
                txt_match = re.search(r'href="([^"]+\.htm)"', idx_r.text)
                if txt_match:
                    doc_url = f"https://www.sec.gov{txt_match.group(1)}"
                    doc_r   = requests.get(doc_url, headers={"User-Agent": "StockResearch research@example.com"}, timeout=10)
                    # Strip HTML tags
                    text = re.sub(r"<[^>]+>", " ", doc_r.text)
                    text = re.sub(r"\s+", " ", text)[:20000]
                    return {
                        "source":       "sec_8k",
                        "transcript":   text,
                        "summary":      "",
                        "guidance":     "",
                        "risk_factors": "",
                        "sentiment":    0,
                        "date":         date,
                        "quarter":      None,
                        "year":         None,
                    }
                break
    except Exception as e:
        print(f"  SEC 8-K fallback failed: {e}")
    return None


def fetch_transcript(ticker: str, year: int = None, quarter: int = None) -> Optional[dict]:
    """Try all sources in priority order."""
    print(f"  Fetching transcript for {ticker}...")

    result = fetch_from_api_ninjas(ticker, year, quarter)
    if result:
        print(f"  ✓ Got transcript from API Ninjas ({len(result['transcript'])} chars)")
        return result

    result = fetch_from_motley_fool(ticker, year, quarter)
    if result:
        return result

    result = fetch_from_earningscall(ticker, year, quarter)
    if result:
        print(f"  ✓ Got transcript from earningscall ({len(result['transcript'])} chars)")
        return result

    result = fetch_from_sec_8k(ticker)
    if result:
        print(f"  ✓ Got 8-K text from SEC EDGAR ({len(result['transcript'])} chars)")
        return result

    print(f"  ✗ No transcript found for {ticker}")
    return None


def save_transcript(ticker: str, data: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO earnings_transcripts
        (ticker, quarter, year, earnings_date, fetched_at, source, raw_transcript, word_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ticker, data.get("quarter"), data.get("year"), data.get("date"),
        datetime.now().isoformat(), data.get("source"),
        data.get("transcript"), len(data.get("transcript", "").split()),
    ))
    conn.commit()
    conn.close()


# ── Claude analyzer ───────────────────────────────────────────────────────────

def get_thesis_indicators(ticker: str) -> List[dict]:
    """Pull leading indicators from stored AI thesis."""
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


def analyze_transcript(ticker: str, transcript_data: dict) -> Optional[dict]:
    """Send transcript to Claude for deep analysis."""
    if not ANTHROPIC_KEY:
        print("  No ANTHROPIC_API_KEY — skipping AI analysis")
        return None

    transcript  = transcript_data.get("transcript", "")
    pre_summary = transcript_data.get("summary", "")
    pre_guidance = transcript_data.get("guidance", "")
    pre_risks   = transcript_data.get("risk_factors", "")
    indicators  = get_thesis_indicators(ticker)

    # Build indicator context
    indicator_text = ""
    if indicators:
        indicator_text = "\n\nTHESIS LEADING INDICATORS TO CHECK:\n"
        for i, ind in enumerate(indicators[:3], 1):
            indicator_text += f"{i}. {ind.get('indicator','?')}\n"
            indicator_text += f"   Confirms if: {ind.get('confirms_thesis_if','?')}\n"
            indicator_text += f"   Breaks if: {ind.get('kills_thesis_if','?')}\n"

    # Truncate transcript to fit context
    max_chars = 12000
    if len(transcript) > max_chars:
        # Prioritize Q&A section — most revealing
        qa_start = transcript.find("Q&A")
        if qa_start > 0:
            transcript = transcript[max(0, qa_start-2000):qa_start+8000]
        else:
            transcript = transcript[:max_chars]

    prompt = f"""You are an expert equity analyst. Analyze this earnings call for {ticker}.

Pre-extracted data:
Summary: {pre_summary[:500] if pre_summary else 'None'}
Guidance: {pre_guidance[:300] if pre_guidance else 'None'}
Risk factors: {pre_risks[:300] if pre_risks else 'None'}
{indicator_text}

Transcript (may be truncated):
{transcript}

Return ONLY valid JSON, no markdown, all strings under 120 chars:
{{
  "management_tone": "confident/cautious/defensive/evasive",
  "guidance_direction": "raised/maintained/lowered/withdrawn/none",
  "guidance_detail": "specific guidance given in one sentence",
  "green_flags": ["up to 4 specific positive signals with context"],
  "red_flags": ["up to 4 specific concerns — vague answers, margin pressure, etc"],
  "what_wasnt_said": "notable topic management avoided or was not asked about",
  "take_rate_comment": "any mention of pricing, take rate, or fee pressure",
  "margin_comment": "any gross or operating margin commentary",
  "pricing_comment": "evidence of pricing power or lack thereof",
  "thesis_status": "on_track/at_risk/broken/insufficient_data",
  "thesis_notes": "how the call affected each leading indicator in one sentence",
  "analyst_pushback": "what analysts challenged management on",
  "follow_up_questions": ["2 questions you would ask next quarter"],
  "sentiment_score": 0.7,
  "overall_verdict": "one sentence investment takeaway from this call"
}}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg    = client.messages.create(
            model      = "claude-sonnet-4-5",
            max_tokens = 1200,
            messages   = [{"role": "user", "content": prompt}],
        )
        raw   = msg.content[0].text.strip()
        lines = [l for l in raw.split("\n") if not l.strip().startswith("```")]
        raw   = "\n".join(lines).strip()
        return json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"  Claude analysis failed: {e}")
        return None


def save_analysis(ticker: str, quarter: int, year: int, analysis: dict):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=DELETE")  # disable WAL for this connection
    conn.execute("""
        INSERT OR REPLACE INTO transcript_analysis
        (ticker, quarter, year, analyzed_at,
         management_tone, guidance_direction, guidance_detail,
         green_flags, red_flags, what_wasnt_said,
         take_rate_comment, margin_comment, pricing_comment,
         thesis_status, thesis_notes, analyst_pushback,
         follow_up_questions, sentiment_score, overall_verdict)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ticker, quarter, year, datetime.now().isoformat(),
        analysis.get("management_tone"),
        analysis.get("guidance_direction"),
        analysis.get("guidance_detail"),
        json.dumps(analysis.get("green_flags", [])),
        json.dumps(analysis.get("red_flags", [])),
        analysis.get("what_wasnt_said"),
        analysis.get("take_rate_comment"),
        analysis.get("margin_comment"),
        analysis.get("pricing_comment"),
        analysis.get("thesis_status"),
        analysis.get("thesis_notes"),
        analysis.get("analyst_pushback"),
        json.dumps(analysis.get("follow_up_questions", [])),
        analysis.get("sentiment_score"),
        analysis.get("overall_verdict"),
    ))

    conn.commit()
    conn.close()

    # Also save to ai_summaries for dashboard
    import time
    time.sleep(0.5)  # ensure first connection fully released
    from db import save_ai_summary
    period = f"{year}-Q{quarter}" if quarter and year else datetime.now().strftime("%Y-%m")
    save_ai_summary(ticker, "earnings", period, {
        "summary":      analysis.get("overall_verdict", ""),
        "green_flags":  analysis.get("green_flags", []),
        "red_flags":    analysis.get("red_flags", []),
        "sentiment":    "positive" if (analysis.get("sentiment_score") or 0) > 0.2
                        else "negative" if (analysis.get("sentiment_score") or 0) < -0.2
                        else "neutral",
        "raw_response": json.dumps(analysis),
    })

    # Log alert if thesis is at risk or broken
    if analysis.get("thesis_status") in ["at_risk", "broken"]:
        from db import log_alert
        log_alert(
            ticker, "earnings_flag",
            f"{ticker} earnings: thesis {analysis['thesis_status']} — {analysis.get('overall_verdict','')}",
            severity="high" if analysis["thesis_status"] == "broken" else "medium"
        )




# ── Main runner ───────────────────────────────────────────────────────────────

def run_transcript_analysis(ticker: str = None, year: int = None, quarter: int = None):
    """
    Run transcript analysis for one or all watchlist stocks.
    Prioritizes stocks with upcoming earnings in next 14 days.
    """
    init_transcript_db()

    if ticker:
        tickers = [ticker.upper()]
    else:
        # Get watchlist sorted by earnings proximity
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT f.ticker, e.days_until, e.next_earnings
            FROM fundamentals f
            LEFT JOIN earnings_dates e ON f.ticker = e.ticker
            INNER JOIN (
                SELECT ticker, MAX(fetched_at) as latest
                FROM fundamentals GROUP BY ticker
            ) l ON f.ticker = l.ticker AND f.fetched_at = l.latest
            WHERE e.days_until IS NOT NULL
            ORDER BY e.days_until ASC
        """).fetchall()
        conn.close()
        # Only analyze stocks with recent earnings (within last 30 days or upcoming 3 days)
        tickers = [r[0] for r in rows if r[1] is not None and r[1] <= 3]
        if not tickers:
            print("No upcoming earnings in next 3 days. Analyzing most recent for watchlist...")
            tickers = [r[0] for r in rows[:10]]

    print(f"\n{'='*65}")
    print("EARNINGS TRANSCRIPT ANALYZER")
    print(f"{'='*65}")
    print(f"Analyzing {len(tickers)} stocks...\n")

    for t in tickers:
        print(f"\n{t}:")
        data = fetch_transcript(t, year=year, quarter=quarter)
        if not data:
            continue

        save_transcript(t, data)
        analysis = analyze_transcript(t, data)

        if not analysis:
            continue

        save_analysis(t, data.get("quarter"), data.get("year"), analysis)

        # Print summary
        tone_icon = {
            "confident": "✅", "cautious": "🟡",
            "defensive": "🟠", "evasive": "🔴"
        }.get(analysis.get("management_tone", ""), "?")

        guidance_icon = {
            "raised": "📈", "maintained": "➡️",
            "lowered": "📉", "withdrawn": "🚨"
        }.get(analysis.get("guidance_direction", ""), "?")

        thesis_icon = {
            "on_track": "✅", "at_risk": "⚠️",
            "broken": "❌", "insufficient_data": "—"
        }.get(analysis.get("thesis_status", ""), "?")

        print(f"  Tone: {tone_icon} {analysis.get('management_tone','?')}")
        print(f"  Guidance: {guidance_icon} {analysis.get('guidance_direction','?')} — {analysis.get('guidance_detail','')[:80]}")
        print(f"  Thesis: {thesis_icon} {analysis.get('thesis_status','?')}")
        print(f"  Verdict: {analysis.get('overall_verdict','')[:100]}")

        if analysis.get("red_flags"):
            print(f"  Red flags:")
            for flag in analysis["red_flags"][:2]:
                print(f"    ⚠️  {flag[:80]}")

        if analysis.get("green_flags"):
            print(f"  Green flags:")
            for flag in analysis["green_flags"][:2]:
                print(f"    ✅ {flag[:80]}")

        time.sleep(1.5)  # Rate limit


# ── Auto-runner ──────────────────────────────────────────────────────────────

def build_transcript_url(ticker: str, year: int, quarter: int) -> Optional[str]:
    """
    Auto-build Motley Fool URL by searching for recent transcripts.
    Falls back to pattern matching.
    """
    import re
    from datetime import datetime

    # Quarter end months
    q_months = {1: range(3,6), 2: range(6,9), 3: range(9,12), 4: [12,1,2]}
    months = q_months.get(quarter, range(1,13))

    ticker_lower = ticker.lower()
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    # Try common URL patterns
    patterns = [
        f"{ticker_lower}-{ticker_lower}-q{quarter}-{year}-earnings-call-transcript",
        f"{ticker_lower}-{ticker_lower}-q{quarter}-{year}-earnings-transcript",
        f"{ticker_lower}-q{quarter}-{year}-earnings-call-transcript",
        f"{ticker_lower}-q{quarter}-{year}-earnings-transcript",
    ]

    for month in months:
        adj_year = year if month >= 3 else year + 1
        for pattern in patterns:
            for day in ["05", "06", "07", "08", "09", "10", "11", "12",
                        "13", "14", "15", "20", "21", "22", "23", "24", "25",
                        "26", "27", "28", "29", "30"]:
                url = (f"https://www.fool.com/earnings/call-transcripts/"
                       f"{adj_year}/{adj_year}-{month:02d}-{day}/{pattern}/")
                try:
                    r = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
                    if r.status_code == 200:
                        return url
                except Exception:
                    continue
    return None


def auto_run_transcripts():
    """
    Auto-fetch and analyze transcripts for:
    1. All stocks in action engine buy list
    2. All stocks in my_portfolio
    3. Any watchlist stock that reported in last 7 days
    Skips stocks already analyzed this quarter.
    """
    init_transcript_db()
    conn = sqlite3.connect(DB_PATH)

    # Get buy list tickers
    buy_tickers = set()
    try:
        rows = conn.execute("""
            SELECT DISTINCT f.ticker FROM fundamentals f
            INNER JOIN ai_summaries a ON f.ticker = a.ticker AND a.summary_type = 'thesis'
            WHERE a.raw_response LIKE '%"conviction": "high"%'
        """).fetchall()
        buy_tickers = {r[0] for r in rows}
    except Exception:
        pass

    # Get portfolio tickers
    portfolio_tickers = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM my_portfolio WHERE status='open'"
        ).fetchall()
        portfolio_tickers = {r[0] for r in rows}
    except Exception:
        pass

    # Get watchlist tickers with recent earnings (last 7 days)
    recent_earners = set()
    try:
        rows = conn.execute("""
            SELECT ticker FROM earnings_dates
            WHERE days_until >= -7 AND days_until <= 0
        """).fetchall()
        recent_earners = {r[0] for r in rows}
    except Exception:
        pass

    all_tickers = buy_tickers | portfolio_tickers | recent_earners
    conn.close()

    if not all_tickers:
        print("No tickers to analyze.")
        return

    print(f"Auto-analyzing {len(all_tickers)} stocks...")
    print(f"  Buy list: {buy_tickers}")
    print(f"  Portfolio: {portfolio_tickers}")
    print(f"  Recent earners: {recent_earners}")

    # Get current quarter
    from datetime import datetime
    now = datetime.now()
    quarter = (now.month - 1) // 3 + 1
    year    = now.year

    # Check which already have analysis this quarter
    conn = sqlite3.connect(DB_PATH)
    analyzed = set()
    try:
        rows = conn.execute("""
            SELECT ticker FROM transcript_analysis
            WHERE quarter = ? AND year = ?
        """, (quarter, year)).fetchall()
        analyzed = {r[0] for r in rows}
    except Exception:
        pass
    conn.close()

    to_analyze = all_tickers - analyzed
    print(f"  Already analyzed: {analyzed}")
    print(f"  To analyze: {to_analyze}\n")

    for ticker in sorted(to_analyze):
        print(f"\n{ticker}:")
        time.sleep(5)  # Rate limit protection between stocks
        data = fetch_transcript(ticker, year=year, quarter=quarter)

        # Try previous quarter if current not found
        if not data:
            prev_q = quarter - 1 if quarter > 1 else 4
            prev_y = year if quarter > 1 else year - 1
            print(f"  Trying Q{prev_q} {prev_y}...")
            data = fetch_transcript(ticker, year=prev_y, quarter=prev_q)

        if not data:
            print(f"  No transcript found — skipping")
            continue

        save_transcript(ticker, data)
        analysis = analyze_transcript(ticker, data)
        if analysis:
            save_analysis(ticker, data.get("quarter"), data.get("year"), analysis)
            tone_icon = {"confident":"✅","cautious":"🟡","defensive":"🟠","evasive":"🔴"}.get(
                analysis.get("management_tone",""), "?")
            print(f"  Tone: {tone_icon} {analysis.get('management_tone','?')}")
            print(f"  Thesis: {analysis.get('thesis_status','?')}")
            print(f"  Verdict: {analysis.get('overall_verdict','')[:80]}")
        time.sleep(2)

    print("\nAuto-run complete.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Earnings transcript analyzer")
    p.add_argument("--ticker",   help="Single ticker to analyze")
    p.add_argument("--quarter",  type=int, help="Quarter (1-4)")
    p.add_argument("--year",     type=int, help="Year (e.g. 2026)")
    p.add_argument("--auto",     action="store_true", help="Auto-run for portfolio + buy list")
    args = p.parse_args()

    if args.auto:
        auto_run_transcripts()
    elif args.ticker:
        run_transcript_analysis(
            ticker  = args.ticker,
            year    = args.year,
            quarter = args.quarter,
        )
    else:
        auto_run_transcripts()
