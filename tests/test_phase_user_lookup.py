"""Phone-based user lookup endpoint — M4 of the audit (Sub-batch 3).

Used by the CA portal's Promoter register form: when the CA blurs
the phone field, the form calls this endpoint to surface 'this is
an existing RootsTalk user' inline. Without it, the register-
promoter call silently attaches the new ClientPromoter row to a
pre-existing User and the CA gets no UX signal.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.clients.router import lookup_user_by_phone
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_lookup_returns_existing_user(db):
    target = await make_user(db, name="Existing Person")
    target.phone = "+919900001234"
    caller = await make_user(db, name="Caller")
    await db.commit()

    out = await lookup_user_by_phone(
        phone="+919900001234", db=db, current_user=caller,
    )
    assert out["exists"] is True
    assert out["name"] == "Existing Person"


@requires_docker
@pytest.mark.asyncio
async def test_lookup_returns_not_exists(db):
    """Phone with no matching User → exists=False, name=None.
    Lets the frontend render 'New user will be created' confidently."""
    caller = await make_user(db, name="Caller")
    await db.commit()

    out = await lookup_user_by_phone(
        phone="+918888888888", db=db, current_user=caller,
    )
    assert out["exists"] is False
    assert out["name"] is None


@requires_docker
@pytest.mark.asyncio
async def test_lookup_rejects_empty_phone(db):
    """Defensive 422 — guards against the frontend firing a lookup
    on an empty/cleared phone field."""
    caller = await make_user(db, name="Caller")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await lookup_user_by_phone(
            phone="", db=db, current_user=caller,
        )
    assert ei.value.status_code == 422
