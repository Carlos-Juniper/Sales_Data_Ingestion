"""
Integration test fixtures — require a live Postgres instance.

Run with:
    docker compose up -d postgres
    python db/run_migrations.py
    ALLOW_DB_INTEGRATION_TESTS=1 pytest -m integration connectors/tests/integration/

The ALLOW_DB_INTEGRATION_TESTS opt-in is required: these tests run DDL and
DROP TABLE, and DATABASE_URL may point at the Cloud SQL Auth Proxy.
"""

import os
import sys

import pytest

# connectors/ — for `from lib import ...`
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
# Also add connectors/lib's parent so `from lib.db import ...` resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require a live Postgres instance",
    )


def _host_of(db_url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(db_url).hostname or "").lower()


def _is_localhost(db_url: str) -> bool:
    return _host_of(db_url) in ("localhost", "127.0.0.1", "::1")


@pytest.fixture(scope="session")
def engine():
    """SQLAlchemy engine pointing at the docker-compose Postgres.

    These tests run DDL and DROP TABLE, so they must never touch a real
    instance. A localhost check is NOT sufficient: the Cloud SQL Auth Proxy
    also listens on 127.0.0.1, so a repointed DATABASE_URL would look local
    while hitting production. Require an explicit opt-in env var as the real
    gate, with the localhost check as defence in depth.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if os.environ.get("ALLOW_DB_INTEGRATION_TESTS", "").lower() not in ("1", "true", "yes"):
        pytest.skip(
            "DB integration tests are opt-in (they run DDL/DROP TABLE). "
            "Set ALLOW_DB_INTEGRATION_TESTS=1 and point DATABASE_URL at your "
            "local docker Postgres (port 5432) — NOT the Cloud SQL proxy."
        )

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set — skipping integration tests")

    if not _is_localhost(db_url):
        pytest.skip(
            f"Refusing to run DDL integration tests against non-local host in "
            f"DATABASE_URL ({_host_of(db_url)!r}). Point it at localhost."
        )

    from lib.db import get_engine
    eng = get_engine()

    # Verify connectivity before any test runs.
    try:
        from sqlalchemy import text
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Cannot connect to Postgres ({exc}) — is docker compose up?")

    return eng
