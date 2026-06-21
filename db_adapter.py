"""
Database Adapter — SQLite-compatible interface for Supabase/PostgreSQL
========================================================================
This lets existing code written for sqlite3 work unchanged against
Supabase Postgres, by translating '?' placeholders to '%s' and
wrapping psycopg2 connections to behave like sqlite3 connections.

Usage (drop-in replacement):
    import db_adapter as sqlite3
    conn = sqlite3.connect(DB_PATH)   # works locally (SQLite) or on cloud (Postgres)
"""

import os
import re
import sqlite3 as _sqlite3

SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "")
USE_POSTGRES = bool(SUPABASE_DB_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras


def _translate_query(query: str) -> str:
    """
    Convert SQLite syntax to Postgres-compatible syntax:
    - '?' placeholders to '%s' (but escape literal '%' first, e.g. in LIKE clauses)
    - 'INTEGER PRIMARY KEY AUTOINCREMENT' to 'SERIAL PRIMARY KEY'
    - bare 'AUTOINCREMENT' removed (Postgres doesn't use it)
    """
    if not USE_POSTGRES:
        return query

    # Escape literal % first (e.g. LIKE '%Purchase%') so psycopg2 doesn't
    # misinterpret them as format specifiers when substituting %s params.
    query = query.replace("%", "%%")

    # Now safely convert '?' placeholders to '%s'
    query = query.replace("?", "%s")

    # SQLite -> Postgres schema syntax differences
    query = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "SERIAL PRIMARY KEY",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\bAUTOINCREMENT\b", "", query, flags=re.IGNORECASE)

    return query


class _CursorWrapper:
    """Wraps a psycopg2 cursor to behave like sqlite3 cursor."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        query = _translate_query(query)
        if params is None:
            return self._cursor.execute(query)
        return self._cursor.execute(query, params)

    def executemany(self, query, seq_of_params):
        query = _translate_query(query)
        return self._cursor.executemany(query, seq_of_params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        if size is None:
            return self._cursor.fetchmany()
        return self._cursor.fetchmany(size)

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        try:
            return self._cursor.fetchone()[0]
        except Exception:
            return None

    def close(self):
        self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)


class _ConnectionWrapper:
    """Wraps a psycopg2 connection to behave like sqlite3 connection."""

    def __init__(self, conn):
        self._conn = conn
        self.row_factory = None  # for API compatibility

    def cursor(self):
        # Plain cursor returns tuples — matches sqlite3 default behavior
        # used throughout the existing codebase (tuple unpacking, pandas).
        cur = self._conn.cursor()
        return _CursorWrapper(cur)

    def execute(self, query, params=None):
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    @property
    def total_changes(self):
        return 0  # not tracked the same way in postgres; safe default


def connect(db_path=None, timeout=30):
    """
    Drop-in replacement for sqlite3.connect().
    Uses Supabase Postgres if SUPABASE_DB_URL is set, otherwise local SQLite.
    """
    if USE_POSTGRES:
        raw_conn = psycopg2.connect(
            SUPABASE_DB_URL,
            sslmode="require",
            connect_timeout=timeout,
        )
        return _ConnectionWrapper(raw_conn)
    else:
        conn = _sqlite3.connect(db_path or "research.db", timeout=timeout)
        conn.row_factory = _sqlite3.Row
        return conn


# Re-export Row for compatibility with code that does `sqlite3.Row`
Row = _sqlite3.Row if not USE_POSTGRES else dict


# ── Status check ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = connect()
    print(f"Connected via: {'Postgres (Supabase)' if USE_POSTGRES else 'SQLite (local)'}")
    cur = conn.execute("SELECT COUNT(*) FROM my_portfolio")
    row = cur.fetchone()
    count = row[0] if not USE_POSTGRES else row["count"]
    print(f"my_portfolio rows: {count}")
    conn.close()
