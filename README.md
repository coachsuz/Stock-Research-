# Stock Research Pipeline

A fully automated stock research system: data ingestion, quality scoring,
13F cluster analysis, AI summarization, and a Streamlit dashboard.

## Quick start

```bash
pip install yfinance requests pandas anthropic streamlit plotly apscheduler

export ANTHROPIC_API_KEY=sk-ant-...
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # optional

python scheduler.py --now        # populate DB on first run
streamlit run dashboard.py       # launch dashboard
```

## Files

| File | Purpose |
|------|---------|
| `db.py` | Database schema + read/write helpers (SQLite) |
| `data_ingestion.py` | yfinance + FMP fetcher + 10-metric quality scorer |
| `filings_13f.py` | SEC EDGAR 13F parser + diff engine + cluster analysis |
| `ai_summarization.py` | Claude API: 10-K summary, earnings flags, thesis generator |
| `dashboard.py` | Streamlit dashboard (6 pages) |
| `scheduler.py` | Master orchestrator — runs all stages on schedule |

## Schedule (automatic)

| Stage | Cadence | What it does |
|-------|---------|--------------|
| Fundamentals | Daily 7:30 AM ET | Fetch financials, score all tickers |
| 13F filings | Every Monday | Parse new 13F filings, detect cluster buys |
| AI summaries | Every Sunday | Generate thesis for stocks scoring ≥7 |
| Alerts | Every 30 min | Dispatch unsent alerts to Slack/email |

## Customise

- Edit `WATCHLIST` in `scheduler.py` to add/remove tickers
- Edit `FUNDS` in `scheduler.py` to track different fund managers
- Edit `QUALITY_METRICS` in `data_ingestion.py` to adjust scoring thresholds
- Set `SLACK_WEBHOOK_URL` env var to receive alerts in Slack
