"""
Stock Research Pipeline — Master Scheduler
==========================================
Run modes:
    python scheduler.py           # Persistent scheduler (runs forever)
    python scheduler.py --now     # Run all stages once, then exit
    python scheduler.py --stage fundamentals
    python scheduler.py --stage filings_13f
    python scheduler.py --stage ai_summaries
"""

import argparse, logging, os, sys, time
from datetime import datetime

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler("logs/pipeline.log"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pipeline")

WATCHLIST = ["AXON","CRWD","MELI","NVO","MNDY","CELH","ASTS","CAVA","IOT","APP"]
FUNDS     = ["Coatue Management","Tiger Global Management","Akre Capital Management",
             "Pershing Square Capital Management","Baillie Gifford"]

def run_fundamentals():
    log.info("STAGE: Fundamentals")
    from db import init_db, upsert_fundamentals, log_alert
    from data_ingestion import fetch_ticker, score_quality
    from market_signals import init_signals_db
    from quality_checks import init_quality_db
    from portfolio import init_portfolio_db
    from my_portfolio import init_my_portfolio_db
    from scorecard import init_scorecard_db
    from valuation import init_valuation_db
    from transcript_analyzer import init_transcript_db
    init_db()
    init_signals_db()
    init_quality_db()
    init_portfolio_db()
    init_my_portfolio_db()
    init_scorecard_db()
    init_valuation_db()
    init_transcript_db()
    for ticker in WATCHLIST:
        try:
            data  = fetch_ticker(ticker, use_fmp=True)
            score = score_quality(data)
            upsert_fundamentals(data, score)
            if score["score"] >= 8:
                log_alert(ticker, "quality_threshold",
                          f"{ticker} scored {score['score']}/10 — {score['verdict']}", "high")
            log.info(f"  {ticker}: {score['score']}/10 — {score['verdict']}")
            time.sleep(0.5)
        except Exception as e:
            log.error(f"  {ticker} failed: {e}")

def run_filings():
    log.info("STAGE: 13F filings")
    from db import init_db
    from filings_13f import run_13f_pipeline, get_cluster_buys
    init_db()
    try:
        new = run_13f_pipeline(FUNDS, delay=0.6)
        cb  = get_cluster_buys(min_funds=2)
        log.info(f"13F complete. New: {len(new)}, Clusters: {len(cb)}")
    except Exception as e:
        log.error(f"13F failed: {e}")

def run_ai():
    log.info("STAGE: AI summarization")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — skipping.")
        return
    from db import init_db, get_watchlist_scores
    from ai_summarization import run_ai_pipeline
    init_db()
    try:
        df = get_watchlist_scores(WATCHLIST)
        if df.empty:
            log.info("No scores yet — run fundamentals first.")
            return
        run_ai_pipeline(df.to_dict(orient="records"), delay=1.5)
        log.info("AI summarization complete.")
    except Exception as e:
        log.error(f"AI stage failed: {e}")

def dispatch_alerts():
    from db import get_unsent_alerts, mark_alerts_sent
    alerts = get_unsent_alerts()
    if not alerts:
        return
    log.info(f"Dispatching {len(alerts)} alert(s)...")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        import requests
        for a in alerts:
            icon = {"high":"🔴","medium":"🟡","low":"🟢"}.get(a["severity"],"⚪")
            try:
                requests.post(slack_url, json={"text": f"{icon} *{a['ticker']}* — {a['message']}"}, timeout=5)
            except Exception as e:
                log.warning(f"Slack failed: {e}")
    mark_alerts_sent([a["id"] for a in alerts])
    log.info(f"  {len(alerts)} alert(s) dispatched.")

def run_all():
    log.info("Pipeline starting — full run")
    start = time.time()
    run_fundamentals(); dispatch_alerts()
    if datetime.now().weekday() == 0:   # Monday
        run_filings(); dispatch_alerts()
    if datetime.now().weekday() == 6:   # Sunday
        run_ai(); dispatch_alerts()
    log.info(f"Pipeline complete in {round(time.time()-start,1)}s")

def start_scheduler():
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.error("Run: pip install apscheduler"); sys.exit(1)
    s = BlockingScheduler(timezone="America/New_York")
    s.add_job(run_fundamentals, CronTrigger(day_of_week="mon-fri", hour=7, minute=30), id="fund")
    s.add_job(run_filings,      CronTrigger(day_of_week="mon",     hour=8, minute=0),  id="13f")
    s.add_job(run_ai,           CronTrigger(day_of_week="sun",     hour=9, minute=0),  id="ai")
    s.add_job(dispatch_alerts,  CronTrigger(day_of_week="mon-fri", hour="7-17", minute="*/30"), id="alerts")
    log.info("Scheduler running. Ctrl+C to stop.")
    try:
        s.start()
    except KeyboardInterrupt:
        log.info("Stopped.")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--now",   action="store_true")
    p.add_argument("--stage", choices=["fundamentals","filings_13f","ai_summaries","alerts"])
    args = p.parse_args()
    {"fundamentals": lambda: (run_fundamentals(), dispatch_alerts()),
     "filings_13f":  lambda: (run_filings(),      dispatch_alerts()),
     "ai_summaries": lambda: (run_ai(),            dispatch_alerts()),
     "alerts":       dispatch_alerts,
    }.get(args.stage, run_all if args.now else start_scheduler)()
