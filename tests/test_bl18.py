"""BL-18 — pure-function tests for the QR dedup-key helper.

Live router wiring is exercised by the integration tests in batch 3.
This file is hermetic.
"""
from __future__ import annotations

import pytest

from app.services.bl18_qr_dedup import (
    DedupKey, DedupKeyError, dedup_key, is_spec_faithful,
)


# ── Spec-faithful keys (match the schema's unique constraints) ───────────────

def test_pesticide_path_uses_brand_cosh_id():
    """Spec-faithful key for pesticides — matches uq_qr_pesticide on
    `(client_id, brand_cosh_id, batch_lot_number)`."""
    key = dedup_key(
        brand_cosh_id="brand:dithane-m45",
        variety_id=None,
        product_display_name="Dithane M-45",
        batch_lot_number="B001",
    )
    assert key == DedupKey(
        column_name="brand_cosh_id",
        column_value="brand:dithane-m45",
        batch_lot_number="B001",
        is_fallback=False,
    )
    assert is_spec_faithful(key) is True


def test_seed_path_uses_variety_id():
    """Spec-faithful key for seeds — matches uq_qr_seed on
    `(client_id, variety_id, batch_lot_number)`."""
    key = dedup_key(
        brand_cosh_id=None,
        variety_id="variety-uuid-1",
        product_display_name="Tomato XYZ Hybrid",
        batch_lot_number="B002",
    )
    assert key.column_name == "variety_id"
    assert key.column_value == "variety-uuid-1"
    assert is_spec_faithful(key) is True


def test_brand_takes_priority_over_variety():
    """Defensive: if both brand AND variety are provided (mixed
    product), brand wins. Pesticide constraint fires first; seed
    constraint is a safety net."""
    key = dedup_key(
        brand_cosh_id="brand:bxyz",
        variety_id="variety-uuid-1",
        product_display_name="Mixed Product",
        batch_lot_number="B003",
    )
    assert key.column_name == "brand_cosh_id"
    assert key.column_value == "brand:bxyz"


# ── Fallback path (display_name only) ────────────────────────────────────────

def test_fallback_to_display_name_when_brand_and_variety_absent():
    """Bulk-import V1 stop-gap. CSV today has no brand_cosh_id or
    variety_id columns, so both come in as None. The helper falls
    back to display_name; in-app dedup still works between bulk rows
    AND across paths if a single-created row shares the display+batch
    with a bulk row."""
    key = dedup_key(
        brand_cosh_id=None,
        variety_id=None,
        product_display_name="BrandXYZ Gold",
        batch_lot_number="B001",
    )
    assert key.column_name == "product_display_name"
    assert key.column_value == "BrandXYZ Gold"
    assert key.is_fallback is True
    assert is_spec_faithful(key) is False


def test_fallback_treats_empty_string_brand_as_absent():
    """A blank/whitespace string from sloppy callers shouldn't be
    treated as a real brand_cosh_id — falls through to variety, then
    display_name."""
    key = dedup_key(
        brand_cosh_id="   ",
        variety_id=None,
        product_display_name="Some Product",
        batch_lot_number="B005",
    )
    assert key.column_name == "product_display_name"
    assert key.is_fallback is True


# ── Error cases ──────────────────────────────────────────────────────────────

def test_missing_all_three_identifiers_raises():
    """Without ANY identifier we'd produce a meaningless key like
    `(client, "", batch)` that would match every other empty-id row.
    Raise instead so the caller turns it into a 422."""
    with pytest.raises(DedupKeyError):
        dedup_key(
            brand_cosh_id=None,
            variety_id=None,
            product_display_name=None,
            batch_lot_number="B001",
        )


def test_missing_batch_raises():
    """batch_lot_number scopes the dedup; without it we'd treat every
    QR for the same brand as a duplicate."""
    with pytest.raises(DedupKeyError):
        dedup_key(
            brand_cosh_id="brand:bxyz",
            variety_id=None,
            product_display_name="Some Product",
            batch_lot_number="",
        )


def test_whitespace_only_batch_rejected():
    with pytest.raises(DedupKeyError):
        dedup_key(
            brand_cosh_id="brand:bxyz",
            variety_id=None,
            product_display_name="Some Product",
            batch_lot_number="   ",
        )


# ── Whitespace handling ──────────────────────────────────────────────────────

def test_whitespace_around_inputs_is_stripped():
    """A pasted brand_cosh_id with trailing whitespace shouldn't
    bypass dedup against the same id without whitespace."""
    key1 = dedup_key(
        brand_cosh_id="  brand:bxyz  ",
        variety_id=None,
        product_display_name=None,
        batch_lot_number=" B001 ",
    )
    key2 = dedup_key(
        brand_cosh_id="brand:bxyz",
        variety_id=None,
        product_display_name=None,
        batch_lot_number="B001",
    )
    assert key1 == key2


# ── is_spec_faithful predicate ───────────────────────────────────────────────

def test_is_spec_faithful_returns_true_for_brand_or_variety_keys():
    brand_key = dedup_key(
        brand_cosh_id="brand:x", variety_id=None,
        product_display_name=None, batch_lot_number="B",
    )
    variety_key = dedup_key(
        brand_cosh_id=None, variety_id="v",
        product_display_name=None, batch_lot_number="B",
    )
    assert is_spec_faithful(brand_key) is True
    assert is_spec_faithful(variety_key) is True


def test_is_spec_faithful_returns_false_for_fallback_keys():
    """Lets the route handler log a warning when a bulk row falls
    through to display_name dedup — useful operational signal that
    the CSV upgrade is overdue."""
    fallback_key = dedup_key(
        brand_cosh_id=None, variety_id=None,
        product_display_name="X", batch_lot_number="B",
    )
    assert is_spec_faithful(fallback_key) is False
