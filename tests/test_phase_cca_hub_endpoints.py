"""CCA hub list endpoints (2026-05-10).

Pins the four endpoints that back the `/cca/{crops,packages,timelines,
practices}` four-screen hub: each one returns a denormalised join of
the row plus its parent context (crop name, package name, status,
counts) so the UI renders a row without follow-up fetches. Filter
chips (?crop_cosh_id=, ?package_id=, ?timeline_id=) are independently
clearable; selecting a parent in one screen pre-applies the chip on
the next.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, Package, PackageStatus, PackageType, Practice, PracticeL0,
    Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    cca_list_crops, cca_list_packages, cca_list_practices,
    cca_list_timelines,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_crop_reference, make_package, make_timeline, make_user,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _seed_two_packages(db):
    """Two crops, three packages between them, with timelines + a few
    practices so the count fields exercise."""
    client = await make_client(db)
    user = await make_user(db, name="SE")
    await make_crop_reference(db, "bn:tomato", name="Tomato", measure="AREA_WISE")
    await make_crop_reference(db, "bn:apple", name="Apple", measure="PLANT_WISE")
    await db.commit()

    pkg_t1 = await make_package(db, client, name="Tomato Kharif", crop_cosh_id="bn:tomato")
    pkg_t2 = await make_package(db, client, name="Tomato Rabi", crop_cosh_id="bn:tomato")
    pkg_a1 = await make_package(db, client, name="Apple All-Season", crop_cosh_id="bn:apple")
    # status default is ACTIVE on the factory; flip one to DRAFT for the breakdown test.
    pkg_t2.status = PackageStatus.DRAFT
    await db.flush()

    tl_t1_w1 = await make_timeline(db, pkg_t1, name="W1", to_value=7)
    tl_t1_w2 = await make_timeline(db, pkg_t1, name="W2", to_value=14)
    tl_a1_w1 = await make_timeline(db, pkg_a1, name="A-W1", to_value=7)

    db.add_all([
        Practice(timeline_id=tl_t1_w1.id, l0_type=PracticeL0.INPUT,
                 l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                 display_order=0),
        Practice(timeline_id=tl_t1_w1.id, l0_type=PracticeL0.INPUT,
                 l1_type="FERTILIZER", l2_type="MANURES",
                 display_order=1),
        Practice(timeline_id=tl_a1_w1.id, l0_type=PracticeL0.INPUT,
                 l1_type="PESTICIDE", l2_type="CHEMICAL_PESTICIDES",
                 display_order=0),
    ])
    await db.commit()
    return client, user, pkg_t1, pkg_t2, pkg_a1, tl_t1_w1, tl_a1_w1


# ── Crops ──────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cca_crops_returns_breakdown(db):
    client, user, *_ = await _seed_two_packages(db)
    out = await cca_list_crops(client_id=client.id, db=db, current_user=user)
    by_id = {c["crop_cosh_id"]: c for c in out}
    # Tomato has 2 packages — 1 ACTIVE, 1 DRAFT.
    assert by_id["bn:tomato"]["package_counts"].get("ACTIVE") == 1
    assert by_id["bn:tomato"]["package_counts"].get("DRAFT") == 1
    # Apple has 1 ACTIVE.
    assert by_id["bn:apple"]["package_counts"].get("ACTIVE") == 1
    # Friendly names came through.
    assert by_id["bn:tomato"]["name_en"] == "Tomato"
    assert by_id["bn:apple"]["name_en"] == "Apple"


@requires_docker
@pytest.mark.asyncio
async def test_cca_crops_excludes_removed_crops(db):
    """Soft-removed crops (CA pulled them from the belt) shouldn't
    appear in the SE's CCA browse — even if packages still exist
    (which would be the cascade-inactivated state)."""
    from datetime import datetime, timezone
    client, user, *_ = await _seed_two_packages(db)
    apple = (await db.execute(
        select(__import__("app.modules.clients.models", fromlist=["ClientCrop"]).ClientCrop)
        .where(
            __import__("app.modules.clients.models", fromlist=["ClientCrop"]).ClientCrop.crop_cosh_id == "bn:apple",
        )
    )).scalar_one()
    apple.removed_at = datetime.now(timezone.utc)
    await db.commit()

    out = await cca_list_crops(client_id=client.id, db=db, current_user=user)
    cosh_ids = {c["crop_cosh_id"] for c in out}
    assert "bn:apple" not in cosh_ids
    assert "bn:tomato" in cosh_ids


# ── Packages ───────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cca_packages_denormalises_crop_name(db):
    client, user, *_ = await _seed_two_packages(db)
    out = await cca_list_packages(client_id=client.id, db=db, current_user=user)
    by_name = {p["name"]: p for p in out}
    assert by_name["Tomato Kharif"]["crop_name_en"] == "Tomato"
    assert by_name["Apple All-Season"]["crop_name_en"] == "Apple"


@requires_docker
@pytest.mark.asyncio
async def test_cca_packages_includes_timeline_and_location_counts(db):
    client, user, pkg_t1, _, pkg_a1, *_ = await _seed_two_packages(db)
    out = await cca_list_packages(client_id=client.id, db=db, current_user=user)
    by_id = {p["id"]: p for p in out}
    assert by_id[pkg_t1.id]["timeline_count"] == 2
    assert by_id[pkg_a1.id]["timeline_count"] == 1
    # make_package factory seeds a single PackageLocation per package.
    assert by_id[pkg_t1.id]["location_count"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_cca_packages_filters_by_crop(db):
    client, user, *_ = await _seed_two_packages(db)
    out = await cca_list_packages(
        client_id=client.id, crop_cosh_id="bn:tomato",
        db=db, current_user=user,
    )
    assert {p["name"] for p in out} == {"Tomato Kharif", "Tomato Rabi"}


@requires_docker
@pytest.mark.asyncio
async def test_cca_packages_filters_by_status(db):
    client, user, *_ = await _seed_two_packages(db)
    out = await cca_list_packages(
        client_id=client.id, status="DRAFT", db=db, current_user=user,
    )
    assert {p["name"] for p in out} == {"Tomato Rabi"}


# ── Timelines ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cca_timelines_carries_package_and_crop_context(db):
    client, user, _, _, _, tl_t1_w1, tl_a1_w1 = await _seed_two_packages(db)
    out = await cca_list_timelines(client_id=client.id, db=db, current_user=user)
    assert len(out) == 3
    by_id = {t["id"]: t for t in out}
    assert by_id[tl_t1_w1.id]["package_name"] == "Tomato Kharif"
    assert by_id[tl_t1_w1.id]["crop_name_en"] == "Tomato"
    assert by_id[tl_t1_w1.id]["practice_count"] == 2
    assert by_id[tl_a1_w1.id]["crop_name_en"] == "Apple"
    assert by_id[tl_a1_w1.id]["practice_count"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_cca_timelines_filters_chip_by_crop(db):
    client, user, _, _, _, _, tl_a1_w1 = await _seed_two_packages(db)
    out = await cca_list_timelines(
        client_id=client.id, crop_cosh_id="bn:apple",
        db=db, current_user=user,
    )
    assert {t["id"] for t in out} == {tl_a1_w1.id}


@requires_docker
@pytest.mark.asyncio
async def test_cca_timelines_filters_chip_by_package(db):
    client, user, pkg_t1, _, _, tl_t1_w1, _ = await _seed_two_packages(db)
    out = await cca_list_timelines(
        client_id=client.id, package_id=pkg_t1.id,
        db=db, current_user=user,
    )
    # Both W1 and W2 from pkg_t1 — verify by package_id, not raw name
    # since the factory appends a uniquifier.
    assert all(t["package_id"] == pkg_t1.id for t in out)
    assert tl_t1_w1.id in {t["id"] for t in out}
    assert len(out) == 2


# ── Practices ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cca_practices_carries_full_breadcrumb(db):
    client, user, *_ = await _seed_two_packages(db)
    out = await cca_list_practices(client_id=client.id, db=db, current_user=user)
    assert out["total"] == 3
    assert len(out["items"]) == 3
    # Every row has the full breadcrumb.
    for it in out["items"]:
        assert it["timeline_name"]
        assert it["package_name"]
        assert it["crop_cosh_id"]
        assert it["crop_name_en"]


@requires_docker
@pytest.mark.asyncio
async def test_cca_practices_filters_by_l1(db):
    """Cross-cutting filter — 'all PESTICIDE practices across the company'."""
    client, user, *_ = await _seed_two_packages(db)
    out = await cca_list_practices(
        client_id=client.id, l1="PESTICIDE", db=db, current_user=user,
    )
    assert out["total"] == 2
    assert {it["l1_type"] for it in out["items"]} == {"PESTICIDE"}


@requires_docker
@pytest.mark.asyncio
async def test_cca_practices_filters_chip_by_timeline(db):
    client, user, *_, tl_t1_w1, _ = await _seed_two_packages(db)
    out = await cca_list_practices(
        client_id=client.id, timeline_id=tl_t1_w1.id,
        db=db, current_user=user,
    )
    assert out["total"] == 2
    assert all(it["timeline_id"] == tl_t1_w1.id for it in out["items"])


@requires_docker
@pytest.mark.asyncio
async def test_cca_practices_pagination(db):
    """Default limit 100 is enough for a 3-row test; verify the total
    field carries the unpaginated count and limit/offset round-trip."""
    client, user, *_ = await _seed_two_packages(db)
    out = await cca_list_practices(
        client_id=client.id, limit=2, offset=0, db=db, current_user=user,
    )
    assert out["total"] == 3
    assert len(out["items"]) == 2
    assert out["limit"] == 2 and out["offset"] == 0

    out2 = await cca_list_practices(
        client_id=client.id, limit=2, offset=2, db=db, current_user=user,
    )
    assert len(out2["items"]) == 1


@requires_docker
@pytest.mark.asyncio
async def test_cca_practices_summary_carries_brand_when_present(db):
    """When a Practice has a BRAND_NAME element, the summary includes
    its cosh_ref so the UI can render brand-level filtering /
    auditing without a follow-up fetch."""
    client, user, *_ = await _seed_two_packages(db)
    practice = (await db.execute(
        select(Practice).where(Practice.l1_type == "PESTICIDE").limit(1)
    )).scalar_one()
    db.add(Element(
        practice_id=practice.id, element_type="BRAND_NAME",
        cosh_ref="brand:confidor", value=None,
    ))
    db.add(Element(
        practice_id=practice.id, element_type="DOSAGE",
        value="0.5", unit_cosh_id="ml/L",
    ))
    await db.commit()

    out = await cca_list_practices(client_id=client.id, db=db, current_user=user)
    rich = next(it for it in out["items"] if it["id"] == practice.id)
    assert rich["brand_cosh_id"] == "brand:confidor"
    assert rich["dosage_summary"] == "0.5 ml/L"
