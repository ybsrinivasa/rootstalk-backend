"""Global PV authoring (Batch 9, 2026-05-11).

The CM needs to set a parameter-variable signature on Global
Packages so multiple Globals for the same crop are distinguishable
(e.g. Tomato-Drip vs Tomato-Flood). On push, the signature
deep-copies to the Local Package; the client-side §4.2 sibling
check picks it up.

Coverage:
  - Global Parameter CRUD with client_id IS NULL.
  - Global Variable add under Global Parameter.
  - Set/get Global PackageVariable fingerprint.
  - push deep-copies PVs from Global to Local.
  - pull deep-copies PVs likewise.
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
    pull_global_package, push_global_package,
    set_global_package_variables,
)
from app.modules.advisory.schemas import (
    ParameterCreate, PackageVariableSet, VariableCreate,
)
from app.modules.clients.models import ClientCrop, ClientUserRole
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_cm_assignment, make_user,
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
        ),
        db=db, current_user=user,
    )
    assert out.client_id is None
    assert out.crop_cosh_id == "crop:tomato"
    assert out.name == "Irrigation"
    assert out.source == ParameterSource.CUSTOM


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
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation"),
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
    assert names == {"Drip", "Flood"}
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
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation"),
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
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation"),
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


# ── push + pull deep-copy ─────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_deep_copies_global_pv_signature_to_local(db):
    """When CM pushes a Global with PVs set, the new Local DRAFT
    inherits the PackageVariable rows pointing at the same
    Parameter and Variable ids (no row cloning)."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)

    param = await create_global_parameter(
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation"),
        db=db, current_user=cm,
    )
    drip = await create_global_variable(
        parameter_id=param.id, request=VariableCreate(parameter_id=param.id, name="Drip"),
        db=db, current_user=cm,
    )
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    db.add(ClientCrop(client_id=client.id, crop_cosh_id="crop:tomato"))
    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[{"parameter_id": param.id, "variable_id": drip.id}]),
        db=db, current_user=cm,
    )
    await db.commit()

    local = await push_global_package(
        client_id=client.id, pkg_id=pkg.id, db=db, current_user=cm,
    )
    pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == local.id)
    )).scalars().all()
    assert len(pvs) == 1
    assert pvs[0].parameter_id == param.id  # same Global Parameter row
    assert pvs[0].variable_id == drip.id    # same Global Variable row


@requires_docker
@pytest.mark.asyncio
async def test_pull_deep_copies_pv_signature_too(db):
    """SE pulling a refresh gets the same PV inheritance — keeps
    the lineage's discriminator stable across pulls."""
    cm = await make_user(db, name="CM")
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    cu = await make_client_user(db, user=se, client=client)
    cu.role = ClientUserRole.SUBJECT_EXPERT
    cu.status = StatusEnum.ACTIVE

    param = await create_global_parameter(
        request=ParameterCreate(crop_cosh_id="crop:tomato", name="Irrigation"),
        db=db, current_user=cm,
    )
    drip = await create_global_variable(
        parameter_id=param.id, request=VariableCreate(parameter_id=param.id, name="Drip"),
        db=db, current_user=cm,
    )
    pkg = await _seed_global_pkg(db, crop="crop:tomato")
    db.add(ClientCrop(client_id=client.id, crop_cosh_id="crop:tomato"))
    await set_global_package_variables(
        pkg_id=pkg.id,
        request=PackageVariableSet(assignments=[{"parameter_id": param.id, "variable_id": drip.id}]),
        db=db, current_user=cm,
    )
    await db.commit()

    pushed = await push_global_package(
        client_id=client.id, pkg_id=pkg.id, db=db, current_user=cm,
    )
    pushed.status = PackageStatus.ACTIVE  # simulate SE publish
    await db.commit()

    pulled = await pull_global_package(
        client_id=client.id, pkg_id=pkg.id, db=db, current_user=se,
    )
    pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == pulled.id)
    )).scalars().all()
    assert len(pvs) == 1
    assert pvs[0].parameter_id == param.id
    assert pvs[0].variable_id == drip.id


# ── CA list_parameters now returns Globals + client-scoped ──────────────────

@requires_docker
@pytest.mark.asyncio
async def test_ca_list_parameters_includes_globals(db):
    """SE-side list_parameters returns both the client's CUSTOM
    parameters AND the Globals — so the SE sees the inherited PV
    after a pulled refresh."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    # Global parameter (visible).
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=None,
        name="GlobalIrrigation", source=ParameterSource.CUSTOM,
    ))
    # Client-scoped parameter (also visible to this client).
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=client.id,
        name="ClientCustom", source=ParameterSource.CUSTOM,
    ))
    # Other-client parameter (must NOT appear).
    other = await make_client(db)
    db.add(Parameter(
        crop_cosh_id="crop:tomato", client_id=other.id,
        name="OtherClient", source=ParameterSource.CUSTOM,
    ))
    await db.commit()

    out = await list_parameters(
        client_id=client.id, crop_cosh_id="crop:tomato",
        db=db, current_user=se,
    )
    names = {p.name for p in out}
    assert "GlobalIrrigation" in names
    assert "ClientCustom" in names
    assert "OtherClient" not in names
