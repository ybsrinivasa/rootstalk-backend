"""CA-side Parameters & Variables authoring (2026-05-17).

The CA-CCA package detail page mirrors the SA Global CCA's "Set
Signature" feature. SA endpoints under /advisory/global/parameters
are already covered by test_phase_global_pv_authoring.py; this
module covers the CA-side mirrors:

  - GET  /client/{cid}/packages/{pkg}/variables    (list_package_variables)
  - PUT  /client/{cid}/parameters/{p}              (update_client_parameter)
  - DELETE /client/{cid}/parameters/{p}            (delete_client_parameter)
  - DELETE /client/{cid}/parameters/{p}/variables/{v} (delete_client_variable)

Coverage rules deliberately mirror the SA suite — when SA gains a
new gate, the same gate ships on CA in the same commit.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, PackageVariable, Parameter,
    ParameterSource, Variable,
)
from app.modules.advisory.router import (
    create_parameter, create_variable, delete_client_parameter,
    delete_client_variable, list_package_variables, list_variables,
    set_package_variables, update_client_parameter, update_variable,
)
from app.modules.advisory.schemas import (
    PackageVariableSet, ParameterCreate, ParameterUpdate, VariableCreate,
    VariableNameIn,
)
from app.modules.clients.models import ClientUserRole
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


_SEED_VARS = [VariableNameIn(name="__seed_a__"), VariableNameIn(name="__seed_b__")]


async def _seed_client_pkg(db, *, client, crop="crop:tomato"):
    pkg = Package(
        client_id=client.id,
        name="P", crop_cosh_id=crop,
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.DRAFT,
    )
    db.add(pkg)
    await db.flush()
    return pkg


async def _seed_se(db, client):
    user = await make_user(db, name="SE")
    await make_client_user(db, user=user, client=client, role=ClientUserRole.SUBJECT_EXPERT)
    return user


# ── list_package_variables ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_package_variables_empty(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    pkg = await _seed_client_pkg(db, client=client)
    await db.commit()
    out = await list_package_variables(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_list_package_variables_after_set(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    pkg = await _seed_client_pkg(db, client=client)
    param = await create_parameter(
        client_id=client.id,
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="Irrigation",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    v = (await db.execute(
        select(Variable).where(Variable.parameter_id == param.id).limit(1)
    )).scalar_one()
    await set_package_variables(
        client_id=client.id, package_id=pkg.id,
        request=PackageVariableSet(
            assignments=[{"parameter_id": param.id, "variable_id": v.id}],
        ),
        db=db, current_user=user,
    )
    out = await list_package_variables(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert len(out) == 1
    assert out[0]["parameter_id"] == param.id
    assert out[0]["variable_id"] == v.id


# ── update_client_parameter (rename) ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_client_parameter_rename(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    p = await create_parameter(
        client_id=client.id,
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="Typo",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    out = await update_client_parameter(
        client_id=client.id, parameter_id=p.id,
        request=ParameterUpdate(name="Corrected"),
        db=db, current_user=user,
    )
    assert out.name == "Corrected"


@requires_docker
@pytest.mark.asyncio
async def test_update_client_parameter_refuses_cosh_mirrored(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=client.id,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_client_parameter(
            client_id=client.id, parameter_id=cosh_param.id,
            request=ParameterUpdate(name="Edited"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cosh_mirrored_parameter_readonly"


# ── delete_client_parameter ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_delete_client_parameter_cascade(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    p = await create_parameter(
        client_id=client.id,
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="Toss",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    await delete_client_parameter(
        client_id=client.id, parameter_id=p.id,
        db=db, current_user=user,
    )
    remaining = (await db.execute(
        select(Parameter).where(Parameter.id == p.id)
    )).scalar_one_or_none()
    assert remaining is None
    remaining_vars = (await db.execute(
        select(Variable).where(Variable.parameter_id == p.id)
    )).scalars().all()
    assert remaining_vars == []


@requires_docker
@pytest.mark.asyncio
async def test_delete_client_parameter_in_use_blocked(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    pkg = await _seed_client_pkg(db, client=client)
    p = await create_parameter(
        client_id=client.id,
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="InUse",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    v = (await db.execute(
        select(Variable).where(Variable.parameter_id == p.id).limit(1)
    )).scalar_one()
    db.add(PackageVariable(
        package_id=pkg.id, parameter_id=p.id, variable_id=v.id,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await delete_client_parameter(
            client_id=client.id, parameter_id=p.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "parameter_in_use"


# ── delete_client_variable ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_delete_client_variable_refuses_when_drops_below_2(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    p = await create_parameter(
        client_id=client.id,
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="Min2",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    vs = (await db.execute(
        select(Variable).where(Variable.parameter_id == p.id)
    )).scalars().all()
    assert len(vs) == 2
    with pytest.raises(HTTPException) as exc:
        await delete_client_variable(
            client_id=client.id, parameter_id=p.id, variable_id=vs[0].id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "min_two_variables"


@requires_docker
@pytest.mark.asyncio
async def test_delete_client_variable_ok_when_3_remain(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    p = await create_parameter(
        client_id=client.id,
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="ThreeVars",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    extra = await create_variable(
        client_id=client.id, parameter_id=p.id,
        request=VariableCreate(parameter_id=p.id, name="extra"),
        db=db, current_user=user,
    )
    await delete_client_variable(
        client_id=client.id, parameter_id=p.id, variable_id=extra.id,
        db=db, current_user=user,
    )
    remaining = (await db.execute(
        select(Variable).where(Variable.parameter_id == p.id)
    )).scalars().all()
    assert len(remaining) == 2


@requires_docker
@pytest.mark.asyncio
async def test_delete_client_variable_refuses_cosh_mirrored(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=client.id,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    cosh_var = Variable(
        parameter_id=cosh_param.id, name="Loamy",
        cosh_id="cosh:soil_type:loamy",
    )
    db.add(cosh_var)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await delete_client_variable(
            client_id=client.id, parameter_id=cosh_param.id,
            variable_id=cosh_var.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cosh_mirrored_variable_readonly"


# ── Per-client filter & Cosh-parent read-only enforcement ───────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_variables_on_cosh_parent_hides_sa_added(db):
    """On a Cosh-mirrored parent, CA must only see Cosh-sourced
    variables (cosh_id IS NOT NULL). Any SA-added siblings stay
    hidden on the CA side."""
    client = await make_client(db)
    user = await _seed_se(db, client)
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    cosh_var = Variable(
        parameter_id=cosh_param.id, name="Loamy",
        cosh_id="cosh:soil_type:loamy",
    )
    sa_added = Variable(
        parameter_id=cosh_param.id, name="SAExtension",
        cosh_id=None,
    )
    db.add(cosh_var)
    db.add(sa_added)
    await db.commit()
    out = await list_variables(
        client_id=client.id, parameter_id=cosh_param.id,
        db=db, current_user=user,
    )
    names = {v.name for v in out}
    assert "Loamy" in names
    assert "SAExtension" not in names


@requires_docker
@pytest.mark.asyncio
async def test_create_variable_refuses_cosh_parent(db):
    """CA cannot add a variable under a Cosh-mirrored parent — Cosh
    is read-only catalogue. 422 cosh_parameter_readonly."""
    client = await make_client(db)
    user = await _seed_se(db, client)
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_variable(
            client_id=client.id, parameter_id=cosh_param.id,
            request=VariableCreate(parameter_id=cosh_param.id, name="X"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cosh_parameter_readonly"


@requires_docker
@pytest.mark.asyncio
async def test_create_variable_refuses_other_client_parent(db):
    """CA cannot add a variable under another client's Custom
    parameter — per-client isolation. 403 parameter_not_owned."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    user_a = await _seed_se(db, client_a)
    b_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=client_b.id,
        name="OtherClientCustom", source=ParameterSource.CUSTOM,
    )
    db.add(b_param)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_variable(
            client_id=client_a.id, parameter_id=b_param.id,
            request=VariableCreate(parameter_id=b_param.id, name="X"),
            db=db, current_user=user_a,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "parameter_not_owned"


@requires_docker
@pytest.mark.asyncio
async def test_update_variable_refuses_cosh_parent(db):
    """Rename refuses on a Cosh-mirrored parent — even the SA-added
    sibling (which CA can't see anyway) can't be edited from CA."""
    client = await make_client(db)
    user = await _seed_se(db, client)
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    cosh_var = Variable(
        parameter_id=cosh_param.id, name="Loamy",
        cosh_id="cosh:soil_type:loamy",
    )
    db.add(cosh_var)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_variable(
            client_id=client.id, parameter_id=cosh_param.id,
            variable_id=cosh_var.id, data={"name": "renamed"},
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cosh_parameter_readonly"


@requires_docker
@pytest.mark.asyncio
async def test_set_package_variables_refuses_global_custom_assignment(db):
    """CA cannot pin its Package signature to a Global-Custom
    parameter (SA-only artefact). 403 parameter_not_visible."""
    client = await make_client(db)
    user = await _seed_se(db, client)
    pkg = await _seed_client_pkg(db, client=client)
    global_custom = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="GlobalCustom", source=ParameterSource.CUSTOM,
    )
    db.add(global_custom)
    await db.flush()
    v = Variable(parameter_id=global_custom.id, name="x", cosh_id=None)
    db.add(v)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await set_package_variables(
            client_id=client.id, package_id=pkg.id,
            request=PackageVariableSet(
                assignments=[{"parameter_id": global_custom.id, "variable_id": v.id}],
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "parameter_not_visible"


@requires_docker
@pytest.mark.asyncio
async def test_set_package_variables_refuses_sa_added_variable_on_cosh_parent(db):
    """Even if CA can see the Cosh parent, an SA-added sibling
    variable under it is invisible to CA — refuse the assignment.
    403 variable_not_visible."""
    client = await make_client(db)
    user = await _seed_se(db, client)
    pkg = await _seed_client_pkg(db, client=client)
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    sa_added = Variable(
        parameter_id=cosh_param.id, name="SAExtension",
        cosh_id=None,
    )
    db.add(sa_added)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await set_package_variables(
            client_id=client.id, package_id=pkg.id,
            request=PackageVariableSet(
                assignments=[{"parameter_id": cosh_param.id, "variable_id": sa_added.id}],
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "variable_not_visible"


@requires_docker
@pytest.mark.asyncio
async def test_delete_client_variable_blocked_when_in_use(db):
    client = await make_client(db)
    user = await _seed_se(db, client)
    pkg = await _seed_client_pkg(db, client=client)
    p = await create_parameter(
        client_id=client.id,
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="InUseVar",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    # add a 3rd variable so the min-2 rule won't trip first
    extra = await create_variable(
        client_id=client.id, parameter_id=p.id,
        request=VariableCreate(parameter_id=p.id, name="extra"),
        db=db, current_user=user,
    )
    db.add(PackageVariable(
        package_id=pkg.id, parameter_id=p.id, variable_id=extra.id,
    ))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await delete_client_variable(
            client_id=client.id, parameter_id=p.id, variable_id=extra.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "variable_in_use"
