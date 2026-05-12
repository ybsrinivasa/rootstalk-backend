"""Read-time mirror of Cosh `crops_parameters_variables` Connect
into the local `parameters` / `variables` tables (2026-05-12).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.modules.advisory.models import Parameter, ParameterSource, Variable
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_CROPS_PARAMS_VARS_CONNECT,
    COSH_PACKAGE_PARAMETERS_CORE,
    COSH_PACKAGE_VARIABLES_CORE,
)
from app.services.cosh_pv_view import ensure_local_parameters_for_crop
from tests.conftest import requires_docker


async def _seed_cosh_pv_for_crop(
    db, *, crop_cosh_id: str, param_id: str, param_name: str,
    variable_ids: list[tuple[str, str]],
) -> None:
    """Seed Cosh-side Core + Connect rows for one (crop, parameter,
    [variables]) tuple."""
    db.add(CoshCoreItem(
        cosh_id=param_id, core_type=COSH_PACKAGE_PARAMETERS_CORE,
        status="active", translations={"en": param_name},
    ))
    for v_id, v_name in variable_ids:
        db.add(CoshCoreItem(
            cosh_id=v_id, core_type=COSH_PACKAGE_VARIABLES_CORE,
            status="active", translations={"en": v_name},
        ))
        db.add(CoshConnectRow(
            connect_id=f"connect:{crop_cosh_id}:{param_id}:{v_id}",
            connect_type=COSH_CROPS_PARAMS_VARS_CONNECT,
            status="active",
            endpoints=[
                {"role": COSH_BIOLOGICAL_NAMES_CORE,
                 "cosh_id": crop_cosh_id, "position": 1},
                {"role": COSH_PACKAGE_PARAMETERS_CORE,
                 "cosh_id": param_id, "position": 2},
                {"role": COSH_PACKAGE_VARIABLES_CORE,
                 "cosh_id": v_id, "position": 3},
            ],
        ))
    await db.flush()


# ── Happy path ────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_ensure_mirrors_one_parameter_with_variables(db):
    crop = f"crop:{uuid.uuid4().hex[:8]}"
    param_id = str(uuid.uuid4())
    v1 = str(uuid.uuid4())
    v2 = str(uuid.uuid4())
    await _seed_cosh_pv_for_crop(
        db, crop_cosh_id=crop, param_id=param_id, param_name="Soil Type",
        variable_ids=[(v1, "Red soil"), (v2, "Black soil")],
    )
    await db.commit()

    await ensure_local_parameters_for_crop(db, crop)

    params = (await db.execute(
        select(Parameter).where(
            Parameter.crop_cosh_id == crop, Parameter.client_id == None,  # noqa: E711
        )
    )).scalars().all()
    assert len(params) == 1
    assert params[0].cosh_id == param_id
    assert params[0].name == "Soil Type"
    assert params[0].source == ParameterSource.COSH

    variables = (await db.execute(
        select(Variable).where(Variable.parameter_id == params[0].id)
    )).scalars().all()
    var_names = sorted(v.name for v in variables)
    assert var_names == ["Black soil", "Red soil"]


@requires_docker
@pytest.mark.asyncio
async def test_ensure_is_idempotent_across_repeat_calls(db):
    crop = f"crop:{uuid.uuid4().hex[:8]}"
    param_id = str(uuid.uuid4())
    v1 = str(uuid.uuid4())
    await _seed_cosh_pv_for_crop(
        db, crop_cosh_id=crop, param_id=param_id, param_name="Irrigation",
        variable_ids=[(v1, "Drip")],
    )
    await db.commit()

    await ensure_local_parameters_for_crop(db, crop)
    first_count_p = len((await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == crop)
    )).scalars().all())
    first_count_v = len((await db.execute(
        select(Variable)
    )).scalars().all())

    # Second call should be a no-op.
    await ensure_local_parameters_for_crop(db, crop)
    await ensure_local_parameters_for_crop(db, crop)
    assert first_count_p == len((await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == crop)
    )).scalars().all())
    assert first_count_v == len((await db.execute(
        select(Variable)
    )).scalars().all())


@requires_docker
@pytest.mark.asyncio
async def test_ensure_refreshes_name_when_cosh_translation_changes(db):
    crop = f"crop:{uuid.uuid4().hex[:8]}"
    param_id = str(uuid.uuid4())
    v1 = str(uuid.uuid4())
    await _seed_cosh_pv_for_crop(
        db, crop_cosh_id=crop, param_id=param_id, param_name="Old Name",
        variable_ids=[(v1, "Drip")],
    )
    await db.commit()
    await ensure_local_parameters_for_crop(db, crop)

    # Cosh edits the parameter's translation.
    core = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == param_id)
    )).scalar_one()
    core.translations = {"en": "New Name"}
    await db.commit()

    await ensure_local_parameters_for_crop(db, crop)
    refreshed = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == crop)
    )).scalar_one()
    assert refreshed.name == "New Name"


@requires_docker
@pytest.mark.asyncio
async def test_ensure_ignores_other_crops(db):
    crop_a = f"crop:{uuid.uuid4().hex[:8]}"
    crop_b = f"crop:{uuid.uuid4().hex[:8]}"
    p_a = str(uuid.uuid4())
    p_b = str(uuid.uuid4())
    v = str(uuid.uuid4())
    db.add(CoshCoreItem(
        cosh_id=p_a, core_type=COSH_PACKAGE_PARAMETERS_CORE,
        status="active", translations={"en": "A-param"},
    ))
    db.add(CoshCoreItem(
        cosh_id=p_b, core_type=COSH_PACKAGE_PARAMETERS_CORE,
        status="active", translations={"en": "B-param"},
    ))
    db.add(CoshCoreItem(
        cosh_id=v, core_type=COSH_PACKAGE_VARIABLES_CORE,
        status="active", translations={"en": "V"},
    ))
    db.add(CoshConnectRow(
        connect_id="c-a", connect_type=COSH_CROPS_PARAMS_VARS_CONNECT,
        status="active",
        endpoints=[
            {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": crop_a, "position": 1},
            {"role": COSH_PACKAGE_PARAMETERS_CORE, "cosh_id": p_a, "position": 2},
            {"role": COSH_PACKAGE_VARIABLES_CORE, "cosh_id": v, "position": 3},
        ],
    ))
    db.add(CoshConnectRow(
        connect_id="c-b", connect_type=COSH_CROPS_PARAMS_VARS_CONNECT,
        status="active",
        endpoints=[
            {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": crop_b, "position": 1},
            {"role": COSH_PACKAGE_PARAMETERS_CORE, "cosh_id": p_b, "position": 2},
            {"role": COSH_PACKAGE_VARIABLES_CORE, "cosh_id": v, "position": 3},
        ],
    ))
    await db.commit()

    await ensure_local_parameters_for_crop(db, crop_a)
    a_params = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == crop_a)
    )).scalars().all()
    b_params = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == crop_b)
    )).scalars().all()
    assert len(a_params) == 1
    assert a_params[0].name == "A-param"
    # crop_b wasn't touched.
    assert len(b_params) == 0


@requires_docker
@pytest.mark.asyncio
async def test_ensure_does_not_disturb_custom_parameters(db):
    """CUSTOM parameters (cosh_id NULL) authored by a CM stay
    untouched when Cosh mirror runs."""
    crop = f"crop:{uuid.uuid4().hex[:8]}"
    custom = Parameter(
        crop_cosh_id=crop, client_id=None,
        cosh_id=None,
        name="Hand-crafted parameter",
        source=ParameterSource.CUSTOM,
    )
    db.add(custom)
    param_id = str(uuid.uuid4())
    v1 = str(uuid.uuid4())
    await _seed_cosh_pv_for_crop(
        db, crop_cosh_id=crop, param_id=param_id, param_name="From Cosh",
        variable_ids=[(v1, "V1")],
    )
    await db.commit()

    await ensure_local_parameters_for_crop(db, crop)

    rows = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == crop)
        .order_by(Parameter.name)
    )).scalars().all()
    names = [r.name for r in rows]
    assert "From Cosh" in names
    assert "Hand-crafted parameter" in names
    # Custom row's cosh_id stays NULL.
    custom_row = next(r for r in rows if r.name == "Hand-crafted parameter")
    assert custom_row.cosh_id is None
    assert custom_row.source == ParameterSource.CUSTOM


@requires_docker
@pytest.mark.asyncio
async def test_ensure_noop_when_no_connect_rows(db):
    """A crop with no Cosh connect rows → no mirror activity, no
    error."""
    crop = f"crop:{uuid.uuid4().hex[:8]}"
    await db.commit()
    await ensure_local_parameters_for_crop(db, crop)
    rows = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == crop)
    )).scalars().all()
    assert rows == []
