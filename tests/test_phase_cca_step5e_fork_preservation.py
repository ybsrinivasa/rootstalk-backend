"""CCA Step 5 / Batch 5E — Global→Local fork field-preservation tests.

Three deep-copy paths exist for advisory content:
  • POST /client/{id}/packages/{pkg_id}/fork           — fork_global_package
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
fields aren't silently dropped on fork/import.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, PackageStatus, PackageType, PGPractice, PGRecommendation,
    PGTimeline, Practice, PracticeL0, Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    fork_global_package, import_global_pg, import_timeline,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_crop_reference, make_package, make_timeline, make_user,
)


# ── fork_global_package ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fork_package_preserves_practice_fields(db):
    """Forking a global Package into a client must copy
    common_name_cosh_id and frequency_days onto each Practice."""
    user = await make_user(db, name="GlobalSE")

    # Global package: client_id=NULL.
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")
    global_pkg = await make_package(
        db, await make_client(db, name="placeholder for FK"),
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

    # Client forks it.
    client = await make_client(db, name="ForkingClient")
    await db.commit()

    forked = await fork_global_package(
        client_id=client.id, pkg_id=global_pkg.id,
        db=db, current_user=user,
    )

    forked_tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == forked.id)
    )).scalars().all()
    assert len(forked_tls) == 1
    forked_practices = (await db.execute(
        select(Practice).where(Practice.timeline_id == forked_tls[0].id)
    )).scalars().all()
    assert len(forked_practices) == 1
    fp = forked_practices[0]
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
        frequency_days=None,  # not all practices are frequency-based
    )
    db.add(src_practice)
    await db.flush()
    await db.commit()

    new_tl = await import_timeline(
        client_id=client.id, package_id=target_pkg.id,
        data={
            "source_timeline_id": src_tl.id,
            "new_name": "ImportedTL",
        },
        db=db, current_user=user,
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
        client_id=client.id, package_id=target_pkg.id,
        data={"source_timeline_id": src_tl.id, "new_name": "ImpFert"},
        db=db, current_user=user,
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
    frequency_days onto each PGPractice. PGPractice doesn't carry
    common_name_cosh_id, so only frequency_days is in scope here."""
    user = await make_user(db, name="GlobalSE")

    global_pg = PGRecommendation(
        problem_group_cosh_id="pg:fungal",
        client_id=None,
        application_type="SPRAY",
    )
    db.add(global_pg)
    await db.flush()

    pg_tl = PGTimeline(
        pg_recommendation_id=global_pg.id, name="PGTL",
        from_type="DAYS_AFTER_DETECTION",
        from_value=0, to_value=14,
    )
    db.add(pg_tl)
    await db.flush()

    db.add(PGPractice(
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
    await db.commit()

    imported = await import_global_pg(
        client_id=client.id, global_pg_id=global_pg.id,
        db=db, current_user=user,
    )

    imported_tls = (await db.execute(
        select(PGTimeline).where(PGTimeline.pg_recommendation_id == imported.id)
    )).scalars().all()
    assert len(imported_tls) == 1
    imported_practices = (await db.execute(
        select(PGPractice).where(PGPractice.timeline_id == imported_tls[0].id)
    )).scalars().all()
    assert len(imported_practices) == 1
    assert imported_practices[0].frequency_days == 5
    assert imported_practices[0].l2_type == "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS"


@requires_docker
@pytest.mark.asyncio
async def test_import_global_pg_idempotent_returns_409_on_duplicate(db):
    """Re-importing the same global PG without ?force=true returns a
    structured 409 — the CA portal renders this as the "this will
    overwrite your local edits" warning. The structured shape was
    introduced in the 2026-05-08 CHA imports work; before that this
    test asserted the legacy free-text "already imported" message."""
    from fastapi import HTTPException

    user = await make_user(db, name="GlobalSE")
    global_pg = PGRecommendation(
        problem_group_cosh_id="pg:fungal",
        client_id=None,
        application_type="SPRAY",
    )
    db.add(global_pg)
    await db.flush()
    await db.commit()

    client = await make_client(db, name="Importer")
    await db.commit()

    # First import succeeds.
    await import_global_pg(
        client_id=client.id, global_pg_id=global_pg.id,
        db=db, current_user=user,
    )

    # Second one without force is rejected with the structured envelope.
    with pytest.raises(HTTPException) as exc:
        await import_global_pg(
            client_id=client.id, global_pg_id=global_pg.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "import_would_overwrite"
