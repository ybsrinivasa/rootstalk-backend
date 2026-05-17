"""Global PV authoring (Batch 9, 2026-05-11; updated Batch 39N-a
2026-05-16 — push no longer carries Global PVs to Local).

The CM needs to set a parameter-variable signature on Global
Packages so multiple Globals for the same crop are distinguishable
(e.g. Tomato-Drip vs Tomato-Flood). Per Batch 39N-a, the Global's
PV signature is an SA-side identifier ONLY — at push time Ram
re-picks PVs in the form (catalogue-only); Global PVs are NOT
carried across.

Coverage:
  - Global Parameter CRUD with client_id IS NULL.
  - Global Variable add under Global Parameter.
  - Set/get Global PackageVariable fingerprint.
  - push does NOT carry Global PVs — Local takes the form's PVs.
  - CA-side `list_parameters` returns both client-scoped AND
    Global Parameters so the SE sees the inherited ones.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, PackageVariable, Parameter,
    ParameterSource, Variable,
)
from app.modules.advisory.router import (
    create_global_parameter, create_global_variable,
    list_global_package_variables, list_global_parameters,
    list_global_variables, list_parameters,
    push_global_package, set_global_package_variables,
)
from app.modules.advisory.schemas import (
    PackagePushRequest, ParameterCreate, PackageVariableSet,
    VariableCreate, VariableNameIn,
)


# Batch 28: ParameterCreate now requires ≥ 2 inline variables. These
# two placeholders make the test seeds atomic without changing the
# test's actual subject (most tests check the Parameter itself or
# the join behaviour, not the seed variables). Names are deliberately
# distinct from the per-test variables added via VariableCreate to
# avoid the (parameter_id, name) unique-constraint collisions.
_SEED_VARS = [VariableNameIn(name="__seed_a__"), VariableNameIn(name="__seed_b__")]
from app.modules.clients.models import ClientCrop, ClientUserRole
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_cm_assignment,
    make_push_request_body, make_user,
)


async def _seed_global_pkg(db, *, name: str | None = None, crop="crop:test"):
    pkg = Package(
        client_id=None,
        name=name or f"GP-{uuid.uuid4().hex[:6]}",
        crop_cosh_id=crop,
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    return pkg


# ── Parameter CRUD ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_global_parameter_lands_with_null_client(db):
    user = await make_user(db, name="CM")
    await db.commit()
    out = await create_global_parameter(
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="Irrigation", display_order=0,
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    assert out.client_id is None
    assert out.crop_cosh_id == "crop:tomato"
    assert out.name == "Irrigation"
    assert out.source == ParameterSource.CUSTOM
    # Batch 28 atomic create: the 2 seed variables are persisted in
    # the same transaction.
    from sqlalchemy import select
    from app.modules.advisory.models import Variable
    vars_ = (await db.execute(
        select(Variable).where(Variable.parameter_id == out.id)
    )).scalars().all()
    assert sorted(v.name for v in vars_) == ["__seed_a__", "__seed_b__"]


@requires_docker
@pytest.mark.asyncio
async def test_list_global_parameters_excludes_client_scoped(db):
    user = await make_user(db, name="CM")
    client = await make_client(db)
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Irrigation", source=ParameterSource.CUSTOM,
    ))
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=client.id,
        name="ClientOnly", source=ParameterSource.CUSTOM,
    ))
    await db.commit()
    out = await list_global_parameters(
        crop_cosh_id="crop:tomato", db=db, current_user=user,
    )
    names = {p.name for p in out}
    assert "Irrigation" in names
    assert "ClientOnly" not in names


# ── Variable CRUD ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_global_variable_under_global_parameter(db):
    user = await make_user(db, name="CM")
    param = await create_global_parameter(
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation", variables=_SEED_VARS),
        db=db, current_user=user,
    )
    drip = await create_global_variable(
        parameter_id=param.id, request=VariableCreate(parameter_id=param.id, name="Drip"),
        db=db, current_user=user,
    )
    flood = await create_global_variable(
        parameter_id=param.id, request=VariableCreate(parameter_id=param.id, name="Flood"),
        db=db, current_user=user,
    )
    out = await list_global_variables(
        parameter_id=param.id, db=db, current_user=user,
    )
    names = {v.name for v in out}
    # Batch 28: param ships with 2 seed variables; this test then
    # adds Drip + Flood via the per-variable endpoint. Total 4.
    assert {"Drip", "Flood"} <= names
    assert drip.parameter_id == param.id
    assert flood.parameter_id == param.id


@requires_docker
@pytest.mark.asyncio
async def test_global_variable_refuses_client_scoped_parameter(db):
    """The Global variables endpoint must refuse to operate on a
    client-scoped Parameter — keeps the global/local separation
    explicit."""
    user = await make_user(db, name="CM")
    client = await make_client(db)
    client_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=client.id,
        name="ClientParam", source=ParameterSource.CUSTOM,
    )
    db.add(client_param)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_variable(
            parameter_id=client_param.id,
            request=VariableCreate(parameter_id=client_param.id, name="X"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 404


# ── Global PackageVariable fingerprint ──────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_set_and_list_global_package_variables(db):
    user = await make_user(db, name="CM")
    param = await create_global_parameter(
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation", variables=_SEED_VARS),
        db=db, current_user=user,
    )
    drip = await create_global_variable(
        parameter_id=param.id, request=VariableCreate(parameter_id=param.id, name="Drip"),
        db=db, current_user=user,
    )
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    await db.commit()

    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(
            assignments=[{"parameter_id": param.id, "variable_id": drip.id}],
        ),
        db=db, current_user=user,
    )
    out = await list_global_package_variables(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert len(out) == 1
    assert out[0]["parameter_id"] == param.id
    assert out[0]["variable_id"] == drip.id


@requires_docker
@pytest.mark.asyncio
async def test_set_global_package_variables_replaces_existing(db):
    """PUT semantics — supplied set replaces whatever was previously
    assigned, not additive."""
    user = await make_user(db, name="CM")
    p = await create_global_parameter(
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation", variables=_SEED_VARS),
        db=db, current_user=user,
    )
    v1 = await create_global_variable(
        parameter_id=p.id, request=VariableCreate(parameter_id=p.id, name="Drip"),
        db=db, current_user=user,
    )
    v2 = await create_global_variable(
        parameter_id=p.id, request=VariableCreate(parameter_id=p.id, name="Flood"),
        db=db, current_user=user,
    )
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    await db.commit()

    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[{"parameter_id": p.id, "variable_id": v1.id}]),
        db=db, current_user=user,
    )
    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[{"parameter_id": p.id, "variable_id": v2.id}]),
        db=db, current_user=user,
    )
    out = await list_global_package_variables(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert len(out) == 1
    assert out[0]["variable_id"] == v2.id


# ── push isolates Global PVs from Local (Batch 39N-a) ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_does_not_carry_global_pvs_to_local(db):
    """Global has PVs (Drip irrigation). At push time, Ram re-picks
    PVs in the form (Flood) — the resulting Local takes the form's
    PVs, NOT Global's. Per Batch 39N-a, Global PVs are an SA-side
    identifier only."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)

    # Global PV (Drip).
    global_param = await create_global_parameter(
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation", variables=_SEED_VARS),
        db=db, current_user=cm,
    )
    drip = await create_global_variable(
        parameter_id=global_param.id,
        request=VariableCreate(parameter_id=global_param.id, name="Drip"),
        db=db, current_user=cm,
    )
    flood = await create_global_variable(
        parameter_id=global_param.id,
        request=VariableCreate(parameter_id=global_param.id, name="Flood"),
        db=db, current_user=cm,
    )
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    db.add(ClientCrop(client_id=client.id, crop_cosh_id="crop:tomato"))
    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": global_param.id, "variable_id": drip.id},
        ]),
        db=db, current_user=cm,
    )
    await db.commit()

    # Push-form body: Ram picks Flood, not Drip.
    body = await make_push_request_body(db, client=client, src=pkg)
    body["pv_assignments"] = [
        {"parameter_id": global_param.id, "variable_id": flood.id},
    ]
    await db.commit()

    local = await push_global_package(
        client_id=client.id, pkg_id=pkg.id,
        request=PackagePushRequest(**body),
        db=db, current_user=cm,
    )
    pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == local.id)
    )).scalars().all()
    assert len(pvs) == 1
    # Local has the form-supplied PV (Flood), NOT Global's (Drip).
    assert pvs[0].variable_id == flood.id
    assert pvs[0].variable_id != drip.id


# ── CA list_parameters: Cosh-globals + own customs only ────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_ca_list_parameters_cosh_global_and_own_customs(db):
    """CA-side list_parameters returns ONLY:
      - Cosh-mirrored Globals (source=COSH, client_id IS NULL).
      - This client's own Customs.
    Excludes Global-Customs (SA-only authoring artefact) and other
    clients' Customs (per-client isolation).
    """
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    # Cosh-mirrored Global (visible — read-only catalogue).
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="CoshSoilType", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    ))
    # Global-Custom (SA-only — must NOT appear on CA).
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="GlobalCustomIrrigation", source=ParameterSource.CUSTOM,
    ))
    # This client's own Custom (visible).
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=client.id,
        name="ClientCustom", source=ParameterSource.CUSTOM,
    ))
    # Other-client Custom (must NOT appear).
    other = await make_client(db)
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=other.id,
        name="OtherClientCustom", source=ParameterSource.CUSTOM,
    ))
    await db.commit()

    out = await list_parameters(
        client_id=client.id, crop_cosh_id="crop:tomato",
        db=db, current_user=se,
    )
    names = {p.name for p in out}
    assert "CoshSoilType" in names
    assert "ClientCustom" in names
    assert "GlobalCustomIrrigation" not in names
    assert "OtherClientCustom" not in names


# ── Batch 28: atomic create min-2 + Custom CRUD ────────────────────────────

@pytest.mark.asyncio
async def test_parameter_create_rejects_fewer_than_2_variables():
    """Schema enforces min_length=2 on variables. Pydantic raises
    ValidationError at construction time — server returns 422."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        ParameterCreate(
            crop_cosh_id="crop:tomato", name="X",
            variables=[VariableNameIn(name="solo")],
        )


@requires_docker
@pytest.mark.asyncio
async def test_update_global_parameter_rename(db):
    """PUT /advisory/global/parameters/{id} renames a CUSTOM
    parameter."""
    from app.modules.advisory.router import update_global_parameter
    from app.modules.advisory.schemas import ParameterUpdate
    user = await make_user(db, name="CM")
    p = await create_global_parameter(
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="Typo",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    out = await update_global_parameter(
        parameter_id=p.id, request=ParameterUpdate(name="Corrected"),
        db=db, current_user=user,
    )
    assert out.name == "Corrected"


@requires_docker
@pytest.mark.asyncio
async def test_update_global_parameter_refuses_cosh_mirrored(db):
    """COSH-mirrored parameters can't be edited (Cosh is the source
    of truth). 422 with cosh_mirrored_parameter_readonly code."""
    from app.modules.advisory.router import update_global_parameter
    from app.modules.advisory.schemas import ParameterUpdate
    user = await make_user(db, name="CM")
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_global_parameter(
            parameter_id=cosh_param.id,
            request=ParameterUpdate(name="Edited"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cosh_mirrored_parameter_readonly"


@requires_docker
@pytest.mark.asyncio
async def test_delete_global_parameter_cascade(db):
    """DELETE /advisory/global/parameters/{id} drops the parameter
    and its variables when unused."""
    from app.modules.advisory.router import delete_global_parameter
    user = await make_user(db, name="CM")
    p = await create_global_parameter(
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="Toss",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    await delete_global_parameter(
        parameter_id=p.id, db=db, current_user=user,
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
async def test_delete_global_parameter_in_use_blocked(db):
    """DELETE refuses if any PackageVariable still references the
    parameter — SE must clear the Package signature first."""
    from app.modules.advisory.router import delete_global_parameter
    user = await make_user(db, name="CM")
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    p = await create_global_parameter(
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
        await delete_global_parameter(
            parameter_id=p.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "parameter_in_use"


@requires_docker
@pytest.mark.asyncio
async def test_delete_global_variable_refuses_when_drops_below_2(db):
    """Removing a variable that would leave the parameter with 1
    variable is rejected with min_two_variables."""
    from app.modules.advisory.router import delete_global_variable
    user = await make_user(db, name="CM")
    p = await create_global_parameter(
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
        await delete_global_variable(
            parameter_id=p.id, variable_id=vs[0].id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "min_two_variables"


@requires_docker
@pytest.mark.asyncio
async def test_delete_global_variable_ok_when_3_remain(db):
    """When the parameter has ≥ 3 variables, deleting one is allowed
    (the post-delete count is still ≥ 2)."""
    from app.modules.advisory.router import delete_global_variable
    user = await make_user(db, name="CM")
    p = await create_global_parameter(
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="ThreeVars",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    extra = await create_global_variable(
        parameter_id=p.id, request=VariableCreate(parameter_id=p.id, name="extra"),
        db=db, current_user=user,
    )
    await delete_global_variable(
        parameter_id=p.id, variable_id=extra.id,
        db=db, current_user=user,
    )
    remaining = (await db.execute(
        select(Variable).where(Variable.parameter_id == p.id)
    )).scalars().all()
    assert len(remaining) == 2


@requires_docker
@pytest.mark.asyncio
async def test_update_global_variable_rename(db):
    """PUT /advisory/global/parameters/{id}/variables/{vid} renames a
    variable under a CUSTOM parameter."""
    from app.modules.advisory.router import update_global_variable
    from app.modules.advisory.schemas import VariableUpdate
    user = await make_user(db, name="CM")
    p = await create_global_parameter(
        request=ParameterCreate(
            crop_cosh_id="crop:tomato", name="RenameVarParent",
            variables=_SEED_VARS,
        ),
        db=db, current_user=user,
    )
    v = (await db.execute(
        select(Variable).where(Variable.parameter_id == p.id).limit(1)
    )).scalar_one()
    out = await update_global_variable(
        parameter_id=p.id, variable_id=v.id,
        request=VariableUpdate(name="renamed"),
        db=db, current_user=user,
    )
    assert out.name == "renamed"


# ── Batch 35 (2026-05-14): per-variable edit/delete on COSH parents ─────────

@requires_docker
@pytest.mark.asyncio
async def test_update_se_added_variable_on_cosh_parent_allowed(db):
    """A variable SE added to a Cosh-mirrored parent (Variable.cosh_id
    NULL) is editable. The gate is on the variable's own cosh_id, not
    the parent parameter's source."""
    from app.modules.advisory.router import (
        create_global_variable, update_global_variable,
    )
    from app.modules.advisory.schemas import VariableUpdate
    user = await make_user(db, name="CM")
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.commit()
    extra = await create_global_variable(
        parameter_id=cosh_param.id,
        request=VariableCreate(parameter_id=cosh_param.id, name="se_added"),
        db=db, current_user=user,
    )
    assert extra.cosh_id is None
    out = await update_global_variable(
        parameter_id=cosh_param.id, variable_id=extra.id,
        request=VariableUpdate(name="se_renamed"),
        db=db, current_user=user,
    )
    assert out.name == "se_renamed"


@requires_docker
@pytest.mark.asyncio
async def test_update_cosh_mirrored_variable_refused(db):
    """A variable whose cosh_id is non-NULL (Cosh-mirrored) is
    read-only regardless of the parent parameter's source."""
    from app.modules.advisory.router import update_global_variable
    from app.modules.advisory.schemas import VariableUpdate
    user = await make_user(db, name="CM")
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
        await update_global_variable(
            parameter_id=cosh_param.id, variable_id=cosh_var.id,
            request=VariableUpdate(name="renamed"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cosh_mirrored_variable_readonly"


@requires_docker
@pytest.mark.asyncio
async def test_delete_se_added_variable_on_cosh_parent_skips_min2(db):
    """Deleting an SE-added variable on a Cosh-mirrored parent does
    not trip the min-2 rule (Cosh decides the floor for COSH parents).
    The deletion succeeds even when the post-delete sibling count
    would be ≤ 2."""
    from app.modules.advisory.router import (
        create_global_variable, delete_global_variable,
    )
    user = await make_user(db, name="CM")
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    # one Cosh-mirrored variable + one SE-added one (total 2)
    cosh_var = Variable(
        parameter_id=cosh_param.id, name="Loamy",
        cosh_id="cosh:soil_type:loamy",
    )
    db.add(cosh_var)
    await db.commit()
    extra = await create_global_variable(
        parameter_id=cosh_param.id,
        request=VariableCreate(parameter_id=cosh_param.id, name="se_added"),
        db=db, current_user=user,
    )
    # Post-delete sibling count would be 1 — for a CUSTOM parent this
    # would 422; for COSH-mirrored it goes through.
    await delete_global_variable(
        parameter_id=cosh_param.id, variable_id=extra.id,
        db=db, current_user=user,
    )
    remaining = (await db.execute(
        select(Variable).where(Variable.parameter_id == cosh_param.id)
    )).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == cosh_var.id


@requires_docker
@pytest.mark.asyncio
async def test_delete_cosh_mirrored_variable_refused(db):
    """The Cosh-mirrored variable itself can never be deleted via the
    SA portal — Cosh is the source of truth."""
    from app.modules.advisory.router import delete_global_variable
    user = await make_user(db, name="CM")
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
        await delete_global_variable(
            parameter_id=cosh_param.id, variable_id=cosh_var.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "cosh_mirrored_variable_readonly"
