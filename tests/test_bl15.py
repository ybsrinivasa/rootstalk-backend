"""BL-15 — pure-function tests for the V1 (Option B) reference format.

V2 spec-faithful format `[2-char crop][2-char client][YY]-[NNNNNN]`
is scheduled separately (see project_rootstalk_v2_ideas.md). These
tests pin the V1 stop-gap format `[2-char client][YY]-[NNNNNN]`.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.bl15_reference import (
    REFERENCE_FALLBACK_CLIENT_CODE,
    client_code_from_short_name, format_reference, parse_sequence,
    reference_prefix, two_digit_year,
)


# ── client_code_from_short_name ───────────────────────────────────────────────

def test_client_code_takes_first_two_chars_uppercase():
    assert client_code_from_short_name("padmashali") == "PA"


def test_client_code_handles_already_uppercase_input():
    assert client_code_from_short_name("AC") == "AC"


def test_client_code_pads_with_fallback_for_too_short_input():
    """Single-char or empty short_name falls back to RT (RootsTalk)
    rather than producing a 1-char code that breaks the format."""
    assert client_code_from_short_name("") == REFERENCE_FALLBACK_CLIENT_CODE
    assert client_code_from_short_name("a") == REFERENCE_FALLBACK_CLIENT_CODE
    assert client_code_from_short_name("   ") == REFERENCE_FALLBACK_CLIENT_CODE
    assert client_code_from_short_name(None) == REFERENCE_FALLBACK_CLIENT_CODE  # type: ignore[arg-type]


def test_client_code_strips_surrounding_whitespace_before_picking():
    """Defensive: a short_name with leading whitespace should still
    produce the correct 2-char code from the actual letters."""
    assert client_code_from_short_name("  padmashali  ") == "PA"


# ── format_reference ─────────────────────────────────────────────────────────

def test_format_reference_zero_pads_sequence_to_six_digits():
    assert format_reference("PA", "26", 147) == "PA-26-000147"
    assert format_reference("PA", "26", 1) == "PA-26-000001"


def test_format_reference_supports_full_six_digit_range():
    assert format_reference("PA", "26", 999999) == "PA-26-999999"


def test_format_reference_rejects_negative_sequence():
    with pytest.raises(ValueError):
        format_reference("PA", "26", -1)


def test_format_reference_rejects_overflow_sequence():
    """A million is out of range for 6 digits — raise rather than
    silently truncate."""
    with pytest.raises(ValueError):
        format_reference("PA", "26", 1_000_000)


def test_format_reference_lexicographic_order_matches_numeric_order():
    """The route handler relies on `ORDER BY reference_number DESC
    LIMIT 1` returning the highest sequence. With zero-padded 6-digit
    suffixes that's equivalent to numeric order — pin it so a future
    refactor doesn't drop the padding."""
    refs = [format_reference("PA", "26", n) for n in (1, 9, 10, 99, 100, 999, 1000)]
    assert sorted(refs) == refs


# ── two_digit_year ────────────────────────────────────────────────────────────

def test_two_digit_year_pads_single_digit_centuries():
    """A future year ending in single digit (e.g. 2007 → "07") must
    be padded to two characters so the format width stays stable."""
    assert two_digit_year(datetime(2007, 1, 1, tzinfo=timezone.utc)) == "07"


def test_two_digit_year_returns_2026_as_26():
    assert two_digit_year(datetime(2026, 5, 6, tzinfo=timezone.utc)) == "26"


# ── reference_prefix ─────────────────────────────────────────────────────────

def test_reference_prefix_matches_format():
    """Pin that `prefix + format(seq)[len(prefix):]` reconstructs a
    full reference — used as the LIKE pattern in the max+1 query."""
    prefix = reference_prefix("PA", "26")
    full = format_reference("PA", "26", 147)
    assert full.startswith(prefix)
    assert prefix == "PA-26-"


# ── parse_sequence ───────────────────────────────────────────────────────────

def test_parse_sequence_extracts_the_numeric_suffix():
    assert parse_sequence("PA-26-000147") == 147
    assert parse_sequence("RT-26-000001") == 1


def test_parse_sequence_returns_negative_for_malformed_reference():
    """A corrupted or non-V1-format value (e.g. legacy
    PADMASHALI26-3847 or freeform RT-XXXXXXXX) shouldn't raise — just
    return -1 so the caller's max+1 query treats it as "no number"
    and starts fresh."""
    assert parse_sequence("PADMASHALI26-3847") == 3847  # actually parses
    assert parse_sequence("RT-AB12345678") == -1
    assert parse_sequence("") == -1
    assert parse_sequence("not-a-reference") == -1
