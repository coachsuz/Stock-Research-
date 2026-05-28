from typing import Optional, List, Dict
"""
Stock Research Pipeline — Database Layer
========================================
Single source of truth for all pipeline data.
Uses SQLite locally; swap DB_PATH for a Supabase connection string to go hosted.

Tables:
  fundamentals        — quality metrics per ticker per fetch date
  price_history       — daily OHLCV
  filings_13f         — parsed 13F holdings per fund per quarter
  insider_transactions — insider buy/sell events
  ai_summaries        — Claude-generated summaries and flags
  thesis              — variant perception thesis + leading indicators
  alerts              — triggered alert log
"""

import sqlite3
import pandas as pd
from datetime import datetime
from contextlib import contextmanager

DB_PATH = "research.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables and indexes. Safe to call repeatedly (IF NOT EXISTS)."""
    with get_conn() as conn:

        # ── Fundamentals ─────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fundamentals (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker           TEXT NOT NULL,
                fetched_at       TEXT NOT NULL,
                name             TEXT,
                sector           TEXT,
                industry         TEXT,
                market_cap       REAL,
                revenue_growth   REAL,
                gross_margin     REAL,
                operating_margin REAL,
                profit_margin    REAL,
                fcf              REAL,
                net_income       REAL,
                fcf_conversion   REAL,
                roic             REAL,
                roe              REAL,
                net_debt         REAL,
                debt_to_equity   REAL,
                insider_pct      REAL,
                analyst_count    INTEGER,
                trailing_pe      REAL,
                forward_pe       REAL,
                ev_ebitda        REAL,
                quality_score    INTEGER,
                quality_verdict  TEXT,
                quality_passed   TEXT,    -- JSON list
                quality_failed   TEXT,    -- JSON list
                UNIQUE(ticker, fetched_at)
            )
        """)

        # ── Price history ─────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                ticker  TEXT NOT NULL,
                date    TEXT NOT NULL,
                open    REAL,
                high    REAL,
                low     REAL,
                close   REAL NOT NULL,
                volume  INTEGER,
                PRIMARY KEY (ticker, date)
            )
        """)

        # ── 13F holdings ─────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS filings_13f (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_name     TEXT NOT NULL,
                fund_cik      TEXT NOT NULL,
                period        TEXT NOT NULL,    -- e.g. "2025-Q1"
                filed_at      TEXT,
                ticker        TEXT NOT NULL,
                cusip         TEXT,
                shares        INTEGER,
                market_value  REAL,
                pct_portfolio REAL,
                is_new        INTEGER DEFAULT 0,  -- 1 if new position this quarter
                pct_change    REAL,               -- % change in shares vs prior quarter
                UNIQUE(fund_cik, period, cusip)
            )
        """)

        # ── Insider transactions ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS insider_transactions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker           TEXT NOT NULL,
                fetched_at       TEXT NOT NULL,
                transaction_date TEXT,
                filer_name       TEXT,
                filer_title      TEXT,
                transaction_type TEXT,   -- "P-Purchase", "S-Sale", etc.
                shares           REAL,
                price            REAL,
                value            REAL,
                UNIQUE(ticker, transaction_date, filer_name, shares)
            )
        """)

        # ── AI summaries ──────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_summaries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker       TEXT NOT NULL,
                summary_type TEXT NOT NULL,  -- '10k', 'earnings', 'thesis'
                period       TEXT,           -- e.g. "2024-Q4", "FY2024"
                generated_at TEXT NOT NULL,
                model        TEXT,
                summary      TEXT,
                green_flags  TEXT,           -- JSON list
                red_flags    TEXT,           -- JSON list
                sentiment    TEXT,           -- 'positive', 'neutral', 'negative'
                raw_response TEXT
            )
        """)

        # ── Thesis tracker ────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thesis (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker              TEXT NOT NULL UNIQUE,
                created_at          TEXT NOT NULL,
                updated_at          TEXT,
                thesis_text         TEXT,        -- variant perception statement
                indicator_1         TEXT,        -- leading indicator to track
                indicator_2         TEXT,
                indicator_3         TEXT,
                indicator_1_status  TEXT,        -- 'on_track', 'at_risk', 'broken'
                indicator_2_status  TEXT,
                indicator_3_status  TEXT,
                conviction          TEXT,        -- 'high', 'medium', 'low'
                status              TEXT DEFAULT 'active'  -- 'active', 'exited', 'broken'
            )
        """)

        # ── Alert log ─────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                triggered_at TEXT NOT NULL,
                ticker       TEXT,
                alert_type   TEXT NOT NULL,  -- 'quality_threshold', 'new_13f_position', 'insider_buy', 'earnings_flag'
                severity     TEXT,           -- 'high', 'medium', 'low'
                message      TEXT,
                sent         INTEGER DEFAULT 0
            )
        """)

        # ── Indexes ───────────────────────────────────────────────────────────
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_fund_ticker ON filings_13f(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_fund_period ON filings_13f(fund_cik, period)",
            "CREATE INDEX IF NOT EXISTS idx_fund_new    ON filings_13f(is_new, period)",
            "CREATE INDEX IF NOT EXISTS idx_insider_ticker ON insider_transactions(ticker)",
            "CREATE INDEX IF NOT EXISTS idx_summary_ticker ON ai_summaries(ticker, summary_type)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent, triggered_at)",
            "CREATE INDEX IF NOT EXISTS idx_fund_score ON fundamentals(quality_score DESC)",
        ]
        for idx in indexes:
            conn.execute(idx)

    print(f"Database initialised at: {DB_PATH}")


# ── Write helpers ─────────────────────────────────────────────────────────────

def upsert_fundamentals(data: dict, score: dict):
    import json
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO fundamentals (
                ticker, fetched_at, name, sector, industry, market_cap,
                revenue_growth, gross_margin, operating_margin, profit_margin,
                fcf, net_income, fcf_conversion, roic, roe, net_debt,
                debt_to_equity, insider_pct, analyst_count,
                trailing_pe, forward_pe, ev_ebitda,
                quality_score, quality_verdict, quality_passed, quality_failed
            ) VALUES (
                :ticker, :fetched_at, :name, :sector, :industry, :market_cap,
                :revenue_growth, :gross_margin, :operating_margin, :profit_margin,
                :fcf, :net_income, :fcf_conversion, :roic, :roe, :net_debt,
                :debt_to_equity, :insider_pct, :analyst_count,
                :trailing_pe, :forward_pe, :ev_ebitda,
                :quality_score, :quality_verdict, :quality_passed, :quality_failed
            )
            ON CONFLICT(ticker, fetched_at) DO UPDATE SET
                quality_score   = excluded.quality_score,
                quality_verdict = excluded.quality_verdict
        """, {
            "ticker":           data.get("ticker"),
            "fetched_at":       data.get("fetched_at"),
            "name":             data.get("name"),
            "sector":           data.get("sector"),
            "industry":         data.get("industry"),
            "market_cap":       data.get("market_cap"),
            "revenue_growth":   data.get("revenue_growth"),
            "gross_margin":     data.get("gross_margin"),
            "operating_margin": data.get("operating_margin"),
            "profit_margin":    data.get("profit_margin"),
            "fcf":              data.get("fcf"),
            "net_income":       data.get("net_income"),
            "fcf_conversion":   data.get("fcf_conversion"),
            "roic":             data.get("roic"),
            "roe":              data.get("roe"),
            "net_debt":         data.get("net_debt"),
            "debt_to_equity":   data.get("debt_to_equity"),
            "insider_pct":      data.get("insider_pct"),
            "analyst_count":    data.get("analyst_count"),
            "trailing_pe":      data.get("trailing_pe"),
            "forward_pe":       data.get("forward_pe"),
            "ev_ebitda":        data.get("ev_ebitda"),
            "quality_score":    score["score"],
            "quality_verdict":  score["verdict"],
            "quality_passed":   json.dumps(score.get("passed", [])),
            "quality_failed":   json.dumps([f["metric"] for f in score.get("failed", [])]),
        })


def upsert_13f_holdings(holdings: List[dict]):
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO filings_13f (
                fund_name, fund_cik, period, filed_at,
                ticker, cusip, shares, market_value, pct_portfolio,
                is_new, pct_change
            ) VALUES (
                :fund_name, :fund_cik, :period, :filed_at,
                :ticker, :cusip, :shares, :market_value, :pct_portfolio,
                :is_new, :pct_change
            )
            ON CONFLICT(fund_cik, period, cusip) DO UPDATE SET
                shares        = excluded.shares,
                market_value  = excluded.market_value,
                pct_portfolio = excluded.pct_portfolio,
                is_new        = excluded.is_new,
                pct_change    = excluded.pct_change
        """, holdings)


def upsert_insider(transactions: List[dict], ticker: str):
    with get_conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO insider_transactions (
                ticker, fetched_at, transaction_date, filer_name,
                filer_title, transaction_type, shares, price, value
            ) VALUES (
                :ticker, :fetched_at, :transaction_date, :filer_name,
                :filer_title, :transaction_type, :shares, :price, :value
            )
        """, [{**t, "ticker": ticker, "fetched_at": datetime.utcnow().isoformat()} for t in transactions])


def save_ai_summary(ticker: str, summary_type: str, period: str, result: dict):
    import json
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ai_summaries (
                ticker, summary_type, period, generated_at, model,
                summary, green_flags, red_flags, sentiment, raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ticker, summary_type, period,
            datetime.utcnow().isoformat(),
            result.get("model", "claude-sonnet-4-20250514"),
            result.get("summary"),
            json.dumps(result.get("green_flags", [])),
            json.dumps(result.get("red_flags", [])),
            result.get("sentiment"),
            result.get("raw_response"),
        ))


def log_alert(ticker: str, alert_type: str, message: str, severity: str = "medium"):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO alerts (triggered_at, ticker, alert_type, severity, message)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), ticker, alert_type, severity, message))


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_watchlist_scores(tickers: List[str] = None) -> pd.DataFrame:
    """Latest quality score for each ticker, ranked."""
    with get_conn() as conn:
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            query = f"""
                SELECT f.ticker, f.name, f.sector, f.quality_score, f.quality_verdict,
                       f.revenue_growth, f.gross_margin, f.fcf_conversion,
                       f.roic, f.insider_pct, f.analyst_count, f.fetched_at
                FROM fundamentals f
                INNER JOIN (
                    SELECT ticker, MAX(fetched_at) AS latest
                    FROM fundamentals GROUP BY ticker
                ) latest ON f.ticker = latest.ticker AND f.fetched_at = latest.latest
                WHERE f.ticker IN ({placeholders})
                ORDER BY f.quality_score DESC
            """
            return pd.read_sql_query(query, conn, params=tickers)
        else:
            return pd.read_sql_query("""
                SELECT f.ticker, f.name, f.sector, f.quality_score, f.quality_verdict,
                       f.revenue_growth, f.gross_margin, f.fcf_conversion,
                       f.roic, f.insider_pct, f.analyst_count, f.fetched_at
                FROM fundamentals f
                INNER JOIN (
                    SELECT ticker, MAX(fetched_at) AS latest
                    FROM fundamentals GROUP BY ticker
                ) latest ON f.ticker = latest.ticker AND f.fetched_at = latest.latest
                ORDER BY f.quality_score DESC
            """, conn)


def get_new_13f_positions(period: str = None, min_funds: int = 1) -> pd.DataFrame:
    """New 13F positions this quarter, optionally filtered by cluster size."""
    with get_conn() as conn:
        query = """
            SELECT ticker,
                   COUNT(DISTINCT fund_cik) AS fund_count,
                   GROUP_CONCAT(fund_name, ', ') AS funds,
                   SUM(market_value) AS total_value,
                   period
            FROM filings_13f
            WHERE is_new = 1
              AND (:period IS NULL OR period = :period)
            GROUP BY ticker, period
            HAVING fund_count >= :min_funds
            ORDER BY fund_count DESC, total_value DESC
        """
        return pd.read_sql_query(query, conn,
                                 params={"period": period, "min_funds": min_funds})


def get_recent_insider_buys(days: int = 90) -> pd.DataFrame:
    """Insider purchase transactions in the last N days."""
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT ticker, filer_name, filer_title, transaction_date,
                   shares, price, value
            FROM insider_transactions
            WHERE transaction_type LIKE '%Purchase%'
              AND transaction_date >= date('now', :offset)
            ORDER BY value DESC
        """, conn, params={"offset": f"-{days} days"})


def get_latest_summary(ticker: str, summary_type: str) -> Optional[dict]:
    """Fetch the most recent AI summary for a ticker."""
    import json
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM ai_summaries
            WHERE ticker = ? AND summary_type = ?
            ORDER BY generated_at DESC LIMIT 1
        """, (ticker, summary_type)).fetchone()
        if not row:
            return None
        result = dict(row)
        for field in ("green_flags", "red_flags"):
            try:
                result[field] = json.loads(result[field] or "[]")
            except Exception:
                result[field] = []
        return result


def get_unsent_alerts() -> List[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM alerts WHERE sent = 0
            ORDER BY triggered_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def mark_alerts_sent(ids: List[int]):
    with get_conn() as conn:
        conn.execute(f"""
            UPDATE alerts SET sent = 1
            WHERE id IN ({','.join('?' * len(ids))})
        """, ids)


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    # Seed a sample record to verify everything works
    sample_data = {
        "ticker": "DEMO", "fetched_at": datetime.utcnow().isoformat(),
        "name": "Demo Corp", "sector": "Technology", "industry": "Software",
        "market_cap": 5e9, "revenue_growth": 0.22, "gross_margin": 0.68,
        "operating_margin": 0.18, "profit_margin": 0.14,
        "fcf": 400e6, "net_income": 350e6, "fcf_conversion": 1.14,
        "roic": 0.19, "roe": 0.24, "net_debt": -200e6,
        "debt_to_equity": 0.3, "insider_pct": 0.15, "analyst_count": 6,
        "trailing_pe": 28, "forward_pe": 22, "ev_ebitda": 18,
    }
    sample_score = {
        "score": 8, "verdict": "Strong — dig into valuation",
        "passed": ["Revenue growth ≥15%", "ROIC ≥15%", "Net cash"],
        "failed": [{"metric": "Low analyst coverage", "value": 6}],
    }
    upsert_fundamentals(sample_data, sample_score)
    log_alert("DEMO", "quality_threshold", "DEMO crossed quality score 8", "high")

    df = get_watchlist_scores()
    print("\nWatchlist (from DB):")
    print(df.to_string(index=False))

    alerts = get_unsent_alerts()
    print(f"\nUnsent alerts: {len(alerts)}")
    for a in alerts:
        print(f"  [{a['severity'].upper()}] {a['ticker']} — {a['message']}")

    print("\nAll tables created successfully.")
