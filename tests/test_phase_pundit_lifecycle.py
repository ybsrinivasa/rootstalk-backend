"""FarmPundit + Promoter lifecycle endpoints — MED Sub-batch 1.

M1: change role (Primary↔Panel) + full delete, gated on INACTIVE
    status AND zero active queries (spec §14.5).
M2: reactivate FarmPundit (inverse of deactivate).
M3: reactivate Promoter (deactivate already existed).

Plus the list-pundits endpoint now includes `active_query_count` so
the CA portal can gate role-change/delete actions client-side.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditProfile, PunditRole, Query, QueryStatus,
)
from app.modules.farmpundit.router import (
    PunditRoleChange, change_company_pundit_role,
    deactivate_company_pundit, delete_company_pundit,
    list_company_pundits, reactivate_company_pundit,
)
from app.modules.clients.models import ClientPromoter
from app.modules.clients.router import (
    deactivate_promoter, reactivate_promoter,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_package, make_subscription, make_user,
)


async def _ca_user_for(db, *, client):
    """A real CA portal user the FarmPundit endpoints' membership
    gate (`_assert_portal_member`) will accept. Seeds a User + a
    matching ClientUser row, returns the User."""
    user = await make_user(db, name=f"CA-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


async def _enrol(db, *, client, profile, role: PunditRole = PunditRole.PRIMARY,
                 status: str = "ACTIVE", sequence: int | None = 1):
    cp = ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=role, status=status,
        round_robin_sequence=sequence if role == PunditRole.PRIMARY else None,
    )
    db.add(cp)
    await db.flush()
    return cp


async def _make_pundit(db, *, name="Pundit"):
    user = await make_user(db, name=name)
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    return user, profile


async def _seed_active_query(db, *, client, holder_profile, status=QueryStatus.NEW):
    """Seed a Query in the given status held by the given pundit.
    The farmer / sub / package are real rows because Query has FKs
    to all three. Package name is uniqued per call because the
    factory's default 'Test PoP' would collide on repeat calls
    against the same client (uq_package_client_crop_name)."""
    import uuid
    farmer = await make_user(db, name="F")
    pkg = await make_package(db, client, name=f"Test PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    q = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Q", severity="MEDIUM", status=status,
        current_holder_id=holder_profile.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(q)
    await db.flush()
    return q


# ── M2: reactivate FarmPundit ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_reactivate_pundit_flips_status_back(db):
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile)
    cp.status = "INACTIVE"
    await db.commit()

    out = await reactivate_company_pundit(
        client_id=client.id, cp_id=cp.id, db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert out["status"] == "ACTIVE"
    await db.refresh(cp)
    assert cp.status == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_reactivate_already_active_pundit_400(db):
    """Reactivating an already-active pundit is a no-op error so the
    UI can show 'no change' instead of a silent success."""
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile)

    with pytest.raises(HTTPException) as ei:
        await reactivate_company_pundit(
            client_id=client.id, cp_id=cp.id, db=db, current_user=await _ca_user_for(db, client=client),
        )
    assert ei.value.status_code == 400


# ── M1: change role ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_change_role_primary_to_panel_clears_sequence(db):
    """Primary→Panel: round_robin_sequence must be cleared so the
    routing service stops handing them new queries."""
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile,
                      role=PunditRole.PRIMARY, sequence=5)
    cp.status = "INACTIVE"
    await db.commit()

    await change_company_pundit_role(
        client_id=client.id, cp_id=cp.id,
        request=PunditRoleChange(role=PunditRole.PANEL),
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    await db.refresh(cp)
    assert cp.role == PunditRole.PANEL
    assert cp.round_robin_sequence is None


@requires_docker
@pytest.mark.asyncio
async def test_change_role_panel_to_primary_assigns_sequence(db):
    """Panel→Primary: assign next available round_robin_sequence so
    the new Primary lands at the end of the rotation."""
    client = await make_client(db)
    # Two existing Primaries in the rotation.
    _, prof_a = await _make_pundit(db, name="A")
    _, prof_b = await _make_pundit(db, name="B")
    await _enrol(db, client=client, profile=prof_a, role=PunditRole.PRIMARY, sequence=1)
    await _enrol(db, client=client, profile=prof_b, role=PunditRole.PRIMARY, sequence=2)

    # Promotion candidate, currently Panel.
    _, prof_c = await _make_pundit(db, name="Promotee")
    cp_c = await _enrol(db, client=client, profile=prof_c,
                        role=PunditRole.PANEL, sequence=None)
    cp_c.status = "INACTIVE"
    await db.commit()

    out = await change_company_pundit_role(
        client_id=client.id, cp_id=cp_c.id,
        request=PunditRoleChange(role=PunditRole.PRIMARY),
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert out["role"] == "PRIMARY"
    # _next_round_robin_sequence returns count(Primaries) + 1 = 3 here.
    assert out["round_robin_sequence"] == 3


@requires_docker
@pytest.mark.asyncio
async def test_change_role_blocked_when_active_status(db):
    """Spec §14.5: must be INACTIVE first. Active pundit role-change
    request is rejected with 409 + actionable message."""
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile)
    # status remains ACTIVE
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await change_company_pundit_role(
            client_id=client.id, cp_id=cp.id,
            request=PunditRoleChange(role=PunditRole.PANEL),
            db=db, current_user=await _ca_user_for(db, client=client),
        )
    assert ei.value.status_code == 409
    assert "Deactivate" in ei.value.detail


@requires_docker
@pytest.mark.asyncio
async def test_change_role_blocked_when_holding_active_queries(db):
    """Spec §14.5: must drain active queries first, even if INACTIVE.
    The deactivate→drain→change-role sequence prevents a Panel pundit
    from suddenly holding what was a Primary-routed query."""
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile)
    cp.status = "INACTIVE"
    await _seed_active_query(db, client=client, holder_profile=profile)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await change_company_pundit_role(
            client_id=client.id, cp_id=cp.id,
            request=PunditRoleChange(role=PunditRole.PANEL),
            db=db, current_user=await _ca_user_for(db, client=client),
        )
    assert ei.value.status_code == 409
    assert "active query" in ei.value.detail


@requires_docker
@pytest.mark.asyncio
async def test_change_role_to_same_role_400(db):
    """Setting the role to its current value is a no-op error."""
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile, role=PunditRole.PRIMARY)
    cp.status = "INACTIVE"
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await change_company_pundit_role(
            client_id=client.id, cp_id=cp.id,
            request=PunditRoleChange(role=PunditRole.PRIMARY),
            db=db, current_user=await _ca_user_for(db, client=client),
        )
    assert ei.value.status_code == 400


# ── M1: delete ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_delete_removes_client_pundit_row_keeps_profile(db):
    """Spec §14.5: removed from company's list, PWA profile remains."""
    client = await make_client(db)
    user, profile = await _make_pundit(db, name="Departing")
    cp = await _enrol(db, client=client, profile=profile)
    cp.status = "INACTIVE"
    cp_id = cp.id
    await db.commit()

    await delete_company_pundit(
        client_id=client.id, cp_id=cp.id, db=db, current_user=await _ca_user_for(db, client=client),
    )

    from sqlalchemy import select
    leftover = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.id == cp_id)
    )).scalar_one_or_none()
    assert leftover is None

    # Profile still there — they can be invited by other companies.
    profile_still_there = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.id == profile.id)
    )).scalar_one_or_none()
    assert profile_still_there is not None


@requires_docker
@pytest.mark.asyncio
async def test_delete_blocked_when_active_status(db):
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile)

    with pytest.raises(HTTPException) as ei:
        await delete_company_pundit(
            client_id=client.id, cp_id=cp.id, db=db, current_user=await _ca_user_for(db, client=client),
        )
    assert ei.value.status_code == 409


@requires_docker
@pytest.mark.asyncio
async def test_delete_blocked_when_holding_active_queries(db):
    client = await make_client(db)
    _, profile = await _make_pundit(db)
    cp = await _enrol(db, client=client, profile=profile)
    cp.status = "INACTIVE"
    await _seed_active_query(db, client=client, holder_profile=profile,
                             status=QueryStatus.FORWARDED)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await delete_company_pundit(
            client_id=client.id, cp_id=cp.id, db=db, current_user=await _ca_user_for(db, client=client),
        )
    assert ei.value.status_code == 409
    assert "active query" in ei.value.detail


# ── list_company_pundits exposes active_query_count ─────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_pundits_includes_active_query_count(db):
    """The CA portal needs this count to gate role-change/delete UI
    actions per spec §14.5. Without it, the frontend would either
    leak buttons that 409 on click or have to round-trip per row."""
    client = await make_client(db)
    _, prof_a = await _make_pundit(db, name="WithQueries")
    _, prof_b = await _make_pundit(db, name="WithoutQueries")
    await _enrol(db, client=client, profile=prof_a)
    await _enrol(db, client=client, profile=prof_b, sequence=2)

    # Two active queries (NEW + RETURNED) on prof_a; one RESPONDED
    # (closed) which must NOT count.
    await _seed_active_query(db, client=client, holder_profile=prof_a,
                             status=QueryStatus.NEW)
    await _seed_active_query(db, client=client, holder_profile=prof_a,
                             status=QueryStatus.RETURNED)
    closed = await _seed_active_query(db, client=client, holder_profile=prof_a,
                                      status=QueryStatus.NEW)
    closed.status = QueryStatus.RESPONDED
    await db.commit()

    out = await list_company_pundits(
        client_id=client.id, db=db, current_user=await _ca_user_for(db, client=client),
    )
    by_name = {row["name"]: row for row in out}
    assert by_name["WithQueries"]["active_query_count"] == 2
    assert by_name["WithoutQueries"]["active_query_count"] == 0


# ── M3: reactivate Promoter ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_reactivate_promoter_flips_status_back(db):
    client = await make_client(db)
    sa_user = await make_user(db, name="SA")
    user = await make_user(db, name="Dealer")
    cp = ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="DEALER", status="INACTIVE",
        registered_by=sa_user.id,
    )
    db.add(cp)
    await db.commit()

    out = await reactivate_promoter(
        client_id=client.id, promoter_id=cp.id,
        db=db, current_user=sa_user,
    )
    assert out["status"] == "ACTIVE"
    await db.refresh(cp)
    assert cp.status == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_reactivate_active_promoter_400(db):
    client = await make_client(db)
    sa_user = await make_user(db, name="SA")
    user = await make_user(db, name="Dealer")
    cp = ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="DEALER", status="ACTIVE",
        registered_by=sa_user.id,
    )
    db.add(cp)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await reactivate_promoter(
            client_id=client.id, promoter_id=cp.id,
            db=db, current_user=sa_user,
        )
    assert ei.value.status_code == 400


@requires_docker
@pytest.mark.asyncio
async def test_deactivate_then_reactivate_round_trip(db):
    """End-to-end: deactivate → reactivate → status back to ACTIVE.
    Confirms the existing deactivate endpoint and the new reactivate
    endpoint compose cleanly."""
    client = await make_client(db)
    sa_user = await make_user(db, name="SA")
    user = await make_user(db, name="Facilitator")
    cp = ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="FACILITATOR", status="ACTIVE",
        registered_by=sa_user.id,
    )
    db.add(cp)
    await db.commit()

    await deactivate_promoter(
        client_id=client.id, promoter_id=cp.id,
        db=db, current_user=sa_user,
    )
    await db.refresh(cp)
    assert cp.status == "INACTIVE"

    await reactivate_promoter(
        client_id=client.id, promoter_id=cp.id,
        db=db, current_user=sa_user,
    )
    await db.refresh(cp)
    assert cp.status == "ACTIVE"
