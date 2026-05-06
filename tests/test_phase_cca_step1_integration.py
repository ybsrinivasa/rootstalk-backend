"""CCA Step 1 — DB-backed integration tests for crop conveyor-belt
soft-removal and re-add cascade.

Pure-function coverage of the cascade/restore service lives in
`tests/test_crop_lifecycle.py`. This file drives the
add_crop / remove_crop / list_crops route handlers directly against
the testcontainer DB to verify the soft-removal flag, PoP cascade,
and revive-on-re-add behaviour end-to-end.

Spec: CCA Step 1 — CA can unilaterally remove a crop. All ACTIVE
PoPs under that (client, crop) become INACTIVE (read-only, blocked
from new subscriptions). Existing farmer subscriptions on those
PoPs continue unabated. CA re-add restores those PoPs to ACTIVE.
DRAFT and independently-INACTIVE PoPs are left alone in both
directions.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import Package, PackageStatus, PackageType
from app.modules.clients.models import ClientCrop
from app.modules.clients.router import add_crop, list_crops, remove_crop
from app.modules.clients.schemas import CropCreate
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, SubscriptionType,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


async def _make_package(
    db, *, client, crop_cosh_id: str, name: str,
    status: PackageStatus = PackageStatus.ACTIVE,
) -> Package:
    p = Package(
        client_id=client.id, crop_cosh_id=crop_cosh_id, name=name,
        package_type=PackageType.ANNUAL, duration_days=120, status=status,
    )
    db.add(p)
    await db.flush()
    return p


# ── add / list ─────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_add_crop_creates_row(db):
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await db.commit()

    out = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    assert out.crop_cosh_id == "crop:paddy"
    assert out.removed_at is None


@requires_docker
@pytest.mark.asyncio
async def test_add_duplicate_active_crop_409s(db):
    """An active row already exists — re-adding without removing
    first is rejected. Prevents accidental double-rows on the belt."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await db.commit()

    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as excinfo:
        await add_crop(
            client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
            db=db, current_user=user,
        )
    assert excinfo.value.status_code == 409


@requires_docker
@pytest.mark.asyncio
async def test_list_crops_filters_removed(db):
    """Only on-the-belt crops appear in the SE/CM CCA list."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await db.commit()

    crop_a = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:tomato"),
        db=db, current_user=user,
    )
    await remove_crop(
        client_id=client.id, crop_id=crop_a.id, db=db, current_user=user,
    )

    listed = await list_crops(client_id=client.id, db=db, current_user=user)
    cosh_ids = [c.crop_cosh_id for c in listed]
    assert "crop:tomato" in cosh_ids
    assert "crop:paddy" not in cosh_ids


# ── remove: soft + cascade ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_remove_crop_soft_deletes_row(db):
    """Row stays in the table with `removed_at` stamped — needed so
    a future re-add can find and revive it."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await db.commit()

    crop = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await remove_crop(client_id=client.id, crop_id=crop.id, db=db, current_user=user)

    refreshed = (await db.execute(
        select(ClientCrop).where(ClientCrop.id == crop.id)
    )).scalar_one()
    assert refreshed.removed_at is not None


@requires_docker
@pytest.mark.asyncio
async def test_remove_crop_cascade_inactivates_active_pop(db):
    """The headline rule: removing the crop flips ACTIVE PoPs to
    INACTIVE with the cascade timestamp set."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    crop_row = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    pkg = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy", name="PoP-1",
        status=PackageStatus.ACTIVE,
    )
    await db.commit()

    await remove_crop(client_id=client.id, crop_id=crop_row.id, db=db, current_user=user)

    refreshed = (await db.execute(
        select(Package).where(Package.id == pkg.id)
    )).scalar_one()
    assert refreshed.status == PackageStatus.INACTIVE
    assert refreshed.cascade_inactivated_at is not None


@requires_docker
@pytest.mark.asyncio
async def test_remove_crop_leaves_draft_alone(db):
    """DRAFT PoPs are not subscribable, so cascading them gains
    nothing and would silently flip them out of DRAFT on re-add.
    Spec says DRAFTs stay DRAFT in both directions."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    crop_row = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    draft = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy", name="PoP-Draft",
        status=PackageStatus.DRAFT,
    )
    await db.commit()

    await remove_crop(client_id=client.id, crop_id=crop_row.id, db=db, current_user=user)

    refreshed = (await db.execute(
        select(Package).where(Package.id == draft.id)
    )).scalar_one()
    assert refreshed.status == PackageStatus.DRAFT
    assert refreshed.cascade_inactivated_at is None


@requires_docker
@pytest.mark.asyncio
async def test_remove_crop_does_not_claim_independent_inactive(db):
    """A PoP that was already INACTIVE (e.g. superseded by a new
    published version) keeps its state and gets NO cascade stamp.
    Important — otherwise re-add would silently republish it."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    crop_row = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    superseded = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy", name="PoP-Old",
        status=PackageStatus.INACTIVE,
    )
    await db.commit()

    await remove_crop(client_id=client.id, crop_id=crop_row.id, db=db, current_user=user)

    refreshed = (await db.execute(
        select(Package).where(Package.id == superseded.id)
    )).scalar_one()
    assert refreshed.status == PackageStatus.INACTIVE
    assert refreshed.cascade_inactivated_at is None


@requires_docker
@pytest.mark.asyncio
async def test_remove_crop_preserves_existing_subscription(db):
    """Spec rule: 'all farmers subscribed to it shall continue
    unabated'. Inactivating the Package does not touch any
    Subscription rows."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    farmer = await make_user(db, name="Farmer Sub")
    crop_row = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    pkg = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy", name="PoP-Sub",
        status=PackageStatus.ACTIVE,
    )
    sub = Subscription(
        farmer_user_id=farmer.id, client_id=client.id, package_id=pkg.id,
        subscription_type=SubscriptionType.SELF,
        status=SubscriptionStatus.ACTIVE,
    )
    db.add(sub)
    await db.commit()

    await remove_crop(client_id=client.id, crop_id=crop_row.id, db=db, current_user=user)

    refreshed_sub = (await db.execute(
        select(Subscription).where(Subscription.id == sub.id)
    )).scalar_one()
    assert refreshed_sub.status == SubscriptionStatus.ACTIVE


@requires_docker
@pytest.mark.asyncio
async def test_remove_already_removed_404s(db):
    """A crop already in the soft-removed state shouldn't be
    re-removable — the next legitimate operation on it is re-add."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    crop_row = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await remove_crop(client_id=client.id, crop_id=crop_row.id, db=db, current_user=user)

    with pytest.raises(HTTPException) as excinfo:
        await remove_crop(
            client_id=client.id, crop_id=crop_row.id, db=db, current_user=user,
        )
    assert excinfo.value.status_code == 404


# ── re-add: revive ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_re_add_revives_cascade_inactivated_pop(db):
    """The whole point of soft-removal: CA changes their mind, and
    everything that was on the conveyor belt is back to ACTIVE
    without the experts having to rebuild anything."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    crop_row = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    pkg = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy", name="PoP-Revive",
        status=PackageStatus.ACTIVE,
    )
    await db.commit()
    await remove_crop(client_id=client.id, crop_id=crop_row.id, db=db, current_user=user)

    out = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    assert out.id == crop_row.id  # same row, revived
    assert out.removed_at is None

    refreshed = (await db.execute(
        select(Package).where(Package.id == pkg.id)
    )).scalar_one()
    assert refreshed.status == PackageStatus.ACTIVE
    assert refreshed.cascade_inactivated_at is None


@requires_docker
@pytest.mark.asyncio
async def test_re_add_does_not_revive_independent_inactive(db):
    """Re-add must not silently republish the superseded version of
    a PoP. Only PoPs we ourselves cascade-inactivated come back."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    crop_row = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    superseded = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy", name="PoP-Old",
        status=PackageStatus.INACTIVE,
    )
    await db.commit()
    await remove_crop(client_id=client.id, crop_id=crop_row.id, db=db, current_user=user)

    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )

    refreshed = (await db.execute(
        select(Package).where(Package.id == superseded.id)
    )).scalar_one()
    assert refreshed.status == PackageStatus.INACTIVE
    assert refreshed.cascade_inactivated_at is None
