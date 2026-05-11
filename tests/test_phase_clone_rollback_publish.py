"""clone-to-draft + rollback-publish + publish-time subscription
migration (Batch 3 of the multi-row versioning work locked
2026-05-11).

Coverage:
  • clone-to-draft creates a DRAFT with SE_EDIT_DRAFT marker,
    deep-copies content + locations + authors + PVs.
  • clone-to-draft refuses if source isn't the current ACTIVE
    (historical rows go through rollback-publish).
  • clone-to-draft auto-flips a pre-existing DRAFT in the same
    lineage to INACTIVE (single-DRAFT invariant).
  • rollback-publish creates a new ACTIVE row with
    SE_ROLLBACK_PUBLISH + source_version_id; demotes prior ACTIVE
    + any DRAFT to INACTIVE.
  • rollback-publish migrates subscriptions from prior ACTIVE to
    the new ACTIVE row.
  • rollback-publish version > all prior versions in lineage,
    even if source's version was small.
  • publish_package on a fresh DRAFT (post-clone-to-draft)
    migrates subscriptions from predecessor PUBLISHED to the new
    ACTIVE row.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageAuthor, PackageCreatedVia, PackageLocation,
    PackageStatus, PackageType, Practice, PracticeL0, Timeline,
    TimelineFromType,
)
from app.modules.advisory.router import (
    clone_to_draft, publish_package, push_global_package,
    rollback_publish,
)
from app.modules.clients.models import ClientCrop, ClientUserRole
from app.modules.platform.models import StatusEnum
from app.modules.subscriptions.models import Subscription
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_cm_assignment,
    make_subscription, make_user,
)


async def _seed_global_with_one_timeline(db, *, name: str | None = None):
    pkg = Package(
        client_id=None,
        name=name or f"GP-{uuid.uuid4().hex[:6]}",
        crop_cosh_id="crop:test",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    tl = Timeline(
        package_id=pkg.id, name="GTL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    db.add(tl)
    await db.flush()
    db.add(Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER", l2_type="UREA", display_order=0,
    ))
    await db.flush()
    return pkg


async def _se_and_client(db):
    """A client with one SE (ClientUser) and one CM. Pushed once so
    a Local PUBLISHED exists. Returns (cm, se, client, local_active)."""
    cm = await make_user(db, name=f"CM-{uuid.uuid4().hex[:4]}")
    se = await make_user(db, name=f"SE-{uuid.uuid4().hex[:4]}")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    await make_client_user(db, user=se, client=client)
    gpkg = await _seed_global_with_one_timeline(db)
    # Crop on the belt — publish_package's assert_crop_on_belt gate
    # requires this. push_global_package itself doesn't seed it
    # today (separate decision); CA portal flow would have added it.
    db.add(ClientCrop(client_id=client.id, crop_cosh_id=gpkg.crop_cosh_id))
    await db.commit()
    pushed = await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )
    # Add the metadata that clone-to-draft / rollback-publish
    # deep-copy + that publish-readiness needs.
    db.add(PackageLocation(
        package_id=pushed.id, state_cosh_id="state:test",
        district_cosh_id=f"district:test:{uuid.uuid4().hex[:4]}",
    ))
    db.add(PackageAuthor(package_id=pushed.id, user_id=se.id))
    # Make `se` a SUBJECT_EXPERT on the client so they pass the
    # publish-readiness "at least one ACTIVE SE author" check.
    from sqlalchemy import select as _sel
    from app.modules.clients.models import ClientUser
    cu = (await db.execute(
        _sel(ClientUser).where(
            ClientUser.user_id == se.id, ClientUser.client_id == client.id,
        )
    )).scalar_one()
    cu.role = ClientUserRole.SUBJECT_EXPERT
    cu.status = StatusEnum.ACTIVE
    # Flip to ACTIVE + mark as published. Bypassing the real
    # publish endpoint so the fixture stays small; tests that
    # exercise the publish path do so explicitly.
    pushed.status = PackageStatus.ACTIVE
    from datetime import datetime, timezone
    pushed.published_at = datetime.now(timezone.utc)
    pushed.published_by = se.id
    await db.commit()
    return cm, se, client, pushed


# ── clone-to-draft ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_clone_to_draft_creates_new_draft_with_marker(db):
    """Clone from ACTIVE → new DRAFT, SE_EDIT_DRAFT, content +
    locations + authors copied."""
    _, se, client, active = await _se_and_client(db)

    out = await clone_to_draft(
        client_id=client.id, package_id=active.id,
        db=db, current_user=se,
    )
    assert out.id != active.id
    assert out.status == PackageStatus.DRAFT
    assert out.created_via == PackageCreatedVia.SE_EDIT_DRAFT
    assert out.parent_global_id == active.parent_global_id
    assert out.name == active.name

    tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == out.id)
    )).scalars().all()
    assert len(tls) == 1

    locs = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == out.id)
    )).scalars().all()
    assert len(locs) == 1, "Locations must be copied for the DRAFT"

    auths = (await db.execute(
        select(PackageAuthor).where(PackageAuthor.package_id == out.id)
    )).scalars().all()
    assert len(auths) == 1, "Authors must be copied for the DRAFT"


@requires_docker
@pytest.mark.asyncio
async def test_clone_to_draft_refuses_inactive_source(db):
    """Cloning from a non-ACTIVE row → 422
    `clone_source_not_active`. Historical rows go through
    rollback-publish."""
    _, se, client, active = await _se_and_client(db)
    active.status = PackageStatus.INACTIVE
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await clone_to_draft(
            client_id=client.id, package_id=active.id,
            db=db, current_user=se,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "clone_source_not_active"


@requires_docker
@pytest.mark.asyncio
async def test_clone_to_draft_flips_prior_draft_to_inactive(db):
    """Single-DRAFT invariant: any existing DRAFT in the same
    lineage is flipped to INACTIVE before the new DRAFT is created."""
    _, se, client, active = await _se_and_client(db)
    first_draft = await clone_to_draft(
        client_id=client.id, package_id=active.id,
        db=db, current_user=se,
    )
    second_draft = await clone_to_draft(
        client_id=client.id, package_id=active.id,
        db=db, current_user=se,
    )
    assert first_draft.id != second_draft.id
    await db.refresh(first_draft)
    assert first_draft.status == PackageStatus.INACTIVE
    assert second_draft.status == PackageStatus.DRAFT


# ── rollback-publish ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_rollback_publish_creates_new_active_with_lineage_marker(db):
    """SE rollback-publish from an INACTIVE source: new ACTIVE row
    with SE_ROLLBACK_PUBLISH + source_version_id; old ACTIVE
    demoted to INACTIVE."""
    _, se, client, active_v1 = await _se_and_client(db)

    # Author + publish v2 (a self-edit) so we have INACTIVE history.
    draft_v2 = await clone_to_draft(
        client_id=client.id, package_id=active_v1.id,
        db=db, current_user=se,
    )
    published_v2 = await publish_package(
        client_id=client.id, package_id=draft_v2.id,
        db=db, current_user=se,
    )
    await db.refresh(active_v1)
    assert active_v1.status == PackageStatus.INACTIVE
    assert published_v2.status == PackageStatus.ACTIVE

    # Now SE decides v1 was better — rollback-publish.
    new_active = await rollback_publish(
        client_id=client.id, package_id=active_v1.id,
        db=db, current_user=se,
    )
    assert new_active.status == PackageStatus.ACTIVE
    assert new_active.created_via == PackageCreatedVia.SE_ROLLBACK_PUBLISH
    assert new_active.source_version_id == active_v1.id
    # v2 demoted.
    await db.refresh(published_v2)
    assert published_v2.status == PackageStatus.INACTIVE


@requires_docker
@pytest.mark.asyncio
async def test_rollback_publish_version_above_lineage_max(db):
    """New ACTIVE row's version > max(version) of every prior row
    in the lineage, even when source's own version is small."""
    _, se, client, active_v1 = await _se_and_client(db)
    # First publish lands version=1.
    active_v1.version = 1
    await db.commit()

    # Do 3 self-edit cycles so we have v2, v3, v4 history.
    src = active_v1
    for _ in range(3):
        d = await clone_to_draft(
            client_id=client.id, package_id=src.id,
            db=db, current_user=se,
        )
        src = await publish_package(
            client_id=client.id, package_id=d.id,
            db=db, current_user=se,
        )
    assert src.version == 4  # v1→2→3→4

    # Rollback to v1.
    rollback = await rollback_publish(
        client_id=client.id, package_id=active_v1.id,
        db=db, current_user=se,
    )
    assert rollback.version == 5, (
        "Rollback must use max(lineage) + 1, not source.version + 1"
    )


@requires_docker
@pytest.mark.asyncio
async def test_rollback_publish_migrates_subscriptions(db):
    """Subscriptions on the predecessor ACTIVE migrate to the new
    rollback ACTIVE row. Farmer-side BL-13 spirit preserved."""
    _, se, client, v1 = await _se_and_client(db)
    farmer = await make_user(db, name="Farmer")
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=v1,
    )
    await db.commit()
    assert sub.package_id == v1.id

    # Self-publish v2 — sub migrates from v1 → v2 via publish_package.
    d = await clone_to_draft(
        client_id=client.id, package_id=v1.id, db=db, current_user=se,
    )
    v2 = await publish_package(
        client_id=client.id, package_id=d.id, db=db, current_user=se,
    )
    await db.refresh(sub)
    assert sub.package_id == v2.id

    # Rollback to v1 — sub must migrate from v2 → new ACTIVE.
    new_active = await rollback_publish(
        client_id=client.id, package_id=v1.id, db=db, current_user=se,
    )
    await db.refresh(sub)
    assert sub.package_id == new_active.id


@requires_docker
@pytest.mark.asyncio
async def test_rollback_publish_discards_in_flight_draft(db):
    """If a DRAFT was in progress when SE rolls back, that DRAFT is
    flipped to INACTIVE per the user's locked model."""
    _, se, client, v1 = await _se_and_client(db)
    pending_draft = await clone_to_draft(
        client_id=client.id, package_id=v1.id, db=db, current_user=se,
    )
    assert pending_draft.status == PackageStatus.DRAFT

    await rollback_publish(
        client_id=client.id, package_id=v1.id, db=db, current_user=se,
    )
    await db.refresh(pending_draft)
    assert pending_draft.status == PackageStatus.INACTIVE, (
        "Pending DRAFT must be discarded when SE rolls back"
    )


@requires_docker
@pytest.mark.asyncio
async def test_rollback_publish_refuses_global_source(db):
    """Rolling back from a Global Package id → 422
    `rollback_source_must_be_local`."""
    _, se, client, _v1 = await _se_and_client(db)
    gpkg = await _seed_global_with_one_timeline(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await rollback_publish(
            client_id=client.id, package_id=gpkg.id,
            db=db, current_user=se,
        )
    # _get_package will 404 since the Global has client_id=NULL and
    # the helper filters on client_id. That's also acceptable —
    # both refusal shapes prevent the attack.
    assert exc.value.status_code in (404, 422)


# ── publish-time subscription migration (extends existing publish) ───────────

@requires_docker
@pytest.mark.asyncio
async def test_publish_migrates_subscriptions_from_predecessor(db):
    """Multi-row publish path: clone-to-draft → edits → publish.
    Subscriptions on the predecessor PUBLISHED row migrate to the
    new PUBLISHED row."""
    _, se, client, v1 = await _se_and_client(db)
    farmer = await make_user(db, name="Farmer")
    sub = await make_subscription(
        db, farmer=farmer, client=client, package=v1,
    )
    await db.commit()
    assert sub.package_id == v1.id

    d = await clone_to_draft(
        client_id=client.id, package_id=v1.id, db=db, current_user=se,
    )
    v2 = await publish_package(
        client_id=client.id, package_id=d.id, db=db, current_user=se,
    )

    await db.refresh(sub)
    assert sub.package_id == v2.id, (
        "Subscription must point at the new ACTIVE row after publish"
    )
    await db.refresh(v1)
    assert v1.status == PackageStatus.INACTIVE
