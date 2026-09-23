"""
Google Places API (New) client — Text Search, synchronous.

Ported from ``juniper-crm-shared/scrapers/google_maps.py``'s
``GoogleMapsScraper`` (a prior-prototype repo, different flat-MySQL-backed
project). That scraper was ``httpx`` + ``asyncio``; this repo's connectors are
synchronous (``requests`` + ``ThreadPoolExecutor`` via
``lib/enrich_runner.py``, see ``deathcare/irs_990_enrich.py`` for the closest
existing analog), so the port swaps:

  - ``httpx.AsyncClient``          -> a plain ``requests.Session``
  - ``asyncio.sleep`` pacing        -> ``RateLimiter`` (``threading.Lock`` +
                                       ``time.monotonic()``), same
                                       "sleep until last_fire + pause has
                                       elapsed" logic as the prototype's
                                       ``orchestrate_pipeline.run_gmaps``
  - ``tenacity.retry`` on the async method -> the same decorator on a sync
    method; tenacity supports both natively, so the retry predicate, backoff
    schedule, and field mask below are ported unchanged.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search

Deliberately not ported here: the prototype's ``lookup_business`` swallowed
request failures and returned an all-``None`` dict. This connector's caller
(``hoa/hoa_gmaps_enrich.py``) needs to distinguish "Places returned zero
results" (``not_found``) from "the request itself failed after retries"
(``error``, with a captured ``error_detail``) — two different values in the
closed ``enrich_status`` enum (``lib/enums.py``) — so ``lookup_business``
here lets request exceptions propagate instead of swallowing them.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Field mask ported verbatim from the prototype — only the fields the
# HOA/cemetery lookup actually consumes (phone, website, id) plus enough
# address/name context for best_match() to score candidates.
_SEARCH_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.addressComponents",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.types",
    "places.rating",
    "nextPageToken",
])

# Corporate-suffix / filler tokens stripped from name matching, same list as
# the prototype's _best_match.
_NOISE_WORDS = {"INC", "LLC", "LTD", "CORP", "ASSN", "CONDO", "ASSOC", "THE", "AND"}


def _is_retryable_http_error(exc: BaseException) -> bool:
    """True for HTTP 429/5xx — the transient statuses worth retrying.

    Ported from the prototype's ``_is_retryable``. Anything else (4xx other
    than 429, connection errors, timeouts) is not retried here — tenacity
    ``reraise=True`` means a non-retryable exception surfaces to the caller
    immediately rather than after ``stop_after_attempt`` re-raises.
    """
    return (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code in (429, 500, 502, 503, 504)
    )


def best_match(
    places: list[dict],
    name: str,
    city: str,
    state: str,
    zip_code: str = "",
    street: str = "",
) -> dict:
    """
    Score each candidate Places result against the known row data and return
    the best match. Falls back to the first result when every score ties
    (including at zero).

    Ported closely from the prototype's ``GoogleMapsScraper._best_match``.
    Scoring (cumulative):
      +4  significant word overlap between the queried name and displayName
      +3  ZIP code found in formattedAddress  (most precise location signal)
      +3  city found in formattedAddress       (more discriminating than state)
      +2  state abbreviation found in formattedAddress
      +1  first token of street address found in formattedAddress

    ``street`` is normally empty here — the TX TREC HOA canonical rows have
    no street address pre-PDF (see ``tx_trec_hoa.to_canonical()``) — but the
    parameter is kept so this function is reusable by anything that does
    have one (e.g. a future PDF-address-informed lookup, or the PA deathcare
    cemetery lookup described in the plan of action's §6.3 decision log).
    """
    if len(places) == 1:
        return places[0]

    name_tokens = {w for w in name.upper().split() if len(w) > 3 and w not in _NOISE_WORDS}

    def _score(place: dict) -> int:
        addr = (place.get("formattedAddress") or "").upper()
        display = ((place.get("displayName") or {}).get("text") or "").upper()
        score = 0
        if name_tokens and (name_tokens & set(display.split())):
            score += 4
        if city and city.upper() in addr:
            score += 3
        if state and state.upper() in addr:
            score += 2
        if zip_code and zip_code in addr:
            score += 3
        if street:
            first_token = street.split()[0].upper()
            if first_token and first_token in addr:
                score += 1
        return score

    return max(places, key=_score)


class RateLimiter:
    """Paces calls so consecutive fires start at least ``pause`` seconds apart.

    Thread-safe: share one instance across every worker thread in a
    ``ThreadPoolExecutor`` batch (see ``lib/enrich_runner.py``). Ported from
    the prototype's ``run_gmaps`` inline ``rate_lock`` + ``state["last_fire"]``
    pattern, generalized into a small reusable class since this repo has no
    single orchestrator module to hold that state inline.
    """

    def __init__(self, pause: float) -> None:
        self._pause = pause
        self._lock = threading.Lock()
        self._last_fire = 0.0

    def wait(self) -> None:
        """Block the calling thread until ``pause`` seconds have elapsed
        since the last call to ``wait()`` returned (across all threads)."""
        with self._lock:
            now = time.monotonic()
            gap = self._last_fire + self._pause - now
            if gap > 0:
                time.sleep(gap)
            self._last_fire = time.monotonic()


class GooglePlacesClient:
    """Synchronous client for the Places API "Text Search (New)" endpoint."""

    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        rate_pause: float = 0.2,
    ) -> None:
        self.api_key = api_key
        self.session = session or requests.Session()
        self._rate_limiter = RateLimiter(rate_pause)

    def _auth_headers(self) -> dict[str, str]:
        return {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": _SEARCH_FIELD_MASK,
            "Content-Type": "application/json",
        }

    @retry(
        retry=retry_if_exception(_is_retryable_http_error),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _text_search(self, query: str, page_token: str | None = None) -> dict:
        """POST one Text Search request. Raises requests.HTTPError/RequestException
        on failure (after retrying transient 429/5xx per the decorator above) —
        callers decide what a failure means for their own status bookkeeping."""
        body: dict[str, Any] = {"textQuery": query}
        if page_token:
            body["pageToken"] = page_token
            time.sleep(2.5)  # new API requires ~2s before a pageToken is valid
        self._rate_limiter.wait()
        resp = self.session.post(
            TEXT_SEARCH_URL,
            json=body,
            headers=self._auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def lookup_business(
        self,
        name: str,
        city: str,
        state: str,
        zip_code: str = "",
        street: str = "",
    ) -> dict[str, Any]:
        """
        Find a business by name + location via Text Search, disambiguating
        multiple candidates with ``best_match``.

        Returns ``{"phone": str|None, "website": str|None, "place_id": str|None,
        "found": bool}`` — ``found`` is False only when Places returned zero
        results. Raises ``requests.RequestException`` (after retries are
        exhausted) on a request failure — this deliberately does NOT swallow
        the exception; see the module docstring for why.
        """
        query = f"{name} {city} {state}".strip()
        data = self._text_search(query)

        places = data.get("places") or []
        if not places:
            return {"phone": None, "website": None, "place_id": None, "found": False}

        p = best_match(places, name, city, state, zip_code=zip_code, street=street)
        return {
            "phone": p.get("internationalPhoneNumber"),
            "website": p.get("websiteUri"),
            "place_id": p.get("id"),
            "found": True,
        }
