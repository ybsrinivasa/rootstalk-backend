"""CCA Step 4 — DB-backed integration tests for relation
validation (Batch 4A: AND/OR rules + structure validation +
practice ownership).

Pure-function coverage of the validators lives in
`tests/test_relation_validation.py`. This file drives
`create_relation` against the testcontainer DB to verify each
rule's stable error code surfaces correctly through the API.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, Package, PackageStatus, PackageType, Practice, PracticeL0,
    Relation, RelationType, Timeline, TimelineFromType,
)
from app.modules.advisory.router import create_relation
from app.modules.advisory.schemas import RelationCreate
from app.modules.clients.router import add_crop
from app.modules.clients.schemas import CropCreate
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_crop_reference, make_package, make_timeline, make_user,
)


async def _practice(
    db, *, timeline, l0=PracticeL0.INPUT, l1="PESTICIDE", l2=None,
    is_special=False, common_name_cosh_id=None,
) -> Practice:
    p = Practice(
        timeline_id=timeline.id, l0_type=l0, l1_type=l1, l2_type=l2,
        is_special_input=is_special,
        common_name_cosh_id=common_name_cosh_id,
    )
    db.add(p)
    await db.flush()
    if common_name_cosh_id:
        # Mirror the COMMON_NAME element so create_relation finds it.
        db.add(Element(
            practice_id=p.id, element_type="COMMON_NAME",
            cosh_ref=common_name_cosh_id, value="",
        ))
        await db.flush()
    return p


async def _setup_timeline(db) -> tuple:
    """Spin up a client + ANNUAL paddy package + one DAS timeline.
    Returns (client, user, package, timeline)."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    pkg = await make_package(db, client, name="P", crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, name="TL", from_type=TimelineFromType.DAS,
                             from_value=0, to_value=15)
    return client, user, pkg, tl


# ── Happy paths ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_pure_and_relation(db):
    client, user, pkg, tl = await _setup_timeline(db)
    p1 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:1")
    p2 = await _practice(db, timeline=tl, l1="FERTILIZER", common_name_cosh_id="cn:2")
    await db.commit()

    out = await create_relation(
        client_id=client.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    assert out["relation_type"] == "AND"

    refreshed = (await db.execute(select(Practice).where(Practice.id.in_([p1.id, p2.id])))).scalars().all()
    for prac in refreshed:
        assert prac.relation_id == out["id"]
        assert prac.relation_role.startswith("PART_1__OPT_1__POS_")


@requires_docker
@pytest.mark.asyncio
async def test_create_or_within_pesticides(db):
    client, user, pkg, tl = await _setup_timeline(db)
    p1 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:1")
    p2 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:2")
    await db.commit()

    out = await create_relation(
        client_id=client.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.OR,
            parts=[[[p1.id], [p2.id]]],
        ),
        db=db, current_user=user,
    )
    assert out["relation_type"] == "OR"


# ── AND restriction: input-only ──────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_and_with_non_input_422(db):
    client, user, pkg, tl = await _setup_timeline(db)
    inp = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:1")
    non_inp = await _practice(
        db, timeline=tl, l0=PracticeL0.NON_INPUT, l1="WATER_MGMT",
    )
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.AND,
                parts=[[[inp.id, non_inp.id]]],
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "relation_validation_failed"
    codes = {e["code"] for e in ei.value.detail["errors"]}
    assert "relation_and_non_input" in codes


# ── OR L1 restriction ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_or_pesticide_plus_fertilizer_422(db):
    client, user, pkg, tl = await _setup_timeline(db)
    pest = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:p")
    fert = await _practice(db, timeline=tl, l1="FERTILIZER", common_name_cosh_id="cn:f")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.OR,
                parts=[[[pest.id], [fert.id]]],
            ),
            db=db, current_user=user,
        )
    codes = {e["code"] for e in ei.value.detail["errors"]}
    assert "relation_or_cross_l1" in codes


@requires_docker
@pytest.mark.asyncio
async def test_or_with_special_input_alongside_pesticide_passes(db):
    """Adjuvant exception: Special Inputs may mix with either side
    of an OR relation."""
    client, user, pkg, tl = await _setup_timeline(db)
    pest = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:p")
    adj = await _practice(
        db, timeline=tl, l1="ADJUVANT", is_special=True,
        common_name_cosh_id="cn:adj",
    )
    await db.commit()

    out = await create_relation(
        client_id=client.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.OR,
            parts=[[[pest.id], [adj.id]]],
        ),
        db=db, current_user=user,
    )
    assert out["relation_type"] == "OR"


# ── Cross-timeline + already-in-relation ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_practice_from_different_timeline_422(db):
    client, user, pkg, tl = await _setup_timeline(db)
    other_tl = await make_timeline(
        db, pkg, name="TL2", from_type=TimelineFromType.DAS,
        from_value=20, to_value=40,
    )
    p1 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:1")
    p2 = await _practice(db, timeline=other_tl, l1="PESTICIDE", common_name_cosh_id="cn:2")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.AND,
                parts=[[[p1.id, p2.id]]],
            ),
            db=db, current_user=user,
        )
    codes = {e["code"] for e in ei.value.detail["errors"]}
    assert "relation_cross_timeline" in codes


@requires_docker
@pytest.mark.asyncio
async def test_practice_already_in_another_relation_422(db):
    """Once a Practice is committed to a Relation, it can't be
    re-used in a second one. Spec §6.4: a practice is in exactly
    one relation OR independent."""
    client, user, pkg, tl = await _setup_timeline(db)
    p1 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:1")
    p2 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:2")
    p3 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:3")
    await db.commit()

    # First relation: p1 + p2
    await create_relation(
        client_id=client.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            parts=[[[p1.id, p2.id]]],
        ),
        db=db, current_user=user,
    )
    # Try to use p2 in a second relation
    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.AND,
                parts=[[[p2.id, p3.id]]],
            ),
            db=db, current_user=user,
        )
    codes = {e["code"] for e in ei.value.detail["errors"]}
    assert "relation_practice_already_in_relation" in codes


# ── Practice not found ───────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_practice_not_found_422(db):
    client, user, pkg, tl = await _setup_timeline(db)
    real = await _practice(db, timeline=tl, common_name_cosh_id="cn:1")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.AND,
                parts=[[[real.id, "00000000-0000-0000-0000-000000000000"]]],
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "relation_practice_not_found"


@requires_docker
@pytest.mark.asyncio
async def test_empty_relation_422(db):
    client, user, pkg, tl = await _setup_timeline(db)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.AND, parts=[[[]]],
            ),
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "relation_empty"


# ── Structural: double brackets ──────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_double_brackets_422(db):
    client, user, pkg, tl = await _setup_timeline(db)
    a = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:a")
    b = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:b")
    c = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:c")
    d = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:d")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.OR,
                parts=[[[a.id, b.id], [c.id, d.id]]],
            ),
            db=db, current_user=user,
        )
    codes = {e["code"] for e in ei.value.detail["errors"]}
    assert "relation_double_brackets" in codes


# ── Combinatorial: branch always duplicates ──────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_branch_always_duplicates_422(db):
    """`(A+B) or (C+D) + A` — branch (A+B) duplicates the mandatory
    A. Save must reject."""
    client, user, pkg, tl = await _setup_timeline(db)
    a1 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:X")
    b = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:B")
    c = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:C")
    a2 = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:X")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_relation(
            client_id=client.id, timeline_id=tl.id,
            request=RelationCreate(
                relation_type=RelationType.AND,
                parts=[
                    [[a1.id, b.id], [c.id]],
                    [[a2.id]],
                ],
            ),
            db=db, current_user=user,
        )
    codes = {e["code"] for e in ei.value.detail["errors"]}
    assert "relation_branch_always_duplicates" in codes


# ── Persistence verification ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_role_strings_correctly_persisted_for_complex_structure(db):
    """`(A+B) or C + D` — Part 1 has compound (A+B) and simple (C),
    Part 2 has simple (D). Role strings should map to:
    A: PART_1__OPT_1__POS_1, B: PART_1__OPT_1__POS_2,
    C: PART_1__OPT_2__POS_1, D: PART_2__OPT_1__POS_1."""
    client, user, pkg, tl = await _setup_timeline(db)
    a = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:a")
    b = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:b")
    c = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:c")
    d = await _practice(db, timeline=tl, l1="PESTICIDE", common_name_cosh_id="cn:d")
    await db.commit()

    out = await create_relation(
        client_id=client.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND,
            parts=[
                [[a.id, b.id], [c.id]],
                [[d.id]],
            ],
        ),
        db=db, current_user=user,
    )
    refreshed = {p.id: p for p in (await db.execute(
        select(Practice).where(Practice.id.in_([a.id, b.id, c.id, d.id]))
    )).scalars().all()}
    assert refreshed[a.id].relation_role == "PART_1__OPT_1__POS_1"
    assert refreshed[b.id].relation_role == "PART_1__OPT_1__POS_2"
    assert refreshed[c.id].relation_role == "PART_1__OPT_2__POS_1"
    assert refreshed[d.id].relation_role == "PART_2__OPT_1__POS_1"
