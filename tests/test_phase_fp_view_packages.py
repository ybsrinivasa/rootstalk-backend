"""F-P View Packages — read-only advisory view (2026-05-29).

`GET /promoter/assignments/{subscription_id}/today` mirrors the
farmer's `/farmer/advisory/today` but scoped to one assigned sub,
auth-gated to the F-P who created the assignment, and with CQ
fields stripped so the F-P never sees the farmer's interactive
prompts.

Backed by the F-P design lock in
memory/project_rootstalk_fp_assign_package_design.md (the
"View Packages" follow-on agreed 2026-05-29 after B4).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.subscriptions.models import (
    AssignmentStatus, PromoterAssignment, Subscription, SubscriptionStatus,
)
from app.modules.subscriptions.router import (
    promoter_assignment_today,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_onboarded_facilitator, make_package,
    make_practice, make_subscription, make_timeline, make_user,
)


# ── Setup helper ────────────────────────────────────────────────────────────

async def _fp_active_assignment(db, *, day_offset: int = 5):
    """Seed an F-P + farmer + ACTIVE assignment on an ACTIVE sub whose
    crop_start_date is `day_offset` days ago, with one in-window
    timeline + practice so the today-helper produces output."""
    client = await make_client(db)
    fac = await make_onboarded_facilitator(db, client=client)
    farmer = await make_user(db, name="Farmer Indu")
    farmer.phone = "+919800000030"

    package = await make_package(db, client, crop_cosh_id="crop:onion")
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=package,
    )
    sub.crop_start_date = datetime.now(timezone.utc) - timedelta(days=day_offset)
    assignment = PromoterAssignment(
        subscription_id=sub.id,
        promoter_user_id=fac.id,
        promoter_type="FACILITATOR",
        status=AssignmentStatus.ACTIVE,
    )
    db.add(assignment)

    tl = await make_timeline(
        db, package, name="TL_active",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA",
    )
    await make_element(db, p, value="50", unit_cosh_id="kg_per_acre")
    await db.commit()
    return client, fac, farmer, sub, assignment


# ── Happy path ─────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_promoter_view_returns_advisory_day_for_active_assignment(db):
    _, fac, _, sub, _ = await _fp_active_assignment(db)

    out = await promoter_assignment_today(
        subscription_id=sub.id, db=db, current_user=fac,
    )

    # AdvisoryDay shape — same keys as the farmer endpoint surfaces.
    assert out["subscription_id"] == sub.id
    assert out["package_id"] == sub.package_id
    assert "timelines" in out
    assert len(out["timelines"]) >= 1


@requires_docker
@pytest.mark.asyncio
async def test_promoter_view_strips_conditional_question_fields(db):
    """Even if a timeline carries a pending CQ, the F-P response
    must not surface it (`pending_conditional_question` removed,
    `has_pending_question` set to False)."""
    _, fac, _, sub, _ = await _fp_active_assignment(db)
    out = await promoter_assignment_today(
        subscription_id=sub.id, db=db, current_user=fac,
    )
    for tl in out["timelines"]:
        assert tl.get("pending_conditional_question") is None
        assert tl.get("blank_path_questions") is None
        # has_pending_question may be absent (CCA) or False (defensively
        # set) but must never be True for an F-P response.
        assert tl.get("has_pending_question", False) is False


# ── Auth gate ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_promoter_view_404_when_not_assignment_owner(db):
    _, _, _, sub, _ = await _fp_active_assignment(db)
    intruder = await make_user(db, name="Other promoter")

    with pytest.raises(HTTPException) as ei:
        await promoter_assignment_today(
            subscription_id=sub.id, db=db, current_user=intruder,
        )
    # 404, not 403 — don't leak existence to a probe.
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_promoter_view_404_when_subscription_id_unknown(db):
    fac = await make_user(db, name="Lonely F-P")
    with pytest.raises(HTTPException) as ei:
        await promoter_assignment_today(
            subscription_id="does-not-exist", db=db, current_user=fac,
        )
    assert ei.value.status_code == 404


# ── State gate ─────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_promoter_view_409_when_assignment_not_active(db):
    """PENDING (farmer hasn't accepted yet) → 409. The F-P can withdraw
    via the B3 self-cancel endpoint but cannot view the advisory."""
    _, fac, _, sub, assignment = await _fp_active_assignment(db)
    assignment.status = AssignmentStatus.PENDING_FARMER_APPROVAL
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await promoter_assignment_today(
            subscription_id=sub.id, db=db, current_user=fac,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "assignment_not_active"


# ── Empty-advisory edge case ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_promoter_view_404_no_advisory_yet_when_no_crop_start(db):
    """Active assignment but farmer hasn't set crop_start_date yet →
    404 no_advisory_yet so the PWA renders an explicit empty state
    rather than a half-empty advisory shell."""
    _, fac, _, sub, _ = await _fp_active_assignment(db)
    # Clear crop_start_date — simulate farmer hasn't set sowing yet.
    await db.execute(
        Subscription.__table__.update()
        .where(Subscription.id == sub.id)
        .values(crop_start_date=None)
    )
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await promoter_assignment_today(
            subscription_id=sub.id, db=db, current_user=fac,
        )
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == "no_advisory_yet"
