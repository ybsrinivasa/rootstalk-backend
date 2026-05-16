"""Push-form validation gates (Batch 39N-a, 2026-05-16).

Covers the field-level refusal codes the form-driven push surfaces
to Ram before any deep-copy runs:

  • `duplicate_package_name`        — name clashes with existing DRAFT/ACTIVE
                                       in (client, crop)
  • `location_not_onboarded`        — at least one (state, district) is not
                                       on the client's ACTIVE ClientLocation list
  • `custom_pv_not_allowed_on_push` — Parameter has client_id != NULL
                                       (CUSTOM, not catalogue)
  • `pv_crop_mismatch`              — Parameter belongs to a different crop
  • `invalid_pv_assignment`         — variable doesn't belong to its parameter
  • `invalid_author`                — user is not an ACTIVE SE ClientUser
  • `duplicate_author`              — same user_id appears twice
  • `pv_conflict_with_sibling`      — §4.2 district-overlap PV clash with
                                       existing client-side sibling

Each gate names the offending value(s) in the 422 body so Ram can
fix and retry.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType, Practice, PracticeL0,
    Timeline, TimelineFromType,
)
from app.modules.advisory.router import push_global_package
from app.modules.advisory.schemas import PackagePushRequest
from app.modules.clients.models import (
    ClientCrop, ClientLocation, ClientUser, ClientUserRole,
)
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_cm_assignment, make_parameter, make_push_request_body,
    make_user, make_variable,
)


async def _seed_global(db, *, crop="crop:tomato", name="GP"):
    pkg = Package(
        client_id=None, name=name, crop_cosh_id=crop,
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    tl = Timeline(
        package_id=pkg.id, name="TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=10,
    )
    db.add(tl)
    await db.flush()
    db.add(Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER", l2_type="UREA",
    ))
    await db.flush()
    return pkg


async def _setup(db, *, crop="crop:tomato"):
    """CM + assigned client + Global with content. Ready for push."""
    cm = await make_user(db, name=f"CM-{uuid.uuid4().hex[:4]}")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    gpkg = await _seed_global(db, crop=crop, name=f"GP-{uuid.uuid4().hex[:6]}")
    db.add(ClientCrop(client_id=client.id, crop_cosh_id=crop))
    await db.commit()
    return cm, client, gpkg


@requires_docker
@pytest.mark.asyncio
async def test_duplicate_package_name_blocks_push(db):
    cm, client, gpkg = await _setup(db)
    # First push lands name "Tomato".
    body1 = await make_push_request_body(
        db, client=client, src=gpkg, name="Tomato",
    )
    await db.commit()
    await push_global_package(
        client_id=client.id, pkg_id=gpkg.id,
        request=PackagePushRequest(**body1),
        db=db, current_user=cm,
    )
    # Second Global, same crop, same target client — try to reuse the
    # name. The push-once gate would normally catch a same-Global
    # retry; we use a different Global lineage to isolate the name
    # check.
    other_gpkg = await _seed_global(
        db, crop=gpkg.crop_cosh_id, name=f"GP-{uuid.uuid4().hex[:6]}",
    )
    body2 = await make_push_request_body(
        db, client=client, src=other_gpkg, name="Tomato",
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=other_gpkg.id,
            request=PackagePushRequest(**body2),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "duplicate_package_name"


@requires_docker
@pytest.mark.asyncio
async def test_location_not_onboarded_blocks_push(db):
    cm, client, gpkg = await _setup(db)
    body = await make_push_request_body(db, client=client, src=gpkg)
    # Replace with an off-list district.
    body["locations"] = [
        {"state_cosh_id": "state:test",
         "district_cosh_id": "district:not-onboarded"},
    ]
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id,
            request=PackagePushRequest(**body),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "location_not_onboarded"
    assert exc.value.detail["invalid_locations"] == [
        {"state_cosh_id": "state:test",
         "district_cosh_id": "district:not-onboarded"},
    ]


@requires_docker
@pytest.mark.asyncio
async def test_custom_pv_blocks_push(db):
    """A Parameter with client_id set (CUSTOM, not catalogue) is
    refused at push time. Custom PVs are added later by the SE in
    their own scope."""
    cm, client, gpkg = await _setup(db)
    # Custom param tied to the client (NOT NULL client_id).
    from app.modules.advisory.models import Parameter, ParameterSource, Variable
    custom_param = Parameter(
        crop_cosh_id=gpkg.crop_cosh_id, client_id=client.id,
        name="CustomP", source=ParameterSource.CUSTOM,
    )
    db.add(custom_param)
    await db.flush()
    custom_var = Variable(parameter_id=custom_param.id, name="CV")
    db.add(custom_var)
    await db.flush()

    body = await make_push_request_body(db, client=client, src=gpkg)
    body["pv_assignments"] = [
        {"parameter_id": custom_param.id, "variable_id": custom_var.id},
    ]
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id,
            request=PackagePushRequest(**body),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "custom_pv_not_allowed_on_push"


@requires_docker
@pytest.mark.asyncio
async def test_pv_crop_mismatch_blocks_push(db):
    """A catalogue Parameter belonging to a different crop is refused."""
    cm, client, gpkg = await _setup(db, crop="crop:tomato")
    other_crop_param = await make_parameter(
        db, crop_cosh_id="crop:paddy", name="WrongCrop",
    )
    other_crop_var = await make_variable(db, other_crop_param, name="V")

    body = await make_push_request_body(db, client=client, src=gpkg)
    body["pv_assignments"] = [
        {"parameter_id": other_crop_param.id,
         "variable_id": other_crop_var.id},
    ]
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id,
            request=PackagePushRequest(**body),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "pv_crop_mismatch"


@requires_docker
@pytest.mark.asyncio
async def test_invalid_pv_assignment_blocks_push(db):
    """Variable doesn't belong to its declared Parameter — refused."""
    cm, client, gpkg = await _setup(db)
    param_a = await make_parameter(
        db, crop_cosh_id=gpkg.crop_cosh_id, name="A",
    )
    param_b = await make_parameter(
        db, crop_cosh_id=gpkg.crop_cosh_id, name="B",
    )
    var_b = await make_variable(db, param_b, name="Vb")

    body = await make_push_request_body(db, client=client, src=gpkg)
    # Wrong wiring: var_b belongs to param_b, not param_a.
    body["pv_assignments"] = [
        {"parameter_id": param_a.id, "variable_id": var_b.id},
    ]
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id,
            request=PackagePushRequest(**body),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_pv_assignment"


@requires_docker
@pytest.mark.asyncio
async def test_invalid_author_blocks_push(db):
    """A user that's not an ACTIVE SE on this client cannot be an author."""
    cm, client, gpkg = await _setup(db)
    body = await make_push_request_body(db, client=client, src=gpkg)
    body["author_ids"] = ["not-an-se-id"]
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id,
            request=PackagePushRequest(**body),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_author"
    assert "not-an-se-id" in exc.value.detail["invalid_user_ids"]


@requires_docker
@pytest.mark.asyncio
async def test_duplicate_author_blocks_push(db):
    cm, client, gpkg = await _setup(db)
    body = await make_push_request_body(db, client=client, src=gpkg)
    # Duplicate the seeded SE id.
    body["author_ids"] = body["author_ids"] * 2
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id,
            request=PackagePushRequest(**body),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "duplicate_author"


@requires_docker
@pytest.mark.asyncio
async def test_pv_signature_conflict_blocks_push(db):
    """§4.2 district-overlap PV uniqueness: pushing with the same PV
    signature into a client that already has a sibling Package
    sharing a district must refuse, naming the sibling."""
    cm, client, gpkg = await _setup(db)
    # First push: lands a Local with one location + one PV.
    body1 = await make_push_request_body(
        db, client=client, src=gpkg, name="Variant A",
    )
    await db.commit()
    await push_global_package(
        client_id=client.id, pkg_id=gpkg.id,
        request=PackagePushRequest(**body1),
        db=db, current_user=cm,
    )

    # Second Global (separate lineage so the once-per gate doesn't
    # fire first), same crop. Try to push with the same district +
    # same PV signature as the first push.
    other_gpkg = await _seed_global(
        db, crop=gpkg.crop_cosh_id, name=f"GP-{uuid.uuid4().hex[:6]}",
    )
    body2 = await make_push_request_body(
        db, client=client, src=other_gpkg, name="Variant B",
    )
    # Reuse Variant A's locations + PVs so the signatures clash.
    body2["locations"] = body1["locations"]
    body2["pv_assignments"] = body1["pv_assignments"]
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=other_gpkg.id,
            request=PackagePushRequest(**body2),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "pv_conflict_with_sibling"
    # The 422 body names the sibling for Ram's fix path.
    assert "conflicts" in exc.value.detail
    assert exc.value.detail["conflicts"][0]["sibling_package_name"] == "Variant A"
