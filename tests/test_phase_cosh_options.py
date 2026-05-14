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
    list_manufacturers_for_common_name,
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
