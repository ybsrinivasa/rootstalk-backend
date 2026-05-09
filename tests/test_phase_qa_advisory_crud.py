"""Q&A advisory CRUD — L4-real Sub-batch 2.

Endpoints under `/client/{cid}/standard-responses/{sr_id}/timelines`
that author the Timeline → Practice → Element advisory body for a
Q&A library entry. Writes go into the polymorphic `pg_timelines`
table (with `standard_response_id` set instead of
`pg_recommendation_id`) — same physical tables as CHA's PG
recommendations, per UCAT.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import PGElement, PGPractice, PGTimeline
from app.modules.advisory.router import (
    add_qa_practice, add_qa_timeline, delete_qa_practice,
    delete_qa_timeline, list_qa_timelines,
)
from app.modules.advisory.schemas import (
    ElementIn, QAPracticeCreate, QATimelineCreate,
)
from app.modules.farmpundit.models import StandardResponse
from app.modules.farmpundit.router import create_standard_response
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


async def _se_for(db, *, client):
    user = await make_user(db, name=f"SE-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


async def _seed_sr(db, *, client, se, question="Q?", crop=None):
    out = await create_standard_response(
        client_id=client.id,
        data={"question_text": question, "crop_cosh_id": crop},
        db=db, current_user=se,
    )
    return out["id"]


# ── Timeline CRUD ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_add_timeline_writes_into_pg_timelines_with_qa_parent(db):
    """A QA timeline lives in the same physical table as PG timelines
    — only the parent FK differs. Confirms the polymorphism is real
    on the write path."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)
    await db.commit()

    out = await add_qa_timeline(
        client_id=client.id, sr_id=sr_id,
        request=QATimelineCreate(name="Recovery week 1", to_value=7),
        db=db, current_user=se,
    )
    assert out["standard_response_id"] == sr_id
    assert out["from_type"] == "DAYS_AFTER_RESPONSE"
    assert out["parent_kind"] == "QA"

    # Direct DB confirmation — row in pg_timelines with the QA parent.
    row = (await db.execute(
        select(PGTimeline).where(PGTimeline.id == out["id"])
    )).scalar_one()
    assert row.pg_recommendation_id is None
    assert row.standard_response_id == sr_id


@requires_docker
@pytest.mark.asyncio
async def test_add_timeline_rejects_cross_client_sr(db):
    """A CA at client A cannot author timelines under client B's
    Standard Response by guessing the URL. 404 keeps existence
    private."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await _se_for(db, client=client_a)
    se_b = await _se_for(db, client=client_b)
    sr_b = await _seed_sr(db, client=client_b, se=se_b)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await add_qa_timeline(
            client_id=client_a.id, sr_id=sr_b,
            request=QATimelineCreate(name="Hijacked", to_value=7),
            db=db, current_user=se_a,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_list_timelines_returns_full_tree(db):
    """The list endpoint returns the whole advisory tree — Timelines
    with nested Practices and Elements — because the CA-portal
    editor (Sub-batch 3) renders it all at once."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)
    await db.commit()

    tl = await add_qa_timeline(
        client_id=client.id, sr_id=sr_id,
        request=QATimelineCreate(name="Week 1", to_value=7),
        db=db, current_user=se,
    )
    await add_qa_practice(
        client_id=client.id, sr_id=sr_id, tl_id=tl["id"],
        request=QAPracticeCreate(
            l0_type="INPUT", display_order=0,
            elements=[
                ElementIn(element_type="PESTICIDE", value="Neem oil"),
                ElementIn(element_type="DOSE", value="5", unit_cosh_id="ml/L"),
            ],
        ),
        db=db, current_user=se,
    )

    tree = await list_qa_timelines(
        client_id=client.id, sr_id=sr_id,
        db=db, current_user=se,
    )
    assert len(tree) == 1
    assert tree[0]["name"] == "Week 1"
    assert tree[0]["parent_kind"] == "QA"
    assert len(tree[0]["practices"]) == 1
    assert len(tree[0]["practices"][0]["elements"]) == 2
    assert {e["element_type"] for e in tree[0]["practices"][0]["elements"]} == {"PESTICIDE", "DOSE"}


@requires_docker
@pytest.mark.asyncio
async def test_delete_timeline_cascades_practices_and_elements(db):
    """Deleting a Q&A timeline removes its practices + elements too.
    No orphan rows remain. The cascade is application-level (matches
    PG/SP delete patterns)."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)
    await db.commit()

    tl = await add_qa_timeline(
        client_id=client.id, sr_id=sr_id,
        request=QATimelineCreate(name="Week 1", to_value=7),
        db=db, current_user=se,
    )
    p = await add_qa_practice(
        client_id=client.id, sr_id=sr_id, tl_id=tl["id"],
        request=QAPracticeCreate(
            l0_type="INPUT",
            elements=[ElementIn(element_type="PESTICIDE", value="X")],
        ),
        db=db, current_user=se,
    )

    await delete_qa_timeline(
        client_id=client.id, sr_id=sr_id, tl_id=tl["id"],
        db=db, current_user=se,
    )

    assert (await db.execute(
        select(PGTimeline).where(PGTimeline.id == tl["id"])
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(PGPractice).where(PGPractice.id == p["id"])
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(PGElement).where(PGElement.practice_id == p["id"])
    )).scalars().all() == []


@requires_docker
@pytest.mark.asyncio
async def test_delete_timeline_rejects_pg_owned_timeline_via_qa_url(db):
    """A timeline owned by a PG recommendation cannot be deleted via
    the Q&A URL. Same physical table; the URL implies the parent."""
    from app.modules.advisory.models import PGRecommendation

    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)

    pg = PGRecommendation(
        problem_group_cosh_id="pg:test",
        client_id=client.id, application_type="SPRAY", status="DRAFT",
    )
    db.add(pg)
    await db.flush()
    pg_tl = PGTimeline(
        pg_recommendation_id=pg.id,
        name="Owned by PG", from_value=0, to_value=7,
    )
    db.add(pg_tl)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await delete_qa_timeline(
            client_id=client.id, sr_id=sr_id, tl_id=pg_tl.id,
            db=db, current_user=se,
        )
    assert ei.value.status_code == 404


# ── Practice CRUD ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_add_practice_with_elements_inline(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)
    await db.commit()

    tl = await add_qa_timeline(
        client_id=client.id, sr_id=sr_id,
        request=QATimelineCreate(name="Week 1", to_value=7),
        db=db, current_user=se,
    )

    out = await add_qa_practice(
        client_id=client.id, sr_id=sr_id, tl_id=tl["id"],
        request=QAPracticeCreate(
            l0_type="INPUT", l1_type="PESTICIDE", display_order=2,
            elements=[
                ElementIn(element_type="DOSE", value="5", unit_cosh_id="ml/L", display_order=0),
                ElementIn(element_type="FREQUENCY", value="weekly", display_order=1),
            ],
        ),
        db=db, current_user=se,
    )
    assert out["l0_type"] == "INPUT"
    assert len(out["elements"]) == 2
    assert out["elements"][0]["element_type"] == "DOSE"
    assert out["elements"][0]["unit_cosh_id"] == "ml/L"


@requires_docker
@pytest.mark.asyncio
async def test_add_practice_rejects_timeline_under_different_sr(db):
    """Creating a practice under timeline T using the wrong sr_id in
    the URL must 404. Defends against the URL-tampering case where
    a CA learns one timeline id but tries to attach a practice via
    a different Standard Response they don't own."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_a = await _seed_sr(db, client=client, se=se, question="A")
    sr_b = await _seed_sr(db, client=client, se=se, question="B")
    await db.commit()

    tl_a = await add_qa_timeline(
        client_id=client.id, sr_id=sr_a,
        request=QATimelineCreate(name="A's timeline", to_value=7),
        db=db, current_user=se,
    )

    with pytest.raises(HTTPException) as ei:
        await add_qa_practice(
            client_id=client.id, sr_id=sr_b,  # wrong parent
            tl_id=tl_a["id"],
            request=QAPracticeCreate(l0_type="INPUT"),
            db=db, current_user=se,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_delete_practice_removes_elements_too(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)
    await db.commit()

    tl = await add_qa_timeline(
        client_id=client.id, sr_id=sr_id,
        request=QATimelineCreate(name="Week 1", to_value=7),
        db=db, current_user=se,
    )
    p = await add_qa_practice(
        client_id=client.id, sr_id=sr_id, tl_id=tl["id"],
        request=QAPracticeCreate(
            l0_type="INPUT",
            elements=[ElementIn(element_type="PESTICIDE", value="X")],
        ),
        db=db, current_user=se,
    )

    await delete_qa_practice(
        client_id=client.id, sr_id=sr_id, tl_id=tl["id"], p_id=p["id"],
        db=db, current_user=se,
    )

    assert (await db.execute(
        select(PGPractice).where(PGPractice.id == p["id"])
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(PGElement).where(PGElement.practice_id == p["id"])
    )).scalars().all() == []


# ── Membership gate (M7) ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_serialise_cha_timeline_accepts_qa_source(db):
    """Sub-batch 5 extension: `serialise_cha_timeline` now accepts
    source='QA' alongside 'PG'/'SP'. The QA source uses the same
    physical PG tables (UCAT polymorphism) so the serialiser just
    routes through the PG model trio."""
    from app.services.snapshot import serialise_cha_timeline

    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)
    await db.commit()

    tl = await add_qa_timeline(
        client_id=client.id, sr_id=sr_id,
        request=QATimelineCreate(name="Recovery", to_value=14),
        db=db, current_user=se,
    )
    await add_qa_practice(
        client_id=client.id, sr_id=sr_id, tl_id=tl["id"],
        request=QAPracticeCreate(
            l0_type="INPUT", l1_type="PESTICIDE",
            elements=[ElementIn(element_type="DOSE", value="5", unit_cosh_id="ml/L")],
        ),
        db=db, current_user=se,
    )
    await db.commit()

    serialised = await serialise_cha_timeline(db, tl["id"], "QA")
    assert serialised["source"] == "QA"
    assert serialised["timeline"]["name"] == "Recovery"
    assert len(serialised["practices"]) == 1
    assert serialised["practices"][0]["elements"][0]["element_type"] == "DOSE"


@requires_docker
@pytest.mark.asyncio
async def test_endpoints_reject_non_member(db):
    """Membership gate identical to the rest of the standard-response
    surface — outsiders get 403 from the assert_portal_member call,
    not a misleading 404."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    sr_id = await _seed_sr(db, client=client, se=se)
    outsider = await make_user(db, name="Outsider")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_qa_timelines(
            client_id=client.id, sr_id=sr_id,
            db=db, current_user=outsider,
        )
    assert ei.value.status_code == 403
