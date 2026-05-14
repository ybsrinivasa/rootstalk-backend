"""CCA Step 4 / Batch 4C-i.D — DB-backed wiring tests.

Pure-function validator coverage lives in
`tests/test_l2_element_validator.py` (mocked cascade lookups).

This file drives `create_practice` and `create_global_practice` end to
end against the testcontainer DB, seeding real cosh_core_items rows
so the cascade service walks live data, to verify:
  • a valid element list creates the Practice + Elements (201)
  • each rule violation surfaces as a 422 with stable error codes
  • frequency_days persists onto the Practice row
  • l2_type=None bypasses the validator (defensive)
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import Element, Practice, PracticeL0, TimelineFromType
from app.modules.advisory.router import create_practice
from app.modules.advisory.schemas import ElementIn, PracticeCreate
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_crop_reference, make_package, make_timeline, make_user,
)


# ── Cosh seeding helpers ────────────────────────────────────────────────────

async def _seed_pesticide_cosh(db) -> None:
    """Seed cosh_core_items rows the cascade service walks for a
    Chemical Pesticide happy-path validation."""
    rows = [
        # Cores referenced by cosh_core: sources
        CoshCoreItem(cosh_id="cn:imida", core_type="common_name",
                     translations={"en": "Imidacloprid"}, status="active"),
        CoshCoreItem(cosh_id="am:foliar_spray", core_type="application_method",
                     translations={"en": "Foliar spray"}, status="active"),
        CoshCoreItem(cosh_id="du:ml_per_l", core_type="dosage_unit",
                     translations={"en": "ml/L"}, status="active"),
        # Brand row: parent=common_name, manufacturer in metadata
        CoshCoreItem(
            cosh_id="brand:confidor", core_type="brand",
            parent_cosh_id="cn:imida",
            translations={"en": "Confidor"},
            metadata_={
                "manufacturer_name": "Bayer",
                "formulation_cosh_id": "form:SC",
                "ai_concentration": "17.8% SL",
            },
            status="active",
        ),
        CoshCoreItem(cosh_id="form:SC", core_type="formulation",
                     translations={"en": "SC"}, status="active"),
    ]
    for r in rows:
        db.add(r)
    await db.flush()


async def _setup_timeline(db) -> tuple:
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await make_crop_reference(db, "crop:test", name="Test Crop")
    pkg = await make_package(db, client, name="P", crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, name="TL", from_type=TimelineFromType.DAS,
                             from_value=0, to_value=15)
    return client, user, pkg, tl


# ── Happy paths ─────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_chemical_pesticide_practice_passes(db):
    """Full Chemical Pesticide element list satisfies the rule book."""
    client, user, pkg, tl = await _setup_timeline(db)
    await _seed_pesticide_cosh(db)
    await db.commit()

    out = await create_practice(
        client_id=client.id, timeline_id=tl.id,
        request=PracticeCreate(
            l0_type=PracticeL0.INPUT,
            l1_type="PESTICIDE",
            l2_type="CHEMICAL_PESTICIDES",
            display_order=1,
            is_special_input=False,
            elements=[
                ElementIn(element_type="COMMON_NAME", cosh_ref="cn:imida"),
                ElementIn(element_type="MANUFACTURER", cosh_ref="Bayer"),
                ElementIn(element_type="BRAND_NAME", cosh_ref="brand:confidor"),
                ElementIn(element_type="FORMULATION", cosh_ref="form:SC"),
                ElementIn(element_type="AI_CONCENTRATION", cosh_ref="17.8% SL"),
                ElementIn(element_type="APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
                ElementIn(element_type="DOSAGE", value="0.5"),
                ElementIn(element_type="DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
            ],
        ),
        db=db, current_user=user,
    )
    assert out.l2_type == "CHEMICAL_PESTICIDES"
    elements = (await db.execute(
        select(Element).where(Element.practice_id == out.id)
    )).scalars().all()
    assert len(elements) == 8


@requires_docker
@pytest.mark.asyncio
async def test_create_instructions_only_post_harvest_practice_passes(db):
    """POST_HARVEST_DRYING — single mandatory INSTRUCTIONS field."""
    client, user, pkg, tl = await _setup_timeline(db)
    await db.commit()

    out = await create_practice(
        client_id=client.id, timeline_id=tl.id,
        request=PracticeCreate(
            l0_type=PracticeL0.NON_INPUT,
            l1_type="POST_HARVEST",
            l2_type="POST_HARVEST_DRYING",
            elements=[
                ElementIn(element_type="INSTRUCTIONS",
                          value="Sun-dry for 3 days, turn twice daily."),
            ],
        ),
        db=db, current_user=user,
    )
    assert out.l2_type == "POST_HARVEST_DRYING"


@requires_docker
@pytest.mark.asyncio
async def test_l2_type_none_bypasses_validation(db):
    """Defensive: practices without an L2 type (legacy / l0-only) skip
    the rule book validation."""
    client, user, pkg, tl = await _setup_timeline(db)
    await db.commit()

    out = await create_practice(
        client_id=client.id, timeline_id=tl.id,
        request=PracticeCreate(
            l0_type=PracticeL0.INPUT, l1_type=None, l2_type=None,
            elements=[],
        ),
        db=db, current_user=user,
    )
    assert out.l2_type is None


# ── Failure shapes — verify 422 envelope + stable error codes ───────────────

@requires_docker
@pytest.mark.asyncio
async def test_missing_mandatory_returns_422_with_error_list(db):
    client, user, pkg, tl = await _setup_timeline(db)
    await _seed_pesticide_cosh(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_practice(
            client_id=client.id, timeline_id=tl.id,
            request=PracticeCreate(
                l0_type=PracticeL0.INPUT, l1_type="PESTICIDE",
                l2_type="CHEMICAL_PESTICIDES",
                elements=[],  # all mandatory fields missing
            ),
            db=db, current_user=user,
        )

    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert detail["code"] == "l2_elements_validation_failed"
    error_codes = {e["code"] for e in detail["errors"]}
    assert "MISSING_MANDATORY" in error_codes
    missing_fields = {e["field_name"] for e in detail["errors"]
                      if e["code"] == "MISSING_MANDATORY"}
    assert {"COMMON_NAME", "APPLICATION_METHOD", "DOSAGE", "DOSAGE_UNIT"} <= missing_fields


@requires_docker
@pytest.mark.asyncio
async def test_invalid_manufacturer_returns_cascade_violation(db):
    """SE picks a manufacturer that's not in Cosh's cascade output for
    the chosen common name."""
    client, user, pkg, tl = await _setup_timeline(db)
    await _seed_pesticide_cosh(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_practice(
            client_id=client.id, timeline_id=tl.id,
            request=PracticeCreate(
                l0_type=PracticeL0.INPUT, l1_type="PESTICIDE",
                l2_type="CHEMICAL_PESTICIDES",
                elements=[
                    ElementIn(element_type="COMMON_NAME", cosh_ref="cn:imida"),
                    ElementIn(element_type="MANUFACTURER", cosh_ref="UnknownVendor"),
                    ElementIn(element_type="APPLICATION_METHOD", cosh_ref="am:foliar_spray"),
                    ElementIn(element_type="DOSAGE", value="0.5"),
                    ElementIn(element_type="DOSAGE_UNIT", cosh_ref="du:ml_per_l"),
                ],
            ),
            db=db, current_user=user,
        )

    assert exc.value.status_code == 422
    detail = exc.value.detail
    error_for_mfr = next(e for e in detail["errors"]
                        if e["field_name"] == "MANUFACTURER")
    assert error_for_mfr["code"] == "CASCADE_VIOLATION"
    assert error_for_mfr["details"]["cascade"] == "manufacturers_for_common_name"


@requires_docker
@pytest.mark.asyncio
async def test_special_input_required_for_adjuvants(db):
    """ADJUVANTS L2 with is_special_input=False is rejected."""
    client, user, pkg, tl = await _setup_timeline(db)
    db.add_all([
        CoshCoreItem(cosh_id="cn:silwet", core_type="common_name",
                           translations={"en": "Silwet"}, status="active"),
        CoshCoreItem(cosh_id="am:foliar", core_type="application_method",
                           translations={"en": "Foliar"}, status="active"),
        CoshCoreItem(cosh_id="du:ml", core_type="dosage_unit",
                           translations={"en": "ml/L"}, status="active"),
    ])
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_practice(
            client_id=client.id, timeline_id=tl.id,
            request=PracticeCreate(
                l0_type=PracticeL0.INPUT, l1_type="SPECIAL_INPUT",
                l2_type="ADJUVANTS",
                is_special_input=False,  # invariant violation
                elements=[
                    ElementIn(element_type="COMMON_NAME", cosh_ref="cn:silwet"),
                    ElementIn(element_type="APPLICATION_METHOD", cosh_ref="am:foliar"),
                    ElementIn(element_type="DOSAGE", value="0.05"),
                    ElementIn(element_type="DOSAGE_UNIT", cosh_ref="du:ml"),
                ],
            ),
            db=db, current_user=user,
        )

    detail = exc.value.detail
    error_codes = {e["code"] for e in detail["errors"]}
    assert "SPECIAL_INPUT_REQUIRED" in error_codes


@requires_docker
@pytest.mark.asyncio
async def test_frequency_days_persists_and_validates(db):
    """FERTIGATION_NPK_DOSAGES with matching frequency_days = FERTIGATION_INTERVAL
    saves successfully and persists the frequency_days column."""
    client, user, pkg, tl = await _setup_timeline(db)
    db.add_all([
        CoshCoreItem(cosh_id="du:kg_acre", core_type="dosage_unit",
                           translations={"en": "kg/acre"}, status="active"),
        CoshCoreItem(cosh_id="form:water_soluble", core_type="formulation",
                           translations={"en": "Water soluble"}, status="active"),
        CoshCoreItem(cosh_id="am:fertigation", core_type="application_method",
                           translations={"en": "Fertigation"}, status="active"),
    ])
    await db.commit()

    out = await create_practice(
        client_id=client.id, timeline_id=tl.id,
        request=PracticeCreate(
            l0_type=PracticeL0.INPUT, l1_type="FERTILIZER",
            l2_type="FERTIGATION_NPK_DOSAGES",
            frequency_days=7,
            elements=[
                ElementIn(element_type="N_DOSAGE", value="100"),
                ElementIn(element_type="P_DOSAGE", value="50"),
                ElementIn(element_type="K_DOSAGE", value="50"),
                ElementIn(element_type="UNIT", cosh_ref="du:kg_acre"),
                ElementIn(element_type="FORMULATION", cosh_ref="form:water_soluble"),
                ElementIn(element_type="APPLICATION_METHOD", cosh_ref="am:fertigation"),
                ElementIn(element_type="FERTIGATION_INTERVAL", value="7"),
            ],
        ),
        db=db, current_user=user,
    )

    refreshed = (await db.execute(
        select(Practice).where(Practice.id == out.id)
    )).scalar_one()
    assert refreshed.frequency_days == 7


@requires_docker
@pytest.mark.asyncio
async def test_frequency_mismatch_returns_422(db):
    """Practice.frequency_days ≠ FERTIGATION_INTERVAL → 422."""
    client, user, pkg, tl = await _setup_timeline(db)
    db.add_all([
        CoshCoreItem(cosh_id="du:kg_acre", core_type="dosage_unit",
                           translations={"en": "kg/acre"}, status="active"),
        CoshCoreItem(cosh_id="form:water_soluble", core_type="formulation",
                           translations={"en": "Water soluble"}, status="active"),
        CoshCoreItem(cosh_id="am:fertigation", core_type="application_method",
                           translations={"en": "Fertigation"}, status="active"),
    ])
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_practice(
            client_id=client.id, timeline_id=tl.id,
            request=PracticeCreate(
                l0_type=PracticeL0.INPUT, l1_type="FERTILIZER",
                l2_type="FERTIGATION_NPK_DOSAGES",
                frequency_days=5,  # mismatch
                elements=[
                    ElementIn(element_type="N_DOSAGE", value="100"),
                    ElementIn(element_type="P_DOSAGE", value="50"),
                    ElementIn(element_type="K_DOSAGE", value="50"),
                    ElementIn(element_type="UNIT", cosh_ref="du:kg_acre"),
                    ElementIn(element_type="FORMULATION", cosh_ref="form:water_soluble"),
                    ElementIn(element_type="APPLICATION_METHOD", cosh_ref="am:fertigation"),
                    ElementIn(element_type="FERTIGATION_INTERVAL", value="7"),
                ],
            ),
            db=db, current_user=user,
        )

    detail = exc.value.detail
    error_codes = {e["code"] for e in detail["errors"]}
    assert "FREQUENCY_MISMATCH" in error_codes
