"""
Stock Research Pipeline — First Run Setup
Run this once after any database reset to initialize all tables.
python3 setup.py
"""
import db_adapter as sqlite3

DB_PATH = "research.db"

print("Initializing all database tables...")

from db import init_db
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

print("All tables ready.")
print()
print("Next steps:")
print("  python3 discover.py --top 25      # populate watchlist")
print("  python3 market_signals.py         # earnings, short interest")
print("  python3 load_insiders.py          # insider transactions")
print("  python3 valuation.py              # valuations")
print("  python3 quality_checks.py         # margin quality")
print("  python3 load_my_portfolio.py      # your real holdings")
print("  python3 -m streamlit run dashboard.py  # launch dashboard")
