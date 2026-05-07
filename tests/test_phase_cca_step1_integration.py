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
from app.modules.sync.models import CoshReferenceCache, CropMeasure
from tests.conftest import requires_docker
from tests.factories import make_client, make_crop_reference, make_user


async def _seed_paddy(db):
    await make_crop_reference(db, "crop:paddy", name="Paddy",
                              scientific_name="Oryza sativa", measure="AREA_WISE")


async def _seed_tomato(db):
    await make_crop_reference(db, "crop:tomato", name="Tomato",
                              scientific_name="Solanum lycopersicum",
                              measure="AREA_WISE")


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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
    await _seed_tomato(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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
    await _seed_paddy(db)
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


# ── Batch 1B: snapshot fields ────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_add_crop_populates_snapshot_fields(db):
    """Snapshot rule: at CA-add time, crop_name_en /
    crop_scientific_name / crop_area_or_plant are captured from
    Cosh + CropMeasure so the company's CCA configuration is frozen
    against future Cosh-side drift."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await make_crop_reference(
        db, "crop:coconut",
        name="Coconut", scientific_name="Cocos nucifera",
        measure="PLANT_WISE",
    )
    await db.commit()

    out = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:coconut"),
        db=db, current_user=user,
    )
    assert out.crop_name_en == "Coconut"
    assert out.crop_scientific_name == "Cocos nucifera"
    assert out.crop_area_or_plant == "PLANT_WISE"


@requires_docker
@pytest.mark.asyncio
async def test_add_crop_422_when_cosh_entity_missing(db):
    """If SA hasn't synced the crop into the reference cache, the
    CA add fails with a stable error code that the portal can map
    to an SA-escalation message."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await add_crop(
            client_id=client.id,
            request=CropCreate(crop_cosh_id="crop:never_synced"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "crop_not_in_cosh"


@requires_docker
@pytest.mark.asyncio
async def test_add_crop_422_when_measure_missing(db):
    """Cosh entity exists but no CropMeasure row — SA must seed the
    area/plant typing first. Fail closed; never default."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    db.add(CoshReferenceCache(
        cosh_id="crop:no_measure", entity_type="crop", status="active",
        translations={"en": "MysteryCrop"},
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await add_crop(
            client_id=client.id,
            request=CropCreate(crop_cosh_id="crop:no_measure"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "crop_missing_measure"


@requires_docker
@pytest.mark.asyncio
async def test_add_crop_422_when_cosh_inactive(db):
    """Inactive Cosh entity must not be addable — same rule the
    spec has for inactive global PG imports (§ 8.8)."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await make_crop_reference(
        db, "crop:retired", name="Retired", measure="AREA_WISE",
        status="inactive",
    )
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await add_crop(
            client_id=client.id,
            request=CropCreate(crop_cosh_id="crop:retired"),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "crop_inactive_in_cosh"


@requires_docker
@pytest.mark.asyncio
async def test_re_add_refreshes_snapshot_from_current_cosh(db):
    """User's explicit decision (2026-05-06): on re-add after a
    soft-removal, the snapshot is re-taken fresh from current Cosh
    state — not preserved from the original add. So if SA fixes a
    scientific name in Cosh while the crop was off the belt, the
    re-add picks up the corrected value."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    cosh_row, _ = await make_crop_reference(
        db, "crop:fennel",
        name="Fennel", scientific_name="OldName",
        measure="AREA_WISE",
    )
    await db.commit()

    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:fennel"),
        db=db, current_user=user,
    )
    crop = (await db.execute(
        select(ClientCrop).where(ClientCrop.client_id == client.id)
    )).scalar_one()
    await remove_crop(client_id=client.id, crop_id=crop.id, db=db, current_user=user)

    cosh_row.metadata_ = {"scientific_name": "Foeniculum vulgare"}
    await db.commit()

    out = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:fennel"),
        db=db, current_user=user,
    )
    assert out.id == crop.id
    assert out.crop_scientific_name == "Foeniculum vulgare"


# ── Batch 1C: PoP create/publish membership gate ─────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_package_succeeds_when_crop_on_belt(db):
    """Happy path — CA has added the crop, expert can build a PoP."""
    from app.modules.advisory.router import create_package
    from app.modules.advisory.schemas import PackageCreate as PkgCreate

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy(db)
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )

    out = await create_package(
        client_id=client.id,
        request=PkgCreate(
            crop_cosh_id="crop:paddy", name="Paddy Kharif PoP",
            package_type=PackageType.ANNUAL, duration_days=120,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )
    assert out.crop_cosh_id == "crop:paddy"
    assert out.status == PackageStatus.DRAFT


@requires_docker
@pytest.mark.asyncio
async def test_create_package_422_when_crop_never_added(db):
    """Spec rule: experts can only build PoPs for crops the CA has
    placed on the belt. With no ClientCrop row at all, create must
    fail with the stable code so the portal can surface the
    'ask the CA to add this crop' message."""
    from app.modules.advisory.router import create_package
    from app.modules.advisory.schemas import PackageCreate as PkgCreate

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_package(
            client_id=client.id,
            request=PkgCreate(
                crop_cosh_id="crop:never_added", name="Phantom PoP",
                package_type=PackageType.ANNUAL, duration_days=120,
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "crop_not_on_belt"


@requires_docker
@pytest.mark.asyncio
async def test_create_package_422_when_crop_soft_removed(db):
    """The crop was on the belt, then the CA removed it. Until they
    re-add, no expert can build a new PoP — the gate fires identically
    to the never-added case."""
    from app.modules.advisory.router import create_package
    from app.modules.advisory.schemas import PackageCreate as PkgCreate

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy(db)
    crop = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await remove_crop(client_id=client.id, crop_id=crop.id, db=db, current_user=user)

    with pytest.raises(HTTPException) as ei:
        await create_package(
            client_id=client.id,
            request=PkgCreate(
                crop_cosh_id="crop:paddy", name="Late PoP",
                package_type=PackageType.ANNUAL, duration_days=120,
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "crop_not_on_belt"


@requires_docker
@pytest.mark.asyncio
async def test_publish_package_422_when_crop_soft_removed_after_draft(db):
    """Critical case: a DRAFT PoP exists, the CA soft-removes the
    crop (DRAFTs are NOT cascade-flagged per Batch 1A), then the
    expert tries to publish. Without the gate, the publish would
    silently succeed despite the crop being off the belt. The gate
    blocks it; the CA must re-add the crop first."""
    from app.modules.advisory.router import publish_package

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy(db)
    crop = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    pkg = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy",
        name="Draft PoP", status=PackageStatus.DRAFT,
    )
    await db.commit()
    await remove_crop(client_id=client.id, crop_id=crop.id, db=db, current_user=user)

    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg.id,
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "crop_not_on_belt"


@requires_docker
@pytest.mark.asyncio
async def test_publish_package_succeeds_after_re_add(db):
    """The recovery path — re-adding the crop unblocks the publish
    that the gate had blocked. Confirms the gate is reversible by
    the legitimate CA action and not a one-way trap door."""
    from app.modules.advisory.router import publish_package

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy(db)
    crop = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    pkg = await _make_package(
        db, client=client, crop_cosh_id="crop:paddy",
        name="Reviving PoP", status=PackageStatus.DRAFT,
    )
    await db.commit()
    await remove_crop(client_id=client.id, crop_id=crop.id, db=db, current_user=user)
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )

    out = await publish_package(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out.status == PackageStatus.ACTIVE


# ── Batch 1D: derived is_active on the list endpoint ────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_emits_is_active_true_when_active_pop_exists(db):
    """Crop with an ACTIVE PoP surfaces as is_active=True; the
    derived `status` string mirrors it for portal compat."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy(db)
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await _make_package(
        db, client=client, crop_cosh_id="crop:paddy",
        name="Live PoP", status=PackageStatus.ACTIVE,
    )
    await db.commit()

    listed = await list_crops(client_id=client.id, db=db, current_user=user)
    assert len(listed) == 1
    assert listed[0].crop_cosh_id == "crop:paddy"
    assert listed[0].is_active is True
    assert listed[0].status == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_list_emits_is_active_false_when_only_draft(db):
    """Crop with only DRAFT PoPs is on the belt but has no live
    advisory — surfaces as inactive in the list."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy(db)
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await _make_package(
        db, client=client, crop_cosh_id="crop:paddy",
        name="WIP PoP", status=PackageStatus.DRAFT,
    )
    await db.commit()

    listed = await list_crops(client_id=client.id, db=db, current_user=user)
    assert listed[0].is_active is False
    assert listed[0].status == "INACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_list_emits_is_active_false_when_zero_pops(db):
    """Crop just added by CA, no PoPs yet — inactive."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy(db)
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await db.commit()

    listed = await list_crops(client_id=client.id, db=db, current_user=user)
    assert listed[0].is_active is False
    assert listed[0].status == "INACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_add_returns_is_active_true_when_re_add_revives_active_pop(db):
    """Re-add path: cascade-inactivated PoP gets revived to ACTIVE
    by `restore_cascade_inactivated_packages`. The CropOut response
    must reflect that, otherwise the portal shows the wrong chip."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy(db)
    crop = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    await _make_package(
        db, client=client, crop_cosh_id="crop:paddy",
        name="Revivable PoP", status=PackageStatus.ACTIVE,
    )
    await db.commit()
    await remove_crop(client_id=client.id, crop_id=crop.id, db=db, current_user=user)

    out = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    assert out.is_active is True
    assert out.status == "ACTIVE"


@requires_docker
@pytest.mark.asyncio
async def test_fresh_add_returns_is_active_false(db):
    """Brand-new add — no PoPs exist yet, so is_active must be False."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy(db)
    await db.commit()

    out = await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )
    assert out.is_active is False
    assert out.status == "INACTIVE"
