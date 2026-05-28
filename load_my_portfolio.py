"""Load all portfolio positions into research.db"""
import sys
sys.path.insert(0, '.')
from my_portfolio import init_my_portfolio_db, add_position

init_my_portfolio_db()

positions = [
    ("MU",    30.29,  23),
    ("AMD",   136.65, 25),
    ("LRCX",  118.88, 37),
    ("KLAC",  786.86, 3),
    ("AMAT",  189.15, 13),
    ("ADI",   208.55, 11),
    ("VISN",  6.10,   1000),
    ("TXN",   179.20, 13),
    ("BE",    157.00, 11),
    ("AVGO",  297.43, 32),
    ("LITE",  657.00, 6),
    ("GOOGL", 294.10, 5),
    ("NVDA",  174.78, 136),
    ("NEE",   71.50,  250),
    ("TSLA",  337.35, 71),
    ("PLTR",  133.94, 16),
    ("ACHR",  6.00,   100),
    ("CBRS",  312.00, 10),
    ("CSTM",  33.00,  50),
    ("CGNT",  10.75,  250),
    ("ICHR",  74.48,  50),
    ("VST",   160.00, 50),
    ("CRDO",  200.00, 3),
    ("LUXE",  8.80,   500),
]

for ticker, avg_cost, shares in positions:
    add_position(ticker, avg_cost, shares)

print(f"\nLoaded {len(positions)} positions into My Portfolio.")
