"""Q&A trigger plumbing — L4-real Sub-batch 4.

When a FarmPundit picks a Standard Response while answering a
farmer's query, a `TriggeredCHAEntry` is created with
recommendation_type='QA' and recommendation_id=standard_response.id.
This is the merge-point with the farmer's advisory: the same table
that already hosts CHA-triggered entries hosts Q&A-triggered ones,
discriminated by recommendation_type.

Spec §14.9: the standard answer's Timelines flow into the farmer's
advisory the same way a CHA recommendation does. No special PWA
plumbing is needed — the existing advisory render walks
TriggeredCHAEntry; with QA entries in the same table, they appear
automatically. Sub-batch 5 adds the origin-icon discriminator.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select

from app.modules.farmpundit.models import (
    FarmPunditProfile, Query, QueryResponse, QueryStatus,
)
from app.modules.farmpundit.router import (
    _trigger_qa_for_query, create_standard_response, publish_standard_response,
    respond_to_query,
)
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, TriggeredCHAEntry,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_package, make_subscription,
    make_user,
)


async def _se_for(db, *, client):
    user = await make_user(db, name=f"SE-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


async def _seed_pundit_with_query(db, *, client, farmer, sub):
    """Seed a Pundit holding a NEW query for the given farmer/sub.
    Returns (user, profile, query)."""
    user = await make_user(db, name="Pundit")
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()

    from app.modules.farmpundit.models import ClientFarmPundit, PunditRole
    db.add(ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    ))

    query = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Aphids on tomato", severity="MODERATE",
        status=QueryStatus.NEW,
        current_holder_id=profile.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(query)
    await db.flush()
    return user, profile, query


# ── Direct trigger helper ───────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_trigger_qa_creates_entry_with_qa_shape(db):
    """The direct helper writes a TriggeredCHAEntry with
    recommendation_type='QA' and recommendation_id pointing at the
    Standard Response. problem_cosh_id is None (Q&A is rooted in a
    question, not a Cosh problem)."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.status = SubscriptionStatus.ACTIVE

    sr_out = await create_standard_response(
        client_id=client.id,
        data={"question_text": "How to handle aphids?"},
        db=db, current_user=se,
    )
    await publish_standard_response(
        client_id=client.id, sr_id=sr_out["id"], db=db, current_user=se,
    )
    _, _, query = await _seed_pundit_with_query(db, client=client, farmer=farmer, sub=sub)
    await db.commit()

    await _trigger_qa_for_query(db, query, sr_out["id"])
    await db.commit()

    entry = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.recommendation_id == sr_out["id"],
        )
    )).scalar_one()
    assert entry.recommendation_type == "QA"
    assert entry.problem_cosh_id is None
    assert entry.problem_name == "How to handle aphids?"
    assert entry.parent_pg_cosh_id is None
    assert entry.triggered_by == "QUERY"
    assert entry.subscription_id == sub.id
    assert entry.farmer_user_id == farmer.id
    assert entry.client_id == client.id


@requires_docker
@pytest.mark.asyncio
async def test_trigger_qa_silent_noop_for_cross_client_sr(db):
    """If the Pundit somehow attaches a Standard Response from a
    different client, the trigger silently no-ops (mirrors CHA path
    when the recommendation can't be resolved). No entry created."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_b = await _se_for(db, client=client_b)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client_a, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client_a, package=pkg)
    sub.status = SubscriptionStatus.ACTIVE

    # SR belongs to B, but query is for client A.
    sr_b = await create_standard_response(
        client_id=client_b.id,
        data={"question_text": "B's question"},
        db=db, current_user=se_b,
    )
    _, _, query = await _seed_pundit_with_query(db, client=client_a, farmer=farmer, sub=sub)
    await db.commit()

    await _trigger_qa_for_query(db, query, sr_b["id"])
    await db.commit()

    entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.recommendation_id == sr_b["id"],
        )
    )).scalars().all()
    assert entries == []


@requires_docker
@pytest.mark.asyncio
async def test_trigger_qa_silent_noop_when_no_active_subscription(db):
    """No active subscription for (farmer, client) → no entry. The
    CHA path has the same behaviour. The Pundit can still respond
    with text/media; just no advisory merge happens."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.status = SubscriptionStatus.LAPSED  # not ACTIVE

    sr = await create_standard_response(
        client_id=client.id,
        data={"question_text": "Q?"},
        db=db, current_user=se,
    )
    # Publish so the no-op below is provably caused by the subscription
    # gate, not the SR-status filter that runs earlier.
    await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    _, _, query = await _seed_pundit_with_query(db, client=client, farmer=farmer, sub=sub)
    await db.commit()

    await _trigger_qa_for_query(db, query, sr["id"])
    await db.commit()

    assert (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.recommendation_id == sr["id"],
        )
    )).scalars().all() == []


# ── End-to-end via respond_to_query ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_respond_to_query_with_standard_response_triggers_qa_entry(db):
    """The wiring inside respond_to_query: when standard_response_id
    is present in the response payload, the QA trigger fires."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.status = SubscriptionStatus.ACTIVE

    sr = await create_standard_response(
        client_id=client.id,
        data={"question_text": "How to control aphids?"},
        db=db, current_user=se,
    )
    await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    pundit_user, _, query = await _seed_pundit_with_query(
        db, client=client, farmer=farmer, sub=sub,
    )
    await db.commit()

    await respond_to_query(
        query_id=query.id,
        data={"standard_response_id": sr["id"]},
        db=db, current_user=pundit_user,
    )

    # QueryResponse persists the standard_response_id link.
    response = (await db.execute(
        select(QueryResponse).where(QueryResponse.query_id == query.id)
    )).scalar_one()
    assert response.standard_response_id == sr["id"]
    assert response.problem_cosh_id is None

    # And the TriggeredCHAEntry is created with QA shape.
    entry = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.recommendation_id == sr["id"],
        )
    )).scalar_one()
    assert entry.recommendation_type == "QA"
    assert entry.subscription_id == sub.id


@requires_docker
@pytest.mark.asyncio
async def test_respond_to_query_with_text_only_does_not_trigger(db):
    """Text-only response (degraded fallback per spec §14.7/§14.9)
    creates a QueryResponse but no TriggeredCHAEntry — fallback
    responses don't drive purchases or merge into the advisory."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.status = SubscriptionStatus.ACTIVE
    pundit_user, _, query = await _seed_pundit_with_query(
        db, client=client, farmer=farmer, sub=sub,
    )
    await db.commit()

    await respond_to_query(
        query_id=query.id,
        data={"text": "Try neem oil weekly."},
        db=db, current_user=pundit_user,
    )

    assert (await db.execute(
        select(QueryResponse).where(QueryResponse.query_id == query.id)
    )).scalar_one().text == "Try neem oil weekly."

    # No CHA-table entry — fallback responses don't merge.
    assert (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.farmer_user_id == farmer.id,
        )
    )).scalars().all() == []
