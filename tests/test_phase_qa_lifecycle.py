"""Q&A publish lifecycle — DRAFT → ACTIVE → INACTIVE.

Standard Responses gain a status column (CA-QA polish, 2026-05-23). Only
ACTIVE rows are visible to Pundits; DRAFTs are curator-only until
explicitly published, and INACTIVE is the curator's hide-during-rewrite
escape hatch. Unlike PG/SP, there's no version history — edits to an
ACTIVE row propagate immediately.

Coverage:
  - Default status on fresh SR.
  - publish (DRAFT → ACTIVE) + state-machine refusals.
  - deactivate (ACTIVE → INACTIVE) + refusals.
  - activate (INACTIVE → ACTIVE) + refusals.
  - Pundit-side search filters non-ACTIVE rows out.
  - _trigger_qa_for_query no-ops on non-ACTIVE rows (defence in depth).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditProfile, PunditRole, Query, QueryStatus,
    StandardResponse,
)
from app.modules.farmpundit.router import (
    _trigger_qa_for_query,
    activate_standard_response,
    create_standard_response,
    deactivate_standard_response,
    publish_standard_response,
    search_standard_responses,
)
from app.modules.subscriptions.models import (
    SubscriptionStatus, TriggeredCHAEntry,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_package, make_subscription, make_user,
)


async def _se_for(db, *, client):
    user = await make_user(db, name=f"SE-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


async def _seed_sr(db, *, client, se, question="Q?"):
    out = await create_standard_response(
        client_id=client.id,
        data={"question_text": question},
        db=db, current_user=se,
    )
    return out


# ── Defaults ────────────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_new_standard_response_defaults_to_draft(db):
    """A freshly created SR is invisible to Pundits until the curator
    explicitly publishes — DRAFT is the safe default."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    out = await _seed_sr(db, client=client, se=se)
    assert out["status"] == "DRAFT"


# ── Publish gate ────────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_publish_promotes_draft_to_active(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()
    sr = await _seed_sr(db, client=client, se=se)

    out = await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    assert out["status"] == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_publish_refused_when_already_active(db):
    """The publish CTA is a one-time DRAFT-only gate. After ACTIVE,
    edits propagate without re-publishing."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()
    sr = await _seed_sr(db, client=client, se=se)
    await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )

    with pytest.raises(HTTPException) as ei:
        await publish_standard_response(
            client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "sr_not_draft"
    assert ei.value.detail["current_status"] == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_publish_refused_when_inactive(db):
    """Re-exposing an INACTIVE SR uses /activate, not /publish — the
    confirmation copy on publish is DRAFT-specific."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()
    sr = await _seed_sr(db, client=client, se=se)
    await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    await deactivate_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )

    with pytest.raises(HTTPException) as ei:
        await publish_standard_response(
            client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
        )
    assert ei.value.detail["code"] == "sr_not_draft"


# ── Deactivate / Activate ───────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_deactivate_moves_active_to_inactive(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()
    sr = await _seed_sr(db, client=client, se=se)
    await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )

    out = await deactivate_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    assert out["status"] == "INACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_deactivate_refused_on_draft(db):
    """Toggling Inactive on a DRAFT makes no sense — DRAFT is already
    invisible. Surface a specific code so the UI hides the toggle."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()
    sr = await _seed_sr(db, client=client, se=se)

    with pytest.raises(HTTPException) as ei:
        await deactivate_standard_response(
            client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
        )
    assert ei.value.detail["code"] == "sr_not_active"


@requires_docker
@pytest.mark.asyncio
async def test_activate_moves_inactive_to_active(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()
    sr = await _seed_sr(db, client=client, se=se)
    await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    await deactivate_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )

    out = await activate_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    assert out["status"] == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_activate_refused_on_draft(db):
    """A DRAFT must go through /publish, not /activate — the two paths
    intentionally diverge so the UI can show distinct confirmation
    copy on first publish."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()
    sr = await _seed_sr(db, client=client, se=se)

    with pytest.raises(HTTPException) as ei:
        await activate_standard_response(
            client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
        )
    assert ei.value.detail["code"] == "sr_not_inactive"


# ── Pundit-side filtering ───────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_pundit_search_excludes_non_active(db):
    """The Pundit's pick list is curated content; DRAFTs and INACTIVE
    rows must not leak in. Curator-side endpoints still see all rows."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    pundit = await make_user(db, name="Pundit")
    await db.commit()

    draft = await _seed_sr(db, client=client, se=se, question="Draft Q")
    active = await _seed_sr(db, client=client, se=se, question="Active Q")
    inactive = await _seed_sr(db, client=client, se=se, question="Inactive Q")
    await publish_standard_response(
        client_id=client.id, sr_id=active["id"], db=db, current_user=se,
    )
    await publish_standard_response(
        client_id=client.id, sr_id=inactive["id"], db=db, current_user=se,
    )
    await deactivate_standard_response(
        client_id=client.id, sr_id=inactive["id"], db=db, current_user=se,
    )

    out = await search_standard_responses(
        client_id=client.id, db=db, current_user=pundit,
    )
    questions = {row["question_text"] for row in out}
    assert questions == {"Active Q"}


# ── Trigger defence ─────────────────────────────────────────────────────────


async def _seed_pundit_with_query(db, *, client, farmer, sub):
    user = await make_user(db, name="Pundit")
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    db.add(ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="ACTIVE", round_robin_sequence=1,
    ))
    query = Query(
        farmer_user_id=farmer.id, subscription_id=sub.id, client_id=client.id,
        title="Q", severity="MODERATE",
        status=QueryStatus.NEW,
        current_holder_id=profile.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(query)
    await db.flush()
    return user, profile, query


@requires_docker
@pytest.mark.asyncio
async def test_trigger_skips_draft_sr(db):
    """A Pundit shouldn't be able to attach a DRAFT SR (the pick list
    filters those out), but if one somehow leaks through, the trigger
    must no-op — no farmer-facing TriggeredCHAEntry from a DRAFT."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.status = SubscriptionStatus.ACTIVE

    sr = await _seed_sr(db, client=client, se=se, question="Draft Q")
    _, _, query = await _seed_pundit_with_query(
        db, client=client, farmer=farmer, sub=sub,
    )
    await db.commit()

    await _trigger_qa_for_query(db, query, sr["id"])
    await db.commit()

    entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.recommendation_id == sr["id"],
        )
    )).scalars().all()
    assert entries == []


@requires_docker
@pytest.mark.asyncio
async def test_trigger_skips_inactive_sr(db):
    """Same defence on the INACTIVE side: if a curator deactivates an
    SR mid-flight, queue races mustn't produce a farmer-facing entry."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    farmer = await make_user(db, name="Farmer")
    pkg = await make_package(db, client, name=f"PoP {uuid.uuid4().hex[:6]}")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    sub.status = SubscriptionStatus.ACTIVE

    sr = await _seed_sr(db, client=client, se=se, question="Q")
    await publish_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    await deactivate_standard_response(
        client_id=client.id, sr_id=sr["id"], db=db, current_user=se,
    )
    _, _, query = await _seed_pundit_with_query(
        db, client=client, farmer=farmer, sub=sub,
    )
    await db.commit()

    await _trigger_qa_for_query(db, query, sr["id"])
    await db.commit()

    entries = (await db.execute(
        select(TriggeredCHAEntry).where(
            TriggeredCHAEntry.recommendation_id == sr["id"],
        )
    )).scalars().all()
    assert entries == []
