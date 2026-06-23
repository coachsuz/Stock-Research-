
import db_adapter as sqlite3
import yfinance as yf, time
from datetime import datetime

conn = sqlite3.connect('research.db')

# Clear bad data
conn.execute("DELETE FROM insider_transactions")
conn.commit()

tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM fundamentals").fetchall()]
print(f"Fetching insider transactions for {len(tickers)} stocks...")

for ticker in tickers:
    try:
        t   = yf.Ticker(ticker)
        ins = t.insider_transactions
        if ins is None or ins.empty:
            print(f"  {ticker} — no data")
            continue

        # Print columns on first ticker so we can see them
        if ticker == tickers[0]:
            print(f"  Columns: {ins.columns.tolist()}")

        count = 0
        for _, row in ins.iterrows():
            shares = 0
            value  = 0
            date   = ""
            name   = ""
            title  = ""
            txtype = ""

            # Try all known column name variants
            for col in ins.columns:
                cl = col.lower()
                if cl == "shares":          shares = row[col] or 0
                elif cl == "value":         value  = row[col] or 0
                elif "date" in cl:          date   = str(row[col])
                elif cl == "insider":       name   = str(row[col])
                elif cl == "position":      title  = str(row[col])
                elif cl == "relation":      title  = str(row[col])
                elif cl == "transaction":   txtype = str(row[col])
                elif cl == "text":          txtype = str(row[col])

            price = round(value / shares, 2) if shares else 0

            conn.execute("""
                INSERT OR IGNORE INTO insider_transactions
                (ticker, fetched_at, transaction_date, filer_name, filer_title,
                 transaction_type, shares, price, value)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (ticker, datetime.utcnow().isoformat(), date, name, title, txtype, shares, price, value))
            count += 1

        conn.commit()
        print(f"  {ticker} — {count} transactions")
        time.sleep(0.3)

    except Exception as e:
        print(f"  {ticker} failed: {e}")

conn.close()
print("Done")
