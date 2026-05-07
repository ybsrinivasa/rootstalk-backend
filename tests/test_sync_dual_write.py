"""Sync handler — dual-write + Connect adapter tests.

Covers the schema-migration transition:
  • Cores land in BOTH cosh_reference_cache (legacy) AND cosh_core_items.
  • Connects land in BOTH cosh_reference_cache AND cosh_connect_rows
    with endpoints extracted from the legacy flat-payload metadata.
  • Native typed payload (`endpoints` array directly on the item)
    bypasses the metadata extraction.
  • Full-sync inactivation flips both tables' rows.
  • Translations validation still gates writes.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.sync.models import (
    CoshConnectRow, CoshCoreItem, CoshReferenceCache, CoshSyncLog,
)
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


async def _new_log(db) -> CoshSyncLog:
    log = CoshSyncLog(sync_id="test", sync_mode="incremental", status="IN_PROGRESS")
    db.add(log)
    await db.flush()
    return log


# ── Cores ───────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_core_dual_write(db):
    """Crop entity lands in both legacy and new tables with matching data."""
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "crop",
        {"cosh_id": "crop:paddy", "translations": {"en": "Paddy", "kn": "ಭತ್ತ"},
         "metadata": {"scientific_name": "Oryza sativa"}},
    )), log)
    await db.commit()

    legacy = (await db.execute(
        select(CoshReferenceCache).where(CoshReferenceCache.cosh_id == "crop:paddy")
    )).scalar_one()
    new = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "crop:paddy")
    )).scalar_one()

    assert legacy.entity_type == "crop"
    assert new.core_type == "crop"
    assert legacy.translations == new.translations
    assert legacy.metadata_ == new.metadata_
    assert result["summary"]["inserted"] == 1


@requires_docker
@pytest.mark.asyncio
async def test_core_with_parent_chain(db):
    """specific_problem with parent=problem_group propagates to new table."""
    log = await _new_log(db)
    await process_payload(db, _payload(
        _batch("problem_group",
               {"cosh_id": "pg:fungal", "translations": {"en": "Fungal diseases"}}),
        _batch("specific_problem",
               {"cosh_id": "sp:powdery", "parent_cosh_id": "pg:fungal",
                "translations": {"en": "Powdery mildew"}}),
    ), log)
    await db.commit()

    new_sp = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "sp:powdery")
    )).scalar_one()
    assert new_sp.core_type == "specific_problem"
    assert new_sp.parent_cosh_id == "pg:fungal"


@requires_docker
@pytest.mark.asyncio
async def test_core_upsert_updates_existing(db):
    """Re-syncing the same cosh_id updates both tables."""
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

    new = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "cn:imida")
    )).scalar_one()
    assert "hi" in new.translations


# ── Connects: legacy adapter (endpoints in metadata) ───────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_connect_extracts_endpoints_from_metadata(db):
    """problem_to_symptom row from legacy flat payload — adapter pulls
    the 5 endpoint cosh_ids out of metadata into a typed array."""
    log = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "problem_to_symptom",
        {
            "cosh_id": "pts:0001",
            "translations": {"en": "Powdery mildew on leaves — white spots"},
            "metadata": {
                "problem_cosh_id": "sp:powdery",
                "plant_part_cosh_id": "part:leaf",
                "symptom_cosh_id": "sym:white_spots",
                "sub_part_cosh_id": "subpart:upper",
                "sub_symptom_cosh_id": None,         # absent endpoint
                "priority_rank": 1,
                "crop_stage_cosh_id": "stage:vegetative",
            },
        },
    )), log)
    await db.commit()

    legacy = (await db.execute(
        select(CoshReferenceCache).where(CoshReferenceCache.cosh_id == "pts:0001")
    )).scalar_one()
    # Legacy still carries everything in metadata for back-compat with BL-08
    assert legacy.metadata_["problem_cosh_id"] == "sp:powdery"
    assert legacy.metadata_["priority_rank"] == 1

    row = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pts:0001")
    )).scalar_one()
    assert row.connect_type == "problem_to_symptom"

    # endpoints array has only the 4 set roles (sub_symptom was None)
    by_role = {ep["role"]: ep["cosh_id"] for ep in row.endpoints}
    assert by_role == {
        "problem":    "sp:powdery",
        "plant_part": "part:leaf",
        "symptom":    "sym:white_spots",
        "sub_part":   "subpart:upper",
    }
    # metadata on the new row strips the role-keyed cosh_ids; keeps the rest
    assert row.metadata_ == {
        "priority_rank": 1, "crop_stage_cosh_id": "stage:vegetative",
    }


@requires_docker
@pytest.mark.asyncio
async def test_connect_native_endpoints_bypass_adapter(db):
    """When Cosh emits typed payload with `endpoints` directly, the
    adapter respects it and skips metadata extraction."""
    log = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "problem_to_symptom",
        {
            "cosh_id": "pts:0002",
            "translations": {"en": "..."},
            "endpoints": [
                {"role": "problem", "cosh_id": "sp:other"},
                {"role": "plant_part", "cosh_id": "part:stem"},
                {"role": "symptom", "cosh_id": "sym:lesion"},
            ],
            "metadata": {"priority_rank": 2},
        },
    )), log)
    await db.commit()

    row = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pts:0002")
    )).scalar_one()
    by_role = {ep["role"]: ep["cosh_id"] for ep in row.endpoints}
    assert by_role == {"problem": "sp:other", "plant_part": "part:stem",
                       "symptom": "sym:lesion"}
    assert row.metadata_ == {"priority_rank": 2}


# ── Validation ──────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_missing_translation_fails_item(db):
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "crop",
        {"cosh_id": "crop:nolang", "translations": {"kn": "ಬಾಟಲಿ"}},  # no en
    )), log)
    await db.commit()

    assert result["summary"]["failed"] == 1
    assert "en" in result["entity_results"][0]["errors"][0]["reason"].lower()


@requires_docker
@pytest.mark.asyncio
async def test_connect_with_no_extractable_endpoints_fails(db):
    """problem_to_symptom row without any of the 5 endpoint keys in
    metadata → the new-table upsert raises and the item is recorded
    failed. The legacy table is rolled-back-by-exception too because
    the dual-write happens inside the same per-item try block."""
    log = await _new_log(db)
    result = await process_payload(db, _payload(_batch(
        "problem_to_symptom",
        {"cosh_id": "pts:bad",
         "translations": {"en": "Bad row"},
         "metadata": {"priority_rank": 1}},
    )), log)
    await db.commit()

    assert result["summary"]["failed"] == 1
    err = result["entity_results"][0]["errors"][0]
    assert err["cosh_id"] == "pts:bad"
    assert "endpoint" in err["reason"].lower()


# ── Full-sync inactivation ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_full_sync_inactivates_absent_rows_in_both_tables(db):
    """First sync seeds two crops; second `full` sync includes only
    one — the absent crop flips to inactive in both tables."""
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

    legacy_dropped = (await db.execute(
        select(CoshReferenceCache).where(CoshReferenceCache.cosh_id == "crop:drop")
    )).scalar_one()
    new_dropped = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "crop:drop")
    )).scalar_one()
    assert legacy_dropped.status == "inactive"
    assert new_dropped.status == "inactive"

    legacy_kept = (await db.execute(
        select(CoshReferenceCache).where(CoshReferenceCache.cosh_id == "crop:keep")
    )).scalar_one()
    new_kept = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "crop:keep")
    )).scalar_one()
    assert legacy_kept.status == "active"
    assert new_kept.status == "active"


@requires_docker
@pytest.mark.asyncio
async def test_full_sync_inactivates_absent_connect_rows(db):
    """Same inactivation rule for Connects — flips cosh_connect_rows."""
    log1 = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "problem_to_symptom",
        {"cosh_id": "pts:keep", "translations": {"en": "..."},
         "metadata": {"problem_cosh_id": "p:1", "plant_part_cosh_id": "pl:1",
                      "symptom_cosh_id": "s:1"}},
        {"cosh_id": "pts:drop", "translations": {"en": "..."},
         "metadata": {"problem_cosh_id": "p:2", "plant_part_cosh_id": "pl:2",
                      "symptom_cosh_id": "s:2"}},
    )), log1)
    await db.commit()

    log2 = await _new_log(db)
    await process_payload(db, _payload(_batch(
        "problem_to_symptom",
        {"cosh_id": "pts:keep", "translations": {"en": "..."},
         "metadata": {"problem_cosh_id": "p:1", "plant_part_cosh_id": "pl:1",
                      "symptom_cosh_id": "s:1"}},
    ), sync_mode="full"), log2)
    await db.commit()

    dropped = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pts:drop")
    )).scalar_one()
    kept = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "pts:keep")
    )).scalar_one()
    assert dropped.status == "inactive"
    assert kept.status == "active"
