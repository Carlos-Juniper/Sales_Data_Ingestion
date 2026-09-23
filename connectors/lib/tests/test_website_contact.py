"""
Tests for lib/website_contact.py — robots.txt-respecting email crawler.

Strategy
--------
All HTTP is mocked via a MagicMock session (session.get(...)). time.sleep is
patched everywhere a crawl happens so the suite doesn't actually wait out
crawl-delays. No real network traffic occurs.

Two classes cover the module:
  TestRobotsCache             — robots.txt fetch/cache/can_fetch/crawl_delay
  TestExtractEmailsFromWebsite — the mailto: crawl loop
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.website_contact import RobotsCache, extract_emails_from_website

# ---------------------------------------------------------------- helpers


def _mock_response(text: str = "", status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


_ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"
_ROBOTS_DISALLOW_CONTACT = "User-agent: *\nDisallow: /contact\n"
_ROBOTS_WITH_CRAWL_DELAY = "User-agent: *\nCrawl-delay: 3\n"


def _html_with_mailto(*emails: str) -> str:
    links = "".join(f'<a href="mailto:{e}">Email us</a>' for e in emails)
    return f"<html><body>{links}</body></html>"


# ===========================================================================
# RobotsCache
# ===========================================================================


class TestRobotsCache:
    def test_can_fetch_true_when_robots_allows(self):
        session = MagicMock()
        session.get.return_value = _mock_response(_ROBOTS_ALLOW_ALL)
        cache = RobotsCache(session=session)

        assert cache.can_fetch("https://example.com/contact") is True

    def test_can_fetch_false_when_robots_disallows(self):
        session = MagicMock()
        session.get.return_value = _mock_response(_ROBOTS_DISALLOW_CONTACT)
        cache = RobotsCache(session=session)

        assert cache.can_fetch("https://example.com/contact") is False

    def test_can_fetch_true_when_robots_txt_missing(self):
        session = MagicMock()
        session.get.return_value = _mock_response("", status_code=404)
        cache = RobotsCache(session=session)

        assert cache.can_fetch("https://example.com/anything") is True

    def test_can_fetch_true_when_robots_fetch_raises(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("dns failure")
        cache = RobotsCache(session=session)

        assert cache.can_fetch("https://example.com/anything") is True

    def test_robots_txt_fetched_once_per_base_url(self):
        session = MagicMock()
        session.get.return_value = _mock_response(_ROBOTS_ALLOW_ALL)
        cache = RobotsCache(session=session)

        cache.can_fetch("https://example.com/contact")
        cache.can_fetch("https://example.com/about")
        cache.crawl_delay("https://example.com/")

        assert session.get.call_count == 1

    def test_different_base_urls_fetched_separately(self):
        session = MagicMock()
        session.get.return_value = _mock_response(_ROBOTS_ALLOW_ALL)
        cache = RobotsCache(session=session)

        cache.can_fetch("https://example.com/contact")
        cache.can_fetch("https://other.example.com/contact")

        assert session.get.call_count == 2

    def test_crawl_delay_parsed_when_present(self):
        session = MagicMock()
        session.get.return_value = _mock_response(_ROBOTS_WITH_CRAWL_DELAY)
        cache = RobotsCache(session=session)

        assert cache.crawl_delay("https://example.com/") == 3.0

    def test_crawl_delay_none_when_absent(self):
        session = MagicMock()
        session.get.return_value = _mock_response(_ROBOTS_ALLOW_ALL)
        cache = RobotsCache(session=session)

        assert cache.crawl_delay("https://example.com/") is None


# ===========================================================================
# extract_emails_from_website
# ===========================================================================


class TestExtractEmailsFromWebsite:
    def test_finds_email_on_contact_page(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),  # robots.txt
            _mock_response(_html_with_mailto("info@sunsetridgehoa.example.com")),  # /contact
        ]

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://sunsetridgehoa.example.com", session=session)

        assert emails == ["info@sunsetridgehoa.example.com"]

    def test_falls_through_paths_in_order(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),                    # robots.txt
            _mock_response(""),                                   # /contact — empty
            _mock_response("<html></html>"),                      # /contact-us — no mailto
            _mock_response(_html_with_mailto("board@example.com")),  # /about — found
        ]

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://example.com", session=session)

        assert emails == ["board@example.com"]
        # robots + 3 page fetches (contact, contact-us, about) = 4 calls.
        assert session.get.call_count == 4

    def test_returns_empty_list_when_no_page_has_mailto(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),
            _mock_response("<html></html>"),
            _mock_response("<html></html>"),
            _mock_response("<html></html>"),
            _mock_response("<html></html>"),
        ]

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://example.com", session=session)

        assert emails == []

    def test_spam_prefixes_filtered_out(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),
            _mock_response(_html_with_mailto("noreply@example.com", "donotreply@example.com", "real@example.com")),
        ]

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://example.com", session=session)

        assert emails == ["real@example.com"]

    def test_duplicate_emails_deduplicated_preserving_order(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),
            _mock_response(_html_with_mailto("a@example.com", "b@example.com", "a@example.com")),
        ]

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://example.com", session=session)

        assert emails == ["a@example.com", "b@example.com"]

    def test_skips_path_disallowed_by_robots(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_DISALLOW_CONTACT),
            # /contact is skipped (robots disallow) — next call is /contact-us
            _mock_response(_html_with_mailto("info@example.com")),
        ]

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://example.com", session=session)

        assert emails == ["info@example.com"]
        assert session.get.call_count == 2

    def test_page_fetch_failure_continues_to_next_path(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),
            requests.ConnectionError("timeout"),
            _mock_response(_html_with_mailto("info@example.com")),
        ]

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://example.com", session=session)

        assert emails == ["info@example.com"]

    def test_sleeps_at_least_min_delay_between_requests(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),
            _mock_response(_html_with_mailto("info@example.com")),
        ]

        with patch("lib.website_contact.time.sleep") as mock_sleep:
            extract_emails_from_website("https://example.com", session=session, min_delay=2.5)

        mock_sleep.assert_called_once_with(2.5)

    def test_sleeps_crawl_delay_when_longer_than_min_delay(self):
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_WITH_CRAWL_DELAY),  # crawl-delay: 3
            _mock_response(_html_with_mailto("info@example.com")),
        ]

        with patch("lib.website_contact.time.sleep") as mock_sleep:
            extract_emails_from_website("https://example.com", session=session, min_delay=1.0)

        mock_sleep.assert_called_once_with(3.0)

    def test_shared_robots_cache_is_reused(self):
        """Passing a pre-built RobotsCache avoids re-fetching robots.txt."""
        session = MagicMock()
        session.get.side_effect = [
            _mock_response(_ROBOTS_ALLOW_ALL),
            _mock_response(_html_with_mailto("a@example.com")),
        ]
        cache = RobotsCache(session=session)
        # Warm the cache before the crawl.
        cache.can_fetch("https://example.com/contact")

        with patch("lib.website_contact.time.sleep"):
            emails = extract_emails_from_website("https://example.com", session=session, robots=cache)

        assert emails == ["a@example.com"]
        # robots.txt fetched once (during warm-up), plus one page fetch.
        assert session.get.call_count == 2
