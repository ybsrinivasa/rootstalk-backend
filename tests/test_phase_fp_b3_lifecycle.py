"""F-P Assign-Package-to-Farmer — B3 lifecycle (2026-05-29).

Two paths:
  - 72h auto-expire via the Celery task (driven directly against the
    real DB; no broker needed).
  - F-P self-cancel via DELETE /promoter/assignments/{id}.

Both terminate identically except for the new status (EXPIRED vs
CANCELLED_BY_PROMOTER) and the FCM event type. Both refund the unit.

Design lock at memory/project_rootstalk_fp_assign_package_design.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.modules.clients.models import ClientPromoter, ClientStatus
from app.modules.subscriptions.models import (
    AssignmentStatus,
    PromoterAssignment,
    Subscription,
    SubscriptionStatus,
    SubscriptionType,
)
from app.modules.subscriptions.promoter_allocation_models import (
    PromoterAllocation,
)
from app.modules.subscriptions.router import (
    PromoterAssignRequest,
    initiate_assignment,
    my_pending_assignments,
    promoter_cancel_assignment,
)
from app.tasks.assignment_expiry import (
    EXPIRY_HOURS, _expire_assignments_with_session,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_facilitator, make_package, make_user,
)


# ── Seed helpers ────────────────────────────────────────────────────────────

async def _fp_with_assignment(db, *, units: int = 5):
    """Set up an F-P locked to ACTIVE client with `units` in kitty, and
    create one PENDING assignment via the real initiate_assignment so
    the seeded state mirrors production exactly."""
    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    fac = await make_onboarded_facilitator(db, client=client)
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == fac.id,
            ClientPromoter.client_id == client.id,
        )
    )).scalar_one()
    cp.is_promoter = True

    db.add(PromoterAllocation(
        client_id=client.id,
        promoter_user_id=fac.id,
        units_balance=units,
        allocated_total=units,
        reclaimed_total=0,
        consumed_total=0,
        refunded_total=0,
    ))

    farmer = await make_user(db, name="Farmer Lakshmi")
    farmer.phone = "+919800000020"

    pkg = await make_package(db, client, crop_cosh_id="crop:cabbage")
    await db.flush()

    res = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone,
            package_id=pkg.id,
            promoter_type="FACILITATOR",
            farm_area_acres=1.5,
        ),
        db=db,
        current_user=fac,
    )
    return client, fac, farmer, res["assignment_id"], res["subscription_id"]


async def _backdate_assignment(db, assignment_id: str, hours: int):
    """Push assignment.assigned_at into the past by `hours`."""
    await db.execute(
        update(PromoterAssignment)
        .where(PromoterAssignment.id == assignment_id)
        .values(
            assigned_at=datetime.now(timezone.utc) - timedelta(hours=hours),
        )
    )
    await db.flush()


# ── Auto-expire sweep ──────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_expire_flips_pending_older_than_72h(db):
    client, fac, _, assignment_id, sub_id = await _fp_with_assignment(db)
    await _backdate_assignment(db, assignment_id, EXPIRY_HOURS + 1)

    n = await _expire_assignments_with_session(db)
    assert n == 1

    a = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.id == assignment_id)
    )).scalar_one()
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == sub_id)
    )).scalar_one()
    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == fac.id,
        )
    )).scalar_one()

    assert a.status == AssignmentStatus.EXPIRED
    assert sub.status == SubscriptionStatus.CANCELLED
    assert alloc.units_balance == 5     # was 4 after consume, now 5
    assert alloc.consumed_total == 1
    assert alloc.refunded_total == 1


@requires_docker
@pytest.mark.asyncio
async def test_expire_leaves_assignments_younger_than_72h_alone(db):
    _, _, _, assignment_id, _ = await _fp_with_assignment(db)
    await _backdate_assignment(db, assignment_id, EXPIRY_HOURS - 1)

    n = await _expire_assignments_with_session(db)
    assert n == 0

    a = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.id == assignment_id)
    )).scalar_one()
    assert a.status == AssignmentStatus.PENDING_FARMER_APPROVAL


@requires_docker
@pytest.mark.asyncio
async def test_expire_idempotent_on_second_run(db):
    _, _, _, assignment_id, _ = await _fp_with_assignment(db)
    await _backdate_assignment(db, assignment_id, EXPIRY_HOURS + 5)

    first = await _expire_assignments_with_session(db)
    second = await _expire_assignments_with_session(db)
    assert first == 1
    assert second == 0


@requires_docker
@pytest.mark.asyncio
async def test_expire_skips_already_active_assignments(db):
    """An Assignment that's already ACTIVE (farmer approved) must not
    be touched by the expiry sweep even if old."""
    client, fac, _, assignment_id, sub_id = await _fp_with_assignment(db)
    # Flip it to ACTIVE manually + sub to ACTIVE.
    a = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.id == assignment_id)
    )).scalar_one()
    a.status = AssignmentStatus.ACTIVE
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == sub_id)
    )).scalar_one()
    sub.status = SubscriptionStatus.ACTIVE
    await db.flush()
    await _backdate_assignment(db, assignment_id, EXPIRY_HOURS + 5)

    n = await _expire_assignments_with_session(db)
    assert n == 0


# ── F-P self-cancel ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_self_cancel_refunds_and_marks_cancelled_by_promoter(db):
    client, fac, _, assignment_id, sub_id = await _fp_with_assignment(db)

    out = await promoter_cancel_assignment(
        assignment_id=assignment_id, db=db, current_user=fac,
    )

    assert out["assignment_id"] == assignment_id
    assert "Cancelled by promoter" in out["status"]

    a = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.id == assignment_id)
    )).scalar_one()
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == sub_id)
    )).scalar_one()
    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == fac.id,
        )
    )).scalar_one()

    assert a.status == AssignmentStatus.CANCELLED_BY_PROMOTER
    assert a.farmer_responded_at is not None
    assert sub.status == SubscriptionStatus.CANCELLED
    assert alloc.units_balance == 5
    assert alloc.refunded_total == 1


@requires_docker
@pytest.mark.asyncio
async def test_self_cancel_403_when_not_owner(db):
    _, _, _, assignment_id, _ = await _fp_with_assignment(db)
    intruder = await make_user(db, name="Other F-P")

    with pytest.raises(HTTPException) as ei:
        await promoter_cancel_assignment(
            assignment_id=assignment_id, db=db, current_user=intruder,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "not_assignment_owner"


@requires_docker
@pytest.mark.asyncio
async def test_self_cancel_404_when_missing(db):
    fac = await make_user(db, name="Lonely F-P")
    with pytest.raises(HTTPException) as ei:
        await promoter_cancel_assignment(
            assignment_id="does-not-exist", db=db, current_user=fac,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_self_cancel_409_when_already_not_pending(db):
    _, fac, _, assignment_id, sub_id = await _fp_with_assignment(db)
    # Flip to ACTIVE first.
    a = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.id == assignment_id)
    )).scalar_one()
    a.status = AssignmentStatus.ACTIVE
    await db.flush()

    with pytest.raises(HTTPException) as ei:
        await promoter_cancel_assignment(
            assignment_id=assignment_id, db=db, current_user=fac,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "assignment_not_pending"


@requires_docker
@pytest.mark.asyncio
async def test_self_cancel_idempotent_via_409(db):
    """A second cancel returns 409 (not 200-no-op) so the PWA shows
    a clear error if the user double-taps the cancel button."""
    _, fac, _, assignment_id, _ = await _fp_with_assignment(db)
    await promoter_cancel_assignment(
        assignment_id=assignment_id, db=db, current_user=fac,
    )
    with pytest.raises(HTTPException) as ei:
        await promoter_cancel_assignment(
            assignment_id=assignment_id, db=db, current_user=fac,
        )
    assert ei.value.status_code == 409


# ── /promoter/me/pending-assignments (B4 backend addition) ─────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pending_assignments_lists_only_own_pending(db):
    """The endpoint surfaces this F-P's PENDING sends with decoration
    (farmer + package + hours_remaining) and excludes other promoters'
    rows and non-PENDING statuses."""
    _, fac, farmer, assignment_id, _ = await _fp_with_assignment(db)

    # Inject a row for a different promoter — must NOT appear.
    other_promoter = await make_user(db, name="Other Promoter")
    other_sub = (await db.execute(
        select(Subscription)
    )).scalars().first()  # any sub will do for the shape
    db.add(PromoterAssignment(
        subscription_id=other_sub.id,
        promoter_user_id=other_promoter.id,
        promoter_type="FACILITATOR",
        status=AssignmentStatus.PENDING_FARMER_APPROVAL,
    ))
    await db.flush()

    out = await my_pending_assignments(db=db, current_user=fac)
    assert len(out) == 1
    row = out[0]
    assert row["assignment_id"] == assignment_id
    assert row["farmer_name"] == farmer.name
    assert row["farmer_phone"] == farmer.phone
    assert row["package_name"] is not None
    assert row["hours_remaining"] > 0


@requires_docker
@pytest.mark.asyncio
async def test_pending_assignments_excludes_already_cancelled(db):
    """An assignment cancelled by the F-P (self-cancel) should drop
    out of the pending list immediately."""
    _, fac, _, assignment_id, _ = await _fp_with_assignment(db)

    out_before = await my_pending_assignments(db=db, current_user=fac)
    assert len(out_before) == 1

    await promoter_cancel_assignment(
        assignment_id=assignment_id, db=db, current_user=fac,
    )

    out_after = await my_pending_assignments(db=db, current_user=fac)
    assert out_after == []


@requires_docker
@pytest.mark.asyncio
async def test_pending_assignments_hours_remaining_within_window(db):
    """hours_remaining must be in (0, 72] for a fresh assignment."""
    _, fac, _, _, _ = await _fp_with_assignment(db)
    out = await my_pending_assignments(db=db, current_user=fac)
    assert 0 < out[0]["hours_remaining"] <= EXPIRY_HOURS


@requires_docker
@pytest.mark.asyncio
async def test_self_cancel_with_no_kitty_row_still_flips_state(db):
    """Defensive: if the allocation row has been deleted under our
    feet, the cancel still flips Assignment + Subscription. The
    audit-totals view will show the missing refund."""
    _, fac, _, assignment_id, sub_id = await _fp_with_assignment(db)

    # Wipe the allocation row.
    await db.execute(
        PromoterAllocation.__table__.delete().where(
            PromoterAllocation.promoter_user_id == fac.id,
        )
    )
    await db.flush()

    out = await promoter_cancel_assignment(
        assignment_id=assignment_id, db=db, current_user=fac,
    )
    assert "Cancelled by promoter" in out["status"]

    a = (await db.execute(
        select(PromoterAssignment).where(PromoterAssignment.id == assignment_id)
    )).scalar_one()
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == sub_id)
    )).scalar_one()
    assert a.status == AssignmentStatus.CANCELLED_BY_PROMOTER
    assert sub.status == SubscriptionStatus.CANCELLED
