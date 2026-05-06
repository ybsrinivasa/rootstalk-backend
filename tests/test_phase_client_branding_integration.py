"""Integration tests for the per-client branding URL pattern.

Two pieces landed together 2026-05-06:

1. `GET /public/clients/{short_name}/branding` — public (no auth)
   endpoint that returns the four branding fields the CA portal's
   `/login/[shortName]` page needs before the user has logged in.

2. `send_ca_credentials_email` now takes an explicit `login_url`
   parameter (built by the caller via `_base_url()`), and the
   approve-client flow constructs `f"{_base_url()}/login/{short_name}"`.
   Pre-fix the email hardcoded `https://rootstalk.in/{short_name}`,
   which was wrong once `rootstalk.in` got earmarked for the PWA.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.modules.clients.models import Client, ClientStatus
from app.modules.clients.router import (
    approve_client, get_public_client_branding,
)
from app.modules.platform.models import User
from tests.conftest import requires_docker
from tests.factories import make_user


async def _make_branded_client(
    db, *, short_name: str = "khaza", status: ClientStatus = ClientStatus.ACTIVE,
) -> Client:
    """Seed a Client row with all four branding fields populated and
    the chosen status."""
    c = Client(
        full_name="Khaza Seeds Pvt Ltd", short_name=short_name,
        ca_name="Test CA", ca_phone="+910000000000", ca_email=f"ca-{short_name}@test.local",
        tagline="Symbol of Quality",
        logo_url="https://cdn.example.com/khaza.png",
        primary_colour="#1A5C2A",
        secondary_colour="#F5C518",
        status=status,
    )
    db.add(c)
    await db.flush()
    return c


# ── Public branding endpoint ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_branding_returns_full_payload_for_active_client(db):
    """Happy path — all four branding fields surface."""
    await _make_branded_client(db, short_name="khaza")
    await db.commit()

    out = await get_public_client_branding(short_name="khaza", db=db)
    assert out.short_name == "khaza"
    assert out.full_name == "Khaza Seeds Pvt Ltd"
    assert out.tagline == "Symbol of Quality"
    assert out.logo_url == "https://cdn.example.com/khaza.png"
    assert out.primary_colour == "#1A5C2A"
    assert out.secondary_colour == "#F5C518"


@requires_docker
@pytest.mark.asyncio
async def test_branding_404_for_unknown_short_name(db):
    """No row at all — 404, not a leak. The CA portal would render
    a generic 'unknown company' page off this."""
    with pytest.raises(HTTPException) as ei:
        await get_public_client_branding(short_name="never_existed", db=db)
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_branding_404_for_pending_review_client(db):
    """A client in PENDING_REVIEW (post-CA-submit, pre-SA-approval)
    must NOT surface to the public branding endpoint — the CA hasn't
    been issued login credentials yet, so there's no legitimate
    reason for the public to need their branding. 404 also avoids
    leaking the existence of pre-launch clients."""
    await _make_branded_client(db, short_name="pending", status=ClientStatus.PENDING_REVIEW)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await get_public_client_branding(short_name="pending", db=db)
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_branding_404_for_inactive_client(db):
    """INACTIVE = wound-down. Same 404 rule — public can't reach
    branding for a client no longer using RootsTalk."""
    await _make_branded_client(db, short_name="winddown", status=ClientStatus.INACTIVE)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await get_public_client_branding(short_name="winddown", db=db)
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_branding_404_for_rejected_client(db):
    """REJECTED = SA never approved. Should be 404 from the public's
    perspective; the rejected CA has no credentials and shouldn't
    have a branded portal page."""
    await _make_branded_client(db, short_name="rejected", status=ClientStatus.REJECTED)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await get_public_client_branding(short_name="rejected", db=db)
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_branding_handles_partially_filled_client(db):
    """A client whose branding fields haven't been uploaded yet still
    has a valid response — the optional fields are None. The frontend
    falls back to defaults when fields are missing."""
    c = Client(
        full_name="Bare Client", short_name="bare",
        ca_name="CA", ca_phone="+91", ca_email="ca-bare@test.local",
        status=ClientStatus.ACTIVE,
    )
    db.add(c)
    await db.commit()

    out = await get_public_client_branding(short_name="bare", db=db)
    assert out.short_name == "bare"
    assert out.full_name == "Bare Client"
    assert out.tagline is None
    assert out.logo_url is None
    assert out.primary_colour is None
    assert out.secondary_colour is None


# ── send_ca_credentials_email URL is built env-driven ────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_approve_client_email_uses_env_driven_login_url(db, monkeypatch):
    """The bug pre-2026-05-06: the email hardcoded
    `https://rootstalk.in/{short_name}` regardless of environment.
    Post-fix: the URL is built from `_base_url()` and includes the
    `/login/<short_name>` path that the CA portal's
    `app/login/[shortName]/page.tsx` route expects."""
    from app.config import settings
    from app.modules.clients import service as clients_service

    # Set a deterministic frontend host so we can assert the URL exactly.
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk.eywa.farm")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "email_smtp_user", "no-reply@example.com")
    monkeypatch.setattr(settings, "email_smtp_pass", "irrelevant")

    captured: dict = {}

    def fake_send_email(to, subject, html, plain):
        captured.update(to=to, subject=subject, html=html, plain=plain)
        return True

    monkeypatch.setattr(clients_service, "_send_email", fake_send_email)

    sa = await make_user(db, name="SA")
    sa.email = "yb@eywa.farm"

    client = Client(
        full_name="ICL India", short_name="icl",
        display_name="ICL",  # required for approve_client to proceed
        ca_name="ICL CA", ca_phone="+919000000000",
        ca_email="ca-icl@test.local",
        status=ClientStatus.PENDING_REVIEW,
    )
    db.add(client)
    await db.commit()

    monkeypatch.setattr(settings, "sa_email", "yb@eywa.farm")
    await approve_client(client_id=client.id, db=db, current_user=sa)

    # The captured email body must point to the env-driven login URL.
    assert "https://rstalk.eywa.farm/login/icl" in captured["html"]
    assert "https://rstalk.eywa.farm/login/icl" in captured["plain"]
    # And must NOT contain the hardcoded production PWA host.
    assert "rootstalk.in" not in captured["html"]
    assert "rootstalk.in" not in captured["plain"]
