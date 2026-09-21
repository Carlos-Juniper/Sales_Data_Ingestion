"""Unit tests for lib.db._normalize_sqlalchemy_url — no live DB required.

Guards the fix for the psycopg2-vs-psycopg3 dialect blocker: SQLAlchemy must
receive a ``postgresql+psycopg://`` URL even though DATABASE_URL is stored in
the bare libpq form that db/run_migrations.py needs.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.db import _normalize_sqlalchemy_url


def test_bare_postgresql_gets_psycopg_driver():
    out = _normalize_sqlalchemy_url("postgresql://u:p@localhost:5432/db")
    assert out == "postgresql+psycopg://u:p@localhost:5432/db"


def test_postgres_alias_gets_psycopg_driver():
    out = _normalize_sqlalchemy_url("postgres://u:p@localhost/db")
    assert out == "postgresql+psycopg://u:p@localhost/db"


def test_explicit_psycopg_driver_is_untouched():
    url = "postgresql+psycopg://u:p@localhost/db"
    assert _normalize_sqlalchemy_url(url) == url


def test_explicit_psycopg2_driver_is_respected():
    url = "postgresql+psycopg2://u:p@localhost/db"
    assert _normalize_sqlalchemy_url(url) == url


def test_encoded_password_is_preserved():
    # '/' url-encoded as %2F must survive untouched.
    url = "postgresql://ingest_app:ab%2Fcd@localhost:5432/ingestion"
    assert _normalize_sqlalchemy_url(url) == (
        "postgresql+psycopg://ingest_app:ab%2Fcd@localhost:5432/ingestion"
    )


def test_non_postgres_scheme_is_unchanged():
    url = "sqlite:///tmp/x.db"
    assert _normalize_sqlalchemy_url(url) == url
