"""Global PG Practice update + delete (Batch 39P-e, 2026-05-16).

Pin the new PG endpoints against the same body validators the CCA
sibling already passes:

  • Atomic PUT replaces element set + scalars; runs L2 validator,
    interval-fits-timeline check, Brand-Lock validation.
  • DELETE drops the Practice (Elements cascade).
  • is_brand_locked extends to PG per UCAT.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, PGRecommendation, Practice, PracticeL0, Timeline,
)
from app.modules.advisory.router import (
    add_global_pg_practice, delete_global_pg_practice,
    update_global_pg_practice,
)
from app.modules.advisory.schemas import ElementIn, PGPracticeCreate
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_pesticide_cosh(db) -> None:
    """Lifted from round1_authoring_parity — gives the cascade service
    real Imidacloprid / Confidor / Foliar Spray rows to walk."""
    for r in [
        CoshCoreItem(cosh_id="cn:imida", core_type="common_names_of_inputs",
                     translations={"en": "Imidacloprid"}, status="active"),
        CoshCoreItem(cosh_id="am:foliar_spray", core_type="application_methods",
                     translations={"en": "Foliar spray"}, status="active"),
        CoshCoreItem(cosh_id="du:ml_per_l", core_type="units_data",
                     translations={"en": "ml/L"}, status="active"),
        CoshCoreItem(cosh_id="brand:confidor", core_type="brand",
                     parent_cosh_id="cn:imida",
                     translations={"en": "Confidor"},
                     metadata_={
                         "manufacturer_name": "Bayer",
                         "formulation_cosh_id": "form:SC",
                         "ai_concentration": "17.8% SL",
                     },
                     status="active"),
        CoshCoreItem(cosh_id="form:SC", core_type="formulations",
                     translations={"en": "SC"}, status="active"),
    ]:
        db.add(r)
    await db.flush()


def _full_pesticide_elements() -> list[ElementIn]:
    return [
        ElementIn(element_type="COMMON_NAME", cosh_ref="cn:imida"),
        ElementIn(element_type="MANUFACTURER", cosh_ref="Bayer"),
        ElementIn(element_type="BRAND_NAME", cosh_ref="brand:confidor"),
        ElementIn(element_type="FORMULATION", cosh_ref="form:SC"),
        ElementIn(element_type="AI_CONCENTRATION", cosh_ref="17.8% SL"),
        ElementIn(element_type="APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
        ElementIn(element_type="DOSAGE", value="0.5"),
        ElementIn(element_type="DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
    ]


async def _seed_pg_with_timeline(db):
    pg = PGRecommendation(
        problem_group_cosh_id=f"pg:{uuid.uuid4().hex[:6]}",
        client_id=None, area_or_plant="AREA_WISE", status="DRAFT",
    )
    db.add(pg)
    await db.flush()
    tl = Timeline(
        pg_recommendation_id=pg.id, name="TL",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=14,
    )
    db.add(tl)
    await db.flush()
    return pg, tl


@requires_docker
@pytest.mark.asyncio
async def test_pg_practice_update_replaces_elements(db):
    user = await make_user(db, name="CM")
    await _seed_pesticide_cosh(db)
    pg, tl = await _seed_pg_with_timeline(db)
    await db.commit()
    p = await add_global_pg_practice(
        pg_id=pg.id, tl_id=tl.id,
        request=PGPracticeCreate(
            l0_type="INPUT", l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
            elements=_full_pesticide_elements(),
        ),
        db=db, current_user=user,
    )
    # Bump DOSAGE — same Practice id, new element set.
    new_elements = _full_pesticide_elements()
    for el in new_elements:
        if el.element_type == "DOSAGE":
            el.value = "0.8"
    out = await update_global_pg_practice(
        pg_id=pg.id, tl_id=tl.id, practice_id=p.id,
        request=PGPracticeCreate(
            l0_type="INPUT", l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
            elements=new_elements,
        ),
        db=db, current_user=user,
    )
    assert out.id == p.id
    rows = (await db.execute(
        select(Element).where(Element.practice_id == p.id)
    )).scalars().all()
    dosage = next(r for r in rows if r.element_type == "DOSAGE")
    assert dosage.value == "0.8"


@requires_docker
@pytest.mark.asyncio
async def test_pg_practice_delete_cascades_elements(db):
    user = await make_user(db, name="CM")
    await _seed_pesticide_cosh(db)
    pg, tl = await _seed_pg_with_timeline(db)
    await db.commit()
    p = await add_global_pg_practice(
        pg_id=pg.id, tl_id=tl.id,
        request=PGPracticeCreate(
            l0_type="INPUT", l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
            elements=_full_pesticide_elements(),
        ),
        db=db, current_user=user,
    )
    await delete_global_pg_practice(
        pg_id=pg.id, tl_id=tl.id, practice_id=p.id,
        db=db, current_user=user,
    )
    assert (await db.execute(
        select(Practice).where(Practice.id == p.id)
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(Element).where(Element.practice_id == p.id)
    )).scalars().all() == []


@requires_docker
@pytest.mark.asyncio
async def test_pg_practice_update_404_on_wrong_timeline(db):
    user = await make_user(db, name="CM")
    await _seed_pesticide_cosh(db)
    pg_a, tl_a = await _seed_pg_with_timeline(db)
    pg_b, tl_b = await _seed_pg_with_timeline(db)
    await db.commit()
    p = await add_global_pg_practice(
        pg_id=pg_a.id, tl_id=tl_a.id,
        request=PGPracticeCreate(
            l0_type="INPUT", l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
            elements=_full_pesticide_elements(),
        ),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as exc:
        await update_global_pg_practice(
            pg_id=pg_b.id, tl_id=tl_b.id, practice_id=p.id,
            request=PGPracticeCreate(
                l0_type="INPUT", l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                elements=_full_pesticide_elements(),
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_pg_practice_carries_is_brand_locked_on_create(db):
    """Brand Lock UCAT extension: PGPracticeCreate.is_brand_locked
    flows through the shared `_create_practice_at_global_timeline`
    helper and lands on the persisted Practice row."""
    user = await make_user(db, name="CM")
    await _seed_pesticide_cosh(db)
    pg, tl = await _seed_pg_with_timeline(db)
    await db.commit()
    p = await add_global_pg_practice(
        pg_id=pg.id, tl_id=tl.id,
        request=PGPracticeCreate(
            l0_type="INPUT", l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
            is_brand_locked=True,
            elements=_full_pesticide_elements(),
        ),
        db=db, current_user=user,
    )
    refreshed = (await db.execute(
        select(Practice).where(Practice.id == p.id)
    )).scalar_one()
    assert refreshed.is_brand_locked is True
