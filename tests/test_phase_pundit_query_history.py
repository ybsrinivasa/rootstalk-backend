"""Pundit query history (2026-05-27 regression).

Bug: after `respond_to_query` nulled `current_holder_id`, the history
endpoint (which filtered on that column) returned nothing — the
Pundit's own response vanished from their History tab.

Fix: history derives from the QueryRemark / QueryResponse audit
trail. Any query the Pundit interacted with (received, forwarded,
returned, responded, rejected) appears in their history once it
hits a terminal status.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditProfile, PunditRole,
    Query, QueryRemark, QueryRemarkAction, QueryResponse, QueryStatus,
)
from app.modules.farmpundit.router import (
    QueryCreate, QueryMediaItem, query_history, submit_query,
    respond_to_query,
)
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)


async def _seed_setup(db):
    """Farmer + sub + a PRIMARY Pundit at the same client + Cosh
    `query_types` row so submit_query passes its preconditions."""
    client = await make_client(db)
    farmer = await make_user(db, name="Hist-Farmer")
    pkg = await make_package(db, client, crop_cosh_id="crop:tomato")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    pundit_user = await make_user(db, name="Hist-Pundit")
    profile = FarmPunditProfile(user_id=pundit_user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    db.add(ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    ))
    db.add(CoshCoreItem(
        cosh_id="qt-insect", core_type="query_types",
        translations={"en": "Insect and disease problems"}, status="active",
    ))
    return farmer, client, sub, pundit_user, profile


@requires_docker
@pytest.mark.asyncio
async def test_history_includes_pundit_own_response(db):
    """The original bug: Pundit responds, then their own response
    doesn't show up in History. After fix: it does."""
    farmer, client, sub, pundit_user, profile = await _seed_setup(db)
    await db.commit()

    submitted = await submit_query(
        request=QueryCreate(
            subscription_id=sub.id, client_id=client.id,
            query_type_cosh_id="qt-insect", severity="HIGH",
            media=[QueryMediaItem(media_type="IMAGE",
                                   url="https://placeholder.rootstalk.in/x.jpg")],
        ),
        db=db, current_user=farmer,
    )
    # Pundit responds — current_holder_id will be nulled by respond.
    await respond_to_query(
        query_id=submitted["id"],
        data={"text": "Spray X tonight"},
        db=db, current_user=pundit_user,
    )

    # Confirm the live column is indeed null (pinning the cause).
    q = (await db.execute(
        select(Query).where(Query.id == submitted["id"])
    )).scalar_one()
    assert q.current_holder_id is None
    assert q.status == QueryStatus.RESPONDED

    # History must still surface this row.
    hist = await query_history(db=db, current_user=pundit_user)
    assert any(h.id == submitted["id"] for h in hist), (
        "Responded query must appear in Pundit's history even though "
        "current_holder_id was nulled at response time."
    )


@requires_docker
@pytest.mark.asyncio
async def test_history_includes_rejected_queries(db):
    """Rejected queries follow the same null-holder pattern and must
    also stay visible in history."""
    farmer, client, sub, pundit_user, profile = await _seed_setup(db)
    await db.commit()

    # Manually craft a REJECTED query with a remark from this Pundit;
    # short-circuits the full reject endpoint (which has guards we
    # don't need to exercise here).
    q = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Rejected one", severity="LOW",
        status=QueryStatus.REJECTED,
        current_holder_id=None,   # nulled at reject time
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db.add(q)
    await db.flush()
    db.add(QueryRemark(
        query_id=q.id, pundit_id=profile.id,
        action=QueryRemarkAction.REJECTED, remark="out of scope",
    ))
    await db.commit()

    hist = await query_history(db=db, current_user=pundit_user)
    assert any(h.id == q.id for h in hist)


@requires_docker
@pytest.mark.asyncio
async def test_history_excludes_non_terminal_statuses(db):
    """Active queries (NEW / FORWARDED / RETURNED) don't belong in
    history — they belong in the New/Pending/Returned tabs."""
    farmer, client, sub, pundit_user, profile = await _seed_setup(db)
    await db.commit()

    q = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Still active", severity="LOW",
        status=QueryStatus.NEW,
        current_holder_id=profile.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db.add(q)
    await db.flush()
    db.add(QueryRemark(
        query_id=q.id, pundit_id=profile.id,
        action=QueryRemarkAction.RECEIVED,
    ))
    await db.commit()

    hist = await query_history(db=db, current_user=pundit_user)
    assert not any(h.id == q.id for h in hist), (
        "NEW query must not appear in history — it's still active."
    )


@requires_docker
@pytest.mark.asyncio
async def test_history_does_not_leak_other_pundits_responses(db):
    """A Pundit's history only includes queries THEY interacted with —
    not every closed query at the same client."""
    farmer, client, sub, p1_user, p1 = await _seed_setup(db)
    # Second Pundit at the same client.
    p2_user = await make_user(db, name="Other-Pundit")
    p2 = FarmPunditProfile(user_id=p2_user.id, declaration_accepted=True)
    db.add(p2)
    await db.flush()
    db.add(ClientFarmPundit(
        client_id=client.id, pundit_id=p2.id,
        role=PunditRole.PANEL, status="ACTIVE", round_robin_sequence=None,
    ))
    await db.commit()

    # A query Pundit 1 responded to.
    q = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="P1's", severity="LOW",
        status=QueryStatus.RESPONDED, current_holder_id=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db.add(q)
    await db.flush()
    db.add(QueryRemark(
        query_id=q.id, pundit_id=p1.id,
        action=QueryRemarkAction.RESPONDED,
    ))
    db.add(QueryResponse(
        query_id=q.id, pundit_id=p1.id, text="answer",
    ))
    await db.commit()

    # Pundit 2 — never touched it — should see nothing.
    hist_p2 = await query_history(db=db, current_user=p2_user)
    assert not any(h.id == q.id for h in hist_p2)
    # Pundit 1 — yes.
    hist_p1 = await query_history(db=db, current_user=p1_user)
    assert any(h.id == q.id for h in hist_p1)
