"""GET /platform/lookup-user-by-phone — DB-backed integration tests.

The PWA calls this whenever a farmer (or any user) types another
person's phone number into a recipient field (alerts, payment
delegation, custom-order routing, helper assignment) and needs to
verify who they're about to add.

Three scenarios:
  - 10-digit input matches an existing user → found + name + photo + roles
  - +91-prefixed input with formatting (spaces, dashes) → normalised + matched
  - phone that doesn't match any user → 200 with found: false
"""
from __future__ import annotations

import pytest

from app.modules.platform.models import RoleType, StatusEnum, UserRole
from app.modules.platform.router import lookup_user_by_phone
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_lookup_matches_registered_user_by_bare_10_digits(db):
    # make_user gives a +91-prefixed phone; reuse it via the bare 10-digit form.
    target = await make_user(db, name="Dealer D")
    target.phone = "+919123456789"
    target.name = "Dealer Demo"
    target.photo_url = "https://example.com/photo.jpg"
    # Override the default CM-only role with DEALER for this test.
    db.add(UserRole(user_id=target.id, role_type=RoleType.DEALER, status=StatusEnum.ACTIVE))
    await db.commit()

    caller = await make_user(db, name="Farmer caller")
    await db.commit()

    out = await lookup_user_by_phone(
        phone="9123456789", db=db, current_user=caller,
    )

    assert out["found"] is True
    assert out["user_id"] == target.id
    assert out["name"] == "Dealer Demo"
    assert out["photo_url"] == "https://example.com/photo.jpg"
    assert "DEALER" in out["roles"]
    assert out["phone"] == "+919123456789"


@requires_docker
@pytest.mark.asyncio
async def test_lookup_normalises_messy_input(db):
    target = await make_user(db, name="Facilitator F")
    target.phone = "+919234567890"
    await db.commit()

    caller = await make_user(db, name="Farmer caller 2")
    await db.commit()

    # Spaces, dashes, extra +91 — all should normalise to last 10 digits.
    for messy in ("+91 92345 67890", "91-92345-67890", "+919234567890", "9234567890"):
        out = await lookup_user_by_phone(phone=messy, db=db, current_user=caller)
        assert out["found"] is True, f"failed on input: {messy!r}"
        assert out["user_id"] == target.id


@requires_docker
@pytest.mark.asyncio
async def test_lookup_unregistered_phone_returns_found_false(db):
    caller = await make_user(db, name="Farmer caller 3")
    await db.commit()

    out = await lookup_user_by_phone(
        phone="9000000001", db=db, current_user=caller,
    )

    assert out["found"] is False
    # Echoes the normalised input so the caller can show "we looked for X".
    assert out["phone"] == "+919000000001"


@requires_docker
@pytest.mark.asyncio
async def test_lookup_short_input_short_circuits(db):
    """Fewer than 10 digits → don't even hit the DB; return found: false."""
    caller = await make_user(db, name="Farmer caller 4")
    await db.commit()

    out = await lookup_user_by_phone(phone="123", db=db, current_user=caller)
    assert out["found"] is False
    assert out["phone"] == "123"  # original passed through
