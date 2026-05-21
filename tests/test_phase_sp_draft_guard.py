"""CA-SP Phase 1 (2026-05-21) — `_assert_sp_draft` on every
SP mutation endpoint.

Pre-fix: the SP mutation endpoints only gated on
`_assert_can_edit_client_advisory` (privilege only) and let writes
against ACTIVE rows go through silently. The published SP would
mutate under farmers' feet without a publish event.

This file pins the new behaviour: every mutation refuses non-DRAFT
SPs with 422 + `sp_not_draft`. DRAFT operations pass through.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.modules.advisory.models import (
    PracticeL0, TimelineFromType,
)
from app.modules.advisory.router import (
    _assert_sp_draft, add_sp_practice, add_sp_timeline,
    delete_sp_timeline, update_sp_timeline,
)
from app.modules.advisory.schemas import (
    PGTimelineUpdate, SPPracticeCreate, SPTimelineCreate,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_sp_recommendation, make_sp_timeline, make_user,
)


# ── Helper itself ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_assert_sp_draft_passes_on_draft(db):
    client = await make_client(db)
    sp = await make_sp_recommendation(db, client)
    sp.status = "DRAFT"
    await db.commit()
    out = await _assert_sp_draft(db, sp.id, client_id=client.id)
    assert out.id == sp.id


@requires_docker
@pytest.mark.asyncio
async def test_assert_sp_draft_rejects_active(db):
    client = await make_client(db)
    sp = await make_sp_recommendation(db, client)
    sp.status = "ACTIVE"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_sp_draft(db, sp.id, client_id=client.id)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "sp_not_draft"
    assert exc.value.detail["current_status"] == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_assert_sp_draft_rejects_inactive(db):
    client = await make_client(db)
    sp = await make_sp_recommendation(db, client)
    sp.status = "INACTIVE"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_sp_draft(db, sp.id, client_id=client.id)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "sp_not_draft"


@requires_docker
@pytest.mark.asyncio
async def test_assert_sp_draft_404_when_missing(db):
    client = await make_client(db)
    user = await make_user(db, name="404 caller")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await _assert_sp_draft(db, "nonexistent-id", client_id=client.id)
    assert exc.value.status_code == 404


# ── Mutation endpoints — sweep ──────────────────────────────────────────────
# Each endpoint must 422 with sp_not_draft when the SP is ACTIVE.

async def _seed_active_sp(db):
    user = await make_user(db, name="SE")
    client = await make_client(db)
    sp = await make_sp_recommendation(db, client)
    sp.status = "ACTIVE"
    await db.commit()
    return user, client, sp


@requires_docker
@pytest.mark.asyncio
async def test_add_sp_timeline_refused_on_active(db):
    user, client, sp = await _seed_active_sp(db)
    with pytest.raises(HTTPException) as exc:
        await add_sp_timeline(
            client_id=client.id, sp_id=sp.id,
            request=SPTimelineCreate(
                name="TL1", from_type=TimelineFromType.DAYS_AFTER_DETECTION,
                from_value=0, to_value=7,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "sp_not_draft"


@requires_docker
@pytest.mark.asyncio
async def test_update_sp_timeline_refused_on_active(db):
    user, client, sp = await _seed_active_sp(db)
    sp.status = "DRAFT"  # add a timeline first
    await db.commit()
    tl = await make_sp_timeline(db, sp)
    sp.status = "ACTIVE"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_sp_timeline(
            client_id=client.id, sp_id=sp.id, tl_id=tl.id,
            request=PGTimelineUpdate(name="renamed"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "sp_not_draft"


@requires_docker
@pytest.mark.asyncio
async def test_delete_sp_timeline_refused_on_active(db):
    user, client, sp = await _seed_active_sp(db)
    sp.status = "DRAFT"
    await db.commit()
    tl = await make_sp_timeline(db, sp)
    sp.status = "ACTIVE"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await delete_sp_timeline(
            client_id=client.id, sp_id=sp.id, tl_id=tl.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "sp_not_draft"


@requires_docker
@pytest.mark.asyncio
async def test_add_sp_practice_refused_on_active(db):
    user, client, sp = await _seed_active_sp(db)
    sp.status = "DRAFT"
    await db.commit()
    tl = await make_sp_timeline(db, sp)
    sp.status = "ACTIVE"
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await add_sp_practice(
            client_id=client.id, sp_id=sp.id, tl_id=tl.id,
            request=SPPracticeCreate(
                l0_type=PracticeL0.NON_INPUT,
                l1_type=None, l2_type="ITKS",
                display_order=0, is_special_input=False,
                elements=[],
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "sp_not_draft"


@requires_docker
@pytest.mark.asyncio
async def test_draft_mutations_pass_through(db):
    """Sanity — none of these gates broke the happy DRAFT path."""
    user = await make_user(db, name="SE happy")
    client = await make_client(db)
    sp = await make_sp_recommendation(db, client)
    sp.status = "DRAFT"
    await db.commit()
    tl = await add_sp_timeline(
        client_id=client.id, sp_id=sp.id,
        request=SPTimelineCreate(
            name="TL_OK", from_type=TimelineFromType.DAYS_AFTER_DETECTION,
            from_value=0, to_value=14,
        ),
        db=db, current_user=user,
    )
    assert tl is not None
    out = await update_sp_timeline(
        client_id=client.id, sp_id=sp.id, tl_id=tl.id,
        request=PGTimelineUpdate(name="TL_renamed"),
        db=db, current_user=user,
    )
    assert out.name == "TL_renamed"
