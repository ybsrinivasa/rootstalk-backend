"""BL-16 audit — DB-backed integration tests for the rewired QR routes.

Pure-function coverage of the URL + payload helpers lives in
`tests/test_bl16.py` (9 tests). This file drives both QR routes
directly with seeded rows in the testcontainer DB to verify the
URL fix, the payload trim, and the parameter_variable_summary
lookup behave end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.subscriptions.models import (
    FarmerSubscriptionHistory, Subscription, SubscriptionStatus,
)
from app.modules.qr.router import (
    get_crop_history_qr, get_crop_public_page,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


async def _seed_active_sub_with_reference(db, *, reference: str = "PA-26-000147"):
    """Seed an ACTIVE subscription with a valid reference number,
    crop start date set, and matching client + package. Returns
    (farmer, sub, client, package)."""
    farmer = await make_user(db, name="Ramu Krishnaswamy")
    client = await make_client(db, full_name="Padmashali Seeds and Agro Private Limited")
    client.short_name = "padmashali"
    client.display_name = "Padmashali Seeds"
    package = await make_package(db, client, name="Tomato Pack 2026")
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    sub.status = SubscriptionStatus.ACTIVE
    sub.crop_start_date = datetime(2026, 5, 1, 8, 30, tzinfo=timezone.utc)
    sub.subscription_date = datetime(2026, 4, 15, tzinfo=timezone.utc)
    sub.reference_number = reference
    await db.commit()
    return farmer, sub, client, package


# ── QR generation route ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_qr_route_returns_png(db):
    """Sanity: the QR route still returns a PNG. The audit only
    changed which URL the QR encodes, not the PNG generation
    pipeline."""
    farmer, sub, _, _ = await _seed_active_sub_with_reference(db)
    response = await get_crop_history_qr(
        sub_id=sub.id, db=db, current_user=farmer,
    )
    assert response.media_type == "image/png"
    assert len(response.body) > 0


# ── Public-page route: URL path fix ──────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_public_page_lookup_succeeds_at_reference(db):
    """The public route now lives at /public/crop-record/{ref}.
    Scoped here as a smoke test that calling the handler with a
    real reference returns a payload; the URL path fix itself is
    pinned by the FastAPI route declaration (one-line route string,
    no separate test needed)."""
    _, sub, _, _ = await _seed_active_sub_with_reference(db)
    out = await get_crop_public_page(
        reference_number=sub.reference_number, db=db,
    )
    assert out["reference_number"] == sub.reference_number


# ── Public-page route: payload trim ──────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_public_payload_returns_expanded_record(db):
    """2026-06-06 — User direction widened the public page spec to
    include farmer phone + location, crop name, closure date, package
    name + id, and the package's parameters-options fingerprint. The
    earlier BL-16 trim is superseded; the farmer themselves prints the
    QR, so their location + phone are intentionally shared."""
    _, sub, _, _ = await _seed_active_sub_with_reference(db)
    out = await get_crop_public_page(
        reference_number=sub.reference_number, db=db,
    )
    assert out["reference_number"] == sub.reference_number
    assert out["farmer_name"] == "Ramu Krishnaswamy"
    # Location keys are present (may be None when farmer didn't set
    # district/state — pinned here that the keys exist on the shape).
    assert "farmer_phone" in out
    assert "farmer_district" in out
    assert "farmer_state" in out
    assert out["company"] == "Padmashali Seeds"
    assert out["package_name"] == "Tomato Pack 2026"
    assert out["package_id"] == sub.package_id
    assert out["crop_start_date"] == "2026-05-01"
    assert "crop_closure_date" in out  # may be None if no duration_days
    assert isinstance(out["parameters_options"], list)


@requires_docker
@pytest.mark.asyncio
async def test_public_page_returns_404_for_unknown_reference(db):
    """Lookup by an unknown reference number returns 404 — the
    public page must not leak the existence of nearby references."""
    with pytest.raises(HTTPException) as exc:
        await get_crop_public_page(
            reference_number="PA-26-999999", db=db,
        )
    assert exc.value.status_code == 404


# ── Legacy alias /public/crop/{ref} → 301 → /public/crop-record/{ref} ────────

@pytest.mark.asyncio
async def test_legacy_alias_redirects_with_301():
    """Anything still calling the old `/public/crop/{ref}` path (PWA
    frontend code that hasn't shipped the BL-16 fix yet, a printed
    QR generated against a pre-audit build) gets a 301 redirect to
    the new spec path. Forwards the reference_number unchanged."""
    from app.modules.qr.router import get_crop_public_page_legacy_alias

    response = await get_crop_public_page_legacy_alias(
        reference_number="PA-26-000147",
    )
    assert response.status_code == 301
    assert response.headers["location"] == "/public/crop-record/PA-26-000147"
