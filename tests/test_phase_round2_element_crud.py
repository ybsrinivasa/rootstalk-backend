"""Round 2 — per-element CRUD across CCA/CHA/QA (2026-05-09).

Adds POST/PUT/DELETE for individual Element / Element / Element
rows so an SE can fix a typo or swap one cosh_ref without re-saving
the whole Practice. The shared helpers re-run the L2 rule book over
the *resulting* element set, so the Practice can never drift out of
spec via a per-element edit:

  • POST    — validate (existing + new) before insert
  • PUT     — validate (siblings + replacement) before mutate
  • DELETE  — validate (remaining siblings only) before delete

Coverage: one happy-path triple (add → edit → delete) per pipe plus
a focused failure-mode test per operation. The pipes are:

  CCA           Element / Practice            /timelines/{tl_id}/...
  CHA-PG global Element / Practice        /advisory/global/pg-recs/...
  CHA-PG local  Element / Practice        /client/{cid}/pg-recs/...
  CHA-SP        Element / Practice        /client/{cid}/sp-recs/...
  Q&A           Element / Practice        /client/{cid}/std-resps/...

This file does NOT re-test the rule book itself (that's
`tests/test_l2_element_validator.py`). It pins that the Round-2
endpoints walk through it on every write.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
Element,Practice,PGRecommendation,Timeline,
PracticeL0,TimelineFromType,
)
from app.modules.advisory.router import (
add_cca_element,add_client_pg_element,add_global_cca_element,
add_global_pg_element,add_qa_element,add_qa_practice,
add_qa_timeline,add_sp_element,create_practice,
delete_cca_element,delete_client_pg_element,delete_global_cca_element,
delete_global_pg_element,delete_qa_element,delete_sp_element,
update_cca_element,update_client_pg_element,update_global_cca_element,
update_global_pg_element,update_qa_element,update_sp_element,
)
from app.modules.advisory.schemas import (
ElementIn,PracticeCreate,QAPracticeCreate,QATimelineCreate,
)
from app.modules.farmpundit.router import create_standard_response
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
make_client,make_client_user,make_crop_reference,make_package,
make_pg_practice,make_pg_recommendation,make_pg_timeline,
make_sp_practice,make_sp_recommendation,make_sp_timeline,
make_timeline,make_user,
)


# ── Shared cosh seeding (same as Round 1) ──────────────────────────────────

async def _seed_pesticide_cosh(db) -> None:
    # Batch 29: validator now resolves cosh_core: slugs to real Cosh
    # `core_type` names. Test seeds updated to match — the values picked
    # in test elements (cn:imida, am:foliar_spray, etc.) are valid
    # provided their core_type matches what the validator now queries.
    rows = [
        CoshCoreItem(cosh_id="cn:imida",core_type="common_names_of_inputs",
translations={"en": "Imidacloprid"},status="active"),
        CoshCoreItem(cosh_id="am:foliar_spray",core_type="application_methods",
translations={"en": "Foliar spray"},status="active"),
        CoshCoreItem(cosh_id="am:soil_drench",core_type="application_methods",
translations={"en": "Soil drench"},status="active"),
        CoshCoreItem(cosh_id="du:ml_per_l",core_type="units_data",
translations={"en": "ml/L"},status="active"),
        # The legacy `brand` Core path still exists in the validator
        # (it falls back to cosh_options_view when empty), so the
        # synthetic brand row is still useful for testing the
        # name-string contract in the legacy code-path.
        CoshCoreItem(
cosh_id="brand:confidor",core_type="brand",
parent_cosh_id="cn:imida",
translations={"en": "Confidor"},
metadata_={
"manufacturer_name": "Bayer",
"formulation_cosh_id": "form:SC",
"ai_concentration": "17.8% SL",
},
status="active",
),
        CoshCoreItem(cosh_id="form:SC",core_type="formulations",
translations={"en": "SC"},status="active"),
    ]
    for r in rows:
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


# ── CCA: full triple + representative failures ─────────────────────────────

async def _seed_cca_practice(db):
    """Build a complete CHEMICAL_PESTICIDES Practice via create_practice
    so subsequent per-element ops have a real validated baseline."""
    client = await make_client(db)
    se = await make_user(db, name="SE-CCA")
    await make_crop_reference(db, "crop:test", name="Test Crop")
    pkg = await make_package(db, client, name="P", crop_cosh_id="crop:test")
    tl = await make_timeline(
db,pkg,name="TL",from_type=TimelineFromType.DAS,
from_value=0,to_value=15,
)
    await _seed_pesticide_cosh(db)
    await db.commit()
    practice = await create_practice(
client_id=client.id,timeline_id=tl.id,
request=PracticeCreate(
l0_type=PracticeL0.INPUT,l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=_full_pesticide_elements(),
        ),
        db=db, current_user=se,
    )
    return client, se, tl, practice


async def _seed_cca_l2none_practice(db):
    """l2_type=None practice with one element. Inserted via ORM so the
    Batch 30 `l2_type_required` create-handler guard doesn't reject
    it — the test's purpose is to exercise element CRUD lifecycle
    without the strict per-L2 allowlist getting in the way, not to
    test Practice create itself."""
    client = await make_client(db)
    se = await make_user(db, name="SE-CCA-N")
    await make_crop_reference(db, "crop:n", name="X")
    pkg = await make_package(db, client, name="P-N", crop_cosh_id="crop:n")
    tl = await make_timeline(db, pkg, name="TL-N")
    await db.commit()
    practice = Practice(
timeline_id=tl.id,
l0_type=PracticeL0.INPUT,l1_type=None,l2_type=None,
display_order=0,is_special_input=False,
)
    db.add(practice)
    await db.flush()
    db.add(Element(
practice_id=practice.id,element_type="NOTE",value="seed",
))
    await db.commit()
    await db.refresh(practice)
    return client, se, tl, practice


@requires_docker
@pytest.mark.asyncio
async def test_cca_element_add_edit_delete_lifecycle(db):
    """Headline UX: add an extra element, edit its value, then delete
    it. Run on an l2_type=None practice so the strict allowlist
    (UNKNOWN_FIELD on every CHEMICAL_PESTICIDES non-spec element)
    doesn't get in the way of testing the lifecycle plumbing."""
    client, se, tl, practice = await _seed_cca_l2none_practice(db)
    added = await add_cca_element(
client_id=client.id,timeline_id=tl.id,practice_id=practice.id,
body=ElementIn(element_type="EXTRA",value="Apply at dawn"),
        db=db, current_user=se,
    )
    assert added["element_type"] == "EXTRA"

    updated = await update_cca_element(
client_id=client.id,timeline_id=tl.id,
practice_id=practice.id,element_id=added["id"],
body=ElementIn(element_type="EXTRA",value="Apply at dusk"),
        db=db, current_user=se,
    )
    assert updated["value"] == "Apply at dusk"

    await delete_cca_element(
client_id=client.id,timeline_id=tl.id,
practice_id=practice.id,element_id=added["id"],
db=db,current_user=se,
)
    rows = (await db.execute(
select(Element).where(Element.id == added["id"])
    )).scalars().all()
    assert rows == []


@requires_docker
@pytest.mark.asyncio
async def test_cca_element_add_unknown_field_returns_422(db):
    """The L2 rule book is a strict allowlist — adding any element
    not declared on CHEMICAL_PESTICIDES surfaces UNKNOWN_FIELD, no row
    inserted. This is what gives `l2_type` its "schema" power: the SE
    can't poison a practice with arbitrary keys."""
    client, se, tl, practice = await _seed_cca_practice(db)
    pre_count = len((await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalars().all())

    with pytest.raises(HTTPException) as exc:
        await add_cca_element(
client_id=client.id,timeline_id=tl.id,practice_id=practice.id,
body=ElementIn(element_type="RANDOM_KEY",value="x"),
            db=db, current_user=se,
        )
    assert exc.value.status_code == 422

    post_count = len((await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalars().all())
    assert pre_count == post_count  # no row added


@requires_docker
@pytest.mark.asyncio
async def test_cca_element_delete_mandatory_blocked_by_validator(db):
    """Cannot DELETE a mandatory element — the resulting set wouldn't
    satisfy the L2 rule book. Forces the 'replace, don't remove'
    pattern. To wipe everything, the SE deletes the whole Practice."""
    client, se, tl, practice = await _seed_cca_practice(db)
    rows = (await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    dosage = next(r for r in rows if r.element_type == "DOSAGE")

    with pytest.raises(HTTPException) as exc:
        await delete_cca_element(
client_id=client.id,timeline_id=tl.id,
practice_id=practice.id,element_id=dosage.id,
db=db,current_user=se,
)
    assert exc.value.status_code == 422
    # The DOSAGE row is still present.
    still_there = (await db.execute(
select(Element).where(Element.id == dosage.id)
    )).scalar_one_or_none()
    assert still_there is not None


@requires_docker
@pytest.mark.asyncio
async def test_cca_element_update_replaces_value(db):
    """The headline UX: an SE realises 0.5 ml/L should've been 0.4. PUT
    on the existing DOSAGE element changes value without rewriting the
    whole Practice. The L2 rule book still passes."""
    client, se, tl, practice = await _seed_cca_practice(db)
    rows = (await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    dosage = next(r for r in rows if r.element_type == "DOSAGE")

    out = await update_cca_element(
client_id=client.id,timeline_id=tl.id,
practice_id=practice.id,element_id=dosage.id,
body=ElementIn(element_type="DOSAGE",value="0.4"),
        db=db, current_user=se,
    )
    assert out["value"] == "0.4"

    refreshed = (await db.execute(
select(Element).where(Element.id == dosage.id)
    )).scalar_one()
    assert refreshed.value == "0.4"


@requires_docker
@pytest.mark.asyncio
async def test_cca_element_update_404_on_wrong_practice(db):
    """element_id belongs to practice A, URL routes via practice B → 404.
    Defends against URL-tampering between two practices the SE can see."""
    client, se, tl, practice = await _seed_cca_practice(db)
    rows = (await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    elem = rows[0]
    # Make a second practice in the same timeline.
    other = await create_practice(
client_id=client.id,timeline_id=tl.id,
request=PracticeCreate(
l0_type=PracticeL0.INPUT,l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
elements=_full_pesticide_elements(),
        ),
        db=db, current_user=se,
    )

    with pytest.raises(HTTPException) as exc:
        await update_cca_element(
client_id=client.id,timeline_id=tl.id,
practice_id=other.id,element_id=elem.id,
body=ElementIn(element_type="DOSAGE",value="0.4"),
            db=db, current_user=se,
        )
    assert exc.value.status_code == 404


# ── CHA-PG global: smoke (same helpers as CCA via _load_cca_practice) ──────

async def _seed_global_pg_practice(db):
    """Seed a global PG with a CHEMICAL_PESTICIDES practice (full
element set seeded directly so the rule book passes). Used by the
    validation-error test below."""
    pg = await make_pg_recommendation(db)
    tl = await make_pg_timeline(db, pg)
    await _seed_pesticide_cosh(db)
    practice = await make_pg_practice(
db,tl,l0_type="INPUT",l1_type="PESTICIDE",
)
    practice.l2_type = "CHEMICAL_PESTICIDES"
    practice.is_special_input = False
    practice.frequency_days = None
    for el in _full_pesticide_elements():
        db.add(Element(practice_id=practice.id, **el.model_dump()))
    await db.commit()
    se = await make_user(db, name="SE-CHA")
    return se, pg, tl, practice


async def _seed_global_pg_l2none_practice(db):
    """l2_type=None PG practice — for lifecycle tests that need the
    validator to no-op."""
    pg = await make_pg_recommendation(db)
    tl = await make_pg_timeline(db, pg)
    practice = await make_pg_practice(db, tl, l0_type="INPUT")
    practice.l2_type = None
    db.add(Element(practice_id=practice.id,element_type="SEED",
value="x"))
    await db.commit()
    se = await make_user(db, name="SE-CHA-N")
    return se, pg, tl, practice


@requires_docker
@pytest.mark.asyncio
async def test_global_pg_element_add_and_delete(db):
    se, pg, tl, practice = await _seed_global_pg_l2none_practice(db)
    added = await add_global_pg_element(
pg_id=pg.id,tl_id=tl.id,practice_id=practice.id,
body=ElementIn(element_type="NOTES",value="Test"),
        db=db, current_user=se,
    )
    await delete_global_pg_element(
pg_id=pg.id,tl_id=tl.id,
practice_id=practice.id,element_id=added["id"],
db=db,current_user=se,
)
    rows = (await db.execute(
select(Element).where(Element.id == added["id"])
    )).scalars().all()
    assert rows == []


@requires_docker
@pytest.mark.asyncio
async def test_global_pg_element_update_validator_runs(db):
    """Mutating DOSAGE_UNIT to a cosh_id that isn't a dosage_unit Core
    (here a brand id) trips the L2 rule book — validator runs on PUT."""
    se, pg, tl, practice = await _seed_global_pg_practice(db)
    rows = (await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    dosage_unit = next(r for r in rows if r.element_type == "DOSAGE_UNIT")

    with pytest.raises(HTTPException) as exc:
        await update_global_pg_element(
pg_id=pg.id,tl_id=tl.id,
practice_id=practice.id,element_id=dosage_unit.id,
body=ElementIn(element_type="DOSAGE_UNIT",cosh_ref="brand:confidor"),
            db=db, current_user=se,
        )
    assert exc.value.status_code == 422


# ── CHA-PG local: smoke ────────────────────────────────────────────────────

async def _seed_local_pg_l2none_practice(db):
    """l2_type=None local-PG practice for lifecycle exercising."""
    client = await make_client(db)
    se = await make_user(db, name="SE-PG-local")
    pg = PGRecommendation(
problem_group_cosh_id="pg:test",client_id=client.id,
area_or_plant="AREA_WISE",
)
    db.add(pg); await db.flush()
    tl = Timeline(
pg_recommendation_id=pg.id,name="Local TL",
from_value=0,to_value=7,
)
    db.add(tl); await db.flush()
    practice = Practice(
timeline_id=tl.id,l0_type="INPUT",l1_type=None,l2_type=None,
)
    db.add(practice); await db.flush()
    db.add(Element(practice_id=practice.id,element_type="SEED",
value="x"))
    await db.commit()
    return client, se, pg, tl, practice


@requires_docker
@pytest.mark.asyncio
async def test_local_pg_element_lifecycle(db):
    client, se, pg, tl, practice = await _seed_local_pg_l2none_practice(db)
    added = await add_client_pg_element(
client_id=client.id,pg_id=pg.id,tl_id=tl.id,
practice_id=practice.id,
body=ElementIn(element_type="NOTES",value="Local note"),
        db=db, current_user=se,
    )
    out = await update_client_pg_element(
client_id=client.id,pg_id=pg.id,tl_id=tl.id,
practice_id=practice.id,element_id=added["id"],
body=ElementIn(element_type="NOTES",value="Updated"),
        db=db, current_user=se,
    )
    assert out["value"] == "Updated"
    await delete_client_pg_element(
client_id=client.id,pg_id=pg.id,tl_id=tl.id,
practice_id=practice.id,element_id=added["id"],
db=db,current_user=se,
)


# ── CHA-SP: smoke ──────────────────────────────────────────────────────────

async def _seed_sp_strict_practice(db):
    """Strict-validation SP practice (CHEMICAL_PESTICIDES). For
    failure-mode tests."""
    client = await make_client(db)
    se = await make_user(db, name="SE-SP")
    sp = await make_sp_recommendation(db, client)
    tl = await make_sp_timeline(db, sp)
    await _seed_pesticide_cosh(db)
    practice = await make_sp_practice(
db,tl,l0_type="INPUT",l1_type="PESTICIDE",
)
    practice.l2_type = "CHEMICAL_PESTICIDES"
    for el in _full_pesticide_elements():
        db.add(Element(practice_id=practice.id, **el.model_dump()))
    await db.commit()
    return client, se, sp, tl, practice


async def _seed_sp_l2none_practice(db):
    client = await make_client(db)
    se = await make_user(db, name="SE-SP-N")
    sp = await make_sp_recommendation(db, client)
    tl = await make_sp_timeline(db, sp)
    practice = await make_sp_practice(db, tl, l0_type="INPUT")
    practice.l2_type = None
    db.add(Element(practice_id=practice.id, element_type="SEED", value="x"))
    await db.commit()
    return client, se, sp, tl, practice


@requires_docker
@pytest.mark.asyncio
async def test_sp_element_lifecycle(db):
    client, se, sp, tl, practice = await _seed_sp_l2none_practice(db)
    added = await add_sp_element(
client_id=client.id,sp_id=sp.id,tl_id=tl.id,
practice_id=practice.id,
body=ElementIn(element_type="NOTES",value="SP note"),
        db=db, current_user=se,
    )
    rows = (await db.execute(
select(Element).where(Element.id == added["id"])
    )).scalars().all()
    assert len(rows) == 1
    await delete_sp_element(
client_id=client.id,sp_id=sp.id,tl_id=tl.id,
practice_id=practice.id,element_id=added["id"],
db=db,current_user=se,
)


@requires_docker
@pytest.mark.asyncio
async def test_sp_element_delete_mandatory_blocked(db):
    """SP shares the same validator path — deleting a mandatory
    element from a strict-typed practice is blocked here too."""
    client, se, sp, tl, practice = await _seed_sp_strict_practice(db)
    dosage = (await db.execute(
select(Element).where(
Element.practice_id == practice.id,
Element.element_type == "DOSAGE",
)
    )).scalar_one()

    with pytest.raises(HTTPException) as exc:
        await delete_sp_element(
client_id=client.id,sp_id=sp.id,tl_id=tl.id,
practice_id=practice.id,element_id=dosage.id,
db=db,current_user=se,
)
    assert exc.value.status_code == 422


# ── Q&A: lifecycle + portal-member auth ────────────────────────────────────

async def _seed_qa_practice(db):
    """l2_type=None QA practice — lifecycle ops use this so the strict
    allowlist doesn't reject a freshly-added element."""
    client = await make_client(db)
    se = await make_user(db, name="SE-QA")
    await make_client_user(db, user=se, client=client)
    sr = await create_standard_response(
client_id=client.id,
data={"question_text": "Q?","crop_cosh_id": None},
db=db,current_user=se,
)
    tl = await add_qa_timeline(
client_id=client.id,sr_id=sr["id"],
request=QATimelineCreate(name="W1",to_value=7),
        db=db, current_user=se,
    )
    practice = await add_qa_practice(
client_id=client.id,sr_id=sr["id"],tl_id=tl["id"],
request=QAPracticeCreate(
l0_type="INPUT",l1_type=None,l2_type=None,
elements=[ElementIn(element_type="SEED",value="x")],
        ),
        db=db, current_user=se,
    )
    return client, se, sr["id"], tl["id"], practice["id"]


@requires_docker
@pytest.mark.asyncio
async def test_qa_element_lifecycle(db):
    client, se, sr_id, tl_id, p_id = await _seed_qa_practice(db)
    added = await add_qa_element(
client_id=client.id,sr_id=sr_id,tl_id=tl_id,
practice_id=p_id,
body=ElementIn(element_type="NOTES",value="QA note"),
        db=db, current_user=se,
    )
    out = await update_qa_element(
client_id=client.id,sr_id=sr_id,tl_id=tl_id,
practice_id=p_id,element_id=added["id"],
body=ElementIn(element_type="NOTES",value="Updated"),
        db=db, current_user=se,
    )
    assert out["value"] == "Updated"
    await delete_qa_element(
client_id=client.id,sr_id=sr_id,tl_id=tl_id,
practice_id=p_id,element_id=added["id"],
db=db,current_user=se,
)


@requires_docker
@pytest.mark.asyncio
async def test_qa_element_rejects_non_member(db):
    """The QA endpoints carry the same _assert_portal_member gate the
    QA practice endpoints do — outsiders get 403, NOT 404, so URL
    enumeration doesn't leak which client has a Q&A library."""
    client, se, sr_id, tl_id, p_id = await _seed_qa_practice(db)
    outsider = await make_user(db, name="Outsider", skip_auto_link=True)

    with pytest.raises(HTTPException) as exc:
        await add_qa_element(
client_id=client.id,sr_id=sr_id,tl_id=tl_id,
practice_id=p_id,
body=ElementIn(element_type="NOTES",value="x"),
            db=db, current_user=outsider,
        )
    assert exc.value.status_code in (401, 403)


# ── Round-2-wide sanity: helpers handle the empty-element-set case ────────

@requires_docker
@pytest.mark.asyncio
async def test_delete_only_element_on_l2_none_practice_succeeds(db):
    """l2_type=None bypasses the validator (same rule as Round 1).
    So the full delete of the only element is allowed — there's
    nothing to validate against."""
    client = await make_client(db)
    se = await make_user(db, name="SE")
    await make_crop_reference(db, "crop:test", name="X")
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg)
    await db.commit()
    # Inserted via ORM — Batch 30's create-handler guard rejects
    # l2_type=None, but the test specifically needs a shell practice
    # to exercise the empty-element-set helper path.
    practice = Practice(
timeline_id=tl.id,
l0_type=PracticeL0.INPUT,l1_type=None,l2_type=None,
display_order=0,is_special_input=False,
)
    db.add(practice)
    await db.flush()
    db.add(Element(
practice_id=practice.id,element_type="NOTES",value="x",
))
    await db.commit()
    await db.refresh(practice)
    elem = (await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalar_one()
    await delete_cca_element(
client_id=client.id,timeline_id=tl.id,
practice_id=practice.id,element_id=elem.id,
db=db,current_user=se,
)
    remaining = (await db.execute(
select(Element).where(Element.practice_id == practice.id)
    )).scalars().all()
    assert remaining == []
