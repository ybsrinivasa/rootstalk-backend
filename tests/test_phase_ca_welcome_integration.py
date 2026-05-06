"""Integration tests for the CA-Welcome batch.

When a CA creates a portal user via `POST /client/{client_id}/users`
(Subject Expert / Field Manager / SDM / Report User / Client RM /
Product Manager), a welcome email is dispatched automatically with
the login URL, email, password, and role.

Sent only when:
1. A fresh User row was created (existing-user-being-added-as-new-role
   path is suppressed because the CA's password parameter is silently
   ignored by the model on that path — emailing it would mislead).
2. SMTP is configured (`email_smtp_user` non-empty).

Login URL is the per-client branded route
`{frontend_base_url}/login/{short_name}` so the user lands on their
company's branded sign-in page (backend wired in commit 556113b).
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.modules.clients import service as clients_service
from app.modules.clients.models import (
    Client, ClientStatus, ClientUserRole,
)
from app.modules.clients.router import add_portal_user
from app.modules.clients.schemas import PortalUserCreate
from app.modules.platform.models import User
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_active_client(db, *, short_name="khaza", full_name="Khaza Seeds Pvt Ltd") -> Client:
    c = Client(
        full_name=full_name, short_name=short_name,
        ca_name="Test CA", ca_phone="+910000000000", ca_email=f"ca-{short_name}@test.local",
        status=ClientStatus.ACTIVE,
    )
    db.add(c)
    await db.flush()
    return c


@requires_docker
@pytest.mark.asyncio
async def test_welcome_email_sent_when_new_user_created(db, monkeypatch):
    """Headline path. CA creates a Subject Expert; the SE's email
    inbox receives a welcome message containing the per-client login
    URL, their email, the password the CA set, and the role display
    name."""
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk.eywa.farm")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "email_smtp_user", "no-reply@example.com")
    monkeypatch.setattr(settings, "email_smtp_pass", "irrelevant")

    captured: dict = {}

    def fake_send_email(to, subject, html, plain):
        captured.update(to=to, subject=subject, html=html, plain=plain)
        return True

    monkeypatch.setattr(clients_service, "_send_email", fake_send_email)

    ca = await make_user(db, name="CA")
    client = await _seed_active_client(db, short_name="khaza", full_name="Khaza Seeds Pvt Ltd")
    await db.commit()

    out = await add_portal_user(
        client_id=client.id,
        request=PortalUserCreate(
            email="se1@example.com", name="Subject Expert One",
            role=ClientUserRole.SUBJECT_EXPERT, password="TempPass!2026",
        ),
        db=db, current_user=ca,
    )
    assert out.email == "se1@example.com"

    assert captured["to"] == "se1@example.com"
    assert "Khaza Seeds Pvt Ltd" in captured["subject"]
    # Login URL points at the env-driven host with /login/<short>
    assert "https://rstalk.eywa.farm/login/khaza" in captured["html"]
    assert "https://rstalk.eywa.farm/login/khaza" in captured["plain"]
    # Credentials surface in the body
    assert "se1@example.com" in captured["plain"]
    assert "TempPass!2026" in captured["plain"]
    # Role rendered in friendly form (not raw enum)
    assert "Subject Expert" in captured["plain"]
    assert "SUBJECT_EXPERT" not in captured["plain"]


@requires_docker
@pytest.mark.asyncio
async def test_no_welcome_email_when_smtp_unset(db, monkeypatch):
    """Matches the existing approve-client pattern: skip silently
    when SMTP isn't wired (typical local-dev case). No exceptions,
    just no email. The CA can still tell the user the password
    manually since the CA typed it."""
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk.eywa.farm")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "email_smtp_user", "")
    monkeypatch.setattr(settings, "email_smtp_pass", "")

    sent: list = []

    def fake_send_email(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr(clients_service, "_send_email", fake_send_email)

    ca = await make_user(db, name="CA")
    client = await _seed_active_client(db, short_name="indam")
    await db.commit()

    await add_portal_user(
        client_id=client.id,
        request=PortalUserCreate(
            email="se2@example.com", name="SE Two",
            role=ClientUserRole.FIELD_MANAGER, password="TempPass!2026",
        ),
        db=db, current_user=ca,
    )
    assert sent == []


@requires_docker
@pytest.mark.asyncio
async def test_no_welcome_email_for_existing_user(db, monkeypatch):
    """The user already has a User row from another company/role.
    The model layer doesn't overwrite their existing password with
    the new one the CA typed — emailing the typed password would
    promise the recipient access they don't actually have. Suppress
    the welcome email on this path; the cross-client invite flow is
    a separate concern."""
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk.eywa.farm")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "email_smtp_user", "no-reply@example.com")
    monkeypatch.setattr(settings, "email_smtp_pass", "irrelevant")

    sent: list = []

    def fake_send_email(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr(clients_service, "_send_email", fake_send_email)

    ca = await make_user(db, name="CA")
    client = await _seed_active_client(db, short_name="icl")
    # Pre-existing User row — e.g. the same person already serves another company.
    pre_existing = User(
        email="multiclient@example.com", name="Multi-Client Pundit",
        password_hash="$dummy$hash", language_code="en",
    )
    db.add(pre_existing)
    await db.commit()

    await add_portal_user(
        client_id=client.id,
        request=PortalUserCreate(
            email="multiclient@example.com", name="Multi-Client Pundit",
            role=ClientUserRole.SUBJECT_EXPERT, password="WhateverCA-Typed",
        ),
        db=db, current_user=ca,
    )
    assert sent == []


@requires_docker
@pytest.mark.asyncio
async def test_role_display_friendly_for_each_role(db, monkeypatch):
    """Every ClientUserRole that goes through this endpoint surfaces
    a human-friendly label in the email — important because the raw
    enum forms ('CLIENT_RM', 'SEED_DATA_MANAGER', etc.) read poorly."""
    from app.modules.clients.service import humanize_client_user_role
    assert humanize_client_user_role("SUBJECT_EXPERT") == "Subject Expert"
    assert humanize_client_user_role("FIELD_MANAGER") == "Field Manager"
    assert humanize_client_user_role("SEED_DATA_MANAGER") == "Seed Data Manager"
    assert humanize_client_user_role("REPORT_USER") == "Report User"
    assert humanize_client_user_role("CLIENT_RM") == "Client RM"
    assert humanize_client_user_role("PRODUCT_MANAGER") == "Product Manager"
    # Unknown role: graceful fallback to raw value (ugly but not broken).
    assert humanize_client_user_role("FUTURE_NEW_ROLE") == "FUTURE_NEW_ROLE"


@requires_docker
@pytest.mark.asyncio
async def test_404_when_client_id_does_not_exist(db, monkeypatch):
    """Defensive: `add_portal_user` previously would let an orphan
    ClientUser INSERT fail with FK 500. Looking up the Client first
    turns that into a clean 404 for the CA portal."""
    from fastapi import HTTPException
    monkeypatch.setattr(settings, "email_smtp_user", "")

    ca = await make_user(db, name="CA")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await add_portal_user(
            client_id="00000000-0000-0000-0000-000000000000",
            request=PortalUserCreate(
                email="x@example.com", name="X",
                role=ClientUserRole.SUBJECT_EXPERT, password="abc",
            ),
            db=db, current_user=ca,
        )
    assert ei.value.status_code == 404
