"""
Plain SQL migration runner.

Tracks applied filenames in a public.schema_migrations table.
Applies any .sql files in db/migrations/ (sorted by name) that are not
already recorded there, each in its own transaction.

Usage:
    python db/run_migrations.py
    DATABASE_URL=postgresql://... python db/run_migrations.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Load .env so DATABASE_URL is available when run directly.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL is not set. Copy .env.example -> .env and fill it in.")

try:
    import psycopg
except ImportError:
    sys.exit("ERROR: psycopg not installed. Run: pip install 'psycopg[binary]'")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_CREATE_TRACKING = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def _applied(conn: "psycopg.Connection") -> set[str]:
    rows = conn.execute("SELECT filename FROM public.schema_migrations").fetchall()
    return {r[0] for r in rows}


def run() -> None:
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        print("No migration files found in", MIGRATIONS_DIR)
        return

    with psycopg.connect(DATABASE_URL) as conn:
        conn.autocommit = True
        conn.execute(_CREATE_TRACKING)

        done = _applied(conn)

        for path in sql_files:
            if path.name in done:
                print(f"  skip  {path.name}  (already applied)")
                continue

            print(f"  apply {path.name} ...", end=" ", flush=True)
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO public.schema_migrations (filename) VALUES (%s)",
                    (path.name,),
                )
            print("ok")

    print("Migrations complete.")


if __name__ == "__main__":
    run()
