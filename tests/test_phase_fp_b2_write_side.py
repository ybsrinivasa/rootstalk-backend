"""F-P Assign-Package-to-Farmer — B2 write-side (2026-05-29).

Covers:
- Extended PromoterAssignRequest validation (P-V branches, mismatch
  guards).
- initiate_assignment server-derives client_id for F-P from the
  locked binding.
- P-V answers persist on Subscription with confirmed_at stamped.
- Farmer reject auto-refunds the unit via refund_to_promoter.
- consumed_total stays as ever-consumed; refunded_total is the
  cancelling running total.
- Idempotency: the BL-11 transition guard prevents a second reject
  from double-refunding.

Backed by the design lock at
memory/project_rootstalk_fp_assign_package_design.md.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PackageLocation, PackageStatus  # noqa: F401
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
    respond_to_assignment,
)
from app.services.promoter_pool import (
    get_company_unallocated_balance,
    refund_to_promoter,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_facilitator, make_package, make_user,
)


# ── Setup helpers ───────────────────────────────────────────────────────────

async def _fp_with_kitty(db, *, units: int = 5):
    """Seeded F-P bound to ACTIVE client with `units` in kitty + a
    farmer + a Package the F-P can assign."""
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

    farmer = await make_user(db, name="Farmer Asha")
    farmer.phone = "+919800000010"

    pkg = await make_package(db, client, crop_cosh_id="crop:cucumber")
    await db.flush()
    return client, fac, farmer, pkg


# ── PromoterAssignRequest validation ───────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_initiate_persists_plant_wise_answers(db):
    client, fac, farmer, pkg = await _fp_with_kitty(db)

    res = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone,
            package_id=pkg.id,
            promoter_type="FACILITATOR",
            number_of_plants=120,
            planting_year=2024,
        ),
        db=db,
        current_user=fac,
    )

    sub = (await db.execute(
        select(Subscription).where(Subscription.id == res["subscription_id"])
    )).scalar_one()
    assert sub.subscription_type == SubscriptionType.ASSIGNED
    assert sub.status == SubscriptionStatus.WAITLISTED
    assert sub.client_id == client.id   # server-derived
    assert sub.number_of_plants == 120
    assert sub.planting_year == 2024
    assert sub.plant_count_confirmed_at is not None
    assert sub.farm_area_acres is None


@requires_docker
@pytest.mark.asyncio
async def test_initiate_persists_area_wise_answers(db):
    client, fac, farmer, pkg = await _fp_with_kitty(db)

    res = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone,
            package_id=pkg.id,
            promoter_type="FACILITATOR",
            farm_area_acres=2.5,
        ),
        db=db,
        current_user=fac,
    )

    sub = (await db.execute(
        select(Subscription).where(Subscription.id == res["subscription_id"])
    )).scalar_one()
    assert float(sub.farm_area_acres) == 2.5
    assert sub.farm_area_confirmed_at is not None
    assert sub.number_of_plants is None
    assert sub.plant_count_confirmed_at is None


@requires_docker
@pytest.mark.asyncio
async def test_initiate_rejects_when_no_measure_given(db):
    _, fac, farmer, pkg = await _fp_with_kitty(db)

    with pytest.raises(HTTPException) as ei:
        await initiate_assignment(
            request=PromoterAssignRequest(
                farmer_phone=farmer.phone,
                package_id=pkg.id,
                promoter_type="FACILITATOR",
            ),
            db=db, current_user=fac,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "measure_required"


@requires_docker
@pytest.mark.asyncio
async def test_initiate_rejects_when_both_measures_given(db):
    _, fac, farmer, pkg = await _fp_with_kitty(db)

    with pytest.raises(HTTPException) as ei:
        await initiate_assignment(
            request=PromoterAssignRequest(
                farmer_phone=farmer.phone,
                package_id=pkg.id,
                promoter_type="FACILITATOR",
                farm_area_acres=1.0,
                number_of_plants=50,
                planting_year=2023,
            ),
            db=db, current_user=fac,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "measure_required"


@requires_docker
@pytest.mark.asyncio
async def test_initiate_rejects_partial_plant_wise(db):
    _, fac, farmer, pkg = await _fp_with_kitty(db)

    with pytest.raises(HTTPException) as ei:
        await initiate_assignment(
            request=PromoterAssignRequest(
                farmer_phone=farmer.phone,
                package_id=pkg.id,
                promoter_type="FACILITATOR",
                number_of_plants=50,   # planting_year missing
            ),
            db=db, current_user=fac,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "plant_wise_incomplete"


# ── Server-derived client_id + mismatch guard ──────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_initiate_derives_client_id_when_omitted(db):
    """F-P can omit client_id entirely; the server fills it from the
    locked binding."""
    client, fac, farmer, pkg = await _fp_with_kitty(db)
    res = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone,
            package_id=pkg.id,
            promoter_type="FACILITATOR",
            farm_area_acres=1.0,
            # client_id absent
        ),
        db=db, current_user=fac,
    )
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == res["subscription_id"])
    )).scalar_one()
    assert sub.client_id == client.id


@requires_docker
@pytest.mark.asyncio
async def test_initiate_403_on_client_id_mismatch_for_facilitator(db):
    _, fac, farmer, pkg = await _fp_with_kitty(db)
    bogus = await make_client(db)
    bogus.status = ClientStatus.ACTIVE

    with pytest.raises(HTTPException) as ei:
        await initiate_assignment(
            request=PromoterAssignRequest(
                farmer_phone=farmer.phone,
                package_id=pkg.id,
                promoter_type="FACILITATOR",
                client_id=bogus.id,   # mismatch
                farm_area_acres=1.0,
            ),
            db=db, current_user=fac,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "client_id_mismatch"


# ── Farmer reject → auto-refund ────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_farmer_reject_refunds_unit_to_promoter(db):
    client, fac, farmer, pkg = await _fp_with_kitty(db, units=5)

    res = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone,
            package_id=pkg.id,
            promoter_type="FACILITATOR",
            farm_area_acres=1.0,
        ),
        db=db, current_user=fac,
    )

    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == fac.id,
        )
    )).scalar_one()
    assert alloc.units_balance == 4
    assert alloc.consumed_total == 1
    assert alloc.refunded_total == 0

    # Farmer rejects.
    await respond_to_assignment(
        subscription_id=res["subscription_id"],
        data={"approved": False},
        db=db, current_user=farmer,
    )

    await db.refresh(alloc)
    assert alloc.units_balance == 5         # refunded
    assert alloc.consumed_total == 1        # still 1 (ever-consumed)
    assert alloc.refunded_total == 1

    assignment = (await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.subscription_id == res["subscription_id"]
        )
    )).scalar_one()
    assert assignment.status == AssignmentStatus.REJECTED_BY_FARMER


@requires_docker
@pytest.mark.asyncio
async def test_farmer_reject_second_call_does_not_double_refund(db):
    """BL-11 transition guard prevents a second reject from reaching
    the refund path."""
    client, fac, farmer, pkg = await _fp_with_kitty(db, units=3)

    res = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone, package_id=pkg.id,
            promoter_type="FACILITATOR", farm_area_acres=1.0,
        ),
        db=db, current_user=fac,
    )
    await respond_to_assignment(
        subscription_id=res["subscription_id"],
        data={"approved": False}, db=db, current_user=farmer,
    )

    # Second reject — must raise (BL-11 guard) and not touch the
    # allocation again.
    with pytest.raises(HTTPException):
        await respond_to_assignment(
            subscription_id=res["subscription_id"],
            data={"approved": False}, db=db, current_user=farmer,
        )

    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == fac.id,
        )
    )).scalar_one()
    assert alloc.refunded_total == 1


@requires_docker
@pytest.mark.asyncio
async def test_farmer_approve_does_not_refund(db):
    """Sanity: approval path leaves consumed_total in place, no
    refund."""
    client, fac, farmer, pkg = await _fp_with_kitty(db, units=4)

    res = await initiate_assignment(
        request=PromoterAssignRequest(
            farmer_phone=farmer.phone, package_id=pkg.id,
            promoter_type="FACILITATOR", farm_area_acres=1.0,
        ),
        db=db, current_user=fac,
    )
    await respond_to_assignment(
        subscription_id=res["subscription_id"],
        data={"approved": True}, db=db, current_user=farmer,
    )

    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client.id,
            PromoterAllocation.promoter_user_id == fac.id,
        )
    )).scalar_one()
    assert alloc.units_balance == 3
    assert alloc.consumed_total == 1
    assert alloc.refunded_total == 0


# ── refund_to_promoter service ─────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_refund_service_raises_on_missing_allocation(db):
    """Calling refund on a (client, promoter) pair with no allocation
    row is a programmer error — surface it loudly."""
    client = await make_client(db)
    user = await make_user(db, name="Phantom")
    with pytest.raises(ValueError):
        await refund_to_promoter(
            db, client_id=client.id, promoter_user_id=user.id,
        )


# ── Unallocated-balance formula accounts for refunded ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_unallocated_balance_accounts_for_refunded(db):
    """A consumed-then-refunded unit must NOT be double-counted against
    the company's unallocated balance. Equivalent to: refund moves the
    unit from `consumed` to `available-to-reclaim` without inflating
    the spent column."""
    from app.modules.subscriptions.models import SubscriptionPool

    client = await make_client(db)
    client.status = ClientStatus.ACTIVE
    fac = await make_user(db, name="P")
    db.add(SubscriptionPool(
        client_id=client.id, units_purchased=10, units_consumed=0,
    ))
    db.add(PromoterAllocation(
        client_id=client.id, promoter_user_id=fac.id,
        units_balance=5,         # 5 sitting with promoter
        allocated_total=5,
        reclaimed_total=0,
        consumed_total=2,        # 2 historically consumed
        refunded_total=1,        # 1 of those came back
    ))
    await db.flush()

    bal = await get_company_unallocated_balance(db, client.id)
    # 10 purchased − 5 in-promoter-row − 2 consumed + 1 refunded = 4
    assert bal == 4
