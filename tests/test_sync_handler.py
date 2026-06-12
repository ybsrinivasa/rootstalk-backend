"""Sync handler — Cores + Connects via Cosh's positions-dict format.

Tests cover the contract in docs/COSH_2_SYNC_CONTRACT.md:
  • Cores (translations + parent_cosh_id) land in cosh_core_items.
  • Connects (positions dict) land in cosh_connect_rows; positions
    are reshaped into a typed endpoints array with role names mirroring
    the linked target's entity_type.
  • Items with `positions` are routed as Connects regardless of the
    batch's entity_type — classification is shape-driven.
  • BlankBox sentinel positions are dropped from the endpoints array.
  • Connect-row scalar attributes (e.g. priority_rank) land in metadata.
  • Full-sync mode inactivates absent rows in both tables.
  • Translations validation gates Core writes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.sync.models import CoshConnectRow, CoshCoreItem, CoshSyncLog
from app.modules.sync.service import process_payload
from tests.conftest import requires_docker


def _payload(*batches, sync_mode="incremental", sync_id="test-sync"):
    return {
        "sync_id": sync_id,
        "sync_mode": sync_mode,
        "entity_batches": list(batches),
    }


def _batch(entity_type, *items):
    return {"entity_type": entity_type, "items": list(items)}


def _positions(*pairs):
    """Build a positions dict from (position, entity_type, cosh_id) tuples."""
    return {
        str(pos): {"cosh_id": cid, "entity_type": etype}
        for pos, etype, cid in pairs
    }


async def _new_log(db) -> CoshSyncLog:
    log = CoshSyncLog(sync_id="test", sync_mode="incremental", status="IN_PROGRESS")
    db.add(log)
    await db.flush()
    return log


# ── Cores ───────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_core_lands_in_cosh_core_items(db):
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "crop",
        {"cosh_id": "crop:paddy", "translations": {"en": "Paddy", "kn": "ಭತ್ತ"},
         "metadata": {"scientific_name": "Oryza sativa"}},
    )), log)
    await db.commit()

    row = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "crop:paddy")
    )).scalar_one()
    assert row.core_type == "crop"
    assert row.translations == {"en": "Paddy", "kn": "ಭತ್ತ"}
    assert row.metadata_ == {"scientific_name": "Oryza sativa"}
    assert result["summary"]["inserted"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_core_with_parent_chain(db):
    log = await _new_log(db)
    await process_payload(db, _payload(
        _batch("problem_group",
               {"cosh_id": "pg:fungal", "translations": {"en": "Fungal diseases"}}),
        _batch("specific_problem",
               {"cosh_id": "sp:powdery", "parent_cosh_id": "pg:fungal",
                "translations": {"en": "Powdery mildew"}}),
    ), log)
    await db.commit()

    row = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "sp:powdery")
    )).scalar_one()
    assert row.core_type == "specific_problem"
    assert row.parent_cosh_id == "pg:fungal"


@requires_docker
@pytest.mark.asyncio
async def test_core_upsert_updates_existing(db):
    log = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "common_name",
        {"cosh_id": "cn:imida", "translations": {"en": "Imidacloprid"}},
    )), log)
    await db.commit()

    log2 = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "common_name",
        {"cosh_id": "cn:imida", "translations": {"en": "Imidacloprid",
                                                  "hi": "इमिडाक्लोप्रिड"}},
    )), log2)
    await db.commit()

    row = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "cn:imida")
    )).scalar_one()
    assert "hi" in row.translations


# ── Connects: positions dict adapter ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_connect_extracts_endpoints_from_positions(db):
    """pest_diagnosis_chain row from a positions-dict payload — adapter
    reshapes into the typed endpoints array, role = each position's
    target entity_type. priority_rank is a Core, surfaced as the 9th
    position pointing at a priority_rank Core item."""
    log = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "pest_diagnosis_chain",
        {
            "cosh_id": "pdc:0001",
            "status": "active",
            "positions": _positions(
                (1, "crop",          "crop:tomato"),
                (2, "crop_stage",    "stage:fruiting"),
                (3, "pest",          "pest:fruit_borer"),
                (4, "pest_stage",    "infest:early"),
                (5, "part",          "part:fruit"),
                (7, "symptom",       "sym:bored_holes"),
                (9, "priority_rank", "pr:1"),
                # positions 6 (sub_part) and 8 (sub_symptom) absent
            ),
        },
    )), log)
    await db.commit()

    row = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pdc:0001")
    )).scalar_one()
    assert row.connect_type == "pest_diagnosis_chain"

    by_role = {ep["role"]: ep["cosh_id"] for ep in row.endpoints}
    assert by_role == {
        "crop":          "crop:tomato",
        "crop_stage":    "stage:fruiting",
        "pest":          "pest:fruit_borer",
        "pest_stage":    "infest:early",
        "part":          "part:fruit",
        "symptom":       "sym:bored_holes",
        "priority_rank": "pr:1",
    }
    # No row-level scalar attributes today — metadata stays empty.
    assert row.metadata_ in (None, {})
    # Endpoints retain position numbers for ordered Compound Connects
    positions_in_endpoints = {ep["position"] for ep in row.endpoints}
    assert positions_in_endpoints == {1, 2, 3, 4, 5, 7, 9}


@requires_docker
@pytest.mark.asyncio
async def test_blank_box_position_is_dropped(db):
    """A position whose cosh_id is a BlankBox sentinel is treated as
    absent — it doesn't show up in the endpoints array."""
    log = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "pest_diagnosis_chain",
        {
            "cosh_id": "pdc:bb",
            "status": "active",
            "positions": _positions(
                (1, "crop",        "crop:paddy"),
                (2, "crop_stage",  "stage:tillering"),
                (3, "pest",        "pest:stem_borer"),
                (5, "part",        "part:stem"),
                (6, "sub_part",    "BlankBox"),         # sentinel
                (7, "symptom",     "sym:dead_heart"),
                (8, "sub_symptom", "Blank Box"),        # variant spelling
            ),
        },
    )), log)
    await db.commit()

    row = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pdc:bb")
    )).scalar_one()
    roles = {ep["role"] for ep in row.endpoints}
    # sub_part and sub_symptom dropped because their cosh_ids were sentinels
    assert "sub_part" not in roles
    assert "sub_symptom" not in roles
    assert {"crop", "crop_stage", "pest", "part", "symptom"} <= roles


@requires_docker
@pytest.mark.asyncio
async def test_image_connect_lands_with_two_positions(db):
    """Image Connects (one per crop) carry 2 positions: the diagnosis
    row and the media item. Same shape-driven adapter handles them
    with no per-crop registration."""
    log = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "tomato_pest_images",
        {
            "cosh_id": "tpi:img1",
            "status": "active",
            "positions": _positions(
                (1, "pest_diagnosis_chain", "pdc:0001"),
                (2, "media",                "med:img_001"),
            ),
        },
    )), log)
    await db.commit()

    row = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "tpi:img1")
    )).scalar_one()
    by_role = {ep["role"]: ep["cosh_id"] for ep in row.endpoints}
    assert by_role == {
        "pest_diagnosis_chain": "pdc:0001",
        "media":                "med:img_001",
    }


# ── Validation ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_missing_translation_fails_core_item(db):
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "crop",
        {"cosh_id": "crop:nolang", "translations": {"kn": "ಬಾಟಲಿ"}},
    )), log)
    await db.commit()

    assert result["summary"]["failed"] == 1
    assert "en" in result["entity_results"][0]["errors"][0]["reason"].lower()


@requires_docker
@pytest.mark.asyncio
async def test_connect_with_no_extractable_endpoints_fails(db):
    """A Connect item whose positions dict has all-empty values fails
    the upsert — no point storing a Connect row with zero endpoints."""
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "pest_diagnosis_chain",
        {"cosh_id": "pdc:bad",
         "status": "active",
         "positions": {
             "1": {"cosh_id": "BlankBox", "entity_type": "crop"},
             "2": {"cosh_id": None, "entity_type": "crop_stage"},
         }},
    )), log)
    await db.commit()

    assert result["summary"]["failed"] == 1
    err = result["entity_results"][0]["errors"][0]
    assert err["cosh_id"] == "pdc:bad"
    assert "endpoint" in err["reason"].lower()


# ── Full-sync inactivation ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_full_sync_inactivates_absent_cores(db):
    log1 = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "crop",
        {"cosh_id": "crop:keep", "translations": {"en": "Paddy"}},
        {"cosh_id": "crop:drop", "translations": {"en": "Maize"}},
    )), log1)
    await db.commit()

    log2 = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "crop",
        {"cosh_id": "crop:keep", "translations": {"en": "Paddy"}},
    ), sync_mode="full"), log2)
    await db.commit()

    dropped = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "crop:drop")
    )).scalar_one()
    kept = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "crop:keep")
    )).scalar_one()
    assert dropped.status == "inactive"
    assert kept.status == "active"


@requires_docker
@pytest.mark.asyncio
async def test_full_sync_inactivates_absent_connect_rows(db):
    log1 = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "pest_diagnosis_chain",
        {"cosh_id": "pdc:keep", "status": "active",
         "positions": _positions(
             (1, "crop", "crop:tomato"),
             (3, "pest", "pest:p1"),
             (5, "part", "part:leaf"),
             (7, "symptom", "sym:s1"),
         )},
        {"cosh_id": "pdc:drop", "status": "active",
         "positions": _positions(
             (1, "crop", "crop:tomato"),
             (3, "pest", "pest:p2"),
             (5, "part", "part:leaf"),
             (7, "symptom", "sym:s2"),
         )},
    )), log1)
    await db.commit()

    log2 = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "pest_diagnosis_chain",
        {"cosh_id": "pdc:keep", "status": "active",
         "positions": _positions(
             (1, "crop", "crop:tomato"),
             (3, "pest", "pest:p1"),
             (5, "part", "part:leaf"),
             (7, "symptom", "sym:s1"),
         )},
    ), sync_mode="full"), log2)
    await db.commit()

    dropped = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pdc:drop")
    )).scalar_one()
    kept = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pdc:keep")
    )).scalar_one()
    assert dropped.status == "inactive"
    assert kept.status == "active"


# ── Brand-cache auto-refresh hook ───────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_brand_cache_rebuild_fires_when_input_manufacturers_synced(db):
    """Sync that touches a brand-cache-feeding Core appends a synthetic
    _brand_lookup_cache_rebuild entry to entity_results so the operator
    knows the cache was refreshed without a separate trigger."""
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "input_manufacturers",
        {"cosh_id": "mfr:bayer", "translations": {"en": "Bayer", "hi": "बायर"}},
    )), log)
    await db.commit()

    rebuild_entries = [
        e for e in result["entity_results"]
        if e.get("entity_type") == "_brand_lookup_cache_rebuild"
    ]
    assert len(rebuild_entries) == 1, (
        "brand cache rebuild should fire for input_manufacturers sync; "
        f"entity_results={result['entity_results']}"
    )
    assert rebuild_entries[0]["failed"] == 0


@requires_docker
@pytest.mark.asyncio
async def test_brand_cache_rebuild_skipped_for_unrelated_sync(db):
    """Sync that doesn't touch any brand-cache-feeding entity_type should
    not trigger the rebuild — keeps the hook free for the common case
    where someone syncs an unrelated Core."""
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "crop",
        {"cosh_id": "crop:wheat", "translations": {"en": "Wheat", "hi": "गेहूँ"}},
    )), log)
    await db.commit()

    rebuild_entries = [
        e for e in result["entity_results"]
        if e.get("entity_type") == "_brand_lookup_cache_rebuild"
    ]
    assert rebuild_entries == [], (
        f"rebuild should be skipped for non-brand sync; entries={rebuild_entries}"
    )


@requires_docker
@pytest.mark.asyncio
async def test_brand_cache_rebuild_skipped_when_no_rows_changed(db):
    """If a brand-cache-feeding sync runs but every row failed validation
    (no inserts/updates), there's nothing to mirror — skip the rebuild."""
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "trade_names",
        # Missing cosh_id triggers the validation path; row counts stay 0.
        {"translations": {"en": "Anonymous Brand"}},
    )), log)
    await db.commit()

    rebuild_entries = [
        e for e in result["entity_results"]
        if e.get("entity_type") == "_brand_lookup_cache_rebuild"
    ]
    assert rebuild_entries == [], (
        "no rebuild when zero rows changed; "
        f"entries={rebuild_entries}, results={result['entity_results']}"
    )

