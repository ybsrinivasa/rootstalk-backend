"""list_formulations: NPK Dosages L2s route through the
`formulations_L2_npk` Connect — 2026-05-22.

The two NPK Dosages L2s (CHEMICAL_FERTILIZERS_NPK_DOSAGES,
FERTIGATION_NPK_DOSAGES) carry no Common Name / Trade Name on the
Practice, so the brand-cascade `tradename_formulation` Connect
returns nothing for them. Cosh's `formulations_L2_npk` Connect
pairs each NPK L2 (via the `l2_data` Core) with its valid
Formulations. `list_formulations` branches on L2 type to pick the
right path.

Sample shape from testing (synced 2026-05-22):
  endpoint roles: 'formulations' + 'l2_data'
  CHEMICAL_FERTILIZERS_NPK_DOSAGES → [Solid]
  FERTIGATION_NPK_DOSAGES          → [Solid, Liquid]
"""
from __future__ import annotations

import pytest

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_FORMULATIONS_CORE,
    COSH_FORMULATIONS_L2_NPK_CONNECT,
    COSH_L2_DATA_CORE,
)
from app.services.cosh_options_view import list_formulations
from tests.conftest import requires_docker


async def _seed_npk_connect(db) -> None:
    """Seed two NPK L2 l2_data rows + two formulation rows + the
    Connect rows mapping them per the testing-DB shape."""
    db.add(CoshCoreItem(
        cosh_id="ld-chem-npk", core_type=COSH_L2_DATA_CORE,
        translations={"en": "Chemical fertilizers - NPK dosages"},
        status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="ld-ferti-npk", core_type=COSH_L2_DATA_CORE,
        translations={"en": "Fertigation - NPK dosages"},
        status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="form-solid", core_type=COSH_FORMULATIONS_CORE,
        translations={"en": "Solid"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="form-liquid", core_type=COSH_FORMULATIONS_CORE,
        translations={"en": "Liquid"}, status="active",
    ))
    # Connect rows: Chemical→Solid, Fertigation→Solid, Fertigation→Liquid
    db.add(CoshConnectRow(
        connect_id="row-chem-solid",
        connect_type=COSH_FORMULATIONS_L2_NPK_CONNECT,
        endpoints=[
            {"role": "formulations", "cosh_id": "form-solid", "position": 1},
            {"role": "l2_data",      "cosh_id": "ld-chem-npk", "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id="row-ferti-solid",
        connect_type=COSH_FORMULATIONS_L2_NPK_CONNECT,
        endpoints=[
            {"role": "formulations", "cosh_id": "form-solid", "position": 1},
            {"role": "l2_data",      "cosh_id": "ld-ferti-npk", "position": 2},
        ],
        status="active",
    ))
    db.add(CoshConnectRow(
        connect_id="row-ferti-liquid",
        connect_type=COSH_FORMULATIONS_L2_NPK_CONNECT,
        endpoints=[
            {"role": "formulations", "cosh_id": "form-liquid", "position": 1},
            {"role": "l2_data",      "cosh_id": "ld-ferti-npk", "position": 2},
        ],
        status="active",
    ))
    await db.commit()


@requires_docker
@pytest.mark.asyncio
async def test_chemical_fertilizers_npk_returns_solid_only(db):
    await _seed_npk_connect(db)
    out = await list_formulations(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    )
    names = sorted([o["name"] for o in out])
    assert names == ["Solid"]


@requires_docker
@pytest.mark.asyncio
async def test_fertigation_npk_returns_solid_and_liquid(db):
    await _seed_npk_connect(db)
    out = await list_formulations(
        db, l2_type="FERTIGATION_NPK_DOSAGES",
    )
    names = sorted([o["name"] for o in out])
    assert names == ["Liquid", "Solid"]


@requires_docker
@pytest.mark.asyncio
async def test_non_npk_l2_does_not_use_npk_connect(db):
    """An L2 that's NOT in NPK_L2_TO_L2DATA_EN must fall through to
    the brand-cascade path — which returns [] when no common_name is
    set. This proves the NPK-only branching."""
    await _seed_npk_connect(db)
    # CHEMICAL_PESTICIDES uses brand-cascade. Without common_name,
    # the brand-cascade returns []. If the NPK branch leaked, we'd
    # see formulations from the test-seeded Connect.
    out = await list_formulations(
        db, l2_type="CHEMICAL_PESTICIDES",
    )
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_npk_l2_without_l2_data_row_returns_empty(db):
    """When Cosh hasn't synced the `l2_data` row for the L2 (or its
    translation is missing), the lookup returns []. No crash, no
    leakage from the other NPK L2."""
    # Don't seed: empty l2_data Core.
    out = await list_formulations(
        db, l2_type="CHEMICAL_FERTILIZERS_NPK_DOSAGES",
    )
    assert out == []
