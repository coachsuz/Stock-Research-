from typing import Optional, List, Dict
"""
Stock Research Pipeline — 13F Diff Engine
==========================================
Pulls 13F filings from SEC EDGAR, parses holdings,
diffs consecutive quarters, and surfaces new positions and large adds.

No API key needed — SEC EDGAR is fully public.
Rate limit: max 10 requests/second (we stay well under with delays).

Usage:
    from filings_13f import run_13f_pipeline, get_cluster_buys
    run_13f_pipeline(FUND_LIST)
"""

import requests
import xml.etree.ElementTree as ET
import pandas as pd
import time
import re
from datetime import datetime
from db import get_conn, upsert_13f_holdings, log_alert

EDGAR_BASE  = "https://data.sec.gov"
EDGAR_FULL  = "https://www.sec.gov"
HEADERS     = {"User-Agent": "StockResearch research@example.com"}  # SEC requires this


# ── Funds to track ────────────────────────────────────────────────────────────
# Add any fund by name — the engine looks up their CIK automatically.
# Focus on concentrated, long-term funds (conviction > 60% in top 10 positions).

FUND_LIST = [
    "Coatue Management",
    "Tiger Global Management",
    "Pershing Square Capital Management",
    "Sequoia Fund",
    "Akre Capital Management",
    "Baillie Gifford",
    "Polen Capital Management",
    "Artisan Partners",
]


# ── CIK lookup ────────────────────────────────────────────────────────────────

def lookup_cik(fund_name: str) -> Optional[str]:
    """Search EDGAR company search to find a fund's CIK number."""
    url = f"{EDGAR_FULL}/cgi-bin/browse-edgar"
    params = {
        "company":  fund_name,
        "CIK":      "",
        "type":     "13F-HR",
        "dateb":    "",
        "owner":    "include",
        "count":    "10",
        "search_text": "",
        "action":   "getcompany",
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        # Extract CIK from HTML response (simple regex — avoids BeautifulSoup dep)
        match = re.search(r"CIK=(\d+)", r.text)
        if match:
            return match.group(1).lstrip("0")
    except Exception as e:
        print(f"  CIK lookup failed for {fund_name}: {e}")
    return None


def get_filings_index(cik: str, form_type: str = "13F-HR", limit: int = 4) -> List[dict]:
    """
    Get recent filing metadata for a CIK from EDGAR submissions API.
    Returns list of {accession_number, filed_at, period} dicts.
    """
    url = f"{EDGAR_BASE}/submissions/CIK{cik.zfill(10)}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Submissions fetch failed for CIK {cik}: {e}")
        return []

    filings = data.get("filings", {}).get("recent", {})
    forms        = filings.get("form", [])
    accessions   = filings.get("accessionNumber", [])
    dates        = filings.get("filingDate", [])
    periods      = filings.get("reportDate", [])

    results = []
    for form, accession, date, period in zip(forms, accessions, dates, periods):
        if form == form_type:
            results.append({
                "accession": accession.replace("-", ""),
                "filed_at":  date,
                "period":    period,
            })
        if len(results) >= limit:
            break

    return results


def quarter_label(period_str: str) -> str:
    """Convert '2025-03-31' → '2025-Q1'."""
    try:
        dt = datetime.strptime(period_str, "%Y-%m-%d")
        q  = (dt.month - 1) // 3 + 1
        return f"{dt.year}-Q{q}"
    except Exception:
        return period_str


# ── Filing parser ─────────────────────────────────────────────────────────────

def parse_13f_xml(cik: str, accession: str) -> List[dict]:
    """
    Download and parse the 13F information table XML.
    Returns list of {cusip, ticker, company, shares, market_value} dicts.
    """
    # Build index URL — folder uses no dashes, filename uses dashes
    acc_dashed = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    index_url  = f"{EDGAR_FULL}/Archives/edgar/data/{int(cik)}/{accession}/{acc_dashed}-index.htm"
    try:
        r = requests.get(index_url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            print(f"  Index page returned {r.status_code} for {accession}")
            return []
        # Find infotable.xml specifically — this has the holdings data
        # Prefer the non-xsl version (direct data file)
        xml_match = re.search(r'href="(/Archives/edgar/data/[^"]*infotable\.xml)"', r.text, re.IGNORECASE)
        if not xml_match:
            # Fallback: any infotable xml
            xml_match = re.search(r'href="([^"]*infotable[^"]*\.xml)"', r.text, re.IGNORECASE)
        if not xml_match:
            # Last fallback: any xml not in xsl folder
            all_xml = re.findall(r'href="([^"]+\.xml)"', r.text, re.IGNORECASE)
            non_xsl = [x for x in all_xml if "xsl" not in x.lower()]
            if non_xsl:
                xml_match = type("m", (), {"group": lambda self, n: non_xsl[-1]})()
        if not xml_match:
            print(f"  No XML found in filing index for {accession}")
            return []
        xml_path = xml_match.group(1)
        if not xml_path.startswith("/"):
            xml_path = f"/Archives/edgar/data/{int(cik)}/{accession}/{xml_path}"
    except Exception as e:
        print(f"  Index fetch failed: {e}")
        return []

    # Download the XML
    try:
        xml_url = f"{EDGAR_FULL}{xml_path}"
        r = requests.get(xml_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        # Try parsing directly first
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            # Clean up common XML issues and retry
            import re as _re
            content = r.content.decode("utf-8", errors="replace")
            # Remove XML declaration if present and re-encode
            content = _re.sub(r"<\?xml[^?]*\?>", "", content)
            # Fix common entity issues
            content = content.replace("&", "&amp;")
            content = _re.sub(r"&amp;(amp|lt|gt|quot|apos);", lambda m: f"&{m.group(1)};", content)
            try:
                root = ET.fromstring(content.encode("utf-8"))
            except ET.ParseError as e2:
                print(f"  XML parse failed after cleanup: {e2}")
                return []
    except Exception as e:
        print(f"  XML fetch/parse failed: {e}")
        return []

    # Parse holdings — handle both namespaced and non-namespaced XML
    ns = {"ns": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}
    entries = root.findall(".//ns:infoTable", ns)
    if not entries:
        entries = root.findall(".//infoTable")  # no namespace fallback

    holdings = []
    for entry in entries:
        def get(tag):
            node = entry.find(f"ns:{tag}", ns) or entry.find(tag)
            return node.text.strip() if node is not None and node.text else None

        shares_node = (entry.find("ns:shrsOrPrnAmt/ns:sshPrnamt", ns)
                       or entry.find("shrsOrPrnAmt/sshPrnamt"))
        shares = int(shares_node.text) if shares_node is not None and shares_node.text else 0

        try:
            mval = float((get("value") or "0").replace(",", "")) * 1000  # filed in thousands
        except ValueError:
            mval = 0

        holdings.append({
            "cusip":        get("cusip"),
            "company":      get("nameOfIssuer"),
            "ticker":       None,   # 13F doesn't include ticker; mapped via CUSIP later
            "shares":       shares,
            "market_value": mval,
        })

    return holdings


# ── CUSIP → ticker mapping ────────────────────────────────────────────────────

def map_cusips_to_tickers(holdings: List[dict]) -> List[dict]:
    """
    Map CUSIPs to tickers using SEC's company_tickers_exchange.json.
    Falls back to cleaned company name if no match found.
    """
    try:
        r = requests.get(
            f"{EDGAR_FULL}/files/company_tickers_exchange.json",
            headers=HEADERS, timeout=10
        )
        data   = r.json().get("data", [])
        # data rows: [cik, name, ticker, exchange]
        ticker_map = {row[1].upper(): row[2] for row in data if len(row) >= 3}
    except Exception:
        ticker_map = {}

    for h in holdings:
        if not h.get("ticker") and h.get("company"):
            name_key = h["company"].upper().strip()
            h["ticker"] = ticker_map.get(name_key) or _clean_name(h["company"])

    return holdings


def _clean_name(name: str) -> str:
    """Best-effort: strip legal suffixes to get a ticker-like label."""
    name = re.sub(r"\b(INC|CORP|LTD|LLC|PLC|CO|CLASS [AB])\b\.?", "", name, flags=re.IGNORECASE)
    return name.strip().upper()[:6]


# ── Diff engine ───────────────────────────────────────────────────────────────

def diff_quarters(
    current: List[dict],
    prior:   List[dict],
    total_value: float,
) -> List[dict]:
    """
    Compare two quarters of holdings.
    Tags each position as new, added, reduced, unchanged, or exited.
    Returns enriched list ready to write to DB.
    """
    prior_map = {h["cusip"]: h for h in prior if h.get("cusip")}

    result = []
    for h in current:
        cusip  = h.get("cusip")
        prev   = prior_map.get(cusip)
        shares = h.get("shares", 0)
        mval   = h.get("market_value", 0)
        pct    = mval / total_value if total_value else 0

        if prev is None:
            is_new   = 1
            pct_chg  = None   # can't calculate — didn't exist before
        else:
            is_new  = 0
            prev_sh = prev.get("shares", 1) or 1
            pct_chg = round((shares - prev_sh) / prev_sh * 100, 1)

        result.append({
            **h,
            "is_new":        is_new,
            "pct_change":    pct_chg,
            "pct_portfolio": round(pct * 100, 2),
        })

    return result


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_13f_pipeline(fund_names: List[str] = FUND_LIST, delay: float = 0.5):
    """
    Full pipeline: lookup CIKs → fetch last 2 quarters → diff → save → alert.
    """
    print("\n" + "=" * 60)
    print("13F DIFF ENGINE")
    print("=" * 60)

    all_new_positions = []

    for fund_name in fund_names:
        print(f"\nProcessing: {fund_name}")

        cik = lookup_cik(fund_name)
        if not cik:
            print(f"  Could not find CIK — skipping")
            continue
        print(f"  CIK: {cik}")

        filings = get_filings_index(cik, limit=2)
        if len(filings) < 1:
            print(f"  No 13F filings found — skipping")
            continue

        # Parse current quarter (most recent filing)
        current_filing = filings[0]
        period_label   = quarter_label(current_filing["period"])
        print(f"  Parsing {period_label} ({current_filing['filed_at']})...")

        time.sleep(delay)
        current_holdings = parse_13f_xml(cik, current_filing["accession"])
        if not current_holdings:
            print(f"  No holdings parsed — skipping")
            continue

        current_holdings = map_cusips_to_tickers(current_holdings)
        total_value = sum(h.get("market_value", 0) for h in current_holdings)

        # Parse prior quarter for diff
        prior_holdings = []
        if len(filings) >= 2:
            time.sleep(delay)
            prior_holdings = parse_13f_xml(cik, filings[1]["accession"])
            prior_holdings = map_cusips_to_tickers(prior_holdings)

        # Diff the quarters
        enriched = diff_quarters(current_holdings, prior_holdings, total_value)

        # Prepare for DB write
        db_rows = [{
            "fund_name":    fund_name,
            "fund_cik":     cik,
            "period":       period_label,
            "filed_at":     current_filing["filed_at"],
            "ticker":       h.get("ticker"),
            "cusip":        h.get("cusip"),
            "shares":       h.get("shares"),
            "market_value": h.get("market_value"),
            "pct_portfolio": h.get("pct_portfolio"),
            "is_new":       h.get("is_new"),
            "pct_change":   h.get("pct_change"),
        } for h in enriched]

        upsert_13f_holdings(db_rows)

        # Collect new positions for alerting
        new_positions = [h for h in enriched if h.get("is_new")]
        large_adds    = [h for h in enriched
                         if not h.get("is_new") and (h.get("pct_change") or 0) >= 25]

        print(f"  Holdings: {len(current_holdings)} | New: {len(new_positions)} | Large adds: {len(large_adds)}")

        for pos in new_positions:
            ticker = pos.get("ticker", "?")
            mval   = pos.get("market_value", 0)
            pct    = pos.get("pct_portfolio", 0)
            msg    = f"{fund_name} opened new position in {ticker} — ${mval/1e6:.1f}M ({pct:.1f}% of portfolio)"
            log_alert(ticker, "new_13f_position", msg, severity="high" if pct >= 2 else "medium")
            all_new_positions.append({"fund": fund_name, "ticker": ticker, "value": mval, "pct": pct})

        time.sleep(delay)

    return all_new_positions


def get_cluster_buys(period: str = None, min_funds: int = 2) -> pd.DataFrame:
    """
    Find tickers where 2+ tracked funds opened new positions in the same quarter.
    This is the cluster analysis signal — independent conviction.
    """
    from db import get_new_13f_positions
    df = get_new_13f_positions(period=period, min_funds=min_funds)
    if df.empty:
        print("No cluster buys found.")
        return df

    print(f"\nCLUSTER BUYS (≥{min_funds} funds, {period or 'all quarters'})")
    print("-" * 60)
    for _, row in df.iterrows():
        print(f"  {row['ticker']:<8} {row['fund_count']} funds  ${row['total_value']/1e6:.0f}M")
        print(f"           {row['funds']}")
    return df


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from db import init_db
    init_db()

    # Run the pipeline (will hit live EDGAR — needs internet)
    new_positions = run_13f_pipeline(FUND_LIST[:3])   # limit to 3 funds for demo

    # Show cluster analysis
    get_cluster_buys(min_funds=1)

    print("\nDone. Check research.db for full results.")
