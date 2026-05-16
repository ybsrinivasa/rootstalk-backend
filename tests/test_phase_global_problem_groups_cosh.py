"""SA-portal `/advisory/global/problem-groups` now reads from Cosh's
`problem_groups` Core (Batch 39Q-frontend, 2026-05-16) instead of the
hardcoded V1 stopgap. New PGRecs created from this endpoint can resolve
applicable-crop chips against the `sp_pg_crops` Connect.

The CA-portal helpers (cha_list_problems etc.) still use the stopgap
list — that swap is a separate batch.
"""
from __future__ import annotations

import pytest

from app.modules.advisory.router import list_global_problem_groups
from app.modules.sync.models import CoshCoreItem
from app.services.cosh_constants import COSH_PROBLEM_GROUPS_CORE
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_endpoint_returns_active_cosh_problem_groups_sorted(db):
    user = await make_user(db, name="CM")
    db.add(CoshCoreItem(
        cosh_id="cosh-pg-fungal", core_type=COSH_PROBLEM_GROUPS_CORE,
        translations={"en": "Fungal Diseases"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh-pg-sucking", core_type=COSH_PROBLEM_GROUPS_CORE,
        translations={"en": "Sucking Pests"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh-pg-bacterial", core_type=COSH_PROBLEM_GROUPS_CORE,
        translations={"en": "Bacterial Diseases"}, status="active",
    ))
    await db.commit()

    out = await list_global_problem_groups(db=db, current_user=user)
    names = [p["name_en"] for p in out]
    assert names == ["Bacterial Diseases", "Fungal Diseases", "Sucking Pests"]
    # Shape parity with stopgap: each row carries cosh_id + name_en + status.
    assert all(set(p.keys()) == {"cosh_id", "name_en", "status"} for p in out)
    assert all(p["status"] == "active" for p in out)


@requires_docker
@pytest.mark.asyncio
async def test_endpoint_drops_inactive_core_items(db):
    user = await make_user(db, name="CM")
    db.add(CoshCoreItem(
        cosh_id="cosh-pg-fungal", core_type=COSH_PROBLEM_GROUPS_CORE,
        translations={"en": "Fungal Diseases"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="cosh-pg-retired", core_type=COSH_PROBLEM_GROUPS_CORE,
        translations={"en": "Retired PG"}, status="inactive",
    ))
    await db.commit()

    out = await list_global_problem_groups(db=db, current_user=user)
    cosh_ids = {p["cosh_id"] for p in out}
    assert cosh_ids == {"cosh-pg-fungal"}


@requires_docker
@pytest.mark.asyncio
async def test_endpoint_returns_empty_when_no_cosh_seed(db):
    user = await make_user(db, name="CM")
    out = await list_global_problem_groups(db=db, current_user=user)
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_endpoint_ignores_other_core_types(db):
    user = await make_user(db, name="CM")
    db.add(CoshCoreItem(
        cosh_id="cosh-pg-fungal", core_type=COSH_PROBLEM_GROUPS_CORE,
        translations={"en": "Fungal Diseases"}, status="active",
    ))
    # Different core_type — must NOT surface.
    db.add(CoshCoreItem(
        cosh_id="cosh-other", core_type="biological_names",
        translations={"en": "Tomato"}, status="active",
    ))
    await db.commit()

    out = await list_global_problem_groups(db=db, current_user=user)
    assert [p["name_en"] for p in out] == ["Fungal Diseases"]
