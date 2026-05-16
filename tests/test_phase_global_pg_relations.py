"""Global PG Relation endpoints (Batch 39P-b, 2026-05-16).

Pin the new `/advisory/global/pg-recommendations/{pg_id}/timelines/
{tl_id}/relations` POST + GET pair against the same body shape as
the CCA Global sibling. After UCAT unification (Batch 39O) the
Relation table is pipe-agnostic, so adding PG just meant a new
auth-prefix wrapper around the shared
`_create_relation_for_global_timeline` /
`_list_relations_for_global_timeline` helpers.

DELETE at `/advisory/global/relations/{id}` is also pipe-agnostic
now — pinned by the existing CCA delete tests; here we verify a
PG-rooted relation can be removed via the same URL.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, PGRecommendation, Practice, PracticeL0,
    Relation, RelationType, Timeline,
)
from app.modules.advisory.router import (
    create_global_pg_relation, delete_global_relation,
    list_global_pg_relations,
)
from app.modules.advisory.schemas import RelationCreate
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_global_pg_with_two_practices(db):
    """Global PG (client_id=NULL) + 1 Timeline + 2 input Practices."""
    pg = PGRecommendation(
        problem_group_cosh_id=f"pg:{uuid.uuid4().hex[:6]}",
        client_id=None, area_or_plant="AREA_WISE", status="DRAFT",
    )
    db.add(pg)
    await db.flush()
    tl = Timeline(
        pg_recommendation_id=pg.id, name="TL",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=7,
    )
    db.add(tl)
    await db.flush()
    p1 = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
        common_name_cosh_id="cn:imida",
    )
    p2 = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
        common_name_cosh_id="cn:thia",
    )
    db.add(p1)
    db.add(p2)
    await db.flush()
    # Common-name elements so the validator can de-dup by CN.
    db.add(Element(practice_id=p1.id, element_type="COMMON_NAME", cosh_ref="cn:imida", value=""))
    db.add(Element(practice_id=p2.id, element_type="COMMON_NAME", cosh_ref="cn:thia", value=""))
    await db.flush()
    return pg, tl, p1, p2


@requires_docker
@pytest.mark.asyncio
async def test_create_pg_relation_persists_and_assigns_roles(db):
    """A CM creates an OR relation on a PG Timeline. The two
    Practices receive `relation_id` + a `PART_n__OPT_m__POS_p` role
    so the relation reconstructs cleanly on read."""
    user = await make_user(db, name="CM")
    pg, tl, p1, p2 = await _seed_global_pg_with_two_practices(db)
    await db.commit()

    out = await create_global_pg_relation(
        pg_id=pg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.OR,
            parts=[[[p1.id], [p2.id]]],
        ),
        db=db, current_user=user,
    )
    assert out["relation_type"] == "OR"

    rel = (await db.execute(
        select(Relation).where(Relation.id == out["id"])
    )).scalar_one()
    assert rel.timeline_id == tl.id
    refreshed = (await db.execute(
        select(Practice).where(Practice.timeline_id == tl.id)
    )).scalars().all()
    assert all(p.relation_id == rel.id for p in refreshed)
    assert {p.relation_role for p in refreshed} == {
        "PART_1__OPT_1__POS_1", "PART_1__OPT_2__POS_1",
    }


@requires_docker
@pytest.mark.asyncio
async def test_list_pg_relations_returns_full_3d_shape(db):
    """GET reconstructs the same `parts` 3-D structure the POST
    received — so the frontend can re-render the chain identically."""
    user = await make_user(db, name="CM")
    pg, tl, p1, p2 = await _seed_global_pg_with_two_practices(db)
    await db.commit()
    await create_global_pg_relation(
        pg_id=pg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    out = await list_global_pg_relations(
        pg_id=pg.id, timeline_id=tl.id, db=db, current_user=user,
    )
    assert len(out) == 1
    rel = out[0]
    assert rel["relation_type"] == "AND"
    assert rel["parts"] == [[[p1.id, p2.id]]]


@requires_docker
@pytest.mark.asyncio
async def test_delete_pg_relation_via_shared_endpoint(db):
    """The DELETE at /advisory/global/relations/{id} is pipe-agnostic
    after Batch 39P-b — it accepts CCA-rooted and PG-rooted relations."""
    user = await make_user(db, name="CM")
    pg, tl, p1, p2 = await _seed_global_pg_with_two_practices(db)
    await db.commit()
    out = await create_global_pg_relation(
        pg_id=pg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.OR,
            parts=[[[p1.id], [p2.id]]],
        ),
        db=db, current_user=user,
    )
    await delete_global_relation(
        relation_id=out["id"], db=db, current_user=user,
    )
    survivors = (await db.execute(
        select(Relation).where(Relation.id == out["id"])
    )).scalars().all()
    assert survivors == []
    # Practices stay; only relation_id/role are cleared.
    practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == tl.id)
    )).scalars().all()
    assert len(practices) == 2
    assert all(p.relation_id is None for p in practices)


@requires_docker
@pytest.mark.asyncio
async def test_pg_relation_404_on_client_scoped_pg(db):
    """The endpoint is Global-only — refuses a client-scoped PG row."""
    user = await make_user(db, name="CM")
    from tests.factories import make_client
    client = await make_client(db)
    pg = PGRecommendation(
        problem_group_cosh_id="pg:test", client_id=client.id,
        area_or_plant="AREA_WISE", status="DRAFT",
    )
    db.add(pg)
    await db.flush()
    tl = Timeline(
        pg_recommendation_id=pg.id, name="TL",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=7,
    )
    db.add(tl)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await list_global_pg_relations(
            pg_id=pg.id, timeline_id=tl.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 404
