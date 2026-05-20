"""BL-13 audit — DB-backed integration tests for the publish flow.

Pure-function coverage of the versioning service lives in
`tests/test_bl13.py` (11 tests). This file drives the FastAPI route
handlers directly with seeded rows in the testcontainer DB, to verify
the off-by-one fix and the sibling-deactivation cascade behave
end-to-end. The headline test is the first-publish-equals-v=1 fix —
pre-audit a brand-new package landed on v=2 because the live route
unconditionally did `version = version + 1`.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PGRecommendation,
)
from app.modules.advisory.router import (
    publish_client_pg, publish_package,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_pg_recommendation, make_user,
)


# ── First publish gives v=1 ───────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_first_publish_of_a_draft_package_lands_at_version_1(db):
    """The headline fix. Pre-audit, a brand-new DRAFT package's first
    publish bumped to v=2 because the live route unconditionally did
    `version + 1`. Post-fix it stays at v=1."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pkg = await make_package(db, client, name="Tomato Pack 2026")
    pkg.status = PackageStatus.DRAFT
    pkg.version = 1
    pkg.published_at = None
    await db.commit()

    out = await publish_package(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=sa,
    )
    assert out.status == PackageStatus.ACTIVE
    assert out.version == 1
    assert out.published_at is not None


@requires_docker
@pytest.mark.asyncio
async def test_second_publish_increments_to_version_2(db):
    """In-place edit republish: a CA edits a live package and
    re-publishes; version goes from 1 to 2."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pkg = await make_package(db, client, name="Tomato Pack 2026")
    await db.commit()

    # First publish
    await publish_package(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=sa,
    )
    # Second publish on the same row
    out = await publish_package(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=sa,
    )
    assert out.version == 2


# ── Spec rule: INACTIVE republish creates new number ──────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_inactive_republish_creates_new_number_does_not_restore(db):
    """Spec: 'INACTIVE version can be republished — creates new
    version number, does not restore old number.' A row that climbed
    to v=3, went INACTIVE due to a sibling publish, then is being
    republished should land at v=4 — never reverting to v=3 or v=1."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pkg = await make_package(db, client, name="Tomato Pack")
    pkg.status = PackageStatus.INACTIVE
    pkg.version = 3
    # Simulate the row had been published before — ensures the "first
    # publish" branch doesn't fire.
    from datetime import datetime, timezone, timedelta
    pkg.published_at = datetime.now(timezone.utc) - timedelta(days=10)
    await db.commit()

    out = await publish_package(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=sa,
    )
    assert out.status == PackageStatus.ACTIVE
    assert out.version == 4


# ── Multi-PoP under the same (client, crop) — sibling NOT demoted ────────────

@requires_docker
@pytest.mark.asyncio
async def test_publishing_a_sibling_pop_does_not_demote_other_pops(db):
    """Multi-PoP rule (corrected 2026-05-20): the partial unique
    index `uq_package_client_crop_name_active` enforces "at most
    one ACTIVE per (client, crop, NAME)" — different-name rows are
    different PoPs (e.g. Tomato-Drip vs Tomato-Flood) and stay
    co-ACTIVE. §4.2 PV-uniqueness handles the case where two PoPs
    accidentally share a district + identical fingerprint.

    Pre-fix the publish handler over-demoted: publishing any
    sibling under the same (client, crop) flipped every other
    ACTIVE PoP to INACTIVE and migrated their subscribers, wiping
    out the entire Multi-PoP model."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pkg_drip = await make_package(db, client, name="Tomato Drip")
    pkg_flood = await make_package(db, client, name="Tomato Flood")
    await db.commit()

    # Activate Drip first.
    await publish_package(
        client_id=client.id, package_id=pkg_drip.id,
        db=db, current_user=sa,
    )
    # Activate Flood — Drip must STAY ACTIVE (different name,
    # same crop = sibling PoP, not a new version of Drip).
    await publish_package(
        client_id=client.id, package_id=pkg_flood.id,
        db=db, current_user=sa,
    )

    refreshed_drip = (await db.execute(
        select(Package).where(Package.id == pkg_drip.id)
    )).scalar_one()
    refreshed_flood = (await db.execute(
        select(Package).where(Package.id == pkg_flood.id)
    )).scalar_one()
    assert refreshed_drip.status == PackageStatus.ACTIVE
    assert refreshed_flood.status == PackageStatus.ACTIVE


@requires_docker
@pytest.mark.asyncio
async def test_publishing_a_new_version_of_same_pop_demotes_prior_version(db):
    """Lineage demotion still applies (BL-13): publishing a second
    Package row with the SAME name under the same (client, crop)
    is a new version of that PoP — the prior ACTIVE version
    INACTIVATES and subscribers migrate."""
    from app.modules.advisory.models import (
        PackageAuthor, PackageLocation, PackageType,
    )

    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pkg_v1 = await make_package(db, client, name="Tomato Drip")
    await db.commit()
    await publish_package(
        client_id=client.id, package_id=pkg_v1.id,
        db=db, current_user=sa,
    )
    # v2 of the same PoP — seeded directly as DRAFT. The factory
    # defaults to ACTIVE which would trip the partial unique
    # `(client, crop, name) WHERE status='ACTIVE'` against the
    # already-published v1. Mirror the publish-gate seed (one
    # location, one author) so the readiness check passes.
    pkg_v2 = Package(
        client_id=client.id,
        crop_cosh_id=pkg_v1.crop_cosh_id,
        name="Tomato Drip",
        package_type=PackageType.ANNUAL,
        duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.DRAFT,
    )
    db.add(pkg_v2)
    await db.flush()
    db.add(PackageLocation(
        package_id=pkg_v2.id, state_cosh_id="S2", district_cosh_id="D2",
    ))
    db.add(PackageAuthor(package_id=pkg_v2.id, user_id=sa.id))
    await db.commit()
    await publish_package(
        client_id=client.id, package_id=pkg_v2.id,
        db=db, current_user=sa,
    )

    r_v1 = (await db.execute(select(Package).where(Package.id == pkg_v1.id))).scalar_one()
    r_v2 = (await db.execute(select(Package).where(Package.id == pkg_v2.id))).scalar_one()
    assert r_v1.status == PackageStatus.INACTIVE
    assert r_v2.status == PackageStatus.ACTIVE


# Note on ILLEGAL_PUBLISH_SOURCE coverage:
# The pure-function service test in tests/test_bl13.py already pins
# the unknown-status rejection at the validator layer. We do NOT add
# an integration test for it here because Postgres rejects writes of
# unknown PackageStatus values at the DB level (SAEnum constraint),
# so a corrupted row simply cannot exist for Package. The
# validate_publish_transition guard remains as defence-in-depth for
# future enum additions and for the PG/SP entities (which use a free
# String column for status, so they CAN carry unrecognised values
# at the DB level — covered by the pure-function tests).


# ── PG recommendation: first publish via DRAFT → ACTIVE keeps v=1 ────────────

@requires_docker
@pytest.mark.asyncio
async def test_pg_first_publish_lands_at_version_1(db):
    """PGRecommendation has no published_at column, so the service
    uses status=='DRAFT' as the first-publish signal. Verify the same
    off-by-one fix applies here."""
    from tests.factories import make_pg_timeline

    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pg = await make_pg_recommendation(db, problem_group_cosh_id="pg:leaf-blight")
    pg.client_id = client.id
    pg.status = "DRAFT"
    pg.version = 1
    # CHA hub Round 4: publish gate now requires ≥1 timeline.
    await make_pg_timeline(db, pg)
    await db.commit()

    out = await publish_client_pg(
        client_id=client.id, pg_id=pg.id,
        db=db, current_user=sa,
    )
    assert out.status == "ACTIVE"
    assert out.version == 1
