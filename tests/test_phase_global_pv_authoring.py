"""Global PV authoring (Batch 9 2026-05-11; updated through Batch EE
2026-05-19 — SA is pure-Cosh).

Batch EE locked the rule: SA-CCA cannot author Global-Custom
Parameters or Variables. Every Global-Custom write endpoint now
422s with code `global_custom_writes_disabled`. The pure-Cosh path
(read-back, Cosh-sourced PV assignments, push-PV-isolation) still
works.

Coverage:
  - Every Global-Custom write endpoint refuses (create/update/delete
    Parameter and Variable; set_global_package_variables with a
    CUSTOM parent or non-Cosh variable).
  - Cosh-sourced read paths still work.
  - Cosh-sourced PV assignments still flow through set + list +
    push (Global PV signature stays an SA identifier; Local takes
    the form's PVs).
  - CA-side `list_parameters` still hides Global-Customs (per-client
    isolation) — historical rows continue to exist in the DB but
    don't appear on either portal.
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
    delete_global_parameter, delete_global_variable,
    list_global_package_variables, list_global_parameters,
    list_global_variables, list_parameters,
    push_global_package, set_global_package_variables,
    update_global_parameter, update_global_variable,
)
from app.modules.advisory.schemas import (
    PackagePushRequest, ParameterCreate, ParameterUpdate,
    PackageVariableSet, VariableCreate, VariableNameIn, VariableUpdate,
)
from app.modules.clients.models import ClientCrop, ClientUserRole
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_cm_assignment,
    make_push_request_body, make_user,
)


_SEED_VARS = [VariableNameIn(name="__seed_a__"), VariableNameIn(name="__seed_b__")]


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


async def _seed_cosh_param_with_two_vars(
    db, *, crop="crop:tomato", name="CoshIrrigation",
    cosh_id_suffix="irrigation",
):
    """Seed a Cosh-sourced Global Parameter + two Cosh-sourced
    Variables via the ORM — Batch EE refuses the
    create_global_parameter endpoint, but Cosh-sourced rows still
    arrive through the Cosh sync pipe in production. These ORM
    seeds simulate that path for tests that need a valid Cosh PV
    to assign on a Global Package."""
    param = Parameter(
        crop_cosh_id=crop, client_id=None, name=name,
        source=ParameterSource.COSH,
        cosh_id=f"cosh:{cosh_id_suffix}",
    )
    db.add(param)
    await db.flush()
    v1 = Variable(
        parameter_id=param.id, name=f"{name}-A",
        cosh_id=f"cosh:{cosh_id_suffix}:a",
    )
    v2 = Variable(
        parameter_id=param.id, name=f"{name}-B",
        cosh_id=f"cosh:{cosh_id_suffix}:b",
    )
    db.add_all([v1, v2])
    await db.flush()
    return param, v1, v2


# ── Batch EE: every Global-Custom write endpoint refuses ───────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_global_parameter_refuses_pure_cosh(db):
    """Batch EE: create_global_parameter unconditionally 422s.
    Every new Global parameter would be CUSTOM, which the
    pure-Cosh rule disallows."""
    user = await make_user(db, name="CM")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_parameter(
            request=ParameterCreate(
                crop_cosh_id="crop:tomato", name="Irrigation",
                display_order=0, variables=_SEED_VARS,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


@requires_docker
@pytest.mark.asyncio
async def test_update_global_parameter_refuses_custom(db):
    """Batch EE: historical Global-Custom parameter renames are
    refused. CM cannot rename SA-side."""
    user = await make_user(db, name="CM")
    legacy = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="LegacyCustom", source=ParameterSource.CUSTOM,
    )
    db.add(legacy)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_global_parameter(
            parameter_id=legacy.id,
            request=ParameterUpdate(name="Renamed"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


@requires_docker
@pytest.mark.asyncio
async def test_update_global_parameter_refuses_cosh_mirrored(db):
    """Cosh-mirrored 422 still fires first (kept stable for the
    UI's existing handler). Either error code closes the surface."""
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
async def test_delete_global_parameter_refuses_custom(db):
    """Batch EE: historical Global-Custom parameter deletes are
    refused. Orphan cleanup is a separate data-migration batch."""
    user = await make_user(db, name="CM")
    legacy = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="LegacyCustom", source=ParameterSource.CUSTOM,
    )
    db.add(legacy)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await delete_global_parameter(
            parameter_id=legacy.id, db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


@requires_docker
@pytest.mark.asyncio
async def test_create_global_variable_refuses(db):
    """Batch EE: adding a Variable on any Global Parameter is
    refused. A new variable would carry no cosh_id (no Cosh source)
    and no client_id (Global scope), which the pure-Cosh rule
    disallows."""
    user = await make_user(db, name="CM")
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_variable(
            parameter_id=cosh_param.id,
            request=VariableCreate(parameter_id=cosh_param.id, name="X"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


@requires_docker
@pytest.mark.asyncio
async def test_update_global_variable_refuses_legacy_se_added(db):
    """Batch EE: historical SE-added variables on Cosh parents
    (cosh_id IS NULL, client_id IS NULL) are now read-only."""
    user = await make_user(db, name="CM")
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    legacy_var = Variable(parameter_id=cosh_param.id, name="legacy_se_added")
    db.add(legacy_var)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_global_variable(
            parameter_id=cosh_param.id, variable_id=legacy_var.id,
            request=VariableUpdate(name="renamed"),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


@requires_docker
@pytest.mark.asyncio
async def test_update_cosh_mirrored_variable_refused(db):
    """A variable whose cosh_id is non-NULL (Cosh-mirrored) is
    read-only regardless of parent. Cosh check fires first; either
    code closes the surface."""
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
async def test_delete_global_variable_refuses_legacy_se_added(db):
    """Batch EE: historical SE-added variables on Cosh parents are
    now read-only — deletes refused too."""
    user = await make_user(db, name="CM")
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    legacy_var = Variable(parameter_id=cosh_param.id, name="legacy_se_added")
    db.add(legacy_var)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await delete_global_variable(
            parameter_id=cosh_param.id, variable_id=legacy_var.id,
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


@requires_docker
@pytest.mark.asyncio
async def test_delete_cosh_mirrored_variable_refused(db):
    """The Cosh-mirrored variable itself can never be deleted via
    the SA portal — Cosh is the source of truth (this check fires
    before the Batch EE gate; either code closes the surface)."""
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


@requires_docker
@pytest.mark.asyncio
async def test_set_global_package_variables_refuses_custom_parent(db):
    """Batch EE: set_global_package_variables refuses if any
    assignment references a Global-Custom Parameter."""
    user = await make_user(db, name="CM")
    legacy_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="LegacyCustom", source=ParameterSource.CUSTOM,
    )
    db.add(legacy_param)
    await db.flush()
    v = Variable(parameter_id=legacy_param.id, name="LegacyVar")
    db.add(v)
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await set_global_package_variables(
            pkg_id=pkg.id,
            request=PackageVariableSet(assignments=[
                {"parameter_id": legacy_param.id, "variable_id": v.id},
            ]),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


@requires_docker
@pytest.mark.asyncio
async def test_set_global_package_variables_refuses_non_cosh_variable(db):
    """Batch EE: set_global_package_variables refuses if any
    assigned Variable has no cosh_id (SE-added legacy custom even
    when sitting on a Cosh parent)."""
    user = await make_user(db, name="CM")
    cosh_param = Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="Soil Type", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    )
    db.add(cosh_param)
    await db.flush()
    legacy_var = Variable(parameter_id=cosh_param.id, name="se_added")
    db.add(legacy_var)
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await set_global_package_variables(
            pkg_id=pkg.id,
            request=PackageVariableSet(assignments=[
                {"parameter_id": cosh_param.id, "variable_id": legacy_var.id},
            ]),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_custom_writes_disabled"


# ── Pure-Cosh read + assignment path still works ─────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_global_parameters_excludes_client_scoped(db):
    """The list endpoint stays read-only and works for Cosh-sourced
    Globals; client-scoped Customs stay hidden."""
    user = await make_user(db, name="CM")
    client = await make_client(db)
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="CoshIrrigation", source=ParameterSource.COSH,
        cosh_id="cosh:irrigation",
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
    assert "CoshIrrigation" in names
    assert "ClientOnly" not in names


@requires_docker
@pytest.mark.asyncio
async def test_list_global_variables_returns_cosh_sourced(db):
    """list_global_variables stays read-only and returns the
    Cosh-sourced variables on a Cosh parent."""
    user = await make_user(db, name="CM")
    param, v1, v2 = await _seed_cosh_param_with_two_vars(db)
    await db.commit()
    out = await list_global_variables(
        parameter_id=param.id, db=db, current_user=user,
    )
    names = {v.name for v in out}
    assert names == {v1.name, v2.name}


@requires_docker
@pytest.mark.asyncio
async def test_set_and_list_global_package_variables_cosh_only(db):
    """The PUT/GET PV-signature pair still works with Cosh-sourced
    Parameters + Variables (the only legal source after Batch EE)."""
    user = await make_user(db, name="CM")
    param, v1, _ = await _seed_cosh_param_with_two_vars(db)
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    await db.commit()

    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param.id, "variable_id": v1.id},
        ]),
        db=db, current_user=user,
    )
    out = await list_global_package_variables(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert len(out) == 1
    assert out[0]["parameter_id"] == param.id
    assert out[0]["variable_id"] == v1.id


@requires_docker
@pytest.mark.asyncio
async def test_set_global_package_variables_replaces_existing_cosh_only(db):
    """PUT semantics — second call replaces the first. Both
    assignments are Cosh-sourced; pure-Cosh path."""
    user = await make_user(db, name="CM")
    p, v1, v2 = await _seed_cosh_param_with_two_vars(db)
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    await db.commit()

    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p.id, "variable_id": v1.id},
        ]),
        db=db, current_user=user,
    )
    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p.id, "variable_id": v2.id},
        ]),
        db=db, current_user=user,
    )
    out = await list_global_package_variables(
        pkg_id=pkg.id, db=db, current_user=user,
    )
    assert len(out) == 1
    assert out[0]["variable_id"] == v2.id


# ── push isolates Global PVs from Local (Batch 39N-a; pure-Cosh) ──────────

@requires_docker
@pytest.mark.asyncio
async def test_push_does_not_carry_global_pvs_to_local_cosh_only(db):
    """Global PV signature uses Cosh-sourced P-V. At push time, Ram
    re-picks PVs in the form — Local takes the form's PVs, NOT
    Global's. Per Batch 39N-a, Global PVs are SA-identifier only."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)

    global_param, drip, flood = await _seed_cosh_param_with_two_vars(
        db, name="CoshIrrigation",
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
    assert pvs[0].variable_id == flood.id
    assert pvs[0].variable_id != drip.id


# ── CA list_parameters: Cosh-globals + own customs; never SA-Customs ──────

@requires_docker
@pytest.mark.asyncio
async def test_ca_list_parameters_cosh_global_and_own_customs(db):
    """CA-side list_parameters returns ONLY:
      - Cosh-mirrored Globals (source=COSH, client_id IS NULL).
      - This client's own Customs.
    Excludes Global-Customs (historical SA artefact) and other
    clients' Customs (per-client isolation)."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="CoshSoilType", source=ParameterSource.COSH,
        cosh_id="cosh:soil_type",
    ))
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="GlobalCustomIrrigation", source=ParameterSource.CUSTOM,
    ))
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=client.id,
        name="ClientCustom", source=ParameterSource.CUSTOM,
    ))
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


# ── Schema-level still 422s the ≥ 2 rule (defensive; endpoint refuses) ───

@pytest.mark.asyncio
async def test_parameter_create_rejects_fewer_than_2_variables():
    """Schema enforces min_length=2 on variables. Even though the
    endpoint itself now refuses, the pydantic ValidationError still
    fires at construction time — keeps the schema contract pinned."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        ParameterCreate(
            crop_cosh_id="crop:tomato", name="X",
            variables=[VariableNameIn(name="solo")],
        )
