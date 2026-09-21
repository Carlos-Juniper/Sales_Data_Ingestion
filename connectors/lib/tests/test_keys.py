"""
Tests for lib.keys — the deterministic entity keys (D1).

These keys ARE the identity of every resolved row, and core.account.account_key
carries a UNIQUE index on them.  A change to the derivation re-identifies rows,
which the core 3-way diff reads as "old account gone, new account appeared".  The
tests therefore pin the exact hash formulas, not just their properties.
"""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.keys import (  # noqa: E402
    DEFAULT_ACCOUNT_KEY_PRIORITY,
    compute_account_key,
    compute_contact_key,
    compute_location_key,
    is_present,
    sha256_key,
)
from lib.normalize import normalize_name, normalize_zip  # noqa: E402


class TestIsPresent:
    @pytest.mark.parametrize("val", ["x", "0", 0, 1, 1.5, "  x  "])
    def test_present(self, val):
        assert is_present(val)

    @pytest.mark.parametrize("val", [None, "", "   ", float("nan")])
    def test_absent(self, val):
        assert not is_present(val)

    def test_the_string_nan_is_absent(self):
        """A pandas float column holding None becomes NaN, and str(NaN) is 'nan',
        so naive truthiness hashes missing data as a four-character string."""
        assert not is_present("nan")
        assert not is_present("NaN")


class TestSha256Key:
    def test_matches_hashlib(self):
        assert sha256_key("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_is_hex_of_expected_length(self):
        key = sha256_key("x")
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


class TestComputeAccountKey:
    def test_priority_order_is_respected(self):
        row = {"ccn": "C", "npi": "N", "geoid": "G", "ein": "E"}
        assert compute_account_key(row) == sha256_key("ccn:C")

    def test_falls_through_to_next_identifier(self):
        assert compute_account_key({"npi": "N", "ein": "E"}) == sha256_key("npi:N")

    def test_geoid_is_supported_for_parks(self):
        """Plan §5.1 names Census GEOID as the parks Tier-1 key."""
        assert "geoid" in DEFAULT_ACCOUNT_KEY_PRIORITY
        assert compute_account_key({"geoid": "3710740"}) == sha256_key("geoid:3710740")

    def test_restricted_priority_ignores_other_columns(self):
        row = {"ccn": "C", "geoid": "G"}
        assert compute_account_key(row, priority=("geoid",)) == sha256_key("geoid:G")

    def test_custom_identifier_via_priority(self):
        """Parks state agencies have no GEOID and key on a declared slug."""
        key = compute_account_key({"agency": "tpwd"}, priority=("agency",))
        assert key == sha256_key("agency:tpwd")

    def test_identifiers_are_stripped(self):
        assert compute_account_key({"ein": " 12-345 "}) == sha256_key("ein:12-345")

    def test_nan_identifier_does_not_become_a_key(self):
        row = {"geoid": float("nan"), "name_raw": "X"}
        assert compute_account_key(row) == compute_account_key({"name_raw": "X"})

    def test_fallback_matches_healthcare_formula(self):
        """normalize_parts=True must reproduce healthcare_pipeline byte for byte."""
        row = {"name_normalized": "Oak Grove Cemetery", "zip5": "07001-1234"}
        expected = sha256_key(
            f"name:{normalize_name('Oak Grove Cemetery')}"
            f"|zip:{normalize_zip('07001-1234')}"
        )
        assert compute_account_key(row, normalize_parts=True) == expected

    def test_fallback_matches_deathcare_formula(self):
        """normalize_parts=False must reproduce deathcare_merge byte for byte."""
        row = {"name_normalized": "OAK GROVE CEMETERY", "zip5": "07001"}
        expected = sha256_key("name:OAK GROVE CEMETERY|zip:07001")
        assert compute_account_key(row, normalize_parts=False) == expected

    def test_the_two_fallbacks_genuinely_differ(self):
        """Documents the known divergence between the two existing verticals."""
        row = {"name_normalized": "Oak Grove, Inc.", "zip5": "07001-1234"}
        assert (
            compute_account_key(row, normalize_parts=True)
            != compute_account_key(row, normalize_parts=False)
        )

    def test_name_raw_used_when_normalized_absent(self):
        assert (
            compute_account_key({"name_raw": "X"})
            == compute_account_key({"name_normalized": "X"})
        )

    def test_deterministic(self):
        row = {"geoid": "3710740"}
        assert compute_account_key(row) == compute_account_key(dict(row))

    def test_empty_row_still_yields_a_key(self):
        assert len(compute_account_key({})) == 64


class TestComputeLocationKey:
    def test_formula_is_stable(self):
        row = {"address_line_1": "1 Main St", "zip5": "07001"}
        expected = sha256_key(
            f"loc:ACC|{normalize_name('1 Main St')}|{normalize_zip('07001')}"
        )
        assert compute_location_key("ACC", row) == expected

    def test_matches_deathcare_formula_unnormalized(self):
        row = {"address_line_1": "1 Main St", "zip5": "07001"}
        assert compute_location_key("ACC", row, normalize_parts=False) == sha256_key(
            "loc:ACC|1 Main St|07001"
        )

    def test_distinct_addresses_yield_distinct_keys(self):
        a = compute_location_key("ACC", {"address_line_1": "1 Main St", "zip5": "1"})
        b = compute_location_key("ACC", {"address_line_1": "2 Main St", "zip5": "1"})
        assert a != b

    def test_address_less_rows_collide_without_a_discriminator(self):
        """Documents exactly why parks need one: every park in a city shares the
        account and has no street address."""
        empty = {"address_line_1": None, "zip5": None}
        assert compute_location_key("ACC", empty) == compute_location_key("ACC", empty)

    def test_discriminator_separates_them(self):
        empty = {"address_line_1": None, "zip5": None}
        a = compute_location_key("ACC", empty, discriminator="padus_parks:P1")
        b = compute_location_key("ACC", empty, discriminator="padus_parks:P2")
        assert a != b

    def test_discriminator_changes_the_key(self):
        empty = {"address_line_1": None, "zip5": None}
        assert (
            compute_location_key("ACC", empty)
            != compute_location_key("ACC", empty, discriminator="x")
        )

    def test_account_key_is_part_of_the_hash(self):
        row = {"address_line_1": "1 Main St", "zip5": "07001"}
        assert compute_location_key("A", row) != compute_location_key("B", row)


class TestComputeContactKey:
    def test_formula_is_stable(self):
        assert compute_contact_key("ACC", "parks_dir", "Jane Doe") == sha256_key(
            f"contact:ACC|parks_dir|{normalize_name('Jane Doe')}"
        )

    def test_matches_deathcare_formula_unnormalized(self):
        assert compute_contact_key(
            "ACC", "primary_phone", "", normalize_parts=False
        ) == sha256_key("contact:ACC|primary_phone|")

    def test_role_participates(self):
        assert (
            compute_contact_key("ACC", "a", "N") != compute_contact_key("ACC", "b", "N")
        )

    def test_none_name_handled(self):
        assert len(compute_contact_key("ACC", "role", None)) == 64
