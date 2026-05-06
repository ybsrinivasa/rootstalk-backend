"""BL-12 audit — DB-backed integration tests for the holder/state/
preference guards from batch 2 plus the rewired expiry sweep.

Pure-function coverage of the routing-priority service lives in
`tests/test_bl12.py` (11 tests), and the state machine in
`tests/test_bl12_state.py` (15 tests). This file drives the FastAPI
route handlers + the expiry sweep directly against the testcontainer
DB to verify the guards land correctly end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditPreference, FarmPunditProfile,
    PunditRole, Query, QueryStatus,
)
from app.modules.farmpundit.router import (
    clear_pundit_preference, forward_query, reject_query,
    respond_to_query, return_query, set_pundit_preference,
)
from app.tasks.query_expiry import _expire_queries_with_session
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


async def _make_pundit(db, *, name: str = "Pundit"):
    """Create a User + matching FarmPunditProfile. Returns (user, profile)."""
    user = await make_user(db, name=name)
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    return user, profile


async def _enrol(db, *, client_id: str, profile, role: PunditRole):
    cp = ClientFarmPundit(
        client_id=client_id, pundit_id=profile.id, role=role, status="ACTIVE",
    )
    db.add(cp)
    await db.flush()
    return cp


async def _seed_query_with_two_pundits(db, *, holder_role: PunditRole = PunditRole.PRIMARY):
    """Seed: client + pkg + sub + farmer + two pundits (one is the
    current holder of a NEW query, the other is an outsider). Returns
    (query, client, holder_user, holder_profile, other_user, other_profile)."""
    farmer = await make_user(db, name="Farmer Q")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    await db.commit()

    holder_user, holder_profile = await _make_pundit(db, name="Holder")
    other_user, other_profile = await _make_pundit(db, name="Outsider")
    await _enrol(db, client_id=client.id, profile=holder_profile, role=holder_role)
    await _enrol(db, client_id=client.id, profile=other_profile, role=PunditRole.PRIMARY)

    query = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Test query", severity="MEDIUM",
        status=QueryStatus.NEW,
        current_holder_id=holder_profile.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(query)
    await db.commit()
    return query, client, holder_user, holder_profile, other_user, other_profile


# ── Holder check on respond/forward/return/reject ─────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_respond_rejects_non_holder_pundit(db):
    """A pundit who isn't current_holder_id of the query gets 403 —
    pre-fix any authenticated pundit could respond to any query."""
    query, _, _, _, other_user, _ = await _seed_query_with_two_pundits(db)
    with pytest.raises(HTTPException) as exc:
        await respond_to_query(
            query_id=query.id,
            data={"text": "Some response"},
            db=db, current_user=other_user,
        )
    assert exc.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_reject_rejects_non_holder_pundit(db):
    query, _, _, _, other_user, _ = await _seed_query_with_two_pundits(db)
    with pytest.raises(HTTPException) as exc:
        await reject_query(
            query_id=query.id,
            data={"remarks": "Cannot help"},
            db=db, current_user=other_user,
        )
    assert exc.value.status_code == 403


# ── PRIMARY-only rule on reject ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_panel_pundit_cannot_reject_a_query(db):
    """Spec: 'Primary Expert only' for reject. Pre-fix the live route
    didn't enforce this — a PANEL pundit holding the query could
    reject it. Now caught by the validate_transition role-set."""
    query, _, holder_user, _, _, _ = await _seed_query_with_two_pundits(
        db, holder_role=PunditRole.PANEL,
    )
    with pytest.raises(HTTPException) as exc:
        await reject_query(
            query_id=query.id,
            data={"remarks": "Out of my expertise"},
            db=db, current_user=holder_user,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "ROLE_NOT_ALLOWED"


# ── Terminal-state guards ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_forward_blocked_on_already_responded_query(db):
    """RESPONDED is terminal. A late forward request must not flip
    the status field on a query that's already been answered."""
    query, _, holder_user, _, _, other_profile = await _seed_query_with_two_pundits(db)
    query.status = QueryStatus.RESPONDED
    query.current_holder_id = None
    await db.commit()

    # The holder ownership check fires first — but pre-fix even setting
    # current_holder_id back to the holder would let the forward flip
    # status. We assert the holder-check 403 here to confirm the
    # ownership gate is the first line of defence; the deeper
    # ILLEGAL_TRANSITION guard is exercised in the next test where
    # the holder DOES match.
    with pytest.raises(HTTPException) as exc:
        await forward_query(
            query_id=query.id,
            data={"to_pundit_id": other_profile.id, "remarks": "Late forward"},
            db=db, current_user=holder_user,
        )
    assert exc.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_forward_blocked_on_returned_holder_when_query_already_terminal(db):
    """When the holder still matches but the status is terminal
    (RESPONDED), forward must raise ILLEGAL_TRANSITION rather than
    silently re-writing the status field."""
    query, _, holder_user, holder_profile, _, other_profile = await _seed_query_with_two_pundits(db)
    # Status is RESPONDED but the holder pointer hasn't been cleared
    # (e.g. a stale request landed). Pin that the transition guard
    # still blocks the move.
    query.status = QueryStatus.RESPONDED
    query.current_holder_id = holder_profile.id
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await forward_query(
            query_id=query.id,
            data={"to_pundit_id": other_profile.id, "remarks": "Stale forward"},
            db=db, current_user=holder_user,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "ILLEGAL_TRANSITION"


# ── Pundit preference: subscription ownership ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_set_pundit_preference_rejects_other_farmer(db):
    """Farmer B cannot set a pundit preference on farmer A's
    subscription — pre-fix the route accepted any subscription_id from
    any authenticated user. Same family as BL-08."""
    farmer_a = await make_user(db, name="Farmer A")
    farmer_b = await make_user(db, name="Farmer B")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer_a, client=client, package=package)
    _, profile = await _make_pundit(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await set_pundit_preference(
            subscription_id=sub.id, data={"pundit_id": profile.id},
            db=db, current_user=farmer_b,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_clear_pundit_preference_rejects_other_farmer(db):
    farmer_a = await make_user(db, name="Farmer A")
    farmer_b = await make_user(db, name="Farmer B")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer_a, client=client, package=package)
    _, profile = await _make_pundit(db)
    db.add(FarmPunditPreference(subscription_id=sub.id, pundit_id=profile.id))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await clear_pundit_preference(
            subscription_id=sub.id, db=db, current_user=farmer_b,
        )
    assert exc.value.status_code == 404


# ── Chained forward by PRIMARY (status already FORWARDED) ────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_chained_forward_by_primary_succeeds(db):
    """A query already in FORWARDED status, held by a PRIMARY pundit,
    can be forwarded onward — only current_holder_id changes; status
    stays FORWARDED. Pinned because the validator's table excludes
    FORWARDED → FORWARDED, so the router has to short-circuit."""
    query, _, holder_user, _, _, other_profile = await _seed_query_with_two_pundits(db)
    query.status = QueryStatus.FORWARDED
    await db.commit()

    out = await forward_query(
        query_id=query.id,
        data={"to_pundit_id": other_profile.id, "remarks": "Onward"},
        db=db, current_user=holder_user,
    )
    assert out["status"] == "FORWARDED"

    refreshed = (await db.execute(
        select(Query).where(Query.id == query.id)
    )).scalar_one()
    assert refreshed.current_holder_id == other_profile.id


# ── Expiry sweep ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_expiry_sweep_flips_past_due_query_to_expired(db):
    """The hourly sweep finds queries past their 7-day window and
    flips them to EXPIRED, clearing current_holder_id. Queries still
    within their window are untouched."""
    farmer = await make_user(db, name="Farmer Exp")
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=farmer, client=client, package=package)
    _, profile = await _make_pundit(db)
    await _enrol(db, client_id=client.id, profile=profile, role=PunditRole.PRIMARY)
    await db.commit()

    past_due = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Past due", severity="LOW", status=QueryStatus.NEW,
        current_holder_id=profile.id,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    fresh = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Fresh", severity="LOW", status=QueryStatus.NEW,
        current_holder_id=profile.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    db.add_all([past_due, fresh])
    await db.commit()

    expired_count = await _expire_queries_with_session(db)
    assert expired_count == 1

    refreshed_past = (await db.execute(
        select(Query).where(Query.id == past_due.id)
    )).scalar_one()
    refreshed_fresh = (await db.execute(
        select(Query).where(Query.id == fresh.id)
    )).scalar_one()
    assert refreshed_past.status == QueryStatus.EXPIRED
    assert refreshed_past.current_holder_id is None
    assert refreshed_fresh.status == QueryStatus.NEW
    assert refreshed_fresh.current_holder_id == profile.id
