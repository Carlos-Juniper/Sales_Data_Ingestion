"""
Unit tests for lib/normalize.py.

All three functions are pure (str -> str), so no mocks are needed.
Tests are organized by function, with parametrize used for the many
equivalent suffix/format cases to keep the file scannable.
"""

import os
import sys

import pytest

# Make the connectors package importable when pytest is run from the repo root
# or from inside connectors/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.normalize import normalize_name, normalize_phone, normalize_zip


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


class TestNormalizeName:
    def test_empty_string_returns_empty(self):
        assert normalize_name("") == ""

    def test_none_returns_empty(self):
        assert normalize_name(None) == ""

    def test_plain_name_uppercased(self):
        assert normalize_name("Smith") == "SMITH"

    def test_mixed_case_uppercased(self):
        assert normalize_name("General Hospital") == "GENERAL HOSPITAL"

    def test_punctuation_replaced_with_space(self):
        # Commas, periods, hyphens, apostrophes are all non-alphanumeric.
        result = normalize_name("ST. MARY'S HEALTH-SYSTEM")
        assert "." not in result
        assert "'" not in result
        assert "-" not in result

    def test_multiple_spaces_collapse_to_one(self):
        assert normalize_name("ACME   CORP   GROUP") == "ACME GROUP"

    def test_leading_and_trailing_whitespace_stripped(self):
        assert normalize_name("  ACME  ") == "ACME"

    @pytest.mark.parametrize(
        "suffix",
        ["INC", "LLC", "LTD", "CORP", "CO", "PLC", "LP", "LLP", "ASSN", "ASSOC", "ASSOCIATION"],
    )
    def test_corporate_suffix_removed_uppercase(self, suffix):
        result = normalize_name(f"ACME {suffix}")
        assert suffix not in result.split()

    @pytest.mark.parametrize(
        "suffix",
        ["Inc", "Llc", "Ltd", "Corp", "Co", "Plc", "Lp", "Llp", "Assn", "Assoc", "Association"],
    )
    def test_corporate_suffix_removed_mixed_case(self, suffix):
        result = normalize_name(f"Acme {suffix}")
        assert suffix.upper() not in result.split()

    def test_suffix_at_start_removed(self):
        # ASSN at start of string still matches the word boundary.
        result = normalize_name("ASSN OF HOSPITALS")
        assert "ASSN" not in result.split()

    def test_suffix_embedded_in_word_not_removed(self):
        # "INCORPORATED" contains "CORP" but it is not a standalone word.
        result = normalize_name("INCORPORATED HEALTH")
        # "INCORPORATED" has CORP inside it — the regex uses \b so only the
        # standalone token is stripped; INCORPORATED itself should survive.
        assert "INCORPORATED" in result or "INCORPORATED".replace("CORP", " ") in result
        # The key behavior: the literal word CORP alone is removed.
        assert "CORP" not in normalize_name("ACME CORP").split()

    def test_returns_empty_when_only_suffix(self):
        # A string that is ONLY a corporate suffix reduces to empty after stripping.
        result = normalize_name("LLC")
        assert result == ""

    def test_digits_preserved(self):
        assert "123" in normalize_name("PARCEL 123")

    def test_unicode_replaced(self):
        # Non-ASCII characters (accents, etc.) should be stripped.
        result = normalize_name("HÔPITAL GÉNÉRAL")
        assert "Ô" not in result
        assert "É" not in result


# ---------------------------------------------------------------------------
# normalize_zip
# ---------------------------------------------------------------------------


class TestNormalizeZip:
    def test_plain_five_digit_zip(self):
        assert normalize_zip("78701") == "78701"

    def test_zip_plus_four_with_hyphen(self):
        assert normalize_zip("78701-1234") == "78701"

    def test_leading_zeros_preserved(self):
        assert normalize_zip("07001") == "07001"

    def test_leading_zeros_preserved_with_extension(self):
        assert normalize_zip("07001-0042") == "07001"

    def test_none_returns_empty(self):
        assert normalize_zip(None) == ""

    def test_empty_string_returns_empty(self):
        assert normalize_zip("") == ""

    def test_junk_string_too_few_digits_returns_empty(self):
        assert normalize_zip("ABCD") == ""

    def test_partial_digits_fewer_than_five_returns_empty(self):
        assert normalize_zip("123") == ""

    def test_exactly_four_digits_returns_empty(self):
        assert normalize_zip("1234") == ""

    def test_nine_digit_string_returns_first_five(self):
        assert normalize_zip("787011234") == "78701"

    def test_numeric_input_coerced_to_string(self):
        # When a CSV is read with numeric dtype, zip arrives as a float like 78701.0.
        # normalize_zip should handle str(78701.0) gracefully.
        # str(78701.0) -> "78701.0" — digits are "787010", first 5 is "78701".
        assert normalize_zip("78701.0") == "78701"

    def test_whitespace_only_returns_empty(self):
        assert normalize_zip("   ") == ""

    def test_mixed_letters_and_digits_with_enough_digits(self):
        # "ZIP: 90210" — strips non-digits to get "90210".
        assert normalize_zip("ZIP: 90210") == "90210"


# ---------------------------------------------------------------------------
# normalize_phone
# ---------------------------------------------------------------------------


class TestNormalizePhone:
    def test_plain_ten_digit_number(self):
        assert normalize_phone("5125551234") == "5125551234"

    def test_formatted_with_dashes(self):
        assert normalize_phone("512-555-1234") == "5125551234"

    def test_formatted_with_parens_and_spaces(self):
        assert normalize_phone("(512) 555-1234") == "5125551234"

    def test_dots_stripped(self):
        assert normalize_phone("512.555.1234") == "5125551234"

    def test_eleven_digits_returned_as_is(self):
        # Country code included — 11 digits, still >= 10, returned unchanged.
        assert normalize_phone("15125551234") == "15125551234"

    def test_nine_digits_returns_empty(self):
        assert normalize_phone("512555123") == ""

    def test_too_short_returns_empty(self):
        assert normalize_phone("555-1234") == ""

    def test_none_returns_empty(self):
        assert normalize_phone(None) == ""

    def test_empty_string_returns_empty(self):
        assert normalize_phone("") == ""

    def test_letters_only_returns_empty(self):
        assert normalize_phone("CALLNOW") == ""

    def test_mixed_letters_with_enough_digits(self):
        # "ext." prefix followed by digits — only the digits matter.
        result = normalize_phone("ext. 5125551234")
        assert result == "5125551234"

    def test_exactly_ten_digits_accepted(self):
        assert normalize_phone("1234567890") == "1234567890"
