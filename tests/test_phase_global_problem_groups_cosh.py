"""SA-portal `/advisory/global/problem-groups` reads from Cosh's
`problem_groups` Core (Batch 39Q-frontend, 2026-05-16).

Note (Batch 39R-bridge, 2026-05-17): the `db` fixture now auto-seeds
the legacy V1 `pg:*` slugs as Cosh `problem_groups` items so the
CA-portal swap can land without rewriting ~10 legacy tests. These
tests therefore filter to *their own* seeded IDs rather than asserting
the full output equals only-what-this-test-added.
"""
from __future__ import annotations

import pytest

from app.modules.advisory.router import list_global_problem_groups
from app.modules.sync.models import CoshCoreItem
from app.services.cosh_constants import COSH_PROBLEM_GROUPS_CORE
from tests.conftest import requires_docker
from tests.factories import make_user


def _ids_for_prefix(rows: list[dict], prefix: str) -> set[str]:
    return {r["cosh_id"] for r in rows if r["cosh_id"].startswith(prefix)}


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["cosh_id"]: r for r in rows}


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
    # Filter to this test's own seeded rows so the conftest legacy seed
    # doesn't change the assertion shape.
    mine = [r for r in out if r["cosh_id"].startswith("cosh-pg-")]
    names = [r["name_en"] for r in mine]
    assert names == ["Bacterial Diseases", "Fungal Diseases", "Sucking Pests"]
    assert all(set(r.keys()) == {"cosh_id", "name_en", "status"} for r in mine)
    assert all(r["status"] == "active" for r in mine)
    # Whole-set sort still holds across legacy + this-test rows.
    all_names = [r["name_en"] for r in out]
    assert all_names == sorted(all_names, key=str.casefold)


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
    my_ids = _ids_for_prefix(out, "cosh-pg-")
    assert my_ids == {"cosh-pg-fungal"}


@requires_docker
@pytest.mark.asyncio
async def test_endpoint_returns_only_legacy_seed_when_no_extra_cosh_seed(db):
    """With no test-added cosh items, the response equals the conftest
    legacy seed exactly (12 entries, all `pg:*`)."""
    user = await make_user(db, name="CM")
    out = await list_global_problem_groups(db=db, current_user=user)
    assert len(out) == 12
    assert all(r["cosh_id"].startswith("pg:") for r in out)
    # Sorted alphabetically by name.
    names = [r["name_en"] for r in out]
    assert names == sorted(names, key=str.casefold)


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
    by_id = _by_id(out)
    assert "cosh-pg-fungal" in by_id
    assert by_id["cosh-pg-fungal"]["name_en"] == "Fungal Diseases"
    assert "cosh-other" not in by_id
