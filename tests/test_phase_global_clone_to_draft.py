"""Global clone-to-draft + extended deep-copy + lineage
(Batch 39L-b, 2026-05-16).

The CM-stated flow: "The Active version is also the Editable
version by default ... its data are populated in the front end for
the expert to edit and once again publish to form a new version."

Concretely: the CM clones the ACTIVE (or INACTIVE) row into a new
DRAFT, edits the DRAFT, and publishes it as v_{max + 1}. The
deep-copy now carries Practices + Elements + Relations + CQs +
is_brand_locked across, so the CM doesn't lose authored
sub-structure on clone.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element, Package,
    PackageStatus, PackageType, Practice, PracticeConditional, PracticeL0,
    Relation, RelationConditional, RelationType, Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    clone_global_to_draft, create_global_conditional_question,
    create_global_relation, get_global_package_lineage,
    link_global_practice_conditional, link_global_relation_conditional,
    publish_global_package,
)
from app.modules.advisory.schemas import (
    ConditionalQuestionCreate, PracticeConditionalCreate, RelationCreate,
)
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_global_pkg(db, *, name: str = "GP") -> Package:
    pkg = Package(
        client_id=None, name=name,
        crop_cosh_id="crop:tomato",
        package_type=PackageType.ANNUAL, duration_days=30,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.DRAFT,
    )
    db.add(pkg)
    await db.flush()
    return pkg


async def _add_timeline(db, pkg, *, name="T1", from_value=0, to_value=10):
    tl = Timeline(
        package_id=pkg.id, name=name,
        from_type=TimelineFromType.DAS,
        from_value=from_value, to_value=to_value,
    )
    db.add(tl)
    await db.flush()
    return tl


async def _add_practice(db, tl, *, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES",
                        common_name="cn:imid", is_brand_locked=False):
    p = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type=l1, l2_type=l2,
        common_name_cosh_id=common_name,
        is_brand_locked=is_brand_locked,
    )
    db.add(p)
    await db.flush()
    if common_name:
        db.add(Element(
            practice_id=p.id, element_type="COMMON_NAME",
            cosh_ref=common_name, value="",
        ))
        await db.flush()
    return p


@requires_docker
@pytest.mark.asyncio
async def test_clone_active_creates_new_draft_with_content(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl = await _add_timeline(db, pkg)
    p = await _add_practice(db, tl)
    await db.commit()

    # Publish the source package so it's ACTIVE.
    pkg = await publish_global_package(pkg_id=pkg.id, db=db, current_user=user)
    assert pkg.status == PackageStatus.ACTIVE

    # Now clone.
    draft = await clone_global_to_draft(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert draft.id != pkg.id
    assert draft.status == PackageStatus.DRAFT
    assert draft.source_version_id == pkg.id
    assert draft.name == pkg.name
    assert draft.crop_cosh_id == pkg.crop_cosh_id

    # Content copied: timeline + practice + element.
    new_tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == draft.id)
    )).scalars().all()
    assert len(new_tls) == 1
    new_ps = (await db.execute(
        select(Practice).where(Practice.timeline_id == new_tls[0].id)
    )).scalars().all()
    assert len(new_ps) == 1
    assert new_ps[0].id != p.id
    new_els = (await db.execute(
        select(Element).where(Element.practice_id == new_ps[0].id)
    )).scalars().all()
    assert {e.element_type for e in new_els} == {"COMMON_NAME"}


@requires_docker
@pytest.mark.asyncio
async def test_clone_carries_is_brand_locked(db):
    """is_brand_locked (Batch 39I-a) survives the clone."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl = await _add_timeline(db, pkg)
    await _add_practice(db, tl, is_brand_locked=True)
    await db.commit()
    pkg = await publish_global_package(pkg_id=pkg.id, db=db, current_user=user)

    draft = await clone_global_to_draft(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    new_p = (await db.execute(
        select(Practice).join(Timeline).where(Timeline.package_id == draft.id)
    )).scalar_one()
    assert new_p.is_brand_locked is True


@requires_docker
@pytest.mark.asyncio
async def test_clone_carries_relations_and_role_mapping(db):
    """Relations are copied; the new Practice rows point at the new
    Relation via relation_id + relation_role."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl = await _add_timeline(db, pkg)
    p_a = await _add_practice(db, tl, common_name="cn:a")
    p_b = await _add_practice(db, tl, common_name="cn:b")
    await db.commit()
    rel = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p_a.id, p_b.id]]],
        ),
        db=db, current_user=user,
    )
    pkg = await publish_global_package(pkg_id=pkg.id, db=db, current_user=user)

    draft = await clone_global_to_draft(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    new_rels = (await db.execute(
        select(Relation).join(Timeline).where(Timeline.package_id == draft.id)
    )).scalars().all()
    assert len(new_rels) == 1
    new_rel_id = new_rels[0].id
    new_ps = (await db.execute(
        select(Practice).join(Timeline).where(Timeline.package_id == draft.id)
    )).scalars().all()
    assert len(new_ps) == 2
    for np in new_ps:
        assert np.relation_id == new_rel_id
        assert np.relation_role and np.relation_role.startswith("PART_1__OPT_1__POS_")


@requires_docker
@pytest.mark.asyncio
async def test_clone_carries_conditional_questions_and_bindings(db):
    """CQs and their YES/NO PracticeConditional + RelationConditional
    bindings survive the clone with re-mapped ids."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl = await _add_timeline(db, pkg)
    p_yes = await _add_practice(db, tl, common_name="cn:yes")
    p_a = await _add_practice(db, tl, common_name="cn:a")
    p_b = await _add_practice(db, tl, common_name="cn:b")
    await db.commit()
    rel = await create_global_relation(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=RelationCreate(
            relation_type=RelationType.AND, parts=[[[p_a.id, p_b.id]]],
        ),
        db=db, current_user=user,
    )
    q = await create_global_conditional_question(
        pkg_id=pkg.id, timeline_id=tl.id,
        request=ConditionalQuestionCreate(question_text="Rained?"),
        db=db, current_user=user,
    )
    await link_global_practice_conditional(
        practice_id=p_yes.id,
        request=PracticeConditionalCreate(
            practice_id=p_yes.id, question_id=q.id, answer=ConditionalAnswer.YES,
        ),
        db=db, current_user=user,
    )
    await link_global_relation_conditional(
        relation_id=rel["id"],
        request=PracticeConditionalCreate(
            practice_id="ignored", question_id=q.id, answer=ConditionalAnswer.NO,
        ),
        db=db, current_user=user,
    )
    pkg = await publish_global_package(pkg_id=pkg.id, db=db, current_user=user)

    draft = await clone_global_to_draft(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    new_cq = (await db.execute(
        select(ConditionalQuestion).join(Timeline)
        .where(Timeline.package_id == draft.id)
    )).scalar_one()
    assert new_cq.id != q.id
    assert new_cq.question_text == "Rained?"
    new_pcs = (await db.execute(
        select(PracticeConditional).where(
            PracticeConditional.question_id == new_cq.id
        )
    )).scalars().all()
    new_rcs = (await db.execute(
        select(RelationConditional).where(
            RelationConditional.question_id == new_cq.id
        )
    )).scalars().all()
    assert len(new_pcs) == 1
    assert len(new_rcs) == 1
    assert new_pcs[0].answer == ConditionalAnswer.YES
    assert new_rcs[0].answer == ConditionalAnswer.NO


@requires_docker
@pytest.mark.asyncio
async def test_clone_flips_existing_draft_to_inactive(db):
    """Single-DRAFT invariant on the Global side: an existing DRAFT
    in the same lineage gets flipped to INACTIVE on a fresh clone."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl = await _add_timeline(db, pkg)
    await _add_practice(db, tl)
    await db.commit()
    pkg = await publish_global_package(pkg_id=pkg.id, db=db, current_user=user)

    draft1 = await clone_global_to_draft(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert draft1.status == PackageStatus.DRAFT

    # Second clone — first draft must flip to INACTIVE.
    draft2 = await clone_global_to_draft(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    refreshed = (await db.execute(
        select(Package).where(Package.id == draft1.id)
    )).scalar_one()
    assert refreshed.status == PackageStatus.INACTIVE
    assert draft2.status == PackageStatus.DRAFT


@requires_docker
@pytest.mark.asyncio
async def test_clone_refuses_when_source_is_draft(db):
    """The CM doesn't clone a DRAFT — that one's already editable."""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    await _add_timeline(db, pkg)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await clone_global_to_draft(
            pkg_id=pkg.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "clone_source_is_draft"


@requires_docker
@pytest.mark.asyncio
async def test_clone_from_inactive_historical_row(db):
    """The CM picks a previous (INACTIVE) version and clones it.
    Per the CM model: 'select a previous version and make its data
    editable.'"""
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db)
    tl = await _add_timeline(db, pkg)
    await _add_practice(db, tl)
    await db.commit()
    pkg = await publish_global_package(pkg_id=pkg.id, db=db, current_user=user)
    # Make the now-ACTIVE row INACTIVE manually (simulating after the
    # CM has published a v2 that demoted v1).
    pkg.status = PackageStatus.INACTIVE
    await db.commit()

    draft = await clone_global_to_draft(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert draft.status == PackageStatus.DRAFT
    assert draft.source_version_id == pkg.id


@requires_docker
@pytest.mark.asyncio
async def test_lineage_endpoint_returns_all_rows(db):
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db, name="Lin")
    tl = await _add_timeline(db, pkg)
    await _add_practice(db, tl)
    await db.commit()
    pkg_active = await publish_global_package(pkg_id=pkg.id, db=db, current_user=user)
    draft = await clone_global_to_draft(
        pkg_id=pkg_active.id, db=db, current_user=user,
    )
    rows = await get_global_package_lineage(
        pkg_id=draft.id, db=db, current_user=user,
    )
    ids = [r["id"] for r in rows]
    assert pkg_active.id in ids
    assert draft.id in ids
    # DRAFT sits first per sort_key.
    assert rows[0]["status"] == "DRAFT"
    assert rows[0]["id"] == draft.id
    assert next(r for r in rows if r["id"] == draft.id)["is_current"] is True
