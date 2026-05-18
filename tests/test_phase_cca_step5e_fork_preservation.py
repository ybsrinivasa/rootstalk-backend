"""CCA Step 5 / Batch 5E — Global→Local field-preservation tests
(renamed from fork-preservation in Batch 39N-a,2026-05-16: fork
became push-as-authoring).

Three deep-copy paths exist for advisory content:
  • POST /client/{id}/packages/{pkg_id}/push            — push_global_package
  • POST /client/{id}/packages/{pkg_id}/timelines/import — import_timeline
  • POST /client/{id}/pg-recommendations/import/{global_pg_id} — import_global_pg

Each must preserve every Practice attribute the SE has set on the
source. In particular:
  - `frequency_days` (added Batch 4C-i.D, 2026-05-07) — without this,
    fertigation L2s lose their interval semantics on import.
  - `common_name_cosh_id` (denormalized from COMMON_NAME element) —
    without this, relation-validation's duplicate-detection breaks
    on the imported copy.

These tests pin the preservation contract so future Practice-level
fields aren't silently dropped on push/import.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
Element,PackageStatus,PackageType,Practice,PGRecommendation,
Timeline,PracticeL0,TimelineFromType,
)
from app.modules.advisory.router import (
import_global_pg,import_timeline,push_global_package,
)
from app.modules.advisory.schemas import PackagePushRequest
from tests.conftest import requires_docker
from tests.factories import (
make_client,make_cm_assignment,make_crop_reference,make_package,
make_push_request_body,make_timeline,make_user,
)


# ── push_global_package ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_package_preserves_practice_fields(db):
    """Pushing a global Package into a client must copy
    common_name_cosh_id and frequency_days onto each Practice."""
    user = await make_user(db, name="GlobalSE")

    # Global package: client_id=NULL.
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")
    global_pkg = await make_package(
db,await make_client(db,full_name="placeholder for FK"),
        crop_cosh_id="crop:paddy",
    )
    global_pkg.client_id = None
    await db.flush()

    tl = await make_timeline(db, global_pkg, name="GTL")
    src_practice = Practice(
timeline_id=tl.id,
l0_type=PracticeL0.INPUT,
l1_type="FERTILIZER",
l2_type="CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
display_order=1,
is_special_input=False,
common_name_cosh_id="cn:urea",
frequency_days=7,
)
    db.add(src_practice)
    await db.flush()
    await db.commit()

    # Client pushes it.
    client = await make_client(db, full_name="PushingClient")
    await make_cm_assignment(db, user=user, client=client)
    await db.commit()

    body = await make_push_request_body(db, client=client, src=global_pkg)
    await db.commit()
    pushed = await push_global_package(
client_id=client.id,pkg_id=global_pkg.id,
request=PackagePushRequest(**body),
        db=db, current_user=user,
    )

    pushed_tls = (await db.execute(
select(Timeline).where(Timeline.package_id == pushed.id)
    )).scalars().all()
    assert len(pushed_tls) == 1
    pushed_practices = (await db.execute(
select(Practice).where(Practice.timeline_id == pushed_tls[0].id)
    )).scalars().all()
    assert len(pushed_practices) == 1
    fp = pushed_practices[0]
    assert fp.common_name_cosh_id == "cn:urea"
    assert fp.frequency_days == 7
    assert fp.l2_type == "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS"
    assert fp.is_special_input is False


# ── import_timeline ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_import_timeline_preserves_practice_fields(db):
    """Cross-package Timeline import (same client, different package)
    must copy common_name_cosh_id and frequency_days."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")

    src_pkg = await make_package(db, client, name="Source", crop_cosh_id="crop:paddy")
    target_pkg = await make_package(db, client, name="Target", crop_cosh_id="crop:paddy")
    src_tl = await make_timeline(db, src_pkg, name="SourceTL")

    src_practice = Practice(
timeline_id=src_tl.id,
l0_type=PracticeL0.INPUT,
l1_type="PESTICIDE",
l2_type="CHEMICAL_PESTICIDES",
display_order=1,
is_special_input=False,
common_name_cosh_id="cn:imida",
frequency_days=None,# not all practices are frequency-based
)
    db.add(src_practice)
    await db.flush()
    await db.commit()

    new_tl = await import_timeline(
client_id=client.id,package_id=target_pkg.id,
data={
"source_timeline_id": src_tl.id,
"new_name": "ImportedTL",
},
db=db,current_user=user,
)

    imported_practices = (await db.execute(
select(Practice).where(Practice.timeline_id == new_tl.id)
    )).scalars().all()
    assert len(imported_practices) == 1
    ip = imported_practices[0]
    assert ip.common_name_cosh_id == "cn:imida"
    assert ip.frequency_days is None
    assert ip.l2_type == "CHEMICAL_PESTICIDES"


@requires_docker
@pytest.mark.asyncio
async def test_import_timeline_preserves_frequency_when_set(db):
    """Same import but with a fertigation practice that does have
    frequency_days set."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")

    src_pkg = await make_package(db, client, name="Src", crop_cosh_id="crop:paddy")
    target_pkg = await make_package(db, client, name="Tgt", crop_cosh_id="crop:paddy")
    src_tl = await make_timeline(db, src_pkg, name="SourceTL2")

    db.add(Practice(
timeline_id=src_tl.id,
l0_type=PracticeL0.INPUT,
l1_type="FERTILIZER",
l2_type="CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
display_order=1,
is_special_input=False,
common_name_cosh_id="cn:urea",
frequency_days=14,
))
    await db.flush()
    await db.commit()

    new_tl = await import_timeline(
client_id=client.id,package_id=target_pkg.id,
data={"source_timeline_id": src_tl.id,"new_name": "ImpFert"},
db=db,current_user=user,
)

    ip = (await db.execute(
select(Practice).where(Practice.timeline_id == new_tl.id)
    )).scalars().one()
    assert ip.frequency_days == 14
    assert ip.common_name_cosh_id == "cn:urea"


# ── import_global_pg ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_import_global_pg_preserves_frequency_days(db):
    """Importing a global PG recommendation into a client must copy
    frequency_days onto each Practice. Practice doesn't carry
    common_name_cosh_id, so only frequency_days is in scope here."""
    user = await make_user(db, name="GlobalSE")

    global_pg = PGRecommendation(
problem_group_cosh_id="pg:fungal",
client_id=None,
area_or_plant="AREA_WISE",
status="ACTIVE",# publish gate
)
    db.add(global_pg)
    await db.flush()

    pg_tl = Timeline(
pg_recommendation_id=global_pg.id,name="PGTL",
from_type="DAYS_AFTER_DETECTION",
from_value=0,to_value=14,
)
    db.add(pg_tl)
    await db.flush()

    db.add(Practice(
timeline_id=pg_tl.id,
l0_type="INPUT",
l1_type="FERTILIZER",
l2_type="CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
display_order=1,
is_special_input=False,
frequency_days=5,
))
    await db.flush()
    await db.commit()

    client = await make_client(db, name="ImportingClient")
    await make_cm_assignment(db, user=user, client=client)
    await db.commit()

    imported = await import_global_pg(
client_id=client.id,global_pg_id=global_pg.id,
db=db,current_user=user,
)

    imported_tls = (await db.execute(
select(Timeline).where(Timeline.pg_recommendation_id == imported.id)
    )).scalars().all()
    assert len(imported_tls) == 1
    imported_practices = (await db.execute(
select(Practice).where(Practice.timeline_id == imported_tls[0].id)
    )).scalars().all()
    assert len(imported_practices) == 1
    assert imported_practices[0].frequency_days == 5
    assert imported_practices[0].l2_type == "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS"


@requires_docker
@pytest.mark.asyncio
async def test_import_global_pg_with_existing_draft_requires_overwrite(db):
    """Batch T (2026-05-18) — re-import 409s when a DRAFT is already
    in the lineage. Passing overwrite=true wipes the existing DRAFT's
    content and copies the import in (same row, no demotion). Only
    Publish creates new rows now."""
    from fastapi import HTTPException
    from sqlalchemy import select

    user = await make_user(db, name="GlobalSE")
    global_pg = PGRecommendation(
        problem_group_cosh_id="pg:fungal",
        client_id=None,
        area_or_plant="AREA_WISE",
        status="ACTIVE",  # publish gate
    )
    db.add(global_pg)
    await db.flush()
    await db.commit()

    client = await make_client(db, name="Importer")
    await make_cm_assignment(db, user=user, client=client)
    await db.commit()

    d1 = await import_global_pg(
        client_id=client.id, global_pg_id=global_pg.id,
        db=db, current_user=user,
    )
    assert d1.status == "DRAFT"

    with pytest.raises(HTTPException) as exc:
        await import_global_pg(
            client_id=client.id, global_pg_id=global_pg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "draft_exists_confirm_overwrite"

    # overwrite=true → same row, content replaced.
    d1_again = await import_global_pg(
        client_id=client.id, global_pg_id=global_pg.id, overwrite=True,
        db=db, current_user=user,
    )
    assert d1_again.id == d1.id
    assert d1_again.status == "DRAFT"
    # Only one row in the lineage.
    rows = (await db.execute(
        select(PGRecommendation).where(
            PGRecommendation.client_id == client.id,
            PGRecommendation.problem_group_cosh_id == "pg:fungal",
        )
    )).scalars().all()
    assert len(rows) == 1
