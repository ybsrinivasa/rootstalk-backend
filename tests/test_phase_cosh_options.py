"""Read-through cascade lookups for the seven input-options Connects
shipped by Cosh on 2026-05-14 — drives the Add Practice modal
dropdowns (Common Name / Application Method / Unit) plus the brand
cascade (Common Name → Trade Name + Manufacturer → Formulation + a.i.).

The tests seed minimal Cosh-side Core + Connect rows and assert the
service walks the right Connect and produces the right shape.
"""
from __future__ import annotations

import pytest

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_AI_CORE,
    COSH_APPLICATION_METHODS_CORE,
    COSH_APPLICATION_METHODS_L2_CONNECT,
    COSH_COMMON_NAMES_CORE,
    COSH_COMMONNAMES_L2_CONNECT,
    COSH_FORMULATIONS_CORE,
    COSH_INPUT_MANUFACTURERS_CORE,
    COSH_L2_DATA_CORE,
    COSH_L2_UNITS_UNITTYPES_CONNECT,
    COSH_TRADE_NAMES_CORE,
    COSH_TRADENAME_AI_CONNECT,
    COSH_TRADENAME_COMMONNAME_CONNECT,
    COSH_TRADENAME_FORMULATION_CONNECT,
    COSH_TRADENAME_MANUFACTURER_CONNECT,
    COSH_UNIT_TYPES_CORE,
    COSH_UNITS_DATA_CORE,
    PYTHON_L2_TO_COSH_UUID,
    UNIT_TYPE_SLUG_TO_COSH_UUIDS,
)
from app.services.cosh_options_view import (
    L2_TYPES_WITHOUT_TRADE_NAMES,
    l2_uses_trade_names,
    list_ai_concentrations,
    list_application_methods_for_l2,
    list_common_names_for_l2,
    list_formulations,
    list_incomplete_cosh_data_for_l2,
    list_itks,
    list_manufacturers_for_common_name,
    list_maturity_indices,
    list_planting_materials,
    list_trade_names_for_common_name,
    list_units_for_l2,
)
from tests.conftest import requires_docker


# ── Shared helpers ────────────────────────────────────────────────────────

def _core(cosh_id: str, core_type: str, name: str, status: str = "active") -> CoshCoreItem:
    return CoshCoreItem(
        cosh_id=cosh_id, core_type=core_type,
        translations={"en": name}, status=status,
    )


def _connect2(
    connect_id: str, connect_type: str,
    role_a: str, id_a: str, role_b: str, id_b: str,
) -> CoshConnectRow:
    return CoshConnectRow(
        connect_id=connect_id, connect_type=connect_type, status="active",
        endpoints=[
            {"role": role_a, "cosh_id": id_a, "position": 1},
            {"role": role_b, "cosh_id": id_b, "position": 2},
        ],
    )


def _connect3(
    connect_id: str, connect_type: str,
    role_a: str, id_a: str,
    role_b: str, id_b: str,
    role_c: str, id_c: str,
) -> CoshConnectRow:
    return CoshConnectRow(
        connect_id=connect_id, connect_type=connect_type, status="active",
        endpoints=[
            {"role": role_a, "cosh_id": id_a, "position": 1},
            {"role": role_b, "cosh_id": id_b, "position": 2},
            {"role": role_c, "cosh_id": id_c, "position": 3},
        ],
    )


CHEM_PEST_L2_UUID = PYTHON_L2_TO_COSH_UUID["CHEMICAL_PESTICIDES"]
BOTANICAL_L2_UUID = PYTHON_L2_TO_COSH_UUID["BOTANICAL_PESTICIDES"]
DOSAGE_UNIT_TYPE_UUID = UNIT_TYPE_SLUG_TO_COSH_UUIDS["dosage_unit"][0]
TIME_UNIT_TYPE_UUID = UNIT_TYPE_SLUG_TO_COSH_UUIDS["time_unit"][0]


# Batch 39D introduced a per-L2 completeness filter for the input
# cascade. Legacy tests in this file pre-date it and don't seed the
# required tradename_X rows. Clear the spec for every test in this
# module so the legacy assertions still pass; the new completeness
# tests at the bottom re-populate the dict locally and exercise the
# filter directly.
@pytest.fixture(autouse=True)
def _no_completeness_filter(monkeypatch):
    from app.services import cosh_options_view as _cov
    monkeypatch.setattr(_cov, "L2_COMPLETENESS_REQUIREMENTS", {})


# ── l2_uses_trade_names ───────────────────────────────────────────────────

def test_l2_uses_trade_names_excludes_npk_dosage_l2s():
    assert l2_uses_trade_names("CHEMICAL_PESTICIDES") is True
    assert l2_uses_trade_names("BIOFERTILIZERS") is True
    assert l2_uses_trade_names("CHEMICAL_FERTILIZERS_NPK_DOSAGES") is False
    assert l2_uses_trade_names("FERTIGATION_NPK_DOSAGES") is False


def test_npk_dosage_l2s_set_pins_two_entries():
    assert L2_TYPES_WITHOUT_TRADE_NAMES == {
        "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
        "FERTIGATION_NPK_DOSAGES",
    }


# ── list_common_names_for_l2 ──────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_common_names_filtered_by_l2(db):
    db.add(_core("cn:imidacloprid", COSH_COMMON_NAMES_CORE, "Imidacloprid"))
    db.add(_core("cn:azadirachtin", COSH_COMMON_NAMES_CORE, "Azadirachtin"))
    db.add(_core("cn:abamectin", COSH_COMMON_NAMES_CORE, "Abamectin"))
    db.add(_connect2(
        "cncn-1", COSH_COMMONNAMES_L2_CONNECT,
        COSH_COMMON_NAMES_CORE, "cn:imidacloprid",
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
    ))
    db.add(_connect2(
        "cncn-2", COSH_COMMONNAMES_L2_CONNECT,
        COSH_COMMON_NAMES_CORE, "cn:abamectin",
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
    ))
    db.add(_connect2(
        "cncn-3", COSH_COMMONNAMES_L2_CONNECT,
        COSH_COMMON_NAMES_CORE, "cn:azadirachtin",
        COSH_L2_DATA_CORE, BOTANICAL_L2_UUID,
    ))
    await db.commit()

    chem = await list_common_names_for_l2(db, "CHEMICAL_PESTICIDES")
    names = sorted(c["name"] for c in chem)
    assert names == ["Abamectin", "Imidacloprid"]

    bot = await list_common_names_for_l2(db, "BOTANICAL_PESTICIDES")
    assert [c["name"] for c in bot] == ["Azadirachtin"]


@requires_docker
@pytest.mark.asyncio
async def test_common_names_l2_unknown_returns_empty(db):
    out = await list_common_names_for_l2(db, "NOT_A_REAL_L2")
    assert out == []


# ── list_application_methods_for_l2 ───────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_application_methods_filtered_by_l2(db):
    db.add(_core("am:spray", COSH_APPLICATION_METHODS_CORE, "Foliar Spray"))
    db.add(_core("am:drench", COSH_APPLICATION_METHODS_CORE, "Soil Drench"))
    db.add(_core("am:seed", COSH_APPLICATION_METHODS_CORE, "Seed Treatment"))
    db.add(_connect2(
        "am-1", COSH_APPLICATION_METHODS_L2_CONNECT,
        COSH_APPLICATION_METHODS_CORE, "am:spray",
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
    ))
    db.add(_connect2(
        "am-2", COSH_APPLICATION_METHODS_L2_CONNECT,
        COSH_APPLICATION_METHODS_CORE, "am:drench",
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
    ))
    db.add(_connect2(
        "am-3", COSH_APPLICATION_METHODS_L2_CONNECT,
        COSH_APPLICATION_METHODS_CORE, "am:seed",
        COSH_L2_DATA_CORE, BOTANICAL_L2_UUID,
    ))
    await db.commit()

    chem = await list_application_methods_for_l2(db, "CHEMICAL_PESTICIDES")
    assert sorted(c["name"] for c in chem) == ["Foliar Spray", "Soil Drench"]


# ── list_units_for_l2 ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_units_filtered_by_l2_and_unit_type(db):
    db.add(_core("u:ml/l", COSH_UNITS_DATA_CORE, "ml/L"))
    db.add(_core("u:g/l", COSH_UNITS_DATA_CORE, "g/L"))
    db.add(_core("u:days", COSH_UNITS_DATA_CORE, "days"))
    db.add(_core(DOSAGE_UNIT_TYPE_UUID, COSH_UNIT_TYPES_CORE, "Dosage Unit"))
    db.add(_core(TIME_UNIT_TYPE_UUID, COSH_UNIT_TYPES_CORE, "Time Unit"))
    # ml/L and g/L are dosage units for CHEMICAL_PESTICIDES;
    # days is a time unit for CHEMICAL_PESTICIDES.
    db.add(_connect3(
        "lu-1", COSH_L2_UNITS_UNITTYPES_CONNECT,
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
        COSH_UNITS_DATA_CORE, "u:ml/l",
        COSH_UNIT_TYPES_CORE, DOSAGE_UNIT_TYPE_UUID,
    ))
    db.add(_connect3(
        "lu-2", COSH_L2_UNITS_UNITTYPES_CONNECT,
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
        COSH_UNITS_DATA_CORE, "u:g/l",
        COSH_UNIT_TYPES_CORE, DOSAGE_UNIT_TYPE_UUID,
    ))
    db.add(_connect3(
        "lu-3", COSH_L2_UNITS_UNITTYPES_CONNECT,
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
        COSH_UNITS_DATA_CORE, "u:days",
        COSH_UNIT_TYPES_CORE, TIME_UNIT_TYPE_UUID,
    ))
    await db.commit()

    dosage = await list_units_for_l2(db, "CHEMICAL_PESTICIDES", "dosage_unit")
    assert sorted(d["name"] for d in dosage) == ["g/L", "ml/L"]

    time = await list_units_for_l2(db, "CHEMICAL_PESTICIDES", "time_unit")
    assert [t["name"] for t in time] == ["days"]


@requires_docker
@pytest.mark.asyncio
async def test_units_unknown_unit_type_returns_empty(db):
    out = await list_units_for_l2(db, "CHEMICAL_PESTICIDES", "not_a_slug")
    assert out == []


# Batch 38 regression — Cosh categorises CHEMICAL_FERTILIZERS_NPK_DOSAGES
# units (g/plant, kg/acre, kg/plant) under a generic "Unit" unit_type
# rather than one of the "Dosage Unit" variants. The dosage_unit slug
# allow-list now includes that generic type so the connect walk
# surfaces those rows; this test pins that contract in place.
@requires_docker
@pytest.mark.asyncio
async def test_npk_dosage_generic_unit_type_surfaces_under_dosage_unit_slug(db):
    npk_l2_uuid = PYTHON_L2_TO_COSH_UUID["CHEMICAL_FERTILIZERS_NPK_DOSAGES"]
    generic_unit_type_uuid = "11a14b5b-1bc9-4d15-8c9a-af1f7310578c"
    # Sanity: the slug allow-list must include the generic Unit type,
    # otherwise this fix has regressed.
    assert generic_unit_type_uuid in UNIT_TYPE_SLUG_TO_COSH_UUIDS["dosage_unit"]

    db.add(_core("u:g/plant", COSH_UNITS_DATA_CORE, "g/plant"))
    db.add(_core("u:kg/acre", COSH_UNITS_DATA_CORE, "kg/acre"))
    db.add(_core("u:kg/plant", COSH_UNITS_DATA_CORE, "kg/plant"))
    db.add(_core(generic_unit_type_uuid, COSH_UNIT_TYPES_CORE, "Unit"))
    for cid, uid in (
        ("npku-1", "u:g/plant"),
        ("npku-2", "u:kg/acre"),
        ("npku-3", "u:kg/plant"),
    ):
        db.add(_connect3(
            cid, COSH_L2_UNITS_UNITTYPES_CONNECT,
            COSH_L2_DATA_CORE, npk_l2_uuid,
            COSH_UNITS_DATA_CORE, uid,
            COSH_UNIT_TYPES_CORE, generic_unit_type_uuid,
        ))
    await db.commit()

    out = await list_units_for_l2(
        db, "CHEMICAL_FERTILIZERS_NPK_DOSAGES", "dosage_unit",
    )
    assert sorted(u["name"] for u in out) == ["g/plant", "kg/acre", "kg/plant"]


# ── Trade names / manufacturers / formulations / a.i. ────────────────────

async def _seed_brand_cascade_data(db):
    """Two common_names; first has two trade_names + two manufacturers
    + formulations + a.i.; second has one trade name."""
    db.add(_core("cn:imidacloprid", COSH_COMMON_NAMES_CORE, "Imidacloprid"))
    db.add(_core("cn:abamectin", COSH_COMMON_NAMES_CORE, "Abamectin"))

    db.add(_core("tn:confidor", COSH_TRADE_NAMES_CORE, "Confidor"))
    db.add(_core("tn:tatamida", COSH_TRADE_NAMES_CORE, "Tatamida"))
    db.add(_core("tn:vertimec", COSH_TRADE_NAMES_CORE, "Vertimec"))

    db.add(_core("mfr:bayer", COSH_INPUT_MANUFACTURERS_CORE, "Bayer"))
    db.add(_core("mfr:tata", COSH_INPUT_MANUFACTURERS_CORE, "Tata Rallis"))
    db.add(_core("mfr:syngenta", COSH_INPUT_MANUFACTURERS_CORE, "Syngenta"))

    db.add(_core("f:sc", COSH_FORMULATIONS_CORE, "SC"))
    db.add(_core("f:wp", COSH_FORMULATIONS_CORE, "WP"))
    db.add(_core("f:ec", COSH_FORMULATIONS_CORE, "EC"))

    db.add(_core("ai:17.8", COSH_AI_CORE, "17.8%"))
    db.add(_core("ai:30.5", COSH_AI_CORE, "30.5%"))
    db.add(_core("ai:1.9", COSH_AI_CORE, "1.9%"))

    # Trade ↔ Common Name
    db.add(_connect2(
        "tn-cn-1", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_COMMON_NAMES_CORE, "cn:imidacloprid",
    ))
    db.add(_connect2(
        "tn-cn-2", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:tatamida",
        COSH_COMMON_NAMES_CORE, "cn:imidacloprid",
    ))
    db.add(_connect2(
        "tn-cn-3", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:vertimec",
        COSH_COMMON_NAMES_CORE, "cn:abamectin",
    ))

    # Trade ↔ Manufacturer
    db.add(_connect2(
        "tn-mfr-1", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:bayer",
    ))
    db.add(_connect2(
        "tn-mfr-2", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:tatamida",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:tata",
    ))
    db.add(_connect2(
        "tn-mfr-3", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:vertimec",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:syngenta",
    ))

    # Trade ↔ Formulation
    db.add(_connect2(
        "tn-f-1", COSH_TRADENAME_FORMULATION_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_FORMULATIONS_CORE, "f:sc",
    ))
    db.add(_connect2(
        "tn-f-2", COSH_TRADENAME_FORMULATION_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:tatamida",
        COSH_FORMULATIONS_CORE, "f:wp",
    ))
    db.add(_connect2(
        "tn-f-3", COSH_TRADENAME_FORMULATION_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:vertimec",
        COSH_FORMULATIONS_CORE, "f:ec",
    ))

    # Trade ↔ a.i.
    db.add(_connect2(
        "tn-ai-1", COSH_TRADENAME_AI_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_AI_CORE, "ai:17.8",
    ))
    db.add(_connect2(
        "tn-ai-2", COSH_TRADENAME_AI_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:tatamida",
        COSH_AI_CORE, "ai:30.5",
    ))
    db.add(_connect2(
        "tn-ai-3", COSH_TRADENAME_AI_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:vertimec",
        COSH_AI_CORE, "ai:1.9",
    ))
    await db.commit()


@requires_docker
@pytest.mark.asyncio
async def test_trade_names_filtered_by_common_name(db):
    await _seed_brand_cascade_data(db)
    out = await list_trade_names_for_common_name(db, "cn:imidacloprid")
    assert sorted(t["name"] for t in out) == ["Confidor", "Tatamida"]


@requires_docker
@pytest.mark.asyncio
async def test_manufacturers_walk_common_name_to_trade_names(db):
    await _seed_brand_cascade_data(db)
    out = await list_manufacturers_for_common_name(db, "cn:imidacloprid")
    assert sorted(m["name"] for m in out) == ["Bayer", "Tata Rallis"]
    # cn:abamectin → vertimec → syngenta only
    out2 = await list_manufacturers_for_common_name(db, "cn:abamectin")
    assert [m["name"] for m in out2] == ["Syngenta"]


# ── Bidirectional MFR ↔ TN filtering (Batch 24) ───────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_trade_names_narrowed_by_manufacturer(db):
    """When MFR is supplied, trade-names list shrinks to that MFR's
    brands (intersected with CN's set)."""
    await _seed_brand_cascade_data(db)
    # Imidacloprid has Confidor (Bayer) + Tatamida (Tata). Filter by Bayer
    # → only Confidor.
    bayer = await list_trade_names_for_common_name(
        db, "cn:imidacloprid", manufacturer_cosh_id="mfr:bayer",
    )
    assert [t["name"] for t in bayer] == ["Confidor"]
    # Filter by Tata → only Tatamida.
    tata = await list_trade_names_for_common_name(
        db, "cn:imidacloprid", manufacturer_cosh_id="mfr:tata",
    )
    assert [t["name"] for t in tata] == ["Tatamida"]
    # Filter by an MFR that doesn't make any of CN's brands → empty.
    syngenta = await list_trade_names_for_common_name(
        db, "cn:imidacloprid", manufacturer_cosh_id="mfr:syngenta",
    )
    assert syngenta == []


@requires_docker
@pytest.mark.asyncio
async def test_manufacturers_narrowed_by_trade_name(db):
    """When TN is supplied, manufacturers list shrinks to the single
    maker of that brand."""
    await _seed_brand_cascade_data(db)
    bayer_only = await list_manufacturers_for_common_name(
        db, "cn:imidacloprid", trade_name_cosh_id="tn:confidor",
    )
    assert [m["name"] for m in bayer_only] == ["Bayer"]
    tata_only = await list_manufacturers_for_common_name(
        db, "cn:imidacloprid", trade_name_cosh_id="tn:tatamida",
    )
    assert [m["name"] for m in tata_only] == ["Tata Rallis"]


@requires_docker
@pytest.mark.asyncio
async def test_manufacturers_with_mismatched_trade_name_returns_empty(db):
    """Defence: if caller passes a TN that isn't in CN's brand set
    (e.g. stale form state), we return empty rather than leaking a
    cross-CN manufacturer."""
    await _seed_brand_cascade_data(db)
    # tn:vertimec is abamectin's brand, not imidacloprid's. Asking
    # for imidacloprid manufacturers with vertimec filter → empty.
    out = await list_manufacturers_for_common_name(
        db, "cn:imidacloprid", trade_name_cosh_id="tn:vertimec",
    )
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_unfiltered_calls_unchanged_after_optional_params(db):
    """Backward-compat: callers that pass only `common_name` (no
    cross-filter) get the same Batch 19 behaviour."""
    await _seed_brand_cascade_data(db)
    tns = await list_trade_names_for_common_name(db, "cn:imidacloprid")
    assert sorted(t["name"] for t in tns) == ["Confidor", "Tatamida"]
    mfrs = await list_manufacturers_for_common_name(db, "cn:imidacloprid")
    assert sorted(m["name"] for m in mfrs) == ["Bayer", "Tata Rallis"]


@requires_docker
@pytest.mark.asyncio
async def test_formulations_span_common_name_when_no_trade_name(db):
    await _seed_brand_cascade_data(db)
    # cn:imidacloprid → both Confidor (SC) and Tatamida (WP) → SC + WP
    out = await list_formulations(db, common_name_cosh_id="cn:imidacloprid")
    assert sorted(f["name"] for f in out) == ["SC", "WP"]


@requires_docker
@pytest.mark.asyncio
async def test_formulations_narrowed_when_trade_name_supplied(db):
    await _seed_brand_cascade_data(db)
    out = await list_formulations(
        db, common_name_cosh_id="cn:imidacloprid",
        trade_name_cosh_id="tn:confidor",
    )
    assert [f["name"] for f in out] == ["SC"]


@requires_docker
@pytest.mark.asyncio
async def test_ai_concentrations_narrowed_when_trade_name_supplied(db):
    await _seed_brand_cascade_data(db)
    # Without trade name: 17.8% + 30.5%
    spanning = await list_ai_concentrations(
        db, common_name_cosh_id="cn:imidacloprid",
    )
    assert sorted(a["name"] for a in spanning) == ["17.8%", "30.5%"]
    # With trade name: just 17.8%
    narrowed = await list_ai_concentrations(
        db, common_name_cosh_id="cn:imidacloprid",
        trade_name_cosh_id="tn:confidor",
    )
    assert [a["name"] for a in narrowed] == ["17.8%"]


@requires_docker
@pytest.mark.asyncio
async def test_formulations_empty_when_no_inputs(db):
    await _seed_brand_cascade_data(db)
    out = await list_formulations(db)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_trade_name_only_resolves_to_single_filter(db):
    """When the modal passes trade_name without common_name (corner
    case, but valid wire shape), narrow to that trade name."""
    await _seed_brand_cascade_data(db)
    out = await list_formulations(db, trade_name_cosh_id="tn:tatamida")
    assert [f["name"] for f in out] == ["WP"]


@requires_docker
@pytest.mark.asyncio
async def test_inactive_core_items_are_excluded(db):
    """Cosh-side lifecycle: a core item with status != 'active' must
    not surface in the cascade."""
    db.add(_core("cn:imidacloprid", COSH_COMMON_NAMES_CORE, "Imidacloprid"))
    db.add(_core("cn:retired", COSH_COMMON_NAMES_CORE, "Old Chem", status="inactive"))
    db.add(_connect2(
        "cncn-1", COSH_COMMONNAMES_L2_CONNECT,
        COSH_COMMON_NAMES_CORE, "cn:imidacloprid",
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
    ))
    db.add(_connect2(
        "cncn-2", COSH_COMMONNAMES_L2_CONNECT,
        COSH_COMMON_NAMES_CORE, "cn:retired",
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
    ))
    await db.commit()

    out = await list_common_names_for_l2(db, "CHEMICAL_PESTICIDES")
    assert [c["name"] for c in out] == ["Imidacloprid"]


# ── Batch 39D — per-L2 completeness filter ─────────────────────────────────


def _restore_completeness_spec(monkeypatch, spec: dict[str, frozenset[str]]):
    """Re-populate the completeness spec for tests that exercise the
    filter. The module-level autouse fixture cleared it; here we put
    a narrowly-scoped spec back."""
    from app.services import cosh_options_view as _cov
    monkeypatch.setattr(_cov, "L2_COMPLETENESS_REQUIREMENTS", spec)


@requires_docker
@pytest.mark.asyncio
async def test_completeness_filter_chemical_pesticides_drops_incomplete_cns(db, monkeypatch):
    """CHEMICAL_PESTICIDES requires M + F + AI per Cosh connect. CNs
    whose only TN is missing any required connect must NOT surface."""
    _restore_completeness_spec(monkeypatch, {
        "CHEMICAL_PESTICIDES": frozenset({"manufacturer", "formulation", "ai"}),
    })
    # Cosh data:
    # - "Imidacloprid" has TN "Confidor" with M + F + AI all linked → complete.
    # - "Acephate" has TN "Acetox" with M only (no F, no AI)        → incomplete.
    db.add(_core("cn:imid", COSH_COMMON_NAMES_CORE, "Imidacloprid"))
    db.add(_core("cn:acephate", COSH_COMMON_NAMES_CORE, "Acephate"))
    db.add(_core("tn:confidor", COSH_TRADE_NAMES_CORE, "Confidor"))
    db.add(_core("tn:acetox", COSH_TRADE_NAMES_CORE, "Acetox"))
    db.add(_core("mfr:bayer", COSH_INPUT_MANUFACTURERS_CORE, "Bayer"))
    db.add(_core("mfr:upl", COSH_INPUT_MANUFACTURERS_CORE, "UPL"))
    db.add(_core("f:wp", COSH_FORMULATIONS_CORE, "WP"))
    db.add(_core("ai:17.8%", COSH_AI_CORE, "17.8%"))

    # Both CNs are linked to the L2.
    for cn in ("cn:imid", "cn:acephate"):
        db.add(_connect2(
            f"cncn-{cn}", COSH_COMMONNAMES_L2_CONNECT,
            COSH_COMMON_NAMES_CORE, cn,
            COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
        ))

    # tradename_commonname links.
    db.add(_connect2(
        "tncn-1", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_COMMON_NAMES_CORE, "cn:imid",
    ))
    db.add(_connect2(
        "tncn-2", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:acetox",
        COSH_COMMON_NAMES_CORE, "cn:acephate",
    ))

    # Confidor: complete (M + F + AI). Acetox: only M.
    db.add(_connect2(
        "tnmfr-1", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:bayer",
    ))
    db.add(_connect2(
        "tnmfr-2", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:acetox",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:upl",
    ))
    db.add(_connect2(
        "tnf-1", COSH_TRADENAME_FORMULATION_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_FORMULATIONS_CORE, "f:wp",
    ))
    db.add(_connect2(
        "tnai-1", COSH_TRADENAME_AI_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_AI_CORE, "ai:17.8%",
    ))
    await db.commit()

    out = await list_common_names_for_l2(db, "CHEMICAL_PESTICIDES")
    assert [c["name"] for c in out] == ["Imidacloprid"]


@requires_docker
@pytest.mark.asyncio
async def test_completeness_filter_microbial_only_needs_manufacturer(db, monkeypatch):
    """MICROBIAL_PESTICIDES requires only the manufacturer connect.
    A CN with a TN linked to a MFR (no F, no AI) passes the filter."""
    _restore_completeness_spec(monkeypatch, {
        "MICROBIAL_PESTICIDES": frozenset({"manufacturer"}),
    })
    MICROBIAL_L2_UUID = PYTHON_L2_TO_COSH_UUID["MICROBIAL_PESTICIDES"]
    db.add(_core("cn:beauv", COSH_COMMON_NAMES_CORE, "Beauveria bassiana"))
    db.add(_core("tn:biosoft", COSH_TRADE_NAMES_CORE, "Biosoft"))
    db.add(_core("mfr:multiplex", COSH_INPUT_MANUFACTURERS_CORE, "Multiplex"))
    db.add(_connect2(
        "cncn-1", COSH_COMMONNAMES_L2_CONNECT,
        COSH_COMMON_NAMES_CORE, "cn:beauv",
        COSH_L2_DATA_CORE, MICROBIAL_L2_UUID,
    ))
    db.add(_connect2(
        "tncn-1", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:biosoft",
        COSH_COMMON_NAMES_CORE, "cn:beauv",
    ))
    db.add(_connect2(
        "tnmfr-1", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:biosoft",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:multiplex",
    ))
    await db.commit()

    out = await list_common_names_for_l2(db, "MICROBIAL_PESTICIDES")
    assert [c["name"] for c in out] == ["Beauveria bassiana"]


@requires_docker
@pytest.mark.asyncio
async def test_completeness_filter_cascades_to_trade_names(db, monkeypatch):
    """When the SE picks a CN, the trade-name dropdown also hides
    incomplete TNs. Sibling TN under the same CN that's missing
    AI must not surface for CHEMICAL_PESTICIDES."""
    _restore_completeness_spec(monkeypatch, {
        "CHEMICAL_PESTICIDES": frozenset({"manufacturer", "formulation", "ai"}),
    })
    db.add(_core("cn:imid", COSH_COMMON_NAMES_CORE, "Imidacloprid"))
    db.add(_core("tn:confidor", COSH_TRADE_NAMES_CORE, "Confidor"))
    db.add(_core("tn:half", COSH_TRADE_NAMES_CORE, "HalfBrand"))
    db.add(_core("mfr:bayer", COSH_INPUT_MANUFACTURERS_CORE, "Bayer"))
    db.add(_core("mfr:upl", COSH_INPUT_MANUFACTURERS_CORE, "UPL"))
    db.add(_core("f:wp", COSH_FORMULATIONS_CORE, "WP"))
    db.add(_core("ai:17.8%", COSH_AI_CORE, "17.8%"))

    db.add(_connect2(
        "cncn-1", COSH_COMMONNAMES_L2_CONNECT,
        COSH_COMMON_NAMES_CORE, "cn:imid",
        COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
    ))
    # Both TNs claim the CN.
    for tn in ("tn:confidor", "tn:half"):
        db.add(_connect2(
            f"tncn-{tn}", COSH_TRADENAME_COMMONNAME_CONNECT,
            COSH_TRADE_NAMES_CORE, tn,
            COSH_COMMON_NAMES_CORE, "cn:imid",
        ))
    # Both have M + F. Only Confidor has AI.
    for tn, mfr in (("tn:confidor", "mfr:bayer"), ("tn:half", "mfr:upl")):
        db.add(_connect2(
            f"tnmfr-{tn}", COSH_TRADENAME_MANUFACTURER_CONNECT,
            COSH_TRADE_NAMES_CORE, tn,
            COSH_INPUT_MANUFACTURERS_CORE, mfr,
        ))
        db.add(_connect2(
            f"tnf-{tn}", COSH_TRADENAME_FORMULATION_CONNECT,
            COSH_TRADE_NAMES_CORE, tn,
            COSH_FORMULATIONS_CORE, "f:wp",
        ))
    db.add(_connect2(
        "tnai-1", COSH_TRADENAME_AI_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_AI_CORE, "ai:17.8%",
    ))
    await db.commit()

    out = await list_trade_names_for_common_name(
        db, "cn:imid", l2_type="CHEMICAL_PESTICIDES",
    )
    assert [t["name"] for t in out] == ["Confidor"]


@requires_docker
@pytest.mark.asyncio
async def test_completeness_filter_omitted_l2_disables_filter(db, monkeypatch):
    """If the caller doesn't supply l2_type, no filter applies — the
    pre-39D behaviour is preserved for legacy callers."""
    _restore_completeness_spec(monkeypatch, {
        "CHEMICAL_PESTICIDES": frozenset({"manufacturer", "formulation", "ai"}),
    })
    db.add(_core("cn:imid", COSH_COMMON_NAMES_CORE, "Imidacloprid"))
    db.add(_core("tn:half", COSH_TRADE_NAMES_CORE, "HalfBrand"))
    db.add(_connect2(
        "tncn-1", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:half",
        COSH_COMMON_NAMES_CORE, "cn:imid",
    ))
    await db.commit()

    # No l2_type → no filter. HalfBrand surfaces even without M/F/AI.
    out = await list_trade_names_for_common_name(db, "cn:imid")
    assert [t["name"] for t in out] == ["HalfBrand"]


# ── Batch 39D-report — incomplete-report endpoint ──────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_incomplete_report_marks_missing_connects(db, monkeypatch):
    """The report lists every CN under the L2 with each TN's set of
    missing connects, plus per-CN summary flags has_complete_tn and
    no_trade_names. SA uses this to spot Cosh-side gaps."""
    _restore_completeness_spec(monkeypatch, {
        "CHEMICAL_PESTICIDES": frozenset({"manufacturer", "formulation", "ai"}),
    })
    db.add(_core("cn:imid", COSH_COMMON_NAMES_CORE, "Imidacloprid"))
    db.add(_core("cn:acephate", COSH_COMMON_NAMES_CORE, "Acephate"))
    db.add(_core("cn:orphan", COSH_COMMON_NAMES_CORE, "Orphan CN"))
    db.add(_core("tn:confidor", COSH_TRADE_NAMES_CORE, "Confidor"))
    db.add(_core("tn:half", COSH_TRADE_NAMES_CORE, "HalfBrand"))
    db.add(_core("mfr:bayer", COSH_INPUT_MANUFACTURERS_CORE, "Bayer"))
    db.add(_core("mfr:upl", COSH_INPUT_MANUFACTURERS_CORE, "UPL"))
    db.add(_core("f:wp", COSH_FORMULATIONS_CORE, "WP"))
    db.add(_core("ai:17.8%", COSH_AI_CORE, "17.8%"))
    for cn in ("cn:imid", "cn:acephate", "cn:orphan"):
        db.add(_connect2(
            f"cncn-{cn}", COSH_COMMONNAMES_L2_CONNECT,
            COSH_COMMON_NAMES_CORE, cn,
            COSH_L2_DATA_CORE, CHEM_PEST_L2_UUID,
        ))
    # Confidor has full M+F+AI for cn:imid. HalfBrand only has M for
    # cn:acephate. cn:orphan has no TN at all.
    db.add(_connect2(
        "tncn-1", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_COMMON_NAMES_CORE, "cn:imid",
    ))
    db.add(_connect2(
        "tncn-2", COSH_TRADENAME_COMMONNAME_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:half",
        COSH_COMMON_NAMES_CORE, "cn:acephate",
    ))
    db.add(_connect2(
        "tnmfr-1", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:bayer",
    ))
    db.add(_connect2(
        "tnmfr-2", COSH_TRADENAME_MANUFACTURER_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:half",
        COSH_INPUT_MANUFACTURERS_CORE, "mfr:upl",
    ))
    db.add(_connect2(
        "tnf-1", COSH_TRADENAME_FORMULATION_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_FORMULATIONS_CORE, "f:wp",
    ))
    db.add(_connect2(
        "tnai-1", COSH_TRADENAME_AI_CONNECT,
        COSH_TRADE_NAMES_CORE, "tn:confidor",
        COSH_AI_CORE, "ai:17.8%",
    ))
    await db.commit()

    report = await list_incomplete_cosh_data_for_l2(db, "CHEMICAL_PESTICIDES")
    assert report["applicable"] is True
    assert report["required"] == ["ai", "formulation", "manufacturer"]
    cns = {cn["name"]: cn for cn in report["common_names"]}
    assert set(cns.keys()) == {"Acephate", "Imidacloprid", "Orphan CN"}
    # Imidacloprid → Confidor complete
    assert cns["Imidacloprid"]["has_complete_tn"] is True
    assert cns["Imidacloprid"]["no_trade_names"] is False
    confidor = next(t for t in cns["Imidacloprid"]["trade_names"] if t["name"] == "Confidor")
    assert confidor["missing"] == []
    # Acephate → HalfBrand missing F+AI
    assert cns["Acephate"]["has_complete_tn"] is False
    half = next(t for t in cns["Acephate"]["trade_names"] if t["name"] == "HalfBrand")
    assert half["missing"] == ["ai", "formulation"]
    # Orphan CN → no TN at all
    assert cns["Orphan CN"]["no_trade_names"] is True
    assert cns["Orphan CN"]["trade_names"] == []


@requires_docker
@pytest.mark.asyncio
async def test_incomplete_report_l2_not_in_spec_returns_applicable_false(db):
    """L2 with no completeness spec (e.g. NPK Dosages) returns
    applicable=False so the UI can label it 'no filter for this L2'."""
    report = await list_incomplete_cosh_data_for_l2(
        db, "CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    )
    assert report["applicable"] is False
    assert report["required"] == []
    assert report["common_names"] == []


# ── Non-input Cores (2026-05-16) ──────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_planting_materials_returns_all_active(db):
    """Flat lookup, no L2 filter — returns every active row sorted by
    English name. Inactive rows are excluded."""
    db.add(_core("pm-1", "planting_material", "Seedlings"))
    db.add(_core("pm-2", "planting_material", "Cuttings"))
    db.add(_core("pm-3", "planting_material", "Old", status="inactive"))
    db.add(_core("other", "common_names_of_inputs", "Imidacloprid"))
    await db.commit()
    out = await list_planting_materials(db)
    assert [o["name"] for o in out] == ["Cuttings", "Seedlings"]


@requires_docker
@pytest.mark.asyncio
async def test_list_itks_reads_itk_data_core(db):
    """Rule-book slug `itk_name` maps to the real Cosh core_type
    `itk_data`. The endpoint serves rows of `itk_data`."""
    db.add(_core("itk-1", "itk_data", "Neem leaf extract"))
    db.add(_core("itk-2", "itk_data", "Ash dusting"))
    db.add(_core("itk-stale", "itk_data", "Removed", status="inactive"))
    await db.commit()
    out = await list_itks(db)
    assert [o["name"] for o in out] == ["Ash dusting", "Neem leaf extract"]


@requires_docker
@pytest.mark.asyncio
async def test_list_maturity_indices_returns_all_active(db):
    db.add(_core("mi-1", "maturity_index", "Colour change"))
    db.add(_core("mi-2", "maturity_index", "Brix value"))
    await db.commit()
    out = await list_maturity_indices(db)
    assert [o["name"] for o in out] == ["Brix value", "Colour change"]
