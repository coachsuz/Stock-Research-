"""
Stock Research Pipeline — Streamlit Dashboard
=============================================
Run with:
    streamlit run dashboard.py

Reads entirely from research.db — no live API calls at render time.
Refresh data by running: python scheduler.py --now
"""

import json
import db_adapter as sqlite3
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
from datetime import datetime

DB_PATH = "research.db"

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Stock Research OS",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  .flag-green { background:#E1F5EE; border-left:3px solid #1D9E75;
                padding:6px 10px; margin:4px 0; border-radius:0 6px 6px 0; font-size:13px; }
  .flag-red   { background:#FAECE7; border-left:3px solid #D85A30;
                padding:6px 10px; margin:4px 0; border-radius:0 6px 6px 0; font-size:13px; }
  [data-testid="stSidebar"] { background:#fafaf8; }
</style>
""", unsafe_allow_html=True)


# ── DB helpers ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_watchlist():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT f.ticker, f.name, f.sector, f.quality_score, f.quality_verdict,
               f.revenue_growth, f.gross_margin, f.operating_margin,
               f.fcf_conversion, f.roic, f.net_debt, f.insider_pct,
               f.analyst_count, f.trailing_pe, f.forward_pe,
               f.quality_passed, f.quality_failed, f.fetched_at
        FROM fundamentals f
        INNER JOIN (
            SELECT ticker, MAX(fetched_at) AS latest
            FROM fundamentals GROUP BY ticker
        ) l ON f.ticker = l.ticker AND f.fetched_at = l.latest
        ORDER BY f.quality_score DESC
    """, conn)
    conn.close()
    return df

@st.cache_data(ttl=300)
def load_cluster_buys(period=None, min_funds=2):
    conn = sqlite3.connect(DB_PATH)
    is_postgres = hasattr(conn, "_conn")  # our adapter wraps postgres conns

    concat_fn = "STRING_AGG(fund_name, ' · ')" if is_postgres else "GROUP_CONCAT(fund_name, ' · ')"

    if period:
        query = f"""
            SELECT ticker,
                   COUNT(DISTINCT fund_cik) AS fund_count,
                   {concat_fn} AS funds,
                   SUM(market_value) / 1e6 AS total_value_m,
                   period
            FROM filings_13f
            WHERE is_new = 1 AND period = ?
            GROUP BY ticker, period
            HAVING COUNT(DISTINCT fund_cik) >= ?
            ORDER BY fund_count DESC, total_value_m DESC
        """
        params = [period, min_funds]
    else:
        query = f"""
            SELECT ticker,
                   COUNT(DISTINCT fund_cik) AS fund_count,
                   {concat_fn} AS funds,
                   SUM(market_value) / 1e6 AS total_value_m,
                   period
            FROM filings_13f
            WHERE is_new = 1
            GROUP BY ticker, period
            HAVING COUNT(DISTINCT fund_cik) >= ?
            ORDER BY fund_count DESC, total_value_m DESC
        """
        params = [min_funds]

    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

@st.cache_data(ttl=300)
def load_insider_buys(days=90):
    from datetime import datetime, timedelta
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT ticker, filer_name, filer_title, transaction_date,
               shares, price, value / 1e6 AS value_m
        FROM insider_transactions
        WHERE (transaction_type LIKE '%Purchase%' OR transaction_type LIKE '%Buy%' OR shares > 0)
          AND transaction_date >= ?
        ORDER BY value_m DESC
    """, conn, params=[cutoff_date])
    conn.close()
    return df

@st.cache_data(ttl=300)
def load_alerts(limit=100):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT triggered_at, ticker, alert_type, severity, message, sent
        FROM alerts ORDER BY triggered_at DESC LIMIT ?
    """, conn, params=[limit])
    conn.close()
    return df

@st.cache_data(ttl=300)
def load_ai_summary(ticker, summary_type):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("""
        SELECT summary, green_flags, red_flags, sentiment, generated_at, raw_response
        FROM ai_summaries
        WHERE ticker = ? AND summary_type = ?
        ORDER BY generated_at DESC LIMIT 1
    """, (ticker, summary_type)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "summary":      row[0],
        "green_flags":  json.loads(row[1] or "[]"),
        "red_flags":    json.loads(row[2] or "[]"),
        "sentiment":    row[3],
        "generated_at": row[4],
        "raw":          json.loads(row[5] or "{}"),
    }

@st.cache_data(ttl=300)
def load_price_history(ticker, days=365):
    from datetime import datetime, timedelta
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT date, close FROM price_history
        WHERE ticker = ? AND date >= ?
        ORDER BY date ASC
    """, conn, params=[ticker, cutoff_date])
    conn.close()
    return df


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_pct(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v * 100:.1f}%"

def score_color(s):
    if s >= 8: return "#1D9E75"
    if s >= 6: return "#639922"
    if s >= 4: return "#BA7517"
    return "#D85A30"

def sev_icon(s):
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(s, "⚪")

def alert_label(t):
    return {"new_13f_position": "13F New Position", "quality_threshold": "Quality Alert",
            "insider_buy": "Insider Buy", "earnings_flag": "Earnings Flag"}.get(t, t)


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 📈 Research OS")
    st.markdown("---")
    page = st.radio("Navigate", [
        "🏠 Watchlist", "🔍 Stock deep dive",
        "💼 My portfolio",
        "🐋 13F tracker", "👥 Insider activity",
        "🤖 AI summaries", "🔔 Alerts",
        "📅 Earnings calendar", "⚡ Action engine",
        "📋 Thesis scorecard", "📈 Hit rate analyzer",
        "💰 Valuation model", "⚡ Relative strength",
        "🔬 Quality checks", "🎙 Earnings analysis",
    ])
    st.markdown("---")
    st.markdown("**Filters**")
    min_score = st.slider("Min quality score", 0, 10, 0)
    sectors   = st.multiselect("Sectors", [
        "Technology", "Healthcare", "Industrials",
        "Consumer", "Financials", "Energy"
    ])
    st.markdown("---")
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}")


# ── Watchlist ─────────────────────────────────────────────────────────────────

if page == "🏠 Watchlist":
    st.title("Watchlist")
    df = load_watchlist()

    if df.empty:
        st.info("No stocks yet. Run `python scheduler.py --now` to populate.")
        st.stop()

    if min_score > 0:
        df = df[df["quality_score"] >= min_score]
    if sectors:
        df = df[df["sector"].isin(sectors)]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Stocks tracked", len(df))
    c2.metric("Avg quality score", f"{df['quality_score'].mean():.1f} / 10")
    c3.metric("High conviction (≥8)", len(df[df["quality_score"] >= 8]))
    alerts_df = load_alerts(50)
    c4.metric("Unread alerts", len(alerts_df[alerts_df["sent"] == 0]))

    st.markdown("---")

    fig = go.Figure(go.Bar(
        x=df["ticker"], y=df["quality_score"],
        marker_color=[score_color(s) for s in df["quality_score"]],
        text=df["quality_score"], textposition="outside",
        hovertemplate="<b>%{x}</b><br>Score: %{y}/10<extra></extra>",
    ))
    fig.update_layout(
        yaxis=dict(range=[0, 11], title="Quality score"),
        height=260, margin=dict(t=10, b=10, l=10, r=10),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="#f0f0ee")
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    for _, row in df.iterrows():
        sc = int(row["quality_score"])
        with st.expander(
            f"**{row['ticker']}** — {row.get('name','?')}  |  "
            f"Score {sc}/10  |  {row.get('sector','')}"
        ):
            c1, c2, c3, c4 = st.columns(4)
            c1.markdown(f"**Rev growth**<br>{fmt_pct(row.get('revenue_growth'))}", unsafe_allow_html=True)
            c2.markdown(f"**Gross margin**<br>{fmt_pct(row.get('gross_margin'))}", unsafe_allow_html=True)
            c3.markdown(f"**FCF conversion**<br>{fmt_pct(row.get('fcf_conversion'))}", unsafe_allow_html=True)
            c4.markdown(f"**ROIC**<br>{fmt_pct(row.get('roic'))}", unsafe_allow_html=True)

            passed = json.loads(row.get("quality_passed") or "[]")
            failed = json.loads(row.get("quality_failed") or "[]")
            if passed:
                st.markdown("**Passed:** " + "  ·  ".join([f"✅ {p}" for p in passed]))
            if failed:
                st.markdown("**Gaps:** "   + "  ·  ".join([f"⚠️ {f}" for f in failed]))
            st.caption(f"Fetched: {str(row.get('fetched_at',''))[:10]}")


# ── Stock deep dive ───────────────────────────────────────────────────────────

elif page == "🔍 Stock deep dive":
    st.title("Stock deep dive")
    df = load_watchlist()
    if df.empty:
        st.info("No stocks yet.")
        st.stop()

    ticker = st.selectbox("Select ticker", df["ticker"].tolist())
    row    = df[df["ticker"] == ticker].iloc[0]
    sc     = int(row["quality_score"])
    col    = score_color(sc)

    st.markdown(f"### {ticker} — {row.get('name','')}")
    st.markdown(
        f'<span style="background:{col}22;border:1.5px solid {col};border-radius:20px;'
        f'padding:4px 14px;color:{col};font-weight:600;font-size:14px;">'
        f'Score {sc}/10 — {row.get("quality_verdict","")}</span>',
        unsafe_allow_html=True
    )
    st.markdown("")

    tab1, tab2, tab3, tab4 = st.tabs(["📊 Financials", "💡 AI thesis", "🎙 Earnings", "📈 Price"])

    with tab1:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Growth & profitability**")
            for label, key in [("Revenue growth", "revenue_growth"), ("Gross margin", "gross_margin"),
                                ("Operating margin", "operating_margin"), ("FCF conversion", "fcf_conversion"),
                                ("ROIC", "roic")]:
                st.markdown(f"`{fmt_pct(row.get(key))}` {label}")
        with c2:
            st.markdown("**Balance sheet & ownership**")
            nd = row.get("net_debt")
            nd_str = (f"${abs(nd)/1e9:.2f}B {'cash' if nd < 0 else 'debt'}"
                      if nd is not None and not pd.isna(nd) else "—")
            for label, val in [
                ("Net debt / cash",  nd_str),
                ("Debt / equity",    f"{row['debt_to_equity']:.2f}x" if row.get("debt_to_equity") else "—"),
                ("Insider ownership", fmt_pct(row.get("insider_pct"))),
                ("Analyst coverage", f"{int(row['analyst_count'])} analysts" if row.get("analyst_count") else "—"),
                ("Trailing P/E",     f"{row['trailing_pe']:.1f}x" if row.get("trailing_pe") else "—"),
            ]:
                st.markdown(f"`{val}` {label}")

    with tab2:
        thesis = load_ai_summary(ticker, "thesis")
        if thesis:
            st.info(thesis["summary"])
            raw = thesis.get("raw", {})
            if raw.get("what_consensus_misses"):
                st.markdown(f"**What consensus misses:** {raw['what_consensus_misses']}")
            if raw.get("leading_indicators"):
                st.markdown("**Leading indicators:**")
                for li in raw["leading_indicators"]:
                    with st.expander(f"📊 {li['indicator']} ({li['frequency']})"):
                        st.markdown(f"✅ **Confirms if:** {li['confirms_thesis_if']}")
                        st.markdown(f"❌ **Breaks if:** {li['kills_thesis_if']}")
            if raw.get("key_risks"):
                st.markdown("**Risks:** " + " · ".join([f"⚠️ {r}" for r in raw["key_risks"]]))
            conv = raw.get("conviction", "?")
            badge = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conv, "⚪")
            st.caption(f"Conviction: {badge} {conv}  ·  Horizon: {raw.get('time_horizon','?')}  ·  {thesis['generated_at'][:10]}")
        else:
            st.info("No thesis yet — needs quality score ≥ 7 and a pipeline run.")

    with tab3:
        earnings = load_ai_summary(ticker, "earnings")
        if earnings:
            badge = {"positive": "🟢", "neutral": "🟡", "negative": "🔴"}.get(earnings["sentiment"], "⚪")
            st.markdown(f"{badge} **{earnings['sentiment'].capitalize()} sentiment**")
            st.write(earnings["summary"])
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Green flags**")
                for f in earnings["green_flags"]:
                    if f: st.markdown(f'<div class="flag-green">{f}</div>', unsafe_allow_html=True)
            with c2:
                st.markdown("**Red flags**")
                for f in earnings["red_flags"]:
                    if f: st.markdown(f'<div class="flag-red">{f}</div>', unsafe_allow_html=True)
        else:
            st.info("No earnings summary yet.")

    with tab4:
        price_df = load_price_history(ticker)
        if not price_df.empty:
            fig = px.line(price_df, x="date", y="close",
                          labels={"close": "Price ($)", "date": ""},
                          template="simple_white")
            fig.update_traces(line_color="#1D9E75", line_width=2)
            fig.update_layout(height=320, margin=dict(t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No price history yet.")


# ── 13F tracker ───────────────────────────────────────────────────────────────

elif page == "🐋 13F tracker":
    st.title("13F tracker — fund cluster analysis")

    c1, c2 = st.columns([2, 1])
    with c1:
        period_filter = st.text_input("Quarter (e.g. 2025-Q1)", "")
    with c2:
        min_funds = st.slider("Min funds buying", 1, 10, 2)

    df = load_cluster_buys(period=period_filter or None, min_funds=min_funds)

    if df.empty:
        st.info("No cluster buys found. Run the 13F pipeline to populate.")
    else:
        st.markdown(f"**{len(df)} name(s) with ≥{min_funds} fund(s) opening positions simultaneously**")
        for _, row in df.iterrows():
            label = "🔥 Strong cluster" if row["fund_count"] >= 3 else "👀 Watch"
            with st.expander(
                f"**{row['ticker']}** — {row['fund_count']} funds  ·  "
                f"${row['total_value_m']:.0f}M  ·  {row['period']}  {label}"
            ):
                st.markdown(f"**Funds:** {row['funds']}")
                st.caption("Independent new positions in the same quarter = thesis convergence signal.")


# ── Insider activity ──────────────────────────────────────────────────────────

elif page == "👥 Insider activity":
    st.title("Insider activity")

    days = st.slider("Lookback (days)", 30, 365, 90)
    df   = load_insider_buys(days)

    if df.empty:
        st.info("No insider purchases in this period.")
    else:
        st.markdown(f"**{len(df)} purchases in the last {days} days**")
        st.dataframe(
            df.rename(columns={
                "ticker": "Ticker", "filer_name": "Name", "filer_title": "Title",
                "transaction_date": "Date", "shares": "Shares",
                "price": "Price ($)", "value_m": "Value ($M)",
            }).style.format({"Price ($)": "${:.2f}", "Value ($M)": "${:.2f}", "Shares": "{:,.0f}"}),
            hide_index=True, use_container_width=True,
        )


# ── AI summaries ──────────────────────────────────────────────────────────────

elif page == "🤖 AI summaries":
    st.title("AI summaries")

    df = load_watchlist()
    if df.empty:
        st.info("No stocks yet.")
        st.stop()

    ticker       = st.selectbox("Ticker", df["ticker"].tolist())
    summary_type = st.radio("Type", ["thesis", "earnings", "10k"], horizontal=True)
    summary      = load_ai_summary(ticker, summary_type)

    if summary:
        badge = {"positive": "🟢", "neutral": "🟡", "negative": "🔴"}.get(summary["sentiment"], "⚪")
        st.markdown(f"**{ticker} — {summary_type.upper()}** {badge}")
        st.markdown(f"> {summary['summary']}")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Green flags**")
            for f in summary["green_flags"]:
                if f: st.markdown(f'<div class="flag-green">{f}</div>', unsafe_allow_html=True)
        with c2:
            st.markdown("**Red flags**")
            for f in summary["red_flags"]:
                if f: st.markdown(f'<div class="flag-red">{f}</div>', unsafe_allow_html=True)
        st.caption(f"Generated: {summary['generated_at'][:10]}")
    else:
        st.info(f"No {summary_type} summary for {ticker} yet.")


# ── Alerts ────────────────────────────────────────────────────────────────────

elif page == "🔔 Alerts":
    st.title("Alerts")

    df = load_alerts(100)
    if df.empty:
        st.info("No alerts yet.")
    else:
        unread = len(df[df["sent"] == 0])
        if unread:
            st.markdown(f"**{unread} new alerts**")
        for _, row in df.iterrows():
            new_tag = "🆕 " if row["sent"] == 0 else ""
            with st.expander(
                f"{new_tag}{sev_icon(row['severity'])} **{row['ticker']}** — "
                f"{alert_label(row['alert_type'])}  ·  {str(row['triggered_at'])[:16]}"
            ):
                st.markdown(row["message"])
                st.caption(f"Severity: {row['severity']}  ·  Sent: {'Yes' if row['sent'] else 'Pending'}")


# ── Earnings calendar ─────────────────────────────────────────────────────────

elif page == "📅 Earnings calendar":
    st.title("Earnings calendar")

    import db_adapter as _sq
    conn = _sq.connect("research.db")
    df = pd.read_sql_query("""
        SELECT e.ticker, e.next_earnings, e.days_until, e.last_earnings,
               f.quality_score, f.name
        FROM earnings_dates e
        JOIN fundamentals f ON e.ticker = f.ticker
        WHERE e.days_until IS NOT NULL AND e.days_until >= 0
        ORDER BY e.days_until ASC
    """, conn)
    conn.close()

    if df.empty:
        st.info("No earnings dates yet. Run `python3 market_signals.py` to populate.")
    else:
        this_week  = df[df["days_until"] <= 7]
        this_month = df[(df["days_until"] > 7) & (df["days_until"] <= 30)]
        later      = df[df["days_until"] > 30]

        if not this_week.empty:
            st.markdown("### 🔴 This week")
            for _, r in this_week.iterrows():
                st.markdown(f"**{r['ticker']}** — {r['next_earnings']} ({r['days_until']}d)  Score: {r['quality_score']}/10")

        if not this_month.empty:
            st.markdown("### 🟡 This month")
            for _, r in this_month.iterrows():
                st.markdown(f"**{r['ticker']}** — {r['next_earnings']} ({r['days_until']}d)  Score: {r['quality_score']}/10")

        if not later.empty:
            st.markdown("### Later")
            for _, r in later.iterrows():
                st.markdown(f"**{r['ticker']}** — {r['next_earnings']} ({r['days_until']}d)")


# ── Action engine ─────────────────────────────────────────────────────────────

elif page == "⚡ Action engine":
    st.title("Action engine")

    import db_adapter as _sq, sys, os
    sys.path.insert(0, os.getcwd())

    try:
        from portfolio import generate_action, init_portfolio_db, add_position, exit_position
        init_portfolio_db()
    except Exception as e:
        st.error(f"Portfolio module error: {e}")
        st.stop()

    conn = _sq.connect("research.db")
    stocks = pd.read_sql_query("""
        SELECT f.ticker, f.name, f.quality_score,
               s.short_pct_float, e.days_until as days_earn,
               ar.revision_trend, ais.raw_response
        FROM fundamentals f
        INNER JOIN (
            SELECT ticker, MAX(fetched_at) as latest
            FROM fundamentals GROUP BY ticker
        ) l ON f.ticker = l.ticker AND f.fetched_at = l.latest
        LEFT JOIN short_interest s ON f.ticker = s.ticker
        LEFT JOIN earnings_dates e ON f.ticker = e.ticker
        LEFT JOIN analyst_revisions ar ON f.ticker = ar.ticker
        LEFT JOIN ai_summaries ais ON f.ticker = ais.ticker AND ais.summary_type = 'thesis'
        ORDER BY f.quality_score DESC
    """, conn)

    open_tickers = {r[0] for r in conn.execute(
        "SELECT ticker FROM portfolio WHERE status='open'"
    ).fetchall()}
    conn.close()

    import json as _json
    action_groups = {"buy": [], "add": [], "trim": [], "sell": [], "watch": [], "hold": []}

    for _idx, row in stocks.iterrows():
        conviction = "medium"
        try:
            raw = row.get("raw_response")
            if raw:
                conviction = _json.loads(raw).get("conviction", "medium")
        except Exception:
            pass

        data = {
            "quality_score":       row.get("quality_score", 0),
            "conviction":          conviction,
            "short_pct_float":     row.get("short_pct_float"),
            "days_until_earnings": row.get("days_earn"),
            "revision_trend":      row.get("revision_trend") or "neutral",
            "in_portfolio":        row["ticker"] in open_tickers,
            "thesis_status":       "active",
        }
        action, reasons, confidence = generate_action(row["ticker"], data)
        action_groups[action].append((row["ticker"], row.get("quality_score", 0), reasons, confidence))

    icons = {"buy": "🟢", "add": "💚", "trim": "🟡", "sell": "🔴", "watch": "👀", "hold": "⏸"}
    labels = {"buy": "Buy", "add": "Add to position", "trim": "Trim", "sell": "Sell", "watch": "Watch", "hold": "Hold"}

    _key_counter = 0
    for action in ["buy", "add", "sell", "trim", "watch", "hold"]:
        group = action_groups[action]
        if not group:
            continue
        st.markdown(f"### {icons[action]} {labels[action]} ({len(group)})")
        for ticker, score, reasons, conf in group:
            _key_counter += 1
            _k = _key_counter
            with st.expander(f"**{ticker}** — Score {score}/10  ·  Confidence: {conf}"):
                for r in reasons:
                    st.markdown(f"• {r}")
                col1, col2 = st.columns(2)
                price = col1.number_input(f"Price for {ticker}", min_value=0.0, step=0.01, key=f"ae_price_{_k}")
                if action in ["buy", "add"]:
                    try:
                        from portfolio import get_buy_price, get_current_price
                        cur = get_current_price(ticker)
                        if cur:
                            _, note = get_buy_price(ticker, cur)
                            st.caption(f"💰 Current: ${cur:.2f}  —  {note}")
                    except Exception:
                        pass
                if action in ["buy", "add"] and col2.button(f"Log buy", key=f"ae_buy_{_k}"):
                    if price > 0:
                        add_position(ticker, price)
                        st.success(f"Logged {ticker} buy at ${price:.2f}")
                if action in ["sell", "trim"] and col2.button(f"Log exit", key=f"ae_sell_{_k}"):
                    if price > 0:
                        exit_position(ticker, price)
                        st.success(f"Logged {ticker} exit at ${price:.2f}")

    st.markdown("---")
    st.markdown("### Open positions")
    conn = _sq.connect("research.db")
    pos_df = pd.read_sql_query("""
        SELECT ticker, entry_price, entry_date, shares, entry_score
        FROM portfolio WHERE status = 'open' ORDER BY entry_date
    """, conn)
    conn.close()

    if pos_df.empty:
        st.info("No open positions. Log a buy above to start tracking.")
    else:
        st.dataframe(pos_df, hide_index=True, use_container_width=True)


# ── Thesis scorecard ──────────────────────────────────────────────────────────

elif page == "📋 Thesis scorecard":
    st.title("Thesis scorecard")

    import db_adapter as _sq, json as _json
    conn = _sq.connect("research.db")

    checkins = pd.read_sql_query("""
        SELECT t.ticker, t.checkin_date, t.overall,
               t.status_1, t.status_2, t.status_3,
               t.indicator_1, t.indicator_2, t.indicator_3,
               t.price_at_checkin, t.notes,
               p.entry_price
        FROM thesis_checkins t
        LEFT JOIN portfolio p ON t.ticker = p.ticker AND p.status = 'open'
        ORDER BY t.checkin_date DESC
    """, conn)
    conn.close()

    if checkins.empty:
        st.info("No check-ins yet. Run `python3 scorecard.py --checkin TICKER` in your terminal to do your first quarterly check-in.")
    else:
        for ticker in checkins["ticker"].unique():
            rows = checkins[checkins["ticker"] == ticker]
            latest = rows.iloc[0]
            overall = latest.get("overall", "")
            icon = {"on_track": "🟢", "at_risk": "🟡", "broken": "🔴"}.get(overall, "⚪")

            with st.expander(f"{icon} **{ticker}** — {overall or '?'}  ·  Last check-in: {str(latest['checkin_date'])[:10]}"):
                s_icons = {"on_track": "✅", "at_risk": "⚠️", "broken": "❌"}
                for i in range(1, 4):
                    ind = latest.get(f"indicator_{i}")
                    status = latest.get(f"status_{i}")
                    if ind:
                        st.markdown(f"{s_icons.get(status,'—')} {ind} — **{status or '?'}**")

                if latest.get("notes"):
                    st.caption(f"Notes: {latest['notes']}")

                if latest.get("entry_price") and latest.get("price_at_checkin"):
                    ret = (latest["price_at_checkin"] - latest["entry_price"]) / latest["entry_price"] * 100
                    st.caption(f"Return at check-in: {ret:+.1f}%")

                if len(rows) > 1:
                    st.markdown("**History:**")
                    for _, row in rows.iloc[1:].iterrows():
                        o_icon = {"on_track": "🟢", "at_risk": "🟡", "broken": "🔴"}.get(row.get("overall",""), "⚪")
                        st.caption(f"{o_icon} {str(row['checkin_date'])[:10]} — {row.get('overall','?')}")

    st.markdown("---")
    st.markdown("**To do a check-in**, run in your terminal:")
    st.code("python3 scorecard.py --checkin ANET")


# ── Hit rate analyzer ─────────────────────────────────────────────────────────

elif page == "📈 Hit rate analyzer":
    st.title("Signal hit rate analyzer")

    import db_adapter as _sq
    conn = _sq.connect("research.db")

    total = conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]

    if total == 0:
        st.info("No closed positions yet. The hit rate analyzer builds over time as you log buys and exits in the Action Engine. The more positions you track, the more useful this becomes.")
        st.markdown("**How it works:**")
        st.markdown("Every time you log an exit with `python3 portfolio.py --exit TICKER PRICE`, the system records what signals triggered the buy and whether it was a win or loss. Over time this reveals which signals to trust most.")
    else:
        # Quality score performance
        score_df = pd.read_sql_query("""
            SELECT signal_value as score,
                   COUNT(*) as trades,
                   ROUND(AVG(return_365d), 1) as avg_return,
                   ROUND(SUM(CASE WHEN outcome='win' THEN 1.0 ELSE 0 END) * 100 / COUNT(*), 0) as win_rate
            FROM signal_outcomes
            WHERE signal_type = 'quality_score'
            GROUP BY signal_value
            ORDER BY CAST(signal_value AS INTEGER) DESC
        """, conn)

        if not score_df.empty:
            st.markdown("### By quality score")
            st.dataframe(score_df.rename(columns={
                "score": "Quality Score", "trades": "Trades",
                "avg_return": "Avg Return %", "win_rate": "Win Rate %"
            }), hide_index=True, use_container_width=True)

        # Conviction performance
        conv_df = pd.read_sql_query("""
            SELECT signal_value as conviction,
                   COUNT(*) as trades,
                   ROUND(AVG(return_365d), 1) as avg_return,
                   ROUND(SUM(CASE WHEN outcome='win' THEN 1.0 ELSE 0 END) * 100 / COUNT(*), 0) as win_rate
            FROM signal_outcomes
            WHERE signal_type = 'conviction'
            GROUP BY signal_value
            ORDER BY avg_return DESC
        """, conn)

        if not conv_df.empty:
            st.markdown("### By Claude conviction level")
            st.dataframe(conv_df.rename(columns={
                "conviction": "Conviction", "trades": "Trades",
                "avg_return": "Avg Return %", "win_rate": "Win Rate %"
            }), hide_index=True, use_container_width=True)

        # Overall stats
        wins = conn.execute("SELECT COUNT(*) FROM signal_outcomes WHERE outcome='win'").fetchone()[0]
        avg  = conn.execute("SELECT AVG(return_365d) FROM signal_outcomes").fetchone()[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Signals tracked", total)
        c2.metric("Win rate", f"{wins/total*100:.0f}%")
        c3.metric("Avg return", f"{avg:+.1f}%")

    conn.close()


# ── Valuation model ───────────────────────────────────────────────────────────

elif page == "💰 Valuation model":
    st.title("Valuation model")

    import db_adapter as _sq
    conn = _sq.connect("research.db")

    try:
        df = pd.read_sql_query("""
            SELECT v.ticker, v.current_price, v.base_target,
                   v.bear_target, v.bull_target,
                   v.bear_return, v.base_return, v.bull_return,
                   v.margin_of_safety, v.verdict,
                   v.implied_growth_rate, v.wacc,
                   f.quality_score, f.name
            FROM valuations v
            JOIN fundamentals f ON v.ticker = f.ticker
            ORDER BY v.margin_of_safety DESC
        """, conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()

    if df.empty:
        st.info("No valuation data yet. Run `python3 valuation.py` to populate.")
    else:
        # Summary metrics
        attractive = len(df[df["verdict"] == "attractive"])
        fair       = len(df[df["verdict"] == "fair"])
        stretched  = len(df[df["verdict"] == "stretched"])
        expensive  = len(df[df["verdict"] == "expensive"])

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🟢 Attractive", attractive)
        c2.metric("🟡 Fair", fair)
        c3.metric("🟠 Stretched", stretched)
        c4.metric("🔴 Expensive", expensive)

        st.markdown("---")

        verdict_icons = {
            "attractive": "🟢", "fair": "🟡",
            "stretched": "🟠", "expensive": "🔴",
            "analyst_target_only": "📊"
        }

        for _, row in df.iterrows():
            icon = verdict_icons.get(row["verdict"], "⚪")
            is_analyst_only = row["verdict"] == "analyst_target_only"

            upside_str = f"{row['base_return']:+.1f}%" if row.get("base_return") else "—"
            target_str = f"${row['base_target']:.2f}" if row.get("base_target") else "—"

            with st.expander(
                f"{icon} **{row['ticker']}** — "
                f"{'Analyst target' if is_analyst_only else row['verdict'].upper()}  ·  "
                f"Current: ${row['current_price']:.2f}  ·  "
                f"Target: {target_str}  ·  "
                f"Upside: {upside_str}"
            ):
                if is_analyst_only:
                    st.caption("⚠️ DCF unreliable for this company type — showing analyst consensus target only")
                    if row.get("base_target"):
                        st.metric("📊 Analyst consensus target", f"${row['base_target']:.2f}", upside_str)
                    st.markdown(f"**Quality score:** {row['quality_score']}/10")
                else:
                    c1, c2, c3 = st.columns(3)
                    if row.get("bear_target"):
                        c1.metric("🐻 Bear", f"${row['bear_target']:.2f}", f"{row['bear_return']:+.1f}%")
                    if row.get("base_target"):
                        c2.metric("📊 Base", f"${row['base_target']:.2f}", f"{row['base_return']:+.1f}%")
                    if row.get("bull_target"):
                        c3.metric("🐂 Bull", f"${row['bull_target']:.2f}", f"{row['bull_return']:+.1f}%")
                    if row.get("margin_of_safety"):
                        st.markdown(f"**Margin of safety:** {row['margin_of_safety']:+.1f}%")
                    if row.get("implied_growth_rate"):
                        st.markdown(
                            f"**Implied growth rate:** {(row['implied_growth_rate'] or 0)*100:.1f}% — "
                            "what the market is pricing in."
                        )
                    st.markdown(f"**Quality score:** {row['quality_score']}/10")
                    st.caption("DCF models are sensitive to assumptions — use as directional guide.")


# ── Relative strength ─────────────────────────────────────────────────────────

elif page == "⚡ Relative strength":
    st.title("Relative strength vs S&P 500")

    import db_adapter as _sq
    conn = _sq.connect("research.db")

    try:
        df = pd.read_sql_query("""
            SELECT r.ticker, r.current_price,
                   r.return_1m, r.return_3m, r.return_6m, r.return_12m,
                   r.spy_return_1m, r.spy_return_3m, r.spy_return_6m, r.spy_return_12m,
                   r.rs_1m, r.rs_3m, r.rs_6m, r.rs_12m,
                   r.momentum_signal, f.quality_score
            FROM relative_strength r
            JOIN fundamentals f ON r.ticker = f.ticker
            ORDER BY r.rs_3m DESC
        """, conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()

    if df.empty:
        st.info("No relative strength data yet. Run `python3 valuation.py` to populate.")
    else:
        momentum_icons = {
            "strong_leader":      "🚀",
            "outperforming":      "📈",
            "neutral":            "➡️",
            "underperforming":    "📉",
            "persistent_laggard": "⚠️",
        }

        leaders    = df[df["momentum_signal"].isin(["strong_leader","outperforming"])]
        laggards   = df[df["momentum_signal"].isin(["persistent_laggard","underperforming"])]
        neutral    = df[df["momentum_signal"] == "neutral"]

        if not leaders.empty:
            st.markdown("### 📈 Outperforming the market")
            for _, row in leaders.iterrows():
                icon = momentum_icons.get(row["momentum_signal"], "")
                rs3  = f"{row['rs_3m']:+.1f}%" if row.get("rs_3m") is not None else "—"
                rs6  = f"{row['rs_6m']:+.1f}%" if row.get("rs_6m") is not None else "—"
                st.markdown(
                    f"{icon} **{row['ticker']}**  ·  "
                    f"3M vs SPY: {rs3}  ·  6M vs SPY: {rs6}  ·  "
                    f"Score: {row['quality_score']}/10"
                )

        if not laggards.empty:
            st.markdown("### 📉 Underperforming the market")
            for _, row in laggards.iterrows():
                icon = momentum_icons.get(row["momentum_signal"], "")
                rs3  = f"{row['rs_3m']:+.1f}%" if row.get("rs_3m") is not None else "—"
                rs6  = f"{row['rs_6m']:+.1f}%" if row.get("rs_6m") is not None else "—"
                st.markdown(
                    f"{icon} **{row['ticker']}**  ·  "
                    f"3M vs SPY: {rs3}  ·  6M vs SPY: {rs6}  ·  "
                    f"Score: {row['quality_score']}/10"
                )
                st.caption(
                    "⚠️ High quality stock underperforming — "
                    "either a buying opportunity or the market sees something you don't. "
                    "Check the thesis carefully."
                )

        if not neutral.empty:
            st.markdown("### ➡️ Tracking the market")
            cols = st.columns(4)
            for i, (_, row) in enumerate(neutral.iterrows()):
                rs3 = f"{row['rs_3m']:+.1f}%" if row.get("rs_3m") is not None else "—"
                cols[i % 4].metric(row["ticker"], rs3, f"Score {row['quality_score']}/10")

        # SPY benchmark row
        if df["spy_return_3m"].notna().any():
            spy_3m = df["spy_return_3m"].iloc[0]
            st.markdown("---")
            st.caption(f"S&P 500 (SPY) returns: 3M {spy_3m:+.1f}%")


# ── My portfolio ──────────────────────────────────────────────────────────────

elif page == "💼 My portfolio":
    st.title("My portfolio")

    import db_adapter as _sq, sys, os
    sys.path.insert(0, os.getcwd())

    try:
        from my_portfolio import init_my_portfolio_db, add_position, trim_position, sell_position, get_signal
        init_my_portfolio_db()
    except Exception as e:
        st.error(f"Portfolio module error: {e}")
        st.stop()

    conn = _sq.connect("research.db")
    positions = pd.read_sql_query("""
        SELECT p.ticker, p.shares, p.avg_cost, p.added_date, p.notes,
               f.quality_score, f.name,
               v.base_target, v.margin_of_safety,
               r.rs_3m, r.rs_6m,
               e.days_until
        FROM my_portfolio p
        LEFT JOIN fundamentals f ON p.ticker = f.ticker
        LEFT JOIN valuations v ON p.ticker = v.ticker
        LEFT JOIN relative_strength r ON p.ticker = r.ticker
        LEFT JOIN earnings_dates e ON p.ticker = e.ticker
        WHERE p.status = 'open'
        ORDER BY p.added_date
    """, conn)
    conn.close()

    # Add new position form
    with st.expander("➕ Add a stock you own"):
        c1, c2, c3 = st.columns(3)
        new_ticker = c1.text_input("Ticker", placeholder="AAPL").upper()
        new_price  = c2.number_input("Avg cost per share ($)", min_value=0.0, step=0.01)
        new_shares = c3.number_input("Number of shares", min_value=0.0, step=1.0)
        new_notes  = st.text_input("Notes (optional)", placeholder="Bought on earnings dip")
        if st.button("Add position") and new_ticker and new_price > 0 and new_shares > 0:
            add_position(new_ticker, new_price, new_shares, new_notes)
            st.success(f"Added {new_ticker} — {new_shares:.0f} shares at ${new_price:.2f}")
            st.rerun()

    if positions.empty:
        st.info("No positions yet. Add your holdings above.")
        st.stop()

    # ── AI Bucket classifier ──────────────────────────────────────────────────
    AI_BUCKETS = {
        "NVDA": "core_infrastructure", "AMD":  "core_infrastructure",
        "AVGO": "core_infrastructure", "ANET": "core_infrastructure",
        "CRDO": "core_infrastructure", "TSM":  "core_infrastructure",
        "MU":   "memory",              "LRCX": "core_infrastructure",
        "KLAC": "core_infrastructure", "AMAT": "core_infrastructure",
        "ADI":  "core_infrastructure", "TXN":  "core_infrastructure",
        "NEE":  "power",               "VST":  "power",
        "BE":   "power",               "ICHR": "core_infrastructure",
        "GOOGL":"hyperscaler",         "TSLA": "application",
        "PLTR": "application",         "ACHR": "application",
        "LITE": "core_infrastructure",
    }

    bucket_labels = {
        "core_infrastructure": "⚙️ Core infrastructure",
        "memory":              "💾 Memory / storage",
        "power":               "⚡ Power / data centers",
        "hyperscaler":         "☁️ Hyperscalers",
        "application":         "📱 Applications",
        "none":                "❓ No AI bottleneck",
    }

    with st.expander("🤖 AI bucket breakdown — does each stock own a bottleneck?"):
        bucket_groups = {}
        for _, row in positions.iterrows():
            bucket = AI_BUCKETS.get(row["ticker"], "none")
            bucket_groups.setdefault(bucket, []).append(row["ticker"])

        for bucket, tickers in sorted(bucket_groups.items()):
            label = bucket_labels.get(bucket, bucket)
            color = "🟢" if bucket != "none" else "🔴"
            st.markdown(f"{color} **{label}:** {', '.join(tickers)}")

        if "none" in bucket_groups:
            st.warning(f"**{', '.join(bucket_groups['none'])}** — no clear AI bottleneck identified. Review whether these still fit your thesis.")

    # ── AI Portfolio Analysis ─────────────────────────────────────────────────
    if st.button("🤖 Run AI portfolio analysis"):
        import anthropic as _anthropic, json as _json, os as _os
        import yfinance as _yf2

        with st.spinner("Claude is analyzing your portfolio..."):
            # Build portfolio summary for Claude
            holdings = []
            for _, row in positions.iterrows():
                try:
                    t   = _yf2.Ticker(row["ticker"])
                    cur = t.info.get("regularMarketPrice") or row["avg_cost"]
                except Exception:
                    cur = row["avg_cost"]
                ret = (cur - row["avg_cost"]) / row["avg_cost"] * 100
                holdings.append({
                    "ticker":      row["ticker"],
                    "shares":      row["shares"],
                    "avg_cost":    row["avg_cost"],
                    "current":     cur,
                    "return_pct":  round(ret, 1),
                    "value":       round(cur * row["shares"], 0),
                    "quality_score": row.get("quality_score"),
                })

            total_val  = sum(h["value"] for h in holdings)
            for h in holdings:
                h["pct_of_portfolio"] = round(h["value"] / total_val * 100, 1)

            # Summarize holdings to reduce token count
            top_holdings = sorted(holdings, key=lambda x: x["value"], reverse=True)[:12]
            summary = [{
                "t": h["ticker"],
                "ret": h["return_pct"],
                "val": h["value"],
                "pct": h["pct_of_portfolio"],
                "score": h.get("quality_score")
            } for h in top_holdings]

            prompt = f"""Expert portfolio manager using this AI investment framework:

AI BOTTLENECK FILTER: High conviction only if company owns a bottleneck AI cannot avoid.
5 buckets: (1)Core infra-chips/networking (2)Memory/storage (3)Power/data centers (4)Hyperscalers (5)Selective apps
Watch-outs: good trend bad price, small-cap hype, overbuild risk, cyclical shortage profits, universal bullishness.

Analyze this ${total_val:,.0f} portfolio. Keep all strings under 80 chars.
Holdings: {_json.dumps(summary)}

Return ONLY valid JSON, no markdown:
{{"overall_assessment":"one sentence","concentration_risks":["risk1","risk2","risk3"],"immediate_actions":[{{"ticker":"XX","action":"trim","reason":"bottleneck or valuation reason","urgency":"high"}},{{"ticker":"YY","action":"hold","reason":"short reason","urgency":"low"}}],"sector_analysis":"one sentence on AI bucket exposure","biggest_risk":"one sentence","biggest_opportunity":"one sentence"}}"""

            try:
                client = _anthropic.Anthropic(api_key=_os.environ.get("ANTHROPIC_API_KEY"))
                msg    = client.messages.create(
                    model      = "claude-sonnet-4-5",
                    max_tokens = 1500,
                    messages   = [{"role": "user", "content": prompt}]
                )
                raw  = msg.content[0].text.strip()
                lines = raw.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                raw   = "\n".join(lines).strip()
                data  = _json.loads(raw)

                st.markdown("---")
                st.markdown("### 🤖 AI Portfolio Analysis")

                st.info(data.get("overall_assessment", ""))

                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**🎯 Immediate actions**")
                    for action in data.get("immediate_actions", []):
                        urgency_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(action.get("urgency"), "⚪")
                        act = action.get("action","").upper()
                        st.markdown(
                            f"{urgency_icon} **{action['ticker']}** — {act}<br>"
                            f"<small>{action['reason']}</small>",
                            unsafe_allow_html=True
                        )

                with col2:
                    st.markdown("**⚠️ Concentration risks**")
                    for risk in data.get("concentration_risks", []):
                        st.markdown(f"• {risk}")

                st.markdown(f"**📊 Sector analysis:** {data.get('sector_analysis','')}")

                c1, c2 = st.columns(2)
                c1.error(f"**Biggest risk:** {data.get('biggest_risk','')}")
                c2.success(f"**Biggest opportunity:** {data.get('biggest_opportunity','')}")
                st.markdown("---")

            except Exception as e:
                st.error(f"Analysis failed: {e}")

    # Summary metrics
    import yfinance as _yf

    total_value = 0
    total_cost  = 0
    rows_data   = []

    for _, row in positions.iterrows():
        try:
            t    = _yf.Ticker(row["ticker"])
            cur  = t.info.get("regularMarketPrice") or row["avg_cost"]
        except Exception:
            cur  = row["avg_cost"]

        mv   = cur * row["shares"]
        cb   = row["avg_cost"] * row["shares"]
        ret  = (cur - row["avg_cost"]) / row["avg_cost"] * 100
        total_value += mv
        total_cost  += cb

        action, signals = get_signal(
            row["ticker"], cur, row["avg_cost"],
            row.get("quality_score"), row.get("margin_of_safety"),
            row.get("rs_3m"), row.get("days_until")
        )
        rows_data.append({
            "row": row, "current": cur, "mv": mv, "cb": cb,
            "ret": ret, "action": action, "signals": signals
        })

    total_ret = (total_value - total_cost) / total_cost * 100 if total_cost else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Portfolio value", f"${total_value:,.0f}")
    c2.metric("Total cost", f"${total_cost:,.0f}")
    c3.metric("Total gain/loss", f"${total_value-total_cost:+,.0f}")
    c4.metric("Total return", f"{total_ret:+.1f}%")

    st.markdown("---")

    action_icons = {
        "sell":  "🔴 SELL",
        "trim":  "🟡 TRIM",
        "add":   "💚 ADD MORE",
        "watch": "👀 WATCH",
        "hold":  "⏸ HOLD",
    }

    for d in sorted(rows_data, key=lambda x: x["ret"], reverse=True):
        row    = d["row"]
        cur    = d["current"]
        ret    = d["ret"]
        action = d["action"]
        signals = d["signals"]
        icon   = "📈" if ret >= 0 else "📉"
        a_icon = action_icons.get(action, action)

        with st.expander(
            f"{icon} **{row['ticker']}** — {ret:+.1f}%  ·  "
            f"${d['mv']:,.0f} value  ·  {a_icon}"
        ):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Shares", f"{row['shares']:.0f}")
            c2.metric("Avg cost", f"${row['avg_cost']:.2f}")
            c3.metric("Current", f"${cur:.2f}")
            c4.metric("Gain/Loss", f"${d['mv']-d['cb']:+,.0f}")

            if row.get("base_target"):
                to_target = (row["base_target"] - cur) / cur * 100
                st.markdown(f"**Base target:** ${row['base_target']:.2f} ({to_target:+.1f}% from here)  ·  "
                           f"**Quality score:** {row.get('quality_score','?')}/10")

            st.markdown(f"**Signal: {a_icon}**")
            for s in signals:
                st.markdown(f"• {s}")

            # Action buttons
            st.markdown("**Log a transaction:**")
            bc1, bc2, bc3 = st.columns(3)
            t_price = bc1.number_input("Price", min_value=0.0,
                                        value=float(cur), step=0.01,
                                        key=f"tp_{row['ticker']}")
            t_shares = bc2.number_input("Shares", min_value=0.0,
                                         value=float(row["shares"]),
                                         step=1.0, key=f"ts_{row['ticker']}")
            with bc3:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                if st.button("Log buy/add", key=f"buy_{row['ticker']}"):
                    if t_price > 0 and t_shares > 0:
                        add_position(row["ticker"], t_price, t_shares)
                        st.success("Added")
                        st.rerun()
                if st.button("Log trim", key=f"trim_{row['ticker']}"):
                    if t_price > 0 and t_shares > 0:
                        trim_position(row["ticker"], t_price, t_shares)
                        st.success("Trimmed")
                        st.rerun()
                if st.button("Log full exit", key=f"sell_{row['ticker']}"):
                    if t_price > 0:
                        sell_position(row["ticker"], t_price)
                        st.success("Position closed")
                        st.rerun()

            st.caption(f"Added: {row['added_date']}  ·  {row.get('notes','')}")


# ── Quality checks ────────────────────────────────────────────────────────────

elif page == "🔬 Quality checks":
    st.title("Quality checks — margin trends & analyst consensus")
    st.caption("Catches the DLO problem: growth at declining margins and high analyst disagreement.")

    import db_adapter as _sq
    conn = _sq.connect("research.db")

    try:
        df = pd.read_sql_query("""
            SELECT m.ticker, m.gm_trend, m.om_trend,
                   m.gm_q1, m.gm_q2, m.gm_q3, m.gm_q4,
                   m.analyst_spread_pct, m.consensus_quality,
                   m.revenue_quality, m.conviction_override,
                   f.quality_score, f.name
            FROM margin_trends m
            JOIN fundamentals f ON m.ticker = f.ticker
            ORDER BY
                CASE m.conviction_override
                    WHEN 'low' THEN 1
                    WHEN 'medium' THEN 2
                    ELSE 3
                END,
                f.quality_score DESC
        """, conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()

    if df.empty:
        st.info("No quality check data yet. Run `python3 quality_checks.py` to populate.")
    else:
        # Summary
        overrides = df[df["conviction_override"].notna()]
        flagged_low = df[df["conviction_override"] == "low"]
        contracting = df[df["gm_trend"] == "contracting"]

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Stocks checked", len(df))
        c2.metric("🔴 Conviction overrides", len(overrides))
        c3.metric("📉 Margin contracting", len(contracting))
        c4.metric("⚠️ High analyst uncertainty", len(df[df["consensus_quality"].isin(["uncertain","very_uncertain"])]))

        st.markdown("---")

        # Flagged stocks first
        if not overrides.empty:
            st.markdown("### ⚠️ Stocks with conviction overrides")
            for _, row in overrides.iterrows():
                override = row.get("conviction_override", "")
                icon = "🔴" if override == "low" else "🟡"
                with st.expander(
                    f"{icon} **{row['ticker']}** — Override to {override.upper()}  ·  "
                    f"Score: {row.get('quality_score','?')}/10"
                ):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown("**Gross margin trend (newest → oldest):**")
                        gm_vals = [row.get(f"gm_q{i}") for i in range(1,5)]
                        gm_str = " → ".join([f"{v:.1f}%" if v else "—" for v in gm_vals])
                        trend_icon = {"contracting":"📉","expanding":"📈","stable":"➡️"}.get(row.get("gm_trend",""),"?")
                        st.markdown(f"{trend_icon} {gm_str}")
                    with col2:
                        st.markdown("**Analyst consensus:**")
                        spread = row.get("analyst_spread_pct")
                        cq = row.get("consensus_quality","?")
                        cq_icon = {"consensus":"🟢","moderate":"🟡","uncertain":"🟠","very_uncertain":"🔴"}.get(cq,"?")
                        st.markdown(f"{cq_icon} {cq} — spread: {spread:.0f}%" if spread else f"{cq_icon} {cq}")
                    st.markdown(f"**Revenue quality:** {row.get('revenue_quality','?')}")

        # All stocks table
        st.markdown("### All stocks")
        trend_icons = {"expanding":"📈","contracting":"📉","stable":"➡️"}
        rq_icons = {"pricing_power":"💪","volume_growth":"📦","volume_at_cost":"⚠️","deteriorating":"🔴","mixed":"➡️"}
        cq_icons = {"consensus":"🟢","moderate":"🟡","uncertain":"🟠","very_uncertain":"🔴"}

        for _, row in df.iterrows():
            gm_icon = trend_icons.get(row.get("gm_trend",""), "?")
            rq_icon = rq_icons.get(row.get("revenue_quality",""), "?")
            cq_icon = cq_icons.get(row.get("consensus_quality",""), "?")
            override = row.get("conviction_override")
            ov_str = f" → **{override.upper()}**" if override else ""
            st.markdown(
                f"`{row['ticker']}`  GM:{gm_icon}  Rev:{rq_icon}{row.get('revenue_quality','?')}  "
                f"Consensus:{cq_icon}  Score:{row.get('quality_score','?')}/10{ov_str}"
            )


# ── Earnings analysis ─────────────────────────────────────────────────────────

elif page == "🎙 Earnings analysis":
    st.title("Earnings call analysis")
    st.caption("Claude reads the transcript so you don't have to.")

    import db_adapter as _sq, json as _json
    conn = _sq.connect("research.db")

    try:
        analyses = pd.read_sql_query("""
            SELECT t.ticker, t.quarter, t.year, t.analyzed_at,
                   t.management_tone, t.guidance_direction, t.guidance_detail,
                   t.green_flags, t.red_flags, t.what_wasnt_said,
                   t.take_rate_comment, t.margin_comment, t.pricing_comment,
                   t.thesis_status, t.thesis_notes, t.analyst_pushback,
                   t.follow_up_questions, t.sentiment_score, t.overall_verdict,
                   f.quality_score, f.name
            FROM transcript_analysis t
            LEFT JOIN fundamentals f ON t.ticker = f.ticker
            ORDER BY t.analyzed_at DESC
        """, conn)
    except Exception:
        analyses = pd.DataFrame()
    conn.close()

    if analyses.empty:
        st.info("No transcript analyses yet.")
        st.markdown("**To populate this tab:**")
        st.code("pip install earningscall\npython3 transcript_analyzer.py --ticker ANET")
        st.markdown("Or get a free API Ninjas key at [api-ninjas.com](https://api-ninjas.com) for automatic transcript fetching.")
    else:
        # Summary metrics
        broken   = len(analyses[analyses["thesis_status"] == "broken"])
        at_risk  = len(analyses[analyses["thesis_status"] == "at_risk"])
        on_track = len(analyses[analyses["thesis_status"] == "on_track"])

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Calls analyzed", len(analyses))
        c2.metric("✅ Thesis on track", on_track)
        c3.metric("⚠️ At risk", at_risk)
        c4.metric("❌ Thesis broken", broken)

        st.markdown("---")

        tone_icons = {"confident":"✅","cautious":"🟡","defensive":"🟠","evasive":"🔴"}
        guidance_icons = {"raised":"📈","maintained":"➡️","lowered":"📉","withdrawn":"🚨","none":"—"}
        thesis_icons = {"on_track":"✅","at_risk":"⚠️","broken":"❌","insufficient_data":"—"}

        for _, row in analyses.iterrows():
            t_icon = tone_icons.get(row.get("management_tone",""), "?")
            g_icon = guidance_icons.get(row.get("guidance_direction",""), "?")
            th_icon = thesis_icons.get(row.get("thesis_status",""), "?")
            period = f"Q{row['quarter']} {row['year']}" if row.get("quarter") else ""

            with st.expander(
                f"{th_icon} **{row['ticker']}** {period}  ·  "
                f"Tone: {t_icon} {row.get('management_tone','?')}  ·  "
                f"Guidance: {g_icon} {row.get('guidance_direction','?')}"
            ):
                st.info(row.get("overall_verdict",""))

                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**✅ Green flags**")
                    try:
                        for f in _json.loads(row.get("green_flags") or "[]"):
                            st.markdown(f'<div class="flag-green">{f}</div>', unsafe_allow_html=True)
                    except Exception:
                        pass

                with col2:
                    st.markdown("**⚠️ Red flags**")
                    try:
                        for f in _json.loads(row.get("red_flags") or "[]"):
                            st.markdown(f'<div class="flag-red">{f}</div>', unsafe_allow_html=True)
                    except Exception:
                        pass

                if row.get("margin_comment"):
                    st.markdown(f"**Margin commentary:** {row['margin_comment']}")
                if row.get("take_rate_comment"):
                    st.markdown(f"**Take rate / pricing:** {row['take_rate_comment']}")
                if row.get("what_wasnt_said"):
                    st.markdown(f"**What wasn't said:** {row['what_wasnt_said']}")
                if row.get("analyst_pushback"):
                    st.markdown(f"**Analyst pushback:** {row['analyst_pushback']}")
                if row.get("thesis_notes"):
                    st.markdown(f"**Thesis status:** {th_icon} {row['thesis_notes']}")

                try:
                    questions = _json.loads(row.get("follow_up_questions") or "[]")
                    if questions:
                        st.markdown("**Questions to track next quarter:**")
                        for q in questions:
                            st.markdown(f"• {q}")
                except Exception:
                    pass

                st.caption(f"Analyzed: {str(row.get('analyzed_at',''))[:16]}  ·  Score: {row.get('quality_score','?')}/10")
