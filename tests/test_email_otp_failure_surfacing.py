"""Surfacing email-send failures from the auth flow.

Pre-fix `_send_email` swallowed any SMTP exception and the route
returned 200 "OTP sent" whether or not the email actually went out.
These tests pin the new behaviour: in production-like envs the route
turns a False return from `_send_email` into a 503 so the user sees
the failure instead of a silent lie.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.auth import router as auth_router
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_request_email_otp_returns_503_in_prod_when_send_fails(db, monkeypatch):
    """Force production env + force _send_email to fail. Route must
    raise 503 with a clear message (not silently 200)."""
    user = await make_user(db)
    user.email = "sa@example.com"
    await db.commit()

    monkeypatch.setattr(auth_router.settings, "environment", "production")
    monkeypatch.setattr(auth_router, "_send_email", lambda *a, **k: False)

    with pytest.raises(HTTPException) as exc:
        await auth_router.request_email_otp(
            data={"email": "sa@example.com", "purpose": "LOGIN"},
            db=db,
        )
    assert exc.value.status_code == 503
    assert "email otp" in exc.value.detail.lower()


@requires_docker
@pytest.mark.asyncio
async def test_request_email_otp_returns_dev_otp_in_dev_even_when_send_fails(db, monkeypatch):
    """Dev environment short-circuits SMTP — the OTP comes back in the
    response body so the developer is unblocked even when no SMTP
    creds are configured. Pin this so a future refactor doesn't
    accidentally start raising 503 in dev too."""
    user = await make_user(db)
    user.email = "dev@example.com"
    await db.commit()

    monkeypatch.setattr(auth_router.settings, "environment", "development")
    monkeypatch.setattr(auth_router, "_send_email", lambda *a, **k: False)

    out = await auth_router.request_email_otp(
        data={"email": "dev@example.com", "purpose": "LOGIN"},
        db=db,
    )
    assert out["dev_otp"] is not None
    assert len(out["dev_otp"]) == 6


@requires_docker
@pytest.mark.asyncio
async def test_forgot_password_503_when_send_fails_for_known_email(db, monkeypatch):
    """For a real email the password-reset path now surfaces SMTP
    failure as 503 instead of returning the generic "if registered"
    message. Unknown emails still get the generic message (anti-
    enumeration) — pinned in the next test."""
    user = await make_user(db)
    user.email = "ca@example.com"
    user.password_hash = "x"  # irrelevant for the request path
    await db.commit()

    monkeypatch.setattr(auth_router.settings, "environment", "production")
    monkeypatch.setattr(auth_router, "_send_email", lambda *a, **k: False)

    with pytest.raises(HTTPException) as exc:
        await auth_router.forgot_password(
            data={"email": "ca@example.com"}, db=db,
        )
    assert exc.value.status_code == 503


@requires_docker
@pytest.mark.asyncio
async def test_forgot_password_silent_for_unknown_email_anti_enumeration(db, monkeypatch):
    """Anti-enumeration: an unknown email always gets the same generic
    message regardless of SMTP state, so an attacker can't probe
    which addresses are registered."""
    monkeypatch.setattr(auth_router.settings, "environment", "production")
    # _send_email shouldn't be called at all for an unknown email.
    called = []
    monkeypatch.setattr(
        auth_router, "_send_email",
        lambda *a, **k: called.append(a) or False,
    )

    out = await auth_router.forgot_password(
        data={"email": "nobody@example.com"}, db=db,
    )
    assert "if this email is registered" in out["detail"].lower()
    assert called == []
