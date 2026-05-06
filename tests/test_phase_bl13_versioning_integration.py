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


# ── Sibling deactivation: one ACTIVE per (client, crop) ───────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_publishing_a_sibling_package_deactivates_the_previous_active(db):
    """Spec: EXACTLY ONE ACTIVE version per (client, crop). Publishing
    a second Package row with the same crop INACTIVATES the previous
    one. The unique-on-(client, crop, name) schema permits multiple
    rows; only one stays ACTIVE."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pkg_old = await make_package(db, client, name="Tomato Pack 2025")
    pkg_new = await make_package(db, client, name="Tomato Pack 2026")
    await db.commit()

    # Activate the old one first.
    await publish_package(
        client_id=client.id, package_id=pkg_old.id,
        db=db, current_user=sa,
    )
    # Publishing the new one should flip the old one to INACTIVE.
    await publish_package(
        client_id=client.id, package_id=pkg_new.id,
        db=db, current_user=sa,
    )

    refreshed_old = (await db.execute(
        select(Package).where(Package.id == pkg_old.id)
    )).scalar_one()
    refreshed_new = (await db.execute(
        select(Package).where(Package.id == pkg_new.id)
    )).scalar_one()
    assert refreshed_old.status == PackageStatus.INACTIVE
    assert refreshed_new.status == PackageStatus.ACTIVE


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
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    pg = await make_pg_recommendation(db, problem_group_cosh_id="pg:leaf-blight")
    pg.client_id = client.id
    pg.status = "DRAFT"
    pg.version = 1
    await db.commit()

    out = await publish_client_pg(
        client_id=client.id, pg_id=pg.id,
        db=db, current_user=sa,
    )
    assert out.status == "ACTIVE"
    assert out.version == 1
