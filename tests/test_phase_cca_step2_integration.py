"""CCA Step 2 — DB-backed integration tests for Package field
validation (Batch 2A: duration range + Perennial lock + Start Date
Label required at create).

Pure-function coverage of `validate_package_duration_for_*` lives in
`tests/test_package_validation.py`. This file drives `create_package`
and `update_package` against the testcontainer DB to verify the
validators produce the right HTTP responses.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.advisory.models import (
    Package, PackageStatus, PackageType,
    PackageAuthor, PackageVariable, PackageLocation, Parameter, Variable,
)
from app.modules.advisory.router import (
    create_package, update_package, set_package_locations,
    set_package_variables, publish_package,
    list_package_authors, set_package_authors,
)
from app.modules.advisory.schemas import (
    PackageCreate, PackageUpdate,
    PackageLocationIn, PackageAuthorIn, PackageVariableSet,
)
from app.modules.clients.models import (
    Client, ClientCrop, ClientUser, ClientUserRole,
)
from app.modules.clients.router import add_crop
from app.modules.clients.schemas import CropCreate
from app.modules.platform.models import StatusEnum, User as UserModel
from tests.conftest import requires_docker
from tests.factories import make_client, make_crop_reference, make_user


async def _seed_paddy_on_belt(db, client, user) -> None:
    """Seed paddy in Cosh + add to the company conveyor belt."""
    await make_crop_reference(db, "crop:paddy", measure="AREA_WISE")
    await db.commit()
    await add_crop(
        client_id=client.id, request=CropCreate(crop_cosh_id="crop:paddy"),
        db=db, current_user=user,
    )


# ── create_package — duration validation ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_annual_with_valid_duration(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    out = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name="Paddy Pop A",
            package_type=PackageType.ANNUAL, duration_days=120,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )
    assert out.duration_days == 120
    assert out.package_type == PackageType.ANNUAL


@requires_docker
@pytest.mark.asyncio
async def test_create_annual_missing_duration_422(db):
    """Spec rule: Annual duration is mandatory. Pre-fix the route
    silently defaulted to 180 days — fail loud instead so the CA
    portal can prompt the expert."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    with pytest.raises(HTTPException) as ei:
        await create_package(
            client_id=client.id,
            request=PackageCreate(
                crop_cosh_id="crop:paddy", name="Paddy Missing Duration",
                package_type=PackageType.ANNUAL,
                # duration_days deliberately omitted
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "duration_required"


@requires_docker
@pytest.mark.asyncio
async def test_create_annual_out_of_range_422(db):
    """A typo (e.g. 9999) must be rejected at create rather than
    shipping a Package with insane timeline arithmetic."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    with pytest.raises(HTTPException) as ei:
        await create_package(
            client_id=client.id,
            request=PackageCreate(
                crop_cosh_id="crop:paddy", name="Paddy Crazy Duration",
                package_type=PackageType.ANNUAL, duration_days=9999,
                start_date_label_cosh_id="label:sowing_date",
            ),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "duration_out_of_range"


@requires_docker
@pytest.mark.asyncio
async def test_create_perennial_forces_365(db):
    """Spec §4.1: Perennial duration is system-set. Whatever the
    caller sends (including None or a typo), persist 365."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)

    out = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name="Paddy Perennial",
            package_type=PackageType.PERENNIAL, duration_days=100,
            start_date_label_cosh_id="label:planting_date",
        ),
        db=db, current_user=user,
    )
    assert out.duration_days == 365


@requires_docker
@pytest.mark.asyncio
async def test_create_missing_start_date_label_422(db):
    """Spec §4.1: Start Date Label is mandatory at create time.
    Validated at the Pydantic layer (not Optional anymore)."""
    client = await make_client(db)
    await db.commit()

    with pytest.raises(ValidationError):
        # Pydantic itself rejects — no router call needed
        PackageCreate(
            crop_cosh_id="crop:paddy", name="No Label PoP",
            package_type=PackageType.ANNUAL, duration_days=120,
            # start_date_label_cosh_id deliberately omitted
        )


# ── update_package — Perennial lock + Annual range ───────────────────────────

async def _create_test_package(
    db, *, client, user, package_type=PackageType.ANNUAL, duration_days=120,
    name: str | None = None,
):
    return await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy",
            name=name or f"PoP {package_type.value} {duration_days}",
            package_type=package_type, duration_days=duration_days,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )


@requires_docker
@pytest.mark.asyncio
async def test_update_annual_duration_to_valid_range(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)

    out = await update_package(
        client_id=client.id, package_id=pkg.id,
        request=PackageUpdate(duration_days=180),
        db=db, current_user=user,
    )
    assert out.duration_days == 180


@requires_docker
@pytest.mark.asyncio
async def test_update_annual_out_of_range_422(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)

    with pytest.raises(HTTPException) as ei:
        await update_package(
            client_id=client.id, package_id=pkg.id,
            request=PackageUpdate(duration_days=500),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "duration_out_of_range"


@requires_docker
@pytest.mark.asyncio
async def test_update_perennial_duration_change_blocked_422(db):
    """The headline rule: Perennial duration is locked. Pre-fix
    `update_package` blindly setattr'd whatever was sent — flipping
    a Perennial to 100 days would have broken advisory alignment.
    Now blocked with a stable error code."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(
        db, client=client, user=user,
        package_type=PackageType.PERENNIAL, duration_days=365,
    )

    with pytest.raises(HTTPException) as ei:
        await update_package(
            client_id=client.id, package_id=pkg.id,
            request=PackageUpdate(duration_days=100),
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "perennial_duration_locked"


@requires_docker
@pytest.mark.asyncio
async def test_update_perennial_resending_365_accepted(db):
    """Friendly-client rule: re-sending the unchanged 365 value
    shouldn't fail. Frontend bodies often include all fields."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(
        db, client=client, user=user,
        package_type=PackageType.PERENNIAL, duration_days=365,
    )

    out = await update_package(
        client_id=client.id, package_id=pkg.id,
        request=PackageUpdate(duration_days=365),
        db=db, current_user=user,
    )
    assert out.duration_days == 365


@requires_docker
@pytest.mark.asyncio
async def test_update_other_fields_unaffected(db):
    """A name/description update on a Perennial package should not
    trip the duration guard — duration_days isn't in the body."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(
        db, client=client, user=user,
        package_type=PackageType.PERENNIAL, duration_days=365,
    )

    out = await update_package(
        client_id=client.id, package_id=pkg.id,
        request=PackageUpdate(name="Renamed Perennial PoP", description="A note"),
        db=db, current_user=user,
    )
    assert out.name == "Renamed Perennial PoP"
    assert out.description == "A note"
    assert out.duration_days == 365  # unchanged


# ── Batch 2D: same-district P/V uniqueness ───────────────────────────────────

async def _create_two_packages(db, *, client, user):
    """Spin up two empty Annual paddy PoPs (A and B) for the
    uniqueness tests to manipulate."""
    pkg_a = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name="PoP A",
            package_type=PackageType.ANNUAL, duration_days=120,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )
    pkg_b = await create_package(
        client_id=client.id,
        request=PackageCreate(
            crop_cosh_id="crop:paddy", name="PoP B",
            package_type=PackageType.ANNUAL, duration_days=120,
            start_date_label_cosh_id="label:sowing_date",
        ),
        db=db, current_user=user,
    )
    return pkg_a, pkg_b


async def _make_param_with_two_vars(db, *, client_id, name="Season"):
    """Seed a custom Parameter with two Variables. Returns
    (parameter_id, var1_id, var2_id)."""
    param = Parameter(
        crop_cosh_id="crop:paddy", client_id=client_id,
        name=name, display_order=0,
    )
    db.add(param)
    await db.flush()
    v1 = Variable(parameter_id=param.id, name=f"{name} V1")
    v2 = Variable(parameter_id=param.id, name=f"{name} V2")
    db.add(v1)
    db.add(v2)
    await db.flush()
    return param.id, v1.id, v2.id


@requires_docker
@pytest.mark.asyncio
async def test_set_pv_blocks_when_sibling_has_same_fingerprint_and_shared_district(db):
    """Headline rule. Both PoPs cover District D. Both want
    {Season: Kharif}. Saving the second one's fingerprint must 422."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    param_id, v1, _v2 = await _make_param_with_two_vars(db, client_id=client.id)
    await db.commit()

    # PoP A: D1 + {param: v1}. PoP B: set PV FIRST (no location yet,
    # so the uniqueness check short-circuits), then location D1 — at
    # which point 2D fires because B's identical fingerprint now
    # overlaps A's district. Setting PV before location matches the
    # realistic expert workflow (build the PV config, then assign
    # districts) and avoids tripping 2E's "empty vs non-empty
    # parameter set" violation prematurely.
    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )

    # PoP B now tries to add the shared district — must be blocked.
    with pytest.raises(HTTPException) as ei:
        await set_package_locations(
            client_id=client.id, package_id=pkg_b.id,
            locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "pv_conflict_with_sibling"
    conflicts = ei.value.detail["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["sibling_package_id"] == pkg_a.id
    assert conflicts[0]["sibling_package_name"] == "PoP A"
    assert conflicts[0]["shared_districts"] == [
        {"state_cosh_id": "S1", "district_cosh_id": "D1"},
    ]


@requires_docker
@pytest.mark.asyncio
async def test_set_pv_succeeds_when_fingerprint_differs(db):
    """Same shared district, different fingerprints — exactly the
    valid case. Save proceeds normally."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    param_id, v1, v2 = await _make_param_with_two_vars(db, client_id=client.id)
    await db.commit()

    # A: location + PV. B: PV first (no district yet, check short-
    # circuits), then location — at which point both 2D and 2E run
    # against the new shared district. 2D doesn't fire (different
    # values). 2E doesn't fire (same parameter set). Save proceeds.
    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v2},
        ]),
        db=db, current_user=user,
    )
    out = await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    assert "saved" in out["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_set_pv_succeeds_when_no_shared_district(db):
    """Same fingerprint but no shared district — no conflict; the
    farmer in District D picks one PoP, the farmer in District E
    picks the other."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    param_id, v1, _v2 = await _make_param_with_two_vars(db, client_id=client.id)
    await db.commit()

    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D2")],
        db=db, current_user=user,
    )

    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    out = await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    assert "saved" in out["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_set_pv_blocks_both_empty_fingerprints_in_shared_district(db):
    """Spec §4.2: when a 2nd PoP gains shared coverage, both PoPs
    must have P/V populated — empty-on-both is a violation. The
    algorithm catches this naturally because `{} == {}` is True."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    await db.commit()

    # PoP A gets D1 (no sibling overlap yet). Neither A nor B has P/V.
    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )

    # Now PoP B tries to also cover D1. Both have empty PV, so the
    # algorithm sees `{} == {}` for two PoPs sharing a district —
    # exactly the §4.2 violation. The location save must 422.
    with pytest.raises(HTTPException) as ei:
        await set_package_locations(
            client_id=client.id, package_id=pkg_b.id,
            locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "pv_conflict_with_sibling"


@requires_docker
@pytest.mark.asyncio
async def test_set_locations_blocks_when_new_district_creates_conflict(db):
    """An expert is editing PoP B's location list. PoP A already
    covers D1 with fingerprint {P: V1}. PoP B has the same
    fingerprint but covers D2 — fine so far. The expert tries to
    ADD D1 to PoP B's locations. Set-locations must catch the
    newly-created conflict."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    param_id, v1, _v2 = await _make_param_with_two_vars(db, client_id=client.id)
    await db.commit()

    # PoP A: D1 + {P: v1}.
    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    # PoP B: D2 (no overlap yet) + {P: v1}.
    await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D2")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )

    # Now add D1 to PoP B — conflict surfaces at the location save.
    with pytest.raises(HTTPException) as ei:
        await set_package_locations(
            client_id=client.id, package_id=pkg_b.id,
            locations=[
                PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1"),
                PackageLocationIn(state_cosh_id="S1", district_cosh_id="D2"),
            ],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "pv_conflict_with_sibling"


@requires_docker
@pytest.mark.asyncio
async def test_inactive_sibling_does_not_block(db):
    """Replacement workflow: PoP A is INACTIVE (e.g. cascade-
    inactivated by a CA crop removal that was later reversed, or a
    superseded older version). PoP B is being built fresh and may
    legitimately reuse A's old fingerprint. Uniqueness check must
    skip INACTIVE siblings."""
    from sqlalchemy import select as sql_select

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    param_id, v1, _v2 = await _make_param_with_two_vars(db, client_id=client.id)
    await db.commit()

    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    # Mark PoP A as INACTIVE directly (simulates supersession).
    pkg_a_db = (await db.execute(
        sql_select(Package).where(Package.id == pkg_a.id)
    )).scalar_one()
    pkg_a_db.status = PackageStatus.INACTIVE
    await db.commit()

    # Now PoP B can reuse A's fingerprint + district without conflict.
    await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    out = await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    assert "saved" in out["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocks_on_conflict_defensively(db):
    """Defensive last-line check: if a conflict somehow exists at
    publish time (e.g. a sibling was edited concurrently or rows
    were inserted via SQL), publish_package refuses to bump the
    version. We force a conflict directly via the ORM to bypass the
    save-time guards and verify publish itself catches it."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    param_id, v1, _v2 = await _make_param_with_two_vars(db, client_id=client.id)
    await db.commit()

    # Bypass the save-time guards — set both PoPs to identical state
    # via direct ORM inserts. Add an author to PoP B so the publish
    # gate (Batch 2C) doesn't surface "no_authors" first; we want
    # the 2D defensive check to be the failure mode under test.
    se = await _make_subject_expert(db, client=client, name="Author")
    for pkg in (pkg_a, pkg_b):
        db.add(PackageLocation(
            package_id=pkg.id, state_cosh_id="S1", district_cosh_id="D1",
        ))
        db.add(PackageVariable(
            package_id=pkg.id, parameter_id=param_id, variable_id=v1,
        ))
    db.add(PackageAuthor(package_id=pkg_b.id, user_id=se.id))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg_b.id,
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "pv_conflict_with_sibling"


# ── Batch 2E: parameter-set consistency within a district ────────────────────

async def _make_two_params_with_vars(db, *, client_id):
    """Seed two custom Parameters (P1, P2), each with two Variables.
    Returns ((p1_id, p1v1, p1v2), (p2_id, p2v1, p2v2))."""
    p1 = Parameter(
        crop_cosh_id="crop:paddy", client_id=client_id,
        name="P1", display_order=0,
    )
    p2 = Parameter(
        crop_cosh_id="crop:paddy", client_id=client_id,
        name="P2", display_order=1,
    )
    db.add_all([p1, p2])
    await db.flush()
    p1v1 = Variable(parameter_id=p1.id, name="P1V1")
    p1v2 = Variable(parameter_id=p1.id, name="P1V2")
    p2v1 = Variable(parameter_id=p2.id, name="P2V1")
    p2v2 = Variable(parameter_id=p2.id, name="P2V2")
    db.add_all([p1v1, p1v2, p2v1, p2v2])
    await db.flush()
    return (p1.id, p1v1.id, p1v2.id), (p2.id, p2v1.id, p2v2.id)


@requires_docker
@pytest.mark.asyncio
async def test_consistency_blocks_when_subset_sibling_in_shared_district(db):
    """Headline 2E case: this PoP uses {P1}, sibling uses {P1, P2}
    in the same district. Spec §4.2 rejects — different parameter
    sets in the same district make the farmer's question sequence
    ambiguous."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    (p1_id, p1v1, _p1v2), (p2_id, p2v1, _p2v2) = \
        await _make_two_params_with_vars(db, client_id=client.id)
    await db.commit()

    # PoP A: D1 + {P1: V1, P2: V1}. PoP B: PV {P1: V1} first (no
    # district yet — the consistency check short-circuits when the
    # package has no locations), then add D1 — at which point 2E
    # fires because B has {P1} but A has {P1, P2} in shared D1.
    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
            {"parameter_id": p2_id, "variable_id": p2v1},
        ]),
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
        ]),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as ei:
        await set_package_locations(
            client_id=client.id, package_id=pkg_b.id,
            locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "pv_parameter_set_mismatch"
    violations = ei.value.detail["violations"]
    assert len(violations) == 1
    assert violations[0]["sibling_package_id"] == pkg_a.id
    assert violations[0]["sibling_package_name"] == "PoP A"
    assert set(violations[0]["this_parameter_ids"]) == {p1_id}
    assert set(violations[0]["sibling_parameter_ids"]) == {p1_id, p2_id}


@requires_docker
@pytest.mark.asyncio
async def test_consistency_blocks_when_superset_sibling_in_shared_district(db):
    """Reverse of above: this PoP uses {P1, P2}, sibling only {P1}.
    Same violation, different role assignment."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    (p1_id, p1v1, _p1v2), (p2_id, p2v1, _p2v2) = \
        await _make_two_params_with_vars(db, client_id=client.id)
    await db.commit()

    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
        ]),
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
            {"parameter_id": p2_id, "variable_id": p2v1},
        ]),
        db=db, current_user=user,
    )
    with pytest.raises(HTTPException) as ei:
        await set_package_locations(
            client_id=client.id, package_id=pkg_b.id,
            locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "pv_parameter_set_mismatch"


@requires_docker
@pytest.mark.asyncio
async def test_consistency_succeeds_with_same_set_different_values(db):
    """Same parameter set, different variable values — exactly the
    spec-compliant case. 2D doesn't fire (different fingerprints),
    2E doesn't fire (same parameter set). Save proceeds."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    (p1_id, p1v1, p1v2), (p2_id, p2v1, p2v2) = \
        await _make_two_params_with_vars(db, client_id=client.id)
    await db.commit()

    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
            {"parameter_id": p2_id, "variable_id": p2v1},
        ]),
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v2},
            {"parameter_id": p2_id, "variable_id": p2v2},
        ]),
        db=db, current_user=user,
    )
    out = await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    assert "saved" in out["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_consistency_allows_different_sets_across_districts(db):
    """Spec: parameters CAN vary across districts. PoP A in D1
    uses {P1, P2}; PoP B in D2 uses just {P1}. They never share
    a district, so no consistency check fires."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    (p1_id, p1v1, _p1v2), (p2_id, p2v1, _p2v2) = \
        await _make_two_params_with_vars(db, client_id=client.id)
    await db.commit()

    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
            {"parameter_id": p2_id, "variable_id": p2v1},
        ]),
        db=db, current_user=user,
    )
    await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D2")],
        db=db, current_user=user,
    )
    out = await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
        ]),
        db=db, current_user=user,
    )
    assert "saved" in out["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_consistency_publish_blocks_defensively(db):
    """Defensive last-line at publish. Bypass save-time guards via
    direct ORM and verify publish refuses."""
    from sqlalchemy import select as sql_select

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    (p1_id, p1v1, _p1v2), (p2_id, p2v1, _p2v2) = \
        await _make_two_params_with_vars(db, client_id=client.id)
    await db.commit()

    # Force a parameter-set mismatch directly via ORM:
    # PoP A → {P1: V1, P2: V1}, PoP B → {P1: V1}, both in D1.
    # Add an author to PoP B so the publish gate (Batch 2C) doesn't
    # surface "no_authors" first; we want the 2E defensive check
    # to be the failure mode under test.
    se = await _make_subject_expert(db, client=client, name="Author")
    for pkg in (pkg_a, pkg_b):
        db.add(PackageLocation(
            package_id=pkg.id, state_cosh_id="S1", district_cosh_id="D1",
        ))
    db.add(PackageVariable(
        package_id=pkg_a.id, parameter_id=p1_id, variable_id=p1v1,
    ))
    db.add(PackageVariable(
        package_id=pkg_a.id, parameter_id=p2_id, variable_id=p2v1,
    ))
    db.add(PackageVariable(
        package_id=pkg_b.id, parameter_id=p1_id, variable_id=p1v1,
    ))
    db.add(PackageAuthor(package_id=pkg_b.id, user_id=se.id))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg_b.id,
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "pv_parameter_set_mismatch"


@requires_docker
@pytest.mark.asyncio
async def test_consistency_inactive_sibling_does_not_block(db):
    """Same exclusion rule as Batch 2D — INACTIVE siblings are
    skipped so a fresh PoP can legitimately use a different
    parameter set than its superseded predecessor."""
    from sqlalchemy import select as sql_select

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a, pkg_b = await _create_two_packages(db, client=client, user=user)
    (p1_id, p1v1, _p1v2), (p2_id, p2v1, _p2v2) = \
        await _make_two_params_with_vars(db, client_id=client.id)
    await db.commit()

    await set_package_locations(
        client_id=client.id, package_id=pkg_a.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
            {"parameter_id": p2_id, "variable_id": p2v1},
        ]),
        db=db, current_user=user,
    )
    pkg_a_db = (await db.execute(
        sql_select(Package).where(Package.id == pkg_a.id)
    )).scalar_one()
    pkg_a_db.status = PackageStatus.INACTIVE
    await db.commit()

    await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    out = await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": p1_id, "variable_id": p1v1},
        ]),
        db=db, current_user=user,
    )
    assert "saved" in out["detail"]


# ── Batch 2B: package authors management ─────────────────────────────────────

async def _make_subject_expert(db, *, client, name="SE One") -> UserModel:
    """Create a User + an ACTIVE SUBJECT_EXPERT ClientUser row for
    the given client. Returns the User."""
    se = await make_user(db, name=name)
    db.add(ClientUser(
        client_id=client.id, user_id=se.id,
        role=ClientUserRole.SUBJECT_EXPERT,
        status=StatusEnum.ACTIVE,
    ))
    await db.flush()
    return se


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_with_two_active_ses(db):
    """Happy path. Two SEs of this client are listed as authors;
    save succeeds, GET returns them in display_order with the
    joined user_name surfaced."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    se1 = await _make_subject_expert(db, client=client, name="Dr A")
    se2 = await _make_subject_expert(db, client=client, name="Dr B")
    await db.commit()

    out = await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[
            PackageAuthorIn(user_id=se1.id, designation="Lead", display_order=0),
            PackageAuthorIn(user_id=se2.id, designation="Reviewer", display_order=1),
        ],
        db=db, current_user=user,
    )
    assert "2 authors saved" in out["detail"]

    listed = await list_package_authors(
        client_id=client.id, package_id=pkg.id, db=db, current_user=user,
    )
    assert [a.user_id for a in listed] == [se1.id, se2.id]
    assert [a.user_name for a in listed] == ["Dr A", "Dr B"]
    assert [a.designation for a in listed] == ["Lead", "Reviewer"]


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_replace_all_semantics(db):
    """Match the locations/variables endpoints: PUT replaces the
    entire set. Setting [B] after [A] leaves only B."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    se_a = await _make_subject_expert(db, client=client, name="Dr A")
    se_b = await _make_subject_expert(db, client=client, name="Dr B")
    await db.commit()

    await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[PackageAuthorIn(user_id=se_a.id)],
        db=db, current_user=user,
    )
    await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[PackageAuthorIn(user_id=se_b.id)],
        db=db, current_user=user,
    )
    listed = await list_package_authors(
        client_id=client.id, package_id=pkg.id, db=db, current_user=user,
    )
    assert [a.user_id for a in listed] == [se_b.id]


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_empty_list_allowed_at_save(db):
    """Spec rules: authors mandatory at PUBLISH, but mid-edit save
    with empty list is allowed (CA may be re-arranging).
    Publish-time enforcement lives in Batch 2C."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    await db.commit()

    out = await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[], db=db, current_user=user,
    )
    assert "0 authors saved" in out["detail"]


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_422_when_user_not_in_client(db):
    """user_id is a real User but has no ClientUser row for THIS
    client. Reject — spec §4.1: authors must be SEs of the company."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    stranger = await make_user(db, name="Stranger")  # exists, no ClientUser row
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_package_authors(
            client_id=client.id, package_id=pkg.id,
            authors=[PackageAuthorIn(user_id=stranger.id)],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "invalid_author"
    assert stranger.id in ei.value.detail["invalid_user_ids"]


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_422_when_user_is_wrong_role(db):
    """user is a ClientUser of this client but as CA, not
    SUBJECT_EXPERT. Authors must specifically be SEs."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    other_ca = await make_user(db, name="Other CA")
    db.add(ClientUser(
        client_id=client.id, user_id=other_ca.id,
        role=ClientUserRole.CA,
        status=StatusEnum.ACTIVE,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_package_authors(
            client_id=client.id, package_id=pkg.id,
            authors=[PackageAuthorIn(user_id=other_ca.id)],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "invalid_author"


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_422_when_user_is_se_of_different_client(db):
    """SE of Client X cannot be listed as author on Client Y's
    Package — defensive cross-tenant guard."""
    client_y = await make_client(db, full_name="Client Y")
    client_x = await make_client(db, full_name="Client X")
    user = await make_user(db, name="CA Y")
    await _seed_paddy_on_belt(db, client_y, user)
    pkg = await _create_test_package(db, client=client_y, user=user)

    se_x = await _make_subject_expert(db, client=client_x, name="Dr X")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_package_authors(
            client_id=client_y.id, package_id=pkg.id,
            authors=[PackageAuthorIn(user_id=se_x.id)],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "invalid_author"


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_422_when_se_is_inactive(db):
    """An SE whose ClientUser row is INACTIVE (e.g. self-removed
    role) must not appear as a Package author."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    from sqlalchemy import select as sql_select
    se = await _make_subject_expert(db, client=client, name="Dr Inactive")
    cu_row = (await db.execute(
        sql_select(ClientUser).where(ClientUser.user_id == se.id)
    )).scalar_one()
    cu_row.status = StatusEnum.INACTIVE
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_package_authors(
            client_id=client.id, package_id=pkg.id,
            authors=[PackageAuthorIn(user_id=se.id)],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "invalid_author"


@requires_docker
@pytest.mark.asyncio
async def test_set_authors_422_when_duplicate_user_ids(db):
    """Same SE listed twice → reject. Stops sloppy CA portal forms
    from creating a meaningless duplicate row."""
    client = await make_client(db)
    user = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    se = await _make_subject_expert(db, client=client, name="Dr Dup")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await set_package_authors(
            client_id=client.id, package_id=pkg.id,
            authors=[
                PackageAuthorIn(user_id=se.id, designation="A"),
                PackageAuthorIn(user_id=se.id, designation="B"),
            ],
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "duplicate_author"


# ── Batch 2C: publish-time mandatory-fields gate ─────────────────────────────

async def _fully_loaded_package(db, *, client, user) -> Package:
    """Spin up a Package with everything filled in: location, author,
    valid PV (no siblings yet so PV can be empty per §4.2 — but we
    set one for completeness). Used as the baseline for tests that
    perturb one specific field."""
    pkg = await _create_test_package(db, client=client, user=user)
    await set_package_locations(
        client_id=client.id, package_id=pkg.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    se = await _make_subject_expert(db, client=client, name="Author Dr")
    await db.commit()
    await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[PackageAuthorIn(user_id=se.id)],
        db=db, current_user=user,
    )
    return pkg


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocked_with_no_locations_no_authors(db):
    """Headline failure mode: a freshly-created PoP with nothing
    filled in. Publish must surface BOTH no_locations and no_authors
    in one consolidated checklist response."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg.id,
            db=db, current_user=user,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "publish_blocked_missing_fields"
    codes = {m["code"] for m in ei.value.detail["missing"]}
    assert "no_locations" in codes
    assert "no_authors" in codes


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocked_with_only_authors_missing(db):
    """One specific missing field — only no_authors should surface."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    await set_package_locations(
        client_id=client.id, package_id=pkg.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg.id,
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "publish_blocked_missing_fields"
    codes = {m["code"] for m in ei.value.detail["missing"]}
    assert codes == {"no_authors"}


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocked_with_only_locations_missing(db):
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _create_test_package(db, client=client, user=user)
    se = await _make_subject_expert(db, client=client, name="A")
    await db.commit()
    await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[PackageAuthorIn(user_id=se.id)],
        db=db, current_user=user,
    )

    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg.id,
            db=db, current_user=user,
        )
    codes = {m["code"] for m in ei.value.detail["missing"]}
    assert codes == {"no_locations"}


@requires_docker
@pytest.mark.asyncio
async def test_publish_succeeds_when_all_fields_set_no_siblings(db):
    """Fully loaded PoP with no siblings — publish bumps to v=1
    and ACTIVE status."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg = await _fully_loaded_package(db, client=client, user=user)

    out = await publish_package(
        client_id=client.id, package_id=pkg.id,
        db=db, current_user=user,
    )
    assert out.status == PackageStatus.ACTIVE
    assert out.version == 1


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocked_no_pv_with_shared_district_sibling(db):
    """Spec §4.2: when a 2nd PoP shares a district, this PoP must
    have non-empty P/V before it can publish.

    Direct ORM setup is required because the save-time guards
    (Batches 2D/2E) explicitly prevent this state from being
    reached via the public API — they're doing their job.
    The publish-time check exists as a defensive last line for
    direct-SQL bypass / concurrent-edit races, and that's what
    we're testing here."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a = await _create_test_package(db, client=client, user=user, name="PoP A")
    pkg_b = await _create_test_package(
        db, client=client, user=user, name="PoP B sibling",
    )
    param_id, v1, _v2 = await _make_param_with_two_vars(db, client_id=client.id)
    se = await _make_subject_expert(db, client=client, name="Author")
    await db.commit()

    # Direct-ORM setup: A has location D1 + author + NO PV;
    # B has location D1 + PV {param: v1}. They share D1.
    db.add(PackageLocation(
        package_id=pkg_a.id, state_cosh_id="S1", district_cosh_id="D1",
    ))
    db.add(PackageLocation(
        package_id=pkg_b.id, state_cosh_id="S1", district_cosh_id="D1",
    ))
    db.add(PackageVariable(
        package_id=pkg_b.id, parameter_id=param_id, variable_id=v1,
    ))
    db.add(PackageAuthor(package_id=pkg_a.id, user_id=se.id))
    await db.commit()

    # A's publish must block with no_pv_with_shared_district_sibling.
    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg_a.id,
            db=db, current_user=user,
        )
    assert ei.value.detail["code"] == "publish_blocked_missing_fields"
    codes = {m["code"] for m in ei.value.detail["missing"]}
    assert "no_pv_with_shared_district_sibling" in codes


@requires_docker
@pytest.mark.asyncio
async def test_publish_blocked_when_shared_district_sibling_lacks_pv(db):
    """Symmetric case: this PoP HAS PV, but a sibling sharing a
    district doesn't. Spec §4.2: neither can publish until both do.
    Direct-ORM setup as above (save-time guards otherwise prevent
    reaching this state)."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a = await _create_test_package(db, client=client, user=user, name="PoP A")
    pkg_b = await _create_test_package(
        db, client=client, user=user, name="PoP B sibling",
    )
    param_id, v1, _v2 = await _make_param_with_two_vars(db, client_id=client.id)
    se = await _make_subject_expert(db, client=client, name="Author")
    await db.commit()

    # A: location D1 + PV + author. B: location D1, NO PV.
    db.add(PackageLocation(
        package_id=pkg_a.id, state_cosh_id="S1", district_cosh_id="D1",
    ))
    db.add(PackageLocation(
        package_id=pkg_b.id, state_cosh_id="S1", district_cosh_id="D1",
    ))
    db.add(PackageVariable(
        package_id=pkg_a.id, parameter_id=param_id, variable_id=v1,
    ))
    db.add(PackageAuthor(package_id=pkg_a.id, user_id=se.id))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await publish_package(
            client_id=client.id, package_id=pkg_a.id,
            db=db, current_user=user,
        )
    sibling_codes = [m for m in ei.value.detail["missing"]
                     if m["code"] == "sibling_has_no_pv"]
    assert len(sibling_codes) == 1
    assert sibling_codes[0]["sibling_package_id"] == pkg_b.id
    assert sibling_codes[0]["shared_districts"] == [
        {"state_cosh_id": "S1", "district_cosh_id": "D1"},
    ]


@requires_docker
@pytest.mark.asyncio
async def test_publish_succeeds_with_shared_district_when_both_have_pv(db):
    """Two PoPs share a district, both have non-empty (different) PV.
    Both should be able to publish."""
    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a = await _fully_loaded_package(db, client=client, user=user)
    pkg_b = await _create_test_package(
        db, client=client, user=user,
        package_type=PackageType.ANNUAL, duration_days=120,
        name="PoP B sibling",
    )
    param_id, v1, v2 = await _make_param_with_two_vars(db, client_id=client.id)
    se = await _make_subject_expert(db, client=client, name="Author B")
    await db.commit()
    await set_package_variables(
        client_id=client.id, package_id=pkg_a.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v1},
        ]),
        db=db, current_user=user,
    )
    await set_package_variables(
        client_id=client.id, package_id=pkg_b.id,
        request=PackageVariableSet(assignments=[
            {"parameter_id": param_id, "variable_id": v2},
        ]),
        db=db, current_user=user,
    )
    await set_package_locations(
        client_id=client.id, package_id=pkg_b.id,
        locations=[PackageLocationIn(state_cosh_id="S1", district_cosh_id="D1")],
        db=db, current_user=user,
    )
    await set_package_authors(
        client_id=client.id, package_id=pkg_b.id,
        authors=[PackageAuthorIn(user_id=se.id)],
        db=db, current_user=user,
    )

    out_a = await publish_package(
        client_id=client.id, package_id=pkg_a.id,
        db=db, current_user=user,
    )
    assert out_a.status == PackageStatus.ACTIVE


@requires_docker
@pytest.mark.asyncio
async def test_publish_inactive_sibling_not_considered(db):
    """An INACTIVE sibling sharing a district doesn't count for the
    §4.2 rule — same exclusion as Batches 2D and 2E.

    Direct-ORM setup: with both PoPs having empty PV in the same
    district, save-time guards would block reaching this state. The
    test explicitly simulates the post-inactivation snapshot to
    verify the publish-time check correctly skips INACTIVE siblings.
    """
    from sqlalchemy import select as sql_select

    client = await make_client(db)
    user = await make_user(db, name="Expert")
    await _seed_paddy_on_belt(db, client, user)
    pkg_a = await _create_test_package(db, client=client, user=user, name="PoP A")
    pkg_b = await _create_test_package(
        db, client=client, user=user, name="PoP B sibling",
    )
    se = await _make_subject_expert(db, client=client, name="Author")
    await db.commit()

    # A: D1 + author + no PV. B: D1, no PV, INACTIVE.
    db.add(PackageLocation(
        package_id=pkg_a.id, state_cosh_id="S1", district_cosh_id="D1",
    ))
    db.add(PackageLocation(
        package_id=pkg_b.id, state_cosh_id="S1", district_cosh_id="D1",
    ))
    db.add(PackageAuthor(package_id=pkg_a.id, user_id=se.id))
    pkg_b_db = (await db.execute(
        sql_select(Package).where(Package.id == pkg_b.id)
    )).scalar_one()
    pkg_b_db.status = PackageStatus.INACTIVE
    await db.commit()

    # PoP A has empty PV but the only sibling sharing D1 is INACTIVE
    # — the §4.2 rule doesn't fire. Publish proceeds.
    out = await publish_package(
        client_id=client.id, package_id=pkg_a.id,
        db=db, current_user=user,
    )
    assert out.status == PackageStatus.ACTIVE
