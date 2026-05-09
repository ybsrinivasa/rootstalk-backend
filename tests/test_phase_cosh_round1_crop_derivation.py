"""Cosh-sync Round 1 — biological_names → Crop derivation (2026-05-09).

Pins the live-Cosh wire shape captured during the first production
sync (sync_id 93e3e668-2266-4839-986f-b09898db3fc0):

  • Cores: `biological_names` (plural) holds every organism;
    `roles_of_biological_names` holds Crop / Pest / Bio Control Agent
    role items.
  • Connect: `biological_names_and_roles` — each row links one
    biological_name (position 1) to one role (position 2).

These tests feed the same shape into `cosh_crop_view.list_crops`,
`is_crop_in_cosh`, and the retrofit `crop_snapshot.fetch_snapshot`,
plus the two list endpoints. If Cosh ever changes the wire shape,
these tests will surface the drift loudly.
"""
from __future__ import annotations

import pytest

from app.modules.clients.models import ClientCrop
from app.modules.sync.models import CoshConnectRow, CoshCoreItem, CropMeasure
from app.modules.sync.router import list_cosh_crops
from app.modules.clients.router import list_available_crops
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE, COSH_NAME_ROLE_CONNECT, COSH_ROLES_CORE,
    COSH_ROLE_BIO_CONTROL_AGENT_UUID, COSH_ROLE_CROP_UUID,
    COSH_ROLE_PEST_UUID,
)
from app.services.cosh_crop_view import (
    _crop_classified_biological_name_ids, is_crop_in_cosh, list_crops,
)
from app.services.crop_snapshot import CropSnapshotError, fetch_snapshot
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


# ── Wire-shape fixture ─────────────────────────────────────────────────────

async def _seed_live_cosh_shape(db) -> None:
    """Mirrors what the 2026-05-09 sync landed: 3 role items, a
    handful of biological_names, and Connect rows tagging them as
    Crop / Pest / BCA. UUIDs match the actual production constants."""
    # Role Cores (the 3 items in roles_of_biological_names)
    db.add_all([
        CoshCoreItem(
            cosh_id=COSH_ROLE_CROP_UUID, core_type=COSH_ROLES_CORE,
            translations={"en": "Crop"}, status="active",
        ),
        CoshCoreItem(
            cosh_id=COSH_ROLE_PEST_UUID, core_type=COSH_ROLES_CORE,
            translations={"en": "Pest"}, status="active",
        ),
        CoshCoreItem(
            cosh_id=COSH_ROLE_BIO_CONTROL_AGENT_UUID, core_type=COSH_ROLES_CORE,
            translations={"en": "Bio Control Agent"}, status="active",
        ),
    ])

    # Biological_names — three crops, two pests, one BCA, one
    # un-classified (no Connect row will reference it).
    db.add_all([
        CoshCoreItem(cosh_id="bn:tomato", core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Tomato"}, status="active"),
        CoshCoreItem(cosh_id="bn:apple",  core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Apple"}, status="active"),
        CoshCoreItem(cosh_id="bn:onion",  core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Onion"}, status="active"),
        CoshCoreItem(cosh_id="bn:aphid",  core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Aphid"}, status="active"),
        CoshCoreItem(cosh_id="bn:thrips", core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Thrips"}, status="active"),
        CoshCoreItem(cosh_id="bn:trichogramma", core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Trichogramma"}, status="active"),
        CoshCoreItem(cosh_id="bn:undecided", core_type=COSH_BIOLOGICAL_NAMES_CORE,
                     translations={"en": "Mystery"}, status="active"),
    ])

    # Connect rows — exactly the production endpoint shape.
    def _row(connect_id: str, name_id: str, role_id: str):
        return CoshConnectRow(
            connect_id=connect_id, connect_type=COSH_NAME_ROLE_CONNECT,
            status="active",
            endpoints=[
                {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": name_id, "position": 1},
                {"role": COSH_ROLES_CORE, "cosh_id": role_id, "position": 2},
            ],
        )
    db.add_all([
        _row("c:1", "bn:tomato",       COSH_ROLE_CROP_UUID),
        _row("c:2", "bn:apple",        COSH_ROLE_CROP_UUID),
        _row("c:3", "bn:onion",        COSH_ROLE_CROP_UUID),
        _row("c:4", "bn:aphid",        COSH_ROLE_PEST_UUID),
        _row("c:5", "bn:thrips",       COSH_ROLE_PEST_UUID),
        _row("c:6", "bn:trichogramma", COSH_ROLE_BIO_CONTROL_AGENT_UUID),
        # bn:undecided gets no Connect row — un-classified.
    ])
    await db.flush()


# ── Derivation service ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_crops_returns_only_crop_classified_names(db):
    await _seed_live_cosh_shape(db)
    await db.commit()

    out = await list_crops(db)
    names = [r["name_en"] for r in out]
    # All three crops, sorted alphabetically.
    assert names == ["Apple", "Onion", "Tomato"]
    # No pests, no BCA, no un-classified row.
    assert "Aphid" not in names
    assert "Trichogramma" not in names
    assert "Mystery" not in names


@requires_docker
@pytest.mark.asyncio
async def test_is_crop_in_cosh_distinguishes_role(db):
    """The classification check is the gate that stops a CA from
    accidentally adding a Pest or BCA UUID as a crop."""
    await _seed_live_cosh_shape(db)
    await db.commit()

    assert await is_crop_in_cosh(db, "bn:tomato") is True
    assert await is_crop_in_cosh(db, "bn:aphid") is False
    assert await is_crop_in_cosh(db, "bn:trichogramma") is False
    assert await is_crop_in_cosh(db, "bn:undecided") is False
    assert await is_crop_in_cosh(db, "bn:does-not-exist") is False


@requires_docker
@pytest.mark.asyncio
async def test_inactive_connect_row_excludes_classification(db):
    """When Cosh marks a Connect row inactive (curator unlinked it),
    the biological_name no longer counts as Crop. This is the
    deactivation path — a name doesn't have to be removed; the
    classification gets revoked."""
    await _seed_live_cosh_shape(db)
    # Deactivate the Tomato → Crop Connect row.
    row = (await db.execute(
        __import__("sqlalchemy").select(CoshConnectRow).where(
            CoshConnectRow.connect_id == "c:1",
        )
    )).scalar_one()
    row.status = "inactive"
    await db.commit()

    crop_ids = await _crop_classified_biological_name_ids(db)
    assert "bn:tomato" not in crop_ids
    assert "bn:apple" in crop_ids


@requires_docker
@pytest.mark.asyncio
async def test_inactive_biological_name_excluded_from_list(db):
    """If Cosh deactivates the biological_name itself, list_crops
    must not surface it — the CA shouldn't pick a deactivated entry
    even if the Connect row still classifies it as Crop."""
    await _seed_live_cosh_shape(db)
    bn = (await db.execute(
        __import__("sqlalchemy").select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == "bn:apple",
        )
    )).scalar_one()
    bn.status = "inactive"
    await db.commit()

    out = await list_crops(db)
    names = [r["name_en"] for r in out]
    assert "Apple" not in names
    assert names == ["Onion", "Tomato"]


@requires_docker
@pytest.mark.asyncio
async def test_empty_universe_returns_empty_list(db):
    """No Connect rows at all → empty list, not crash."""
    out = await list_crops(db)
    assert out == []


# ── crop_snapshot retrofit ────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_fetch_snapshot_succeeds_for_classified_crop(db):
    await _seed_live_cosh_shape(db)
    db.add(CropMeasure(crop_cosh_id="bn:tomato", measure="AREA_WISE"))
    await db.commit()

    snapshot = await fetch_snapshot(db, "bn:tomato")
    assert snapshot.name_en == "Tomato"
    assert snapshot.area_or_plant == "AREA_WISE"
    # Scientific name not sourced in V1 — separate Cosh Connect later.
    assert snapshot.scientific_name is None


@requires_docker
@pytest.mark.asyncio
async def test_fetch_snapshot_refuses_pest_uuid(db):
    """Defensive: if the CA-portal frontend ever passes a Pest UUID
    as crop_cosh_id, the snapshot path 422s with a stable code rather
    than silently letting the pest into the company's crop belt."""
    await _seed_live_cosh_shape(db)
    db.add(CropMeasure(crop_cosh_id="bn:aphid", measure="AREA_WISE"))
    await db.commit()

    with pytest.raises(CropSnapshotError) as exc:
        await fetch_snapshot(db, "bn:aphid")
    assert exc.value.code == "biological_name_not_classified_as_crop"


@requires_docker
@pytest.mark.asyncio
async def test_fetch_snapshot_refuses_bca_uuid(db):
    await _seed_live_cosh_shape(db)
    db.add(CropMeasure(crop_cosh_id="bn:trichogramma", measure="AREA_WISE"))
    await db.commit()

    with pytest.raises(CropSnapshotError) as exc:
        await fetch_snapshot(db, "bn:trichogramma")
    assert exc.value.code == "biological_name_not_classified_as_crop"


@requires_docker
@pytest.mark.asyncio
async def test_fetch_snapshot_blocks_when_measure_missing(db):
    """Until the Area/Plant Connect ships, every CA add 422s with
    `crop_missing_measure`. This is intentional — the picker shows
    the universe; the form action waits for measure data."""
    await _seed_live_cosh_shape(db)
    await db.commit()
    # No CropMeasure row for bn:tomato.

    with pytest.raises(CropSnapshotError) as exc:
        await fetch_snapshot(db, "bn:tomato")
    assert exc.value.code == "crop_missing_measure"


# ── List endpoints ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_admin_cosh_crops_endpoint(db):
    """SA-portal CM browse — full Crop universe."""
    await _seed_live_cosh_shape(db)
    user = await make_user(db, name="CM")
    await db.commit()

    out = await list_cosh_crops(db=db, current_user=user)
    assert {r["name_en"] for r in out} == {"Apple", "Onion", "Tomato"}


@requires_docker
@pytest.mark.asyncio
async def test_available_crops_excludes_already_added(db):
    """CA picker — full universe minus crops this client already has
    on the belt. Re-adding the same crop is friendlier from a fresh
    list with the option already removed."""
    await _seed_live_cosh_shape(db)
    client = await make_client(db)
    user = await make_user(db, name="CA")
    db.add(CropMeasure(crop_cosh_id="bn:tomato", measure="AREA_WISE"))
    db.add(ClientCrop(
        client_id=client.id, crop_cosh_id="bn:tomato",
        crop_name_en="Tomato", crop_area_or_plant="AREA_WISE",
    ))
    await db.commit()

    out = await list_available_crops(
        client_id=client.id, db=db, current_user=user,
    )
    names = {r["name_en"] for r in out}
    assert names == {"Apple", "Onion"}  # Tomato removed


@requires_docker
@pytest.mark.asyncio
async def test_available_crops_other_clients_not_excluded(db):
    """A crop on Client B's belt must still show up for Client A's
    picker — exclusion is per-client."""
    await _seed_live_cosh_shape(db)
    client_a = await make_client(db)
    client_b = await make_client(db)
    user = await make_user(db, name="CA")
    db.add(ClientCrop(
        client_id=client_b.id, crop_cosh_id="bn:tomato",
        crop_name_en="Tomato", crop_area_or_plant="AREA_WISE",
    ))
    await db.commit()

    out = await list_available_crops(
        client_id=client_a.id, db=db, current_user=user,
    )
    assert "bn:tomato" in {r["cosh_id"] for r in out}
