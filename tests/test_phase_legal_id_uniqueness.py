"""CA onboarding submit — PAN/GST uniqueness pre-check.

Background: clients.gst_number and clients.pan_number both have DB-level
unique constraints. Without a pre-check in submit_onboarding, a clash
surfaces as a raw 500 (IntegrityError → SQLAlchemy → uvicorn) — the CA
sees a generic banner with no actionable hint. Surfaced 2026-05-08 in
the testing-server flow when the testing crew reused PAN BMHPR0109M
across two onboarding stubs for the same legal entity.

Fix: pre-check both fields before the UPDATE; raise 422 with structured
detail `{field, code, message}` so the CA portal pins the message to
the right input.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.clients.models import (
    Client, ClientStatus, PaymentModel,
)
from app.modules.clients.router import submit_onboarding
from app.modules.clients.schemas import ClientCASubmit
from app.modules.clients.service import generate_token
from tests.conftest import requires_docker
from tests.factories import make_client


def _submit_payload(**overrides) -> ClientCASubmit:
    base = dict(
        display_name="Bighaat",
        tagline="For farmer",
        primary_colour="#21a141",
        secondary_colour="#f08f19",
        hq_address="Srinivaspur, Kolar",
        gst_number="BFGHFFJFJ123445",
        pan_number="BMHPR0109M",
        website="https://eywa.farm/",
        support_phone="9877656476",
        office_phone="9877678987",
        social_links={"twitter": "", "instagram": "", "linkedin": "", "facebook": ""},
        org_type_cosh_ids=["org_type_pesticide_mfr"],
    )
    base.update(overrides)
    return ClientCASubmit(**base)


async def _seed_pending_client(db, *, with_token: str | None = None) -> Client:
    """Seed a fresh PENDING_REVIEW client stub the way init_onboarding
    would have produced it. The CA-submit handler looks up by token,
    so each test gets its own non-colliding token."""
    token = with_token or generate_token()
    client = await make_client(db)
    client.status = ClientStatus.PENDING_REVIEW
    client.payment_model = PaymentModel.FARMER_PAYS
    client.onboarding_link_token = token
    await db.flush()
    return client


# ── Clash paths ─────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_submit_onboarding_rejects_duplicate_pan(db):
    """Another client already holds the PAN → 422 with structured
    detail pointing to `pan_number`. Previously raised 500."""
    # Seed a prior client that already owns the PAN.
    prior = await make_client(db)
    prior.pan_number = "BMHPR0109M"
    prior.gst_number = "PRIOR1234567890"
    await db.flush()

    # New stub the CA is now submitting against.
    fresh = await _seed_pending_client(db)

    with pytest.raises(HTTPException) as ei:
        await submit_onboarding(
            token=fresh.onboarding_link_token,
            request=_submit_payload(
                pan_number="BMHPR0109M",
                gst_number="UNIQUEGST123456",
            ),
            db=db,
        )

    assert ei.value.status_code == 422
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail["field"] == "pan_number"
    assert detail["code"] == "pan_already_registered"
    assert "PAN" in detail["message"]


@requires_docker
@pytest.mark.asyncio
async def test_submit_onboarding_rejects_duplicate_gst(db):
    """Same for GST — independent of PAN."""
    prior = await make_client(db)
    prior.gst_number = "BFGHFFJFJ123445"
    prior.pan_number = "PRIORPAN01"
    await db.flush()

    fresh = await _seed_pending_client(db)

    with pytest.raises(HTTPException) as ei:
        await submit_onboarding(
            token=fresh.onboarding_link_token,
            request=_submit_payload(
                gst_number="BFGHFFJFJ123445",
                pan_number="UNIQUEPAN1",
            ),
            db=db,
        )

    assert ei.value.status_code == 422
    detail = ei.value.detail
    assert detail["field"] == "gst_number"
    assert detail["code"] == "gst_already_registered"


@requires_docker
@pytest.mark.asyncio
async def test_submit_onboarding_rejects_lowercase_duplicate_pan(db):
    """The handler uppercases before persisting, so the pre-check must
    also uppercase before comparing — otherwise a CA typing the same
    PAN in lowercase would slip past and trigger the 500 again."""
    prior = await make_client(db)
    prior.pan_number = "BMHPR0109M"
    prior.gst_number = "PRIOR1234567890"
    await db.flush()

    fresh = await _seed_pending_client(db)

    with pytest.raises(HTTPException) as ei:
        await submit_onboarding(
            token=fresh.onboarding_link_token,
            request=_submit_payload(
                pan_number="bmhpr0109m",  # lowercase
                gst_number="UNIQUEGST123456",
            ),
            db=db,
        )

    assert ei.value.status_code == 422
    assert ei.value.detail["field"] == "pan_number"


# ── Success path ────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_submit_onboarding_succeeds_with_unique_legal_ids(db):
    """A first-time CA submission with truly unique IDs persists
    cleanly. Guards against the pre-check accidentally rejecting
    legitimate submissions."""
    fresh = await _seed_pending_client(db)

    out = await submit_onboarding(
        token=fresh.onboarding_link_token,
        request=_submit_payload(
            gst_number="UNIQUEGST123456",
            pan_number="UNIQUEPAN1",
        ),
        db=db,
    )

    assert out.gst_number == "UNIQUEGST123456"
    assert out.pan_number == "UNIQUEPAN1"


@requires_docker
@pytest.mark.asyncio
async def test_submit_onboarding_allows_resubmit_on_same_client(db):
    """Re-submitting against the *same* client row (not via regen-link
    yet, but theoretically possible if status logic changes) must not
    flag the client's own existing PAN as a duplicate. The pre-check
    excludes self-id."""
    fresh = await _seed_pending_client(db)
    fresh.pan_number = "EXISTPAN01"
    fresh.gst_number = "EXISTGST1234567"
    await db.flush()

    # The handler refuses if status != PENDING_REVIEW (already used),
    # but if it ever did reach the unique check with the same IDs, the
    # pre-check would correctly skip self. We exercise the helper
    # directly here to assert that semantic.
    from app.modules.clients.router import _assert_unique_legal_ids
    # Same IDs, same client_id → no clash.
    await _assert_unique_legal_ids(
        db,
        self_client_id=fresh.id,
        gst_number="EXISTGST1234567",
        pan_number="EXISTPAN01",
    )
