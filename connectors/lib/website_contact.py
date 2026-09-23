"""
Website contact-email crawling with robots.txt compliance — synchronous.

Ported from ``juniper-crm-shared/scrapers/base.py``'s ``BaseScraper``: only
two pieces are taken, per the porting task's scope —

  - ``_extract_emails_from_website``: crawls a site's ``/contact``,
    ``/contact-us``, ``/about``, then ``/``, for ``mailto:`` links, filtering
    ``noreply@``/``donotreply@`` and de-duplicating while preserving order.
  - the robots.txt-respecting rate limiter (``_fetch_robots`` / ``_can_fetch``
    / crawl-delay caching per base URL).

Everything else in that prototype file (Playwright browser lifecycle, HTML
snapshotting, the generic ``_get_html``/``_get_json`` helpers) is out of scope
here — see the porting task notes — and is not reproduced.

Adapted from ``httpx`` + ``asyncio`` to ``requests`` + stdlib
``urllib.robotparser`` (both already synchronous), matching this repo's
``requests``/``ThreadPoolExecutor`` connector style. The per-base-URL robots
cache is guarded by a ``threading.Lock`` so concurrent worker threads share
one fetch per host instead of racing to re-fetch the same robots.txt.
"""

from __future__ import annotations

import threading
import time
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

_SPAM_EMAIL_PREFIXES = ("noreply@", "donotreply@")

# Same path order as the prototype: try the most likely contact pages first,
# fall back to the homepage last.
_CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/")

_DEFAULT_TIMEOUT = 15


class RobotsCache:
    """Thread-safe per-base-URL robots.txt cache with crawl-delay lookup.

    Ported from ``BaseScraper._fetch_robots`` / ``_can_fetch`` / the
    ``_crawl_delay_cache`` dict. A missing or unreachable robots.txt is
    treated as "allow everything" (``rp.allow_all = True``), same as the
    prototype — matching how a browser would behave rather than failing shut.
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._robots: dict[str, RobotFileParser] = {}
        self._crawl_delay: dict[str, Optional[float]] = {}

    def _fetch(self, base_url: str) -> RobotFileParser:
        with self._lock:
            cached = self._robots.get(base_url)
            if cached is not None:
                return cached

            robots_url = f"{base_url.rstrip('/')}/robots.txt"
            rp = RobotFileParser()
            rp.set_url(robots_url)
            try:
                resp = self.session.get(robots_url, timeout=_DEFAULT_TIMEOUT)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.allow_all = True
            except requests.RequestException:
                rp.allow_all = True

            self._robots[base_url] = rp
            delay = rp.crawl_delay("*")
            self._crawl_delay[base_url] = float(delay) if delay is not None else None
            return rp

    def can_fetch(self, url: str) -> bool:
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._fetch(base_url)
        return rp.can_fetch("*", url)

    def crawl_delay(self, url: str) -> Optional[float]:
        """Return the site's declared crawl-delay in seconds, or None if it
        doesn't declare one. Fetches/caches robots.txt as a side effect."""
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._fetch(base_url)
        return self._crawl_delay.get(base_url)


def extract_emails_from_website(
    website_url: str,
    session: requests.Session | None = None,
    robots: RobotsCache | None = None,
    min_delay: float = 1.0,
) -> list[str]:
    """
    Crawl a business website's contact-ish pages for ``mailto:`` email
    addresses, stopping at the first page that yields at least one.

    Tries ``/contact``, ``/contact-us``, ``/about``, then the homepage, in
    that order — ported unchanged from the prototype's
    ``_extract_emails_from_website``.

    Respects robots.txt via ``robots`` (pass a shared ``RobotsCache`` when
    calling this across many threads/websites so a given host's robots.txt is
    only fetched once) and sleeps at least ``min_delay`` seconds — or the
    site's declared crawl-delay, whichever is longer — between each page
    request, mirroring the prototype's ``_delay()``.

    Returns a de-duplicated list of lowercased email strings (first-seen
    order preserved). Returns ``[]`` on any failure or when no page yields a
    ``mailto:`` link — this never raises for the ordinary "no contact page"
    outcome; callers that want to distinguish "not found" from "request
    failed" should catch exceptions around the network calls themselves, this
    function already swallows them per-path so a single dead page doesn't
    abort the whole crawl.
    """
    sess = session or requests.Session()
    rc = robots or RobotsCache(session=sess)
    parsed = urlparse(website_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    for path in _CONTACT_PATHS:
        url = base_url + path
        if not rc.can_fetch(url):
            continue

        delay = rc.crawl_delay(url)
        time.sleep(max(min_delay, delay or 0.0))

        try:
            resp = sess.get(url, timeout=_DEFAULT_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except requests.RequestException:
            continue

        if not html:
            continue

        soup = BeautifulSoup(html, "html.parser")
        emails: list[str] = []
        for link in soup.select("a[href^='mailto:']"):
            raw = link.get("href", "").replace("mailto:", "").split("?")[0].strip().lower()
            if raw and not raw.startswith(_SPAM_EMAIL_PREFIXES):
                emails.append(raw)

        if emails:
            return list(dict.fromkeys(emails))

    return []
