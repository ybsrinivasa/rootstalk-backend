"""POST /platform/fcm-token — DB-backed integration tests.

The PWA calls this route to register the device's FCM
registration token against the authenticated user's row. Single
device per user for V1; the value replaces any prior token.
Passing `null` clears the token (used on logout).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.platform.models import User
from app.modules.platform.router import register_fcm_token
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_register_fcm_token_stores_token_on_user_row(db):
    user = await make_user(db, name="Farmer F")
    await db.commit()
    assert user.fcm_token is None

    out = await register_fcm_token(
        data={"token": "fake-fcm-registration-token-abcdef"},
        db=db, current_user=user,
    )
    assert out["detail"].startswith("FCM token registered")

    refreshed = (await db.execute(
        select(User).where(User.id == user.id)
    )).scalar_one()
    assert refreshed.fcm_token == "fake-fcm-registration-token-abcdef"


@requires_docker
@pytest.mark.asyncio
async def test_register_fcm_token_replaces_existing_token(db):
    """Single-device V1: a new token from the same user simply
    overwrites. (Multi-device support deferred to V2 — separate
    table.)"""
    user = await make_user(db, name="Farmer R")
    user.fcm_token = "old-token"
    await db.commit()

    await register_fcm_token(
        data={"token": "new-token"},
        db=db, current_user=user,
    )
    refreshed = (await db.execute(
        select(User).where(User.id == user.id)
    )).scalar_one()
    assert refreshed.fcm_token == "new-token"


@requires_docker
@pytest.mark.asyncio
async def test_register_fcm_token_with_null_clears(db):
    """On logout the PWA calls this with token=null to invalidate
    push notifications to the device."""
    user = await make_user(db, name="Farmer C")
    user.fcm_token = "to-be-cleared"
    await db.commit()

    out = await register_fcm_token(
        data={"token": None}, db=db, current_user=user,
    )
    assert "cleared" in out["detail"].lower()
    refreshed = (await db.execute(
        select(User).where(User.id == user.id)
    )).scalar_one()
    assert refreshed.fcm_token is None


@requires_docker
@pytest.mark.asyncio
async def test_register_fcm_token_with_whitespace_only_treated_as_clear(db):
    """A `"   "` payload from a buggy client is normalised to None
    rather than stored as a useless whitespace token."""
    user = await make_user(db, name="Farmer W")
    user.fcm_token = "still-there"
    await db.commit()

    await register_fcm_token(
        data={"token": "   "}, db=db, current_user=user,
    )
    refreshed = (await db.execute(
        select(User).where(User.id == user.id)
    )).scalar_one()
    assert refreshed.fcm_token is None


@requires_docker
@pytest.mark.asyncio
async def test_register_fcm_token_rejects_non_string_payload(db):
    """A misbehaving client sending `{"token": 123}` gets a 422
    rather than silently coercing to "123" and storing nonsense."""
    user = await make_user(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await register_fcm_token(
            data={"token": 123}, db=db, current_user=user,
        )
    assert exc.value.status_code == 422
