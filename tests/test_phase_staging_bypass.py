"""POST /farmer/subscriptions/{id}/payment/staging-bypass — tests.

The bypass flips a WAITLISTED subscription to ACTIVE without
going through Razorpay. Used for demos and end-to-end testing
where Razorpay's test mode can't process real UPI handles. Must
refuse hard in production.
"""
from __future__ import annotations

import pytest

from app.modules.subscriptions.models import SubscriptionStatus
from app.modules.subscriptions.router import staging_bypass_activation
from fastapi import HTTPException
from tests.conftest import requires_docker
from tests.factories import make_client, make_package, make_subscription, make_user


@requires_docker
@pytest.mark.asyncio
async def test_bypass_flips_waitlisted_to_active_and_mints_reference(db, monkeypatch):
    """Happy path on staging: WAITLISTED → ACTIVE, BL-15 reference number
    is generated, subscription_date is stamped."""
    from app.config import settings
    monkeypatch.setattr(settings, "environment", "staging")

    user = await make_user(db, name="Farmer S")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=pkg,
    )
    # Factory hardcodes ACTIVE; bypass entry point is WAITLISTED.
    sub.status = SubscriptionStatus.WAITLISTED
    sub.reference_number = None
    sub.subscription_date = None
    await db.commit()
    assert sub.reference_number is None
    assert sub.subscription_date is None

    out = await staging_bypass_activation(
        subscription_id=sub.id, db=db, current_user=user,
    )

    assert out["status"] == SubscriptionStatus.ACTIVE
    assert out["bypass"] is True
    assert out["reference_number"] is not None
    # BL-15 format: ClientCode-YY-NNNNNN
    assert len(out["reference_number"].split("-")) == 3
    await db.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.subscription_date is not None


@requires_docker
@pytest.mark.asyncio
async def test_bypass_refuses_in_production(db, monkeypatch):
    """Hard gate: production must never permit this endpoint."""
    from app.config import settings
    monkeypatch.setattr(settings, "environment", "production")

    user = await make_user(db, name="Farmer P")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=pkg,
    )
    # Factory hardcodes ACTIVE; bypass entry point is WAITLISTED.
    sub.status = SubscriptionStatus.WAITLISTED
    sub.reference_number = None
    sub.subscription_date = None
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await staging_bypass_activation(
            subscription_id=sub.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 403
    assert "production" in exc.value.detail.lower()

    # Subscription must remain WAITLISTED — no side effects.
    await db.refresh(sub)
    assert sub.status == SubscriptionStatus.WAITLISTED
    assert sub.reference_number is None


@requires_docker
@pytest.mark.asyncio
async def test_bypass_is_idempotent_on_already_active(db, monkeypatch):
    """A second call against an already-ACTIVE sub must be rejected
    (BL-11 transition guard fires), not silently re-stamp the
    subscription_date or re-mint a reference."""
    from app.config import settings
    monkeypatch.setattr(settings, "environment", "staging")

    user = await make_user(db, name="Farmer I")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=pkg,
    )
    # Factory hardcodes ACTIVE; bypass entry point is WAITLISTED.
    sub.status = SubscriptionStatus.WAITLISTED
    sub.reference_number = None
    sub.subscription_date = None
    await db.commit()

    out1 = await staging_bypass_activation(
        subscription_id=sub.id, db=db, current_user=user,
    )
    ref1 = out1["reference_number"]
    sub_date_1 = sub.subscription_date

    # Second call must be refused by the BL-11 transition validator.
    with pytest.raises(HTTPException):
        await staging_bypass_activation(
            subscription_id=sub.id, db=db, current_user=user,
        )

    await db.refresh(sub)
    assert sub.reference_number == ref1
    assert sub.subscription_date == sub_date_1
