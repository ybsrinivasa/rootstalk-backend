"""CA-side `/client/{client_id}/parameters` auto-ensure (2026-05-29).

Pre-fix: the Cosh P-V mirror was only populated by the SA-portal
endpoint `/advisory/global/parameters`; CAs that browsed directly to
a crop's P-V saw an empty list until SA pre-warmed it. On prod, 0 of
2237 active organisms were mirrored — Indam's CM couldn't see any
P-V data when authoring a Package for Cucumber.

This test seeds the Cosh Connect for a crop, then calls the CA
endpoint with an empty local `parameters` table and asserts the
mirror was populated transparently.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Parameter, ParameterSource, Variable,
)
from app.modules.advisory.router import list_parameters
from app.modules.clients.models import ClientUserRole
from app.modules.subscriptions.models import Subscription  # register mapper
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_pv_view import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_CROPS_PARAMS_VARS_CONNECT,
    COSH_PACKAGE_PARAMETERS_CORE,
    COSH_PACKAGE_VARIABLES_CORE,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


CROP = "crop:cucumber-test"
PARAM_A = "pp:soil-type"
PARAM_B = "pp:irrigation"
VAR_A1 = "pv:sandy"
VAR_A2 = "pv:clay"
VAR_B1 = "pv:drip"


async def _seed_cosh(db):
    """Seed a minimal Cosh side: one crop, two parameters, three
    variables, three pair rows in `crops_parameters_variables`."""
    db.add_all([
        CoshCoreItem(
            cosh_id=CROP, core_type=COSH_BIOLOGICAL_NAMES_CORE,
            status="active", translations={"en": "Cucumber"},
        ),
        CoshCoreItem(
            cosh_id=PARAM_A, core_type=COSH_PACKAGE_PARAMETERS_CORE,
            status="active", translations={"en": "Soil type"},
        ),
        CoshCoreItem(
            cosh_id=PARAM_B, core_type=COSH_PACKAGE_PARAMETERS_CORE,
            status="active", translations={"en": "Irrigation"},
        ),
        CoshCoreItem(
            cosh_id=VAR_A1, core_type=COSH_PACKAGE_VARIABLES_CORE,
            status="active", translations={"en": "Sandy"},
        ),
        CoshCoreItem(
            cosh_id=VAR_A2, core_type=COSH_PACKAGE_VARIABLES_CORE,
            status="active", translations={"en": "Clay"},
        ),
        CoshCoreItem(
            cosh_id=VAR_B1, core_type=COSH_PACKAGE_VARIABLES_CORE,
            status="active", translations={"en": "Drip"},
        ),
    ])
    db.add_all([
        CoshConnectRow(
            connect_id=f"cpv:{i}", connect_type=COSH_CROPS_PARAMS_VARS_CONNECT,
            status="active",
            endpoints=[
                {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": CROP, "position": 1},
                {"role": COSH_PACKAGE_PARAMETERS_CORE, "cosh_id": p, "position": 2},
                {"role": COSH_PACKAGE_VARIABLES_CORE, "cosh_id": v, "position": 3},
            ],
        )
        for i, (p, v) in enumerate(
            [(PARAM_A, VAR_A1), (PARAM_A, VAR_A2), (PARAM_B, VAR_B1)]
        )
    ])
    await db.flush()


@requires_docker
@pytest.mark.asyncio
async def test_ca_list_parameters_auto_mirrors_on_first_read(db):
    """The CA endpoint must populate the Cosh mirror itself — no
    SA pre-warm required."""
    await _seed_cosh(db)
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.CA,
    )

    # Pre-condition: zero local Parameters for this crop.
    pre = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == CROP)
    )).scalars().all()
    assert pre == []

    result = await list_parameters(
        client_id=client.id, crop_cosh_id=CROP,
        db=db, current_user=user,
    )

    # Post-condition: both Parameters mirrored from Cosh + visible in
    # the response.
    names = sorted(p.name for p in result)
    assert names == ["Irrigation", "Soil type"]
    assert all(p.source == ParameterSource.COSH for p in result)
    assert all(p.client_id is None for p in result)

    # Variables: 2 under Soil type, 1 under Irrigation. Total 3.
    vars_row = (await db.execute(
        select(Variable).join(Parameter, Parameter.id == Variable.parameter_id)
        .where(Parameter.crop_cosh_id == CROP)
    )).scalars().all()
    assert len(vars_row) == 3
    assert sorted(v.name for v in vars_row) == ["Clay", "Drip", "Sandy"]


@requires_docker
@pytest.mark.asyncio
async def test_ca_list_parameters_idempotent_on_second_read(db):
    """Second read does not duplicate rows — the partial unique
    indexes deduplicate even though the function is called every
    time."""
    await _seed_cosh(db)
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await make_client_user(
        db, user=user, client=client, role=ClientUserRole.CA,
    )

    await list_parameters(
        client_id=client.id, crop_cosh_id=CROP, db=db, current_user=user,
    )
    after_first = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == CROP)
    )).scalars().all()
    assert len(after_first) == 2

    # Second call must not create duplicates.
    await list_parameters(
        client_id=client.id, crop_cosh_id=CROP, db=db, current_user=user,
    )
    after_second = (await db.execute(
        select(Parameter).where(Parameter.crop_cosh_id == CROP)
    )).scalars().all()
    assert len(after_second) == 2
