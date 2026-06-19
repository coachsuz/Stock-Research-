"""
Database Configuration
======================
Switches between SQLite (local dev) and Supabase (production)
based on the SUPABASE_URL environment variable.

Set these environment variables for production:
    export SUPABASE_URL=https://gkusithjxjutrxwzousl.supabase.co
    export SUPABASE_KEY=your_service_role_key
    export SUPABASE_DB_URL=postgresql://postgres.gkusithjxjutrxwzousl:PASSWORD@aws-1-us-west-2.pooler.supabase.com:5432/postgres
"""

import os
import sqlite3

# ── Configuration ─────────────────────────────────────────────────────────────

SQLITE_PATH    = "research.db"
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY", "")
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "")

USE_SUPABASE   = bool(SUPABASE_URL and SUPABASE_KEY)


# ── Connection helper ─────────────────────────────────────────────────────────

def get_connection():
    """
    Returns a database connection.
    Uses Supabase PostgreSQL if env vars are set, otherwise SQLite.
    """
    if USE_SUPABASE and SUPABASE_DB_URL:
        import psycopg2
        conn = psycopg2.connect(
            SUPABASE_DB_URL,
            sslmode='require',
            connect_timeout=10
        )
        conn.autocommit = False
        return conn, "postgres"
    else:
        conn = sqlite3.connect(SQLITE_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"


def get_db_type():
    return "postgres" if USE_SUPABASE else "sqlite"


def placeholder(n=1):
    """Return correct SQL placeholder for current DB type."""
    if USE_SUPABASE:
        return ", ".join(["%s"] * n)
    else:
        return ", ".join(["?"] * n)


def ph():
    """Single placeholder."""
    return "%s" if USE_SUPABASE else "?"


# ── Status ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn, db_type = get_connection()
    print(f"Connected to: {db_type}")
    if db_type == "postgres":
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM my_portfolio WHERE status='open'")
        count = cur.fetchone()[0]
        print(f"Open positions in Supabase: {count}")
    else:
        count = conn.execute("SELECT COUNT(*) FROM my_portfolio WHERE status='open'").fetchone()[0]
        print(f"Open positions in SQLite: {count}")
    conn.close()
