"""BL-18 — Product QR Duplicate Check (pure functions, no DB).

Spec rule: "Duplicate check" — a brand + batch combo (or variety +
batch combo for seeds) should produce only one ProductQRCode row
per client. The schema enforces this with two unique constraints:

    UniqueConstraint(client_id, brand_cosh_id, batch_lot_number)  # pesticides
    UniqueConstraint(client_id, variety_id, batch_lot_number)     # seeds

Pre-audit, the live router's two write paths (single create + bulk
create) each had their own inline dedup queries — and the keys
disagreed. The single path checked
`(client, brand_cosh_id, variety_id, batch)`; the bulk path checked
`(client, display_name, batch)`. A bulk-imported row with the same
brand+batch as a single-created row would not be caught by either
path, even though the schema's unique constraint would (eventually)
reject it at commit time.

This module owns the dedup-key derivation. Both write paths build
their lookup query through `dedup_key` so they always agree on what
counts as a duplicate.

V1 stop-gap (Option B, 2026-05-06): when the caller has no
`brand_cosh_id` or `variety_id` (e.g. the bulk-import CSV doesn't
yet ship those columns), the helper returns a best-available key
falling back to `display_name + batch`. Documented degradation —
not as strong as the spec-faithful key, but ensures bulk and single
paths can still detect cross-path duplicates by display name.

V2 (scheduled in `project_rootstalk_v2_ideas.md` — alongside the
BL-15 spec-faithful format work): bulk CSV gains `brand_cosh_id` /
`variety_id` columns and the fallback path is no longer needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DedupKey:
    """Describes which (client, identifier-column, batch) tuple is
    the dedup key for a given input. The route handler uses
    `column_name` + `column_value` to build a SELECT statement
    matching the appropriate unique constraint. `is_fallback=True`
    signals we couldn't compute the spec-faithful key (no brand or
    variety provided) and dedup is best-effort by display_name."""
    column_name: str           # "brand_cosh_id" | "variety_id" | "product_display_name"
    column_value: str
    batch_lot_number: str
    is_fallback: bool = False


# ── Errors ────────────────────────────────────────────────────────────────────

class DedupKeyError(ValueError):
    """Raised when the caller provided no usable identifier — neither
    brand_cosh_id, variety_id, nor display_name. Without at least one
    of these we cannot dedupe at all and must reject the create."""


# ── Public API ────────────────────────────────────────────────────────────────

def dedup_key(
    *,
    brand_cosh_id: Optional[str],
    variety_id: Optional[str],
    product_display_name: Optional[str],
    batch_lot_number: str,
) -> DedupKey:
    """Compute the dedup key for a ProductQRCode insert.

    Priority order:
    1. `brand_cosh_id` (matches `uq_qr_pesticide`).
    2. `variety_id` (matches `uq_qr_seed`).
    3. `product_display_name` (fallback — no schema-level unique
       constraint exists; in-app dedup only).

    `batch_lot_number` is required in every case — without it there's
    nothing to scope the dedup against, and the caller must reject
    the insert separately (DB column is NOT NULL anyway).

    Raises `DedupKeyError` if none of the three identifiers is set —
    rather than producing a meaningless key like `(client, "", batch)`
    that would match every other empty-identifier row in the table.
    """
    if not batch_lot_number or not batch_lot_number.strip():
        raise DedupKeyError("batch_lot_number is required for dedup")

    batch = batch_lot_number.strip()
    brand = (brand_cosh_id or "").strip()
    variety = (variety_id or "").strip()
    display = (product_display_name or "").strip()

    if brand:
        return DedupKey(
            column_name="brand_cosh_id",
            column_value=brand,
            batch_lot_number=batch,
        )
    if variety:
        return DedupKey(
            column_name="variety_id",
            column_value=variety,
            batch_lot_number=batch,
        )
    if display:
        return DedupKey(
            column_name="product_display_name",
            column_value=display,
            batch_lot_number=batch,
            is_fallback=True,
        )
    raise DedupKeyError(
        "At least one of brand_cosh_id, variety_id, or "
        "product_display_name is required for dedup"
    )


def is_spec_faithful(key: DedupKey) -> bool:
    """True iff the key matches one of the schema's unique
    constraints. False iff we fell back to display_name. Useful for
    callers that want to log a warning when bulk imports rely on the
    fallback path."""
    return not key.is_fallback
