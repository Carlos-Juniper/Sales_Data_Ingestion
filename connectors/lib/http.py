"""
Shared HTTP session and secret-loading helpers.

make_session  — requests.Session with retry/backoff for 429 and 5xx.
get_secret    — load a value from the environment (or .env file in dev).
"""

from __future__ import annotations

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_DEFAULT_RETRIES = 3
_DEFAULT_BACKOFF = 1.5


def make_session(
    retries: int = _DEFAULT_RETRIES,
    backoff: float = _DEFAULT_BACKOFF,
) -> requests.Session:
    """Return a Session with retry logic wired up for both http and https."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_secret(name: str, required: bool = False) -> str | None:
    """
    Return the value of environment variable ``name``.

    Calls dotenv.load_dotenv() once (idempotent; no-op when no .env file
    exists — the Cloud Run case).  When required=True and the variable is
    absent, raises RuntimeError pointing at .env.example.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed; fall through to os.getenv

    value = os.getenv(name)
    if required and not value:
        raise RuntimeError(
            f"Required secret {name!r} is not set. "
            "In dev: copy .env.example -> .env and fill in the value. "
            "In Cloud Run: inject it as an env var in the service configuration."
        )
    return value
