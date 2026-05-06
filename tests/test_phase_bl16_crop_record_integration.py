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
async def test_public_payload_omits_privacy_leaking_fields(db):
    """The most consequential audit fix. Pre-fix the route exposed
    farmer_district, farmer_state, package_name, subscription_date,
    status, and company_display_name alongside company_name on this
    unauthenticated URL. Now strictly limited to the six spec-
    permitted keys."""
    _, sub, _, _ = await _seed_active_sub_with_reference(db)
    out = await get_crop_public_page(
        reference_number=sub.reference_number, db=db,
    )
    assert set(out.keys()) == {
        "reference_number", "farmer_name", "crop", "company",
        "start_date", "parameter_variable_summary",
    }
    # Spot-check the location leak is closed.
    assert "farmer_district" not in out
    assert "farmer_state" not in out


@requires_docker
@pytest.mark.asyncio
async def test_public_payload_uses_company_display_name_and_iso_start_date(db):
    """The helper prefers display_name over full_name and renders
    start_date as ISO date (no time component). Pin both via the
    seeded data."""
    _, sub, _, _ = await _seed_active_sub_with_reference(db)
    out = await get_crop_public_page(
        reference_number=sub.reference_number, db=db,
    )
    assert out["farmer_name"] == "Ramu Krishnaswamy"
    assert out["company"] == "Padmashali Seeds"
    assert out["start_date"] == "2026-05-01"


# ── Public-page route: parameter_variable_summary lookup ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_public_payload_includes_parameter_variable_summary_when_present(db):
    """When a FarmerSubscriptionHistory row exists with the summary
    populated, the public page exposes it. Pre-audit the route never
    queried the history table at all."""
    _, sub, _, _ = await _seed_active_sub_with_reference(db)
    db.add(FarmerSubscriptionHistory(
        subscription_id=sub.id,
        parameter_variable_summary="Loam soil, NPK every 21 days",
    ))
    await db.commit()

    out = await get_crop_public_page(
        reference_number=sub.reference_number, db=db,
    )
    assert out["parameter_variable_summary"] == "Loam soil, NPK every 21 days"


@requires_docker
@pytest.mark.asyncio
async def test_public_payload_returns_null_summary_when_no_history(db):
    """No FarmerSubscriptionHistory row → summary is null. Reflects
    today's reality (the writer for this column isn't wired yet —
    deferred follow-up). Pinned so the field stays forward-
    compatible when the writer lands."""
    _, sub, _, _ = await _seed_active_sub_with_reference(db)
    out = await get_crop_public_page(
        reference_number=sub.reference_number, db=db,
    )
    assert out["parameter_variable_summary"] is None


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
