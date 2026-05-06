"""BL-15 — Subscription Reference Number Generation (pure functions, no DB).

V1 (Option B, 2026-05-06): stop-gap closer-to-spec format.

  Format:  {client_code}-{YY}-{NNNNNN}
  Example: PA-26-000147

  client_code: first 2 chars of `client.short_name`, uppercased.
               Fallback to "RT" if short_name is shorter than 2 chars.
  YY:          2-digit year, UTC.
  NNNNNN:      6-digit zero-padded sequential counter, scoped to
               (client_code, year). Lexicographic order matches numeric
               order at this padding, so "ORDER BY reference_number DESC
               LIMIT 1" correctly returns the highest sequence.

V2 (scheduled, see ~/.claude/.../memory/project_rootstalk_v2_ideas.md):
the spec-faithful format `[2-char crop][2-char client][YY]-[6-digit
sequential]` like `PD-AC-26-000147`. Requires schema migration
(`Client.code: String(2)`), a crop-code Cosh extension, and a
backfill plan that preserves V1-issued numbers (BL-15 spec: "Never
updated"). Out of scope today.

This module owns the format and the client-code derivation. Sequence
allocation (max+1 with retry-on-conflict for concurrency) lives in
the route handler because it needs DB access.
"""
from __future__ import annotations

from datetime import datetime, timezone


REFERENCE_DIGITS = 6
REFERENCE_FALLBACK_CLIENT_CODE = "RT"


def client_code_from_short_name(short_name: str) -> str:
    """Derive the V1 stop-gap 2-char client code from `short_name`.

    Returns the first two ASCII letters of `short_name`, uppercased.
    Falls back to "RT" (RootsTalk) when short_name is shorter than 2
    chars or is missing — matches the existing fallback behaviour for
    clients without a short_name set.

    Note: this is the lossy step. Two clients with short_names
    "padmashali" and "padmashree" both map to "PA". V2 (Client.code
    column) replaces this with admin-assigned, uniqueness-checked
    2-char codes — see `project_rootstalk_v2_ideas.md`.
    """
    if not short_name or len(short_name.strip()) < 2:
        return REFERENCE_FALLBACK_CLIENT_CODE
    return short_name.strip()[:2].upper()


def two_digit_year(now: datetime | None = None) -> str:
    """UTC year as a 2-digit string. Injectable `now` for tests."""
    moment = now or datetime.now(timezone.utc)
    return f"{moment.year % 100:02d}"


def format_reference(
    client_code: str, year_two_digit: str, sequence: int,
) -> str:
    """Compose a reference number from its three parts.

    `sequence` must be a non-negative integer that fits in 6 digits
    (i.e. 0–999999). The caller is responsible for picking the right
    next value via a max+1 query against existing references for the
    same (client_code, year_two_digit).
    """
    if sequence < 0 or sequence >= 10 ** REFERENCE_DIGITS:
        raise ValueError(
            f"sequence {sequence} out of range "
            f"[0, {10 ** REFERENCE_DIGITS - 1}]"
        )
    return f"{client_code}-{year_two_digit}-{sequence:0{REFERENCE_DIGITS}d}"


def reference_prefix(client_code: str, year_two_digit: str) -> str:
    """The LIKE-pattern-matching prefix for finding all references in
    a (client_code, year) bucket. Used by the route handler's max+1
    query: `WHERE reference_number LIKE prefix || '%'`."""
    return f"{client_code}-{year_two_digit}-"


def parse_sequence(reference: str) -> int:
    """Extract the 6-digit suffix as an int. Returns -1 for malformed
    references (so a corrupted row doesn't poison the max+1 query)."""
    try:
        return int(reference.rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return -1
