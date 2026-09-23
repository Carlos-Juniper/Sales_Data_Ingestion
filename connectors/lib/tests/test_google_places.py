"""
Tests for lib/google_places.py — Places API (New) Text Search client.

Strategy
--------
All HTTP is mocked via unittest.mock.patch on requests.Session.post (the
client always calls self.session.post(...), so patching the instance's
session — or the Session class — intercepts every call regardless of how
GooglePlacesClient is constructed). tenacity's own sleep (tenacity.nap.sleep,
which wraps time.sleep) is patched in the one test that exercises a real
retry, so the suite stays fast.

Five classes cover the module:
  TestIsRetryableHttpError — the retry predicate
  TestBestMatch             — the scoring/disambiguation logic
  TestRateLimiter           — pacing behavior
  TestTextSearch            — request shape + retry wiring
  TestLookupBusiness        — found/not-found/error behavior
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.google_places import (
    GooglePlacesClient,
    RateLimiter,
    _is_retryable_http_error,
    best_match,
)

# ---------------------------------------------------------------- helpers


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if status_code >= 400:
        err = requests.HTTPError(f"{status_code} error")
        err.response = resp
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _place(name: str, address: str = "", phone: str | None = None, website: str | None = None, place_id: str = "p1") -> dict:
    return {
        "id": place_id,
        "displayName": {"text": name},
        "formattedAddress": address,
        "internationalPhoneNumber": phone,
        "websiteUri": website,
    }


# ===========================================================================
# _is_retryable_http_error
# ===========================================================================


class TestIsRetryableHttpError:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_retryable_statuses_return_true(self, status):
        resp = MagicMock(status_code=status)
        exc = requests.HTTPError(response=resp)
        assert _is_retryable_http_error(exc) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_non_retryable_statuses_return_false(self, status):
        resp = MagicMock(status_code=status)
        exc = requests.HTTPError(response=resp)
        assert _is_retryable_http_error(exc) is False

    def test_non_http_error_returns_false(self):
        assert _is_retryable_http_error(requests.ConnectionError("boom")) is False

    def test_http_error_with_no_response_returns_false(self):
        exc = requests.HTTPError()
        exc.response = None
        assert _is_retryable_http_error(exc) is False


# ===========================================================================
# best_match
# ===========================================================================


class TestBestMatch:
    def test_single_result_returned_without_scoring(self):
        places = [_place("Anything At All")]
        result = best_match(places, "Sunset Ridge HOA", "Houston", "TX")
        assert result is places[0]

    def test_name_token_overlap_wins(self):
        places = [
            _place("Totally Unrelated Business", address="Houston TX 77001"),
            _place("Sunset Ridge Homeowners Association", address="Houston TX 77001"),
        ]
        result = best_match(places, "Sunset Ridge Homeowners Association", "Houston", "TX")
        assert result["id"] == places[1]["id"]

    def test_zip_match_breaks_a_tie(self):
        places = [
            _place("Sunset Ridge", address="Dallas TX 75201"),
            _place("Sunset Ridge", address="Houston TX 77001"),
        ]
        result = best_match(places, "Sunset Ridge", "Houston", "TX", zip_code="77001")
        assert result["formattedAddress"] == "Houston TX 77001"

    def test_city_match_scores_above_state_only_match(self):
        places = [
            _place("Sunset Ridge Assoc", address="Some Other City TX"),
            _place("Sunset Ridge Assoc", address="Houston TX"),
        ]
        result = best_match(places, "Sunset Ridge Assoc", "Houston", "TX")
        assert result["formattedAddress"] == "Houston TX"

    def test_street_first_token_contributes_to_score(self):
        places = [
            _place("Sunset Ridge Assoc", address="Houston TX 77001"),
            _place("Sunset Ridge Assoc", address="500 Main St Houston TX 77001"),
        ]
        result = best_match(
            places, "Sunset Ridge Assoc", "Houston", "TX",
            zip_code="77001", street="500 Main St",
        )
        assert result["formattedAddress"].startswith("500 Main St")

    def test_noise_words_excluded_from_name_tokens(self):
        # "The", "Inc", "Assoc" are noise words — with them excluded, neither
        # candidate has a token to overlap on, so scoring falls through to
        # location signals (here, only the second has a matching city).
        places = [
            _place("The Assoc Inc", address="Nowhere TX"),
            _place("The Assoc Inc", address="Houston TX"),
        ]
        result = best_match(places, "The Assoc Inc", "Houston", "TX")
        assert result["formattedAddress"] == "Houston TX"

    def test_all_zero_scores_falls_back_to_first(self):
        places = [
            _place("Alpha", address="Nowhere"),
            _place("Beta", address="Elsewhere"),
        ]
        result = best_match(places, "Sunset Ridge Homeowners Association", "Houston", "TX")
        assert result["id"] == places[0]["id"]

    def test_missing_display_name_does_not_raise(self):
        places = [
            {"id": "a", "formattedAddress": "Houston TX"},
            _place("Sunset Ridge Assoc", address="Houston TX"),
        ]
        result = best_match(places, "Sunset Ridge Assoc", "Houston", "TX")
        assert result["id"] == places[1]["id"]


# ===========================================================================
# RateLimiter
# ===========================================================================


class TestRateLimiter:
    def test_first_call_does_not_sleep(self):
        limiter = RateLimiter(pause=1.0)
        with patch("lib.google_places.time.sleep") as mock_sleep:
            limiter.wait()
        mock_sleep.assert_not_called()

    def test_sleeps_remaining_gap_when_pause_not_elapsed(self):
        limiter = RateLimiter(pause=1.0)
        limiter._last_fire = 100.0  # pretend a previous wait() fired at t=100
        with patch("lib.google_places.time.monotonic", return_value=100.2):
            with patch("lib.google_places.time.sleep") as mock_sleep:
                limiter.wait()
        # gap = last_fire(100.0) + pause(1.0) - now(100.2) = 0.8
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == pytest.approx(0.8)

    def test_no_sleep_when_pause_already_elapsed(self):
        limiter = RateLimiter(pause=1.0)
        limiter._last_fire = 100.0
        with patch("lib.google_places.time.monotonic", return_value=105.0):
            with patch("lib.google_places.time.sleep") as mock_sleep:
                limiter.wait()
        mock_sleep.assert_not_called()


# ===========================================================================
# GooglePlacesClient._text_search
# ===========================================================================


class TestTextSearch:
    def test_posts_to_text_search_url(self):
        session = MagicMock()
        session.post.return_value = _mock_response({"places": []})
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        client._text_search("Sunset Ridge Houston TX")

        args, kwargs = session.post.call_args
        assert args[0] == "https://places.googleapis.com/v1/places:searchText"

    def test_request_body_carries_text_query(self):
        session = MagicMock()
        session.post.return_value = _mock_response({"places": []})
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        client._text_search("Sunset Ridge Houston TX")

        assert session.post.call_args.kwargs["json"] == {"textQuery": "Sunset Ridge Houston TX"}

    def test_auth_headers_include_api_key_and_field_mask(self):
        session = MagicMock()
        session.post.return_value = _mock_response({"places": []})
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        client._text_search("query")

        headers = session.post.call_args.kwargs["headers"]
        assert headers["X-Goog-Api-Key"] == "fake-key"
        assert "places.internationalPhoneNumber" in headers["X-Goog-FieldMask"]

    def test_page_token_added_to_body_and_sleeps(self):
        session = MagicMock()
        session.post.return_value = _mock_response({"places": []})
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        with patch("lib.google_places.time.sleep") as mock_sleep:
            client._text_search("query", page_token="tok123")

        assert session.post.call_args.kwargs["json"]["pageToken"] == "tok123"
        # Called at least once for the pageToken pre-delay (2.5s).
        assert any(call.args[0] == 2.5 for call in mock_sleep.call_args_list)

    def test_retries_on_429_then_succeeds(self):
        """End-to-end proof the tenacity wiring actually retries a 429."""
        session = MagicMock()
        session.post.side_effect = [
            _mock_response({}, status_code=429),
            _mock_response({"places": []}, status_code=200),
        ]
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        with patch("tenacity.nap.time.sleep"):
            result = client._text_search("query")

        assert result == {"places": []}
        assert session.post.call_count == 2

    def test_non_retryable_error_raises_immediately(self):
        session = MagicMock()
        session.post.return_value = _mock_response({}, status_code=403)
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        with pytest.raises(requests.HTTPError):
            client._text_search("query")

        assert session.post.call_count == 1


# ===========================================================================
# GooglePlacesClient.lookup_business
# ===========================================================================


class TestLookupBusiness:
    def test_no_results_returns_found_false(self):
        session = MagicMock()
        session.post.return_value = _mock_response({"places": []})
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        result = client.lookup_business("Sunset Ridge Assoc", "Houston", "TX")

        assert result["found"] is False
        assert result["phone"] is None
        assert result["website"] is None

    def test_single_result_populates_phone_website_place_id(self):
        session = MagicMock()
        session.post.return_value = _mock_response({
            "places": [_place(
                "Sunset Ridge Assoc",
                address="Houston TX 77001",
                phone="+18325551234",
                website="https://sunsetridgehoa.example.com",
                place_id="abc123",
            )]
        })
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        result = client.lookup_business("Sunset Ridge Assoc", "Houston", "TX", zip_code="77001")

        assert result == {
            "phone": "+18325551234",
            "website": "https://sunsetridgehoa.example.com",
            "place_id": "abc123",
            "found": True,
        }

    def test_request_exception_propagates(self):
        session = MagicMock()
        session.post.return_value = _mock_response({}, status_code=403)
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        with pytest.raises(requests.HTTPError):
            client.lookup_business("Sunset Ridge Assoc", "Houston", "TX")

    def test_query_combines_name_city_state(self):
        session = MagicMock()
        session.post.return_value = _mock_response({"places": []})
        client = GooglePlacesClient("fake-key", session=session, rate_pause=0)

        client.lookup_business("Sunset Ridge Assoc", "Houston", "TX")

        assert session.post.call_args.kwargs["json"]["textQuery"] == "Sunset Ridge Assoc Houston TX"
