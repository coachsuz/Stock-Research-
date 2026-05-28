"""
Stock Research Pipeline — Data Ingestion Module
================================================
Pulls financial data from:
  - yfinance       (free, no key needed)
  - Financial Modeling Prep (free tier: 250 calls/day)

Install:
    pip install yfinance requests pandas

Usage:
    from data_ingestion import fetch_ticker, fetch_batch, score_quality
    data = fetch_ticker("AAPL")
    scores = score_quality(data)
"""

import yfinance as yf
import requests
import pandas as pd
import json
import time
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# ─── CONFIG ──────────────────────────────────────────────────────────────────

FMP_API_KEY = "YOUR_FMP_KEY"   # free at financialmodelingprep.com
FMP_BASE    = "https://financialmodelingprep.com/api/v3"
DB_PATH     = "research.db"


# ─── DATABASE SETUP ──────────────────────────────────────────────────────────

def init_db():
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            ticker          TEXT,
            fetched_at      TEXT,
            name            TEXT,
            sector          TEXT,
            market_cap      REAL,
            revenue_growth  REAL,
            gross_margin    REAL,
            operating_margin REAL,
            fcf             REAL,
            net_income      REAL,
            fcf_conversion  REAL,
            roic            REAL,
            net_debt        REAL,
            insider_pct     REAL,
            analyst_count   INTEGER,
            trailing_pe     REAL,
            forward_pe      REAL,
            quality_score   INTEGER,
            PRIMARY KEY (ticker, fetched_at)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            ticker  TEXT,
            date    TEXT,
            close   REAL,
            volume  INTEGER,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.commit()
    conn.close()


# ─── YFINANCE FETCHER ─────────────────────────────────────────────────────────

def fetch_yfinance(ticker: str) -> dict:
    """
    Pull all quality-scoring metrics from yfinance.
    Free, no API key. Rate limit: ~2000 calls/hour.
    """
    t = yf.Ticker(ticker)
    info = t.info

    # Income statement for FCF conversion check
    cf = t.cashflow  # DataFrame: rows = line items, cols = dates
    inc = t.income_stmt

    # Calculate FCF conversion: FCF / Net Income
    fcf_conversion = None
    try:
        fcf       = cf.loc["Free Cash Flow"].iloc[0]          if "Free Cash Flow" in cf.index else None
        net_income = inc.loc["Net Income"].iloc[0]            if "Net Income"     in inc.index else None
        if fcf and net_income and net_income != 0:
            fcf_conversion = round(fcf / net_income, 2)
    except Exception:
        pass

    # Net debt = total debt - cash
    total_debt = info.get("totalDebt", 0) or 0
    total_cash = info.get("totalCash", 0) or 0
    net_debt   = total_debt - total_cash

    return {
        "ticker":           ticker.upper(),
        "source":           "yfinance",
        "fetched_at":       datetime.utcnow().isoformat(),
        "name":             info.get("longName"),
        "sector":           info.get("sector"),
        "industry":         info.get("industry"),
        "market_cap":       info.get("marketCap"),
        "enterprise_value": info.get("enterpriseValue"),

        # Growth
        "revenue_growth":   info.get("revenueGrowth"),         # YoY %
        "earnings_growth":  info.get("earningsGrowth"),

        # Margins
        "gross_margin":     info.get("grossMargins"),
        "operating_margin": info.get("operatingMargins"),
        "profit_margin":    info.get("profitMargins"),

        # Cash flow
        "fcf":              info.get("freeCashflow"),
        "net_income":       info.get("netIncomeToCommon"),
        "fcf_conversion":   fcf_conversion,

        # Returns
        "roe":              info.get("returnOnEquity"),
        "roa":              info.get("returnOnAssets"),

        # Balance sheet
        "total_debt":       total_debt,
        "total_cash":       total_cash,
        "net_debt":         net_debt,
        "debt_to_equity":   info.get("debtToEquity"),
        "current_ratio":    info.get("currentRatio"),

        # Ownership & coverage
        "insider_pct":      info.get("heldPercentInsiders"),
        "analyst_count":    info.get("numberOfAnalystOpinions"),

        # Valuation
        "trailing_pe":      info.get("trailingPE"),
        "forward_pe":       info.get("forwardPE"),
        "ps_ratio":         info.get("priceToSalesTrailing12Months"),
        "pb_ratio":         info.get("priceToBook"),
        "peg_ratio":        info.get("pegRatio"),
        "ev_ebitda":        info.get("enterpriseToEbitda"),
    }


def fetch_price_history(ticker: str, period: str = "2y") -> pd.DataFrame:
    """
    Pull OHLCV price history.
    period options: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
    """
    t = yf.Ticker(ticker)
    df = t.history(period=period)
    df = df[["Close", "Volume"]].reset_index()
    df.columns = ["date", "close", "volume"]
    df["ticker"] = ticker.upper()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


# ─── FINANCIAL MODELING PREP FETCHER ─────────────────────────────────────────
# FMP gives cleaner structured data and covers ROIC which yfinance misses.
# Free tier: 250 calls/day. Paid starts at $19/mo for unlimited.

def _fmp_get(endpoint: str, params: dict = {}) -> Optional[dict]:
    """Generic FMP API call with error handling."""
    params["apikey"] = FMP_API_KEY
    try:
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data) and "Error Message" in data:
            print(f"FMP error for {endpoint}: {data['Error Message']}")
            return None
        return data
    except requests.RequestException as e:
        print(f"FMP request failed: {e}")
        return None


def fetch_fmp_ratios(ticker: str) -> dict:
    """
    Pull key ratios from FMP — especially ROIC which yfinance doesn't expose.
    Endpoint: /ratios/{ticker}?limit=1
    """
    data = _fmp_get(f"ratios/{ticker}", {"limit": 1})
    if not data or not isinstance(data, list):
        return {}

    r = data[0]
    return {
        "roic":                 r.get("returnOnCapitalEmployed"),  # proxy for ROIC
        "gross_margin_fmp":     r.get("grossProfitMargin"),
        "operating_margin_fmp": r.get("operatingProfitMargin"),
        "fcf_margin":           r.get("freeCashFlowPerShare"),     # use with price
        "current_ratio_fmp":    r.get("currentRatio"),
        "debt_equity_fmp":      r.get("debtEquityRatio"),
        "dividend_yield":       r.get("dividendYield"),
        "pe_ratio_fmp":         r.get("priceEarningsRatio"),
        "pb_ratio_fmp":         r.get("priceToBookRatio"),
        "ev_ebitda_fmp":        r.get("enterpriseValueMultiple"),
    }


def fetch_fmp_income(ticker: str, years: int = 3) -> List[dict]:
    """
    Pull annual income statements — revenue growth trend over multiple years.
    Endpoint: /income-statement/{ticker}?limit=N
    """
    data = _fmp_get(f"income-statement/{ticker}", {"limit": years})
    if not data:
        return []

    results = []
    for row in data:
        results.append({
            "date":             row.get("date"),
            "revenue":          row.get("revenue"),
            "gross_profit":     row.get("grossProfit"),
            "operating_income": row.get("operatingIncome"),
            "net_income":       row.get("netIncome"),
            "eps":              row.get("eps"),
            "gross_margin":     row.get("grossProfitRatio"),
            "operating_margin": row.get("operatingIncomeRatio"),
            "net_margin":       row.get("netIncomeRatio"),
        })
    return results


def fetch_fmp_cashflow(ticker: str, years: int = 3) -> List[dict]:
    """
    Pull cash flow statements — FCF, capex, buybacks.
    Endpoint: /cash-flow-statement/{ticker}?limit=N
    """
    data = _fmp_get(f"cash-flow-statement/{ticker}", {"limit": years})
    if not data:
        return []

    results = []
    for row in data:
        op_cf  = row.get("operatingCashFlow", 0) or 0
        capex  = row.get("capitalExpenditure", 0) or 0
        net_inc = row.get("netIncome", 1) or 1
        fcf    = op_cf + capex  # capex is negative in FMP
        results.append({
            "date":              row.get("date"),
            "operating_cf":      op_cf,
            "capex":             capex,
            "fcf":               fcf,
            "fcf_conversion":    round(fcf / net_inc, 2) if net_inc else None,
            "buybacks":          row.get("commonStockRepurchased"),
            "dividends_paid":    row.get("dividendsPaid"),
        })
    return results


def fetch_fmp_insider(ticker: str) -> List[dict]:
    """
    Insider transaction history from FMP.
    Endpoint: /insider-trading?symbol={ticker}&limit=20
    """
    data = _fmp_get("insider-trading", {"symbol": ticker, "limit": 20})
    if not data:
        return []

    return [{
        "date":            row.get("transactionDate"),
        "name":            row.get("reportingName"),
        "title":           row.get("typeOfOwner"),
        "transaction":     row.get("transactionType"),
        "shares":          row.get("securitiesTransacted"),
        "price":           row.get("price"),
        "value":           (row.get("securitiesTransacted") or 0) * (row.get("price") or 0),
    } for row in data]


# ─── COMBINED FETCH ───────────────────────────────────────────────────────────

def fetch_ticker(ticker: str, use_fmp: bool = True) -> dict:
    """
    Master fetch function. Combines yfinance + FMP for a complete picture.
    Falls back gracefully if either source fails.
    """
    print(f"Fetching {ticker}...")
    result = {"ticker": ticker.upper()}

    # Layer 1: yfinance (always)
    try:
        yf_data = fetch_yfinance(ticker)
        result.update(yf_data)
    except Exception as e:
        print(f"  yfinance failed: {e}")

    # Layer 2: FMP ratios (if key provided)
    if use_fmp and FMP_API_KEY != "YOUR_FMP_KEY":
        try:
            fmp_ratios = fetch_fmp_ratios(ticker)
            # FMP fills ROIC gap from yfinance
            result.update(fmp_ratios)

            fmp_cf = fetch_fmp_cashflow(ticker)
            if fmp_cf:
                result["fcf_conversion_fmp"] = fmp_cf[0].get("fcf_conversion")
                result["fcf_fmp"]            = fmp_cf[0].get("fcf")

            result["insider_transactions"] = fetch_fmp_insider(ticker)
            result["income_history"]       = fetch_fmp_income(ticker)
            time.sleep(0.25)  # stay under FMP rate limit
        except Exception as e:
            print(f"  FMP failed: {e}")

    return result


def fetch_batch(tickers: List[str], delay: float = 0.5) -> List[dict]:
    """
    Fetch a list of tickers with a delay between calls.
    yfinance handles ~2000 calls/hour. FMP free: 250/day total.
    """
    results = []
    for i, ticker in enumerate(tickers):
        data = fetch_ticker(ticker)
        results.append(data)
        if i < len(tickers) - 1:
            time.sleep(delay)
    return results


# ─── QUALITY SCORER ───────────────────────────────────────────────────────────

QUALITY_METRICS = [
    # (metric_key, threshold_fn, label)
    ("revenue_growth",   lambda v: v is not None and v >= 0.15,  "Revenue growth ≥15% YoY"),
    ("gross_margin",     lambda v: v is not None and v >= 0.30,  "Gross margin ≥30%"),
    ("fcf_conversion",   lambda v: v is not None and v >= 0.80,  "FCF conversion ≥80%"),
    ("roic",             lambda v: v is not None and v >= 0.15,  "ROIC ≥15%"),
    ("net_debt",         lambda v: v is not None and v <= 0,     "Net cash (no net debt)"),
    ("insider_pct",      lambda v: v is not None and v >= 0.10,  "Insider ownership ≥10%"),
    ("analyst_count",    lambda v: v is not None and v <= 10,    "Low analyst coverage ≤10"),
    ("revenue_growth",   lambda v: v is not None and v >= 0.10,  "Growth + profitability combo"),
    ("operating_margin", lambda v: v is not None and v >= 0.15,  "Operating margin ≥15%"),
    ("debt_to_equity",   lambda v: v is None or v <= 2.0,        "Debt/equity ≤2x"),
]


def score_quality(data: dict) -> dict:
    """
    Score a ticker against the 10-metric quality checklist.
    Returns score (0-10) and per-metric pass/fail breakdown.
    """
    passed  = []
    failed  = []
    total   = 0

    for key, threshold_fn, label in QUALITY_METRICS:
        value = data.get(key)
        try:
            passes = threshold_fn(value)
        except Exception:
            passes = False

        if passes:
            passed.append(label)
            total += 1
        else:
            failed.append({"metric": label, "value": value})

    verdict = (
        "Strong — dig into valuation"  if total >= 8 else
        "Decent — understand the gaps" if total >= 6 else
        "Mixed — quality risks present" if total >= 4 else
        "Weak — proceed with caution"
    )

    return {
        "ticker":  data.get("ticker"),
        "name":    data.get("name"),
        "score":   total,
        "max":     len(QUALITY_METRICS),
        "verdict": verdict,
        "passed":  passed,
        "failed":  failed,
    }


# ─── PERSISTENCE ─────────────────────────────────────────────────────────────

def save_to_db(data: dict, score: dict):
    """Save fetched data and quality score to SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO fundamentals VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """, (
        data.get("ticker"),
        data.get("fetched_at"),
        data.get("name"),
        data.get("sector"),
        data.get("market_cap"),
        data.get("revenue_growth"),
        data.get("gross_margin"),
        data.get("operating_margin"),
        data.get("fcf"),
        data.get("net_income"),
        data.get("fcf_conversion"),
        data.get("roic"),
        data.get("net_debt"),
        data.get("insider_pct"),
        data.get("analyst_count"),
        data.get("trailing_pe"),
        data.get("forward_pe"),
        score.get("score"),
    ))
    conn.commit()
    conn.close()


def load_watchlist(tickers: List[str]) -> pd.DataFrame:
    """Load latest scores for a list of tickers from the DB."""
    conn  = sqlite3.connect(DB_PATH)
    query = f"""
        SELECT ticker, name, sector, revenue_growth, gross_margin,
               fcf_conversion, roic, insider_pct, quality_score, fetched_at
        FROM fundamentals
        WHERE ticker IN ({','.join('?' for _ in tickers)})
        AND fetched_at = (
            SELECT MAX(fetched_at) FROM fundamentals f2
            WHERE f2.ticker = fundamentals.ticker
        )
        ORDER BY quality_score DESC
    """
    df = pd.read_sql_query(query, conn, params=tickers)
    conn.close()
    return df


# ─── MAIN — example run ──────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    WATCHLIST = ["AAPL", "MSFT", "CRWD", "AXON", "NVO"]

    print("=" * 60)
    print("STOCK RESEARCH PIPELINE — DATA INGESTION")
    print("=" * 60)

    all_scores = []

    for ticker in WATCHLIST:
        data  = fetch_ticker(ticker, use_fmp=True)
        score = score_quality(data)
        save_to_db(data, score)
        all_scores.append(score)

        print(f"\n{score['ticker']} — {score['name']}")
        print(f"  Score:   {score['score']}/{score['max']} — {score['verdict']}")
        print(f"  Passed:  {', '.join(score['passed'][:3])}{'...' if len(score['passed']) > 3 else ''}")
        if score["failed"]:
            print(f"  Gaps:    {score['failed'][0]['metric']}, ...")

        time.sleep(0.5)

    # Print ranked summary
    print("\n" + "=" * 60)
    print("RANKED BY QUALITY SCORE")
    print("=" * 60)
    ranked = sorted(all_scores, key=lambda x: x["score"], reverse=True)
    for r in ranked:
        bar = "█" * r["score"] + "░" * (r["max"] - r["score"])
        print(f"  {r['ticker']:<6} {bar}  {r['score']}/{r['max']}  {r['verdict']}")
