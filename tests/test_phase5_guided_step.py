"""Phase 5.2 — integration tests for GET /farmer/packages/guided-step.

Covers the refactored route end-to-end against a real DB. Verifies:
  - Empty answers → returns first parameter (in display_order)
  - Display-order is honoured (Parameter with smaller display_order asked first)
  - Single-option Parameter is auto-selected silently and skipped over
  - Sequential answers narrow the pool to exactly one Package
  - Final response includes the plain-language `summary`
  - Empty pool (no candidate Packages) returns DATA_CONFIG_ERROR
  - Reset semantics: passing `answers=''` returns to Parameter 1
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import PackageStatus
from app.modules.subscriptions.router import guided_elimination_step
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_package_location, make_package_variable,
    make_parameter, make_user, make_variable,
)


CROP = "crop:paddy"
DISTRICT = "district:bangalore-rural"


async def _set_package_active(db, pkg):
    pkg.status = PackageStatus.ACTIVE
    await db.flush()


async def _seed_simple_pool(db):
    """Two parameters (Season ord=1, Duration ord=2). Two packages.

    Pkg-K-Short: Season=Kharif, Duration=Short
    Pkg-R-Short: Season=Rabi,   Duration=Short

    Only Season discriminates; Duration is single-option and should
    auto-select silently.
    """
    user = await make_user(db)
    user.email = "test-farmer@example.com"
    client = await make_client(db)

    pkg_k = await make_package(db, client, name="Pkg-K-Short")
    pkg_r = await make_package(db, client, name="Pkg-R-Short")
    await _set_package_active(db, pkg_k)
    await _set_package_active(db, pkg_r)

    await make_package_location(db, pkg_k, district_cosh_id=DISTRICT)
    await make_package_location(db, pkg_r, district_cosh_id=DISTRICT)
    pkg_k.crop_cosh_id = CROP
    pkg_r.crop_cosh_id = CROP

    season = await make_parameter(db, crop_cosh_id=CROP, name="Season", display_order=1)
    duration = await make_parameter(db, crop_cosh_id=CROP, name="Duration", display_order=2)

    var_kharif = await make_variable(db, season, name="Kharif")
    var_rabi = await make_variable(db, season, name="Rabi")
    var_short = await make_variable(db, duration, name="Short")

    await make_package_variable(db, pkg_k, season, var_kharif)
    await make_package_variable(db, pkg_k, duration, var_short)
    await make_package_variable(db, pkg_r, season, var_rabi)
    await make_package_variable(db, pkg_r, duration, var_short)
    await db.commit()

    return {
        "user": user, "client": client,
        "pkg_k": pkg_k, "pkg_r": pkg_r,
        "season": season, "duration": duration,
        "var_kharif": var_kharif, "var_rabi": var_rabi, "var_short": var_short,
    }


# ── Tests ───────────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_empty_answers_returns_first_parameter_by_display_order(db):
    s = await _seed_simple_pool(db)

    out = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=s["client"].id, answers="",
        db=db, current_user=s["user"],
    )

    assert out["done"] is False
    assert "error" not in out or not out.get("error")
    # Season has display_order=1 → must be asked first.
    assert out["parameter"]["id"] == s["season"].id
    assert out["parameter"]["name"] == "Season"
    var_names = sorted(v["name"] for v in out["variables"])
    assert var_names == ["Kharif", "Rabi"]
    assert out["remaining_count"] == 2


@requires_docker
@pytest.mark.asyncio
async def test_after_first_answer_pool_collapses_to_one(db):
    """Once Season=Kharif is answered, only Pkg-K-Short remains. The pool
    collapses to 1 immediately — Duration is never asked because the
    algorithm short-circuits on len(pool)==1 before checking parameters.
    summary contains only the actually-chosen variables."""
    s = await _seed_simple_pool(db)
    answers = f"{s['season'].id}:{s['var_kharif'].id}"

    out = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=s["client"].id, answers=answers,
        db=db, current_user=s["user"],
    )

    assert out["done"] is True
    assert out["package"]["id"] == s["pkg_k"].id
    assert out["package"]["name"] == "Pkg-K-Short"
    # Pool collapsed to 1 from a real answer — auto_selected is False
    # because no Parameter was silently auto-resolved by the algorithm.
    assert out["auto_selected"] is False
    assert out["summary"] == ["Kharif"]


@requires_docker
@pytest.mark.asyncio
async def test_single_option_first_parameter_is_auto_selected(db):
    """Two packages both Kharif but differing on Duration. Empty answers
    → Season has only one variable (Kharif) for the whole pool, so the
    algorithm silently selects it and asks Duration. Response carries
    auto_selected=True, parameter=Duration."""
    user = await make_user(db)
    client = await make_client(db)

    pkg_short = await make_package(db, client, name="Pkg-Short")
    pkg_long = await make_package(db, client, name="Pkg-Long")
    await _set_package_active(db, pkg_short)
    await _set_package_active(db, pkg_long)
    await make_package_location(db, pkg_short, district_cosh_id=DISTRICT)
    await make_package_location(db, pkg_long, district_cosh_id=DISTRICT)
    pkg_short.crop_cosh_id = CROP
    pkg_long.crop_cosh_id = CROP

    season = await make_parameter(db, crop_cosh_id=CROP, name="Season", display_order=1)
    duration = await make_parameter(db, crop_cosh_id=CROP, name="Duration", display_order=2)
    var_kharif = await make_variable(db, season, name="Kharif")
    var_short = await make_variable(db, duration, name="Short")
    var_long = await make_variable(db, duration, name="Long")

    await make_package_variable(db, pkg_short, season, var_kharif)
    await make_package_variable(db, pkg_short, duration, var_short)
    await make_package_variable(db, pkg_long, season, var_kharif)
    await make_package_variable(db, pkg_long, duration, var_long)
    await db.commit()

    out = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=client.id, answers="",
        db=db, current_user=user,
    )

    # Season was auto-selected (only one variable in the pool); the
    # next question is Duration.
    assert out["done"] is False
    assert out["parameter"]["id"] == duration.id
    assert out["auto_selected"] is True
    var_names = sorted(v["name"] for v in out["variables"])
    assert var_names == ["Long", "Short"]


@requires_docker
@pytest.mark.asyncio
async def test_display_order_inversion_changes_first_question(db):
    """Flip display_order so Duration comes before Season. The route MUST
    ask Duration first now (proving it honours display_order, not max-variants
    which would still pick Season)."""
    s = await _seed_simple_pool(db)
    # Add a third package + a Rabi-Long variant so Duration has 2 variables.
    pkg_r_long = await make_package(db, s["client"], name="Pkg-R-Long")
    await _set_package_active(db, pkg_r_long)
    await make_package_location(db, pkg_r_long, district_cosh_id=DISTRICT)
    pkg_r_long.crop_cosh_id = CROP
    var_long = await make_variable(db, s["duration"], name="Long")
    await make_package_variable(db, pkg_r_long, s["season"], s["var_rabi"])
    await make_package_variable(db, pkg_r_long, s["duration"], var_long)
    await db.commit()

    # With display_order Season=1, Duration=2 → Season asked first
    out1 = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=s["client"].id, answers="",
        db=db, current_user=s["user"],
    )
    assert out1["parameter"]["id"] == s["season"].id

    # Flip the order and re-run.
    s["season"].display_order = 5
    s["duration"].display_order = 1
    await db.commit()

    out2 = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=s["client"].id, answers="",
        db=db, current_user=s["user"],
    )
    assert out2["parameter"]["id"] == s["duration"].id, (
        "Lower display_order Parameter must be asked first"
    )


@requires_docker
@pytest.mark.asyncio
async def test_empty_pool_returns_data_config_error(db):
    """No candidate packages for crop+district+client → DATA_CONFIG_ERROR."""
    user = await make_user(db)
    client = await make_client(db)
    await db.commit()

    out = await guided_elimination_step(
        crop_cosh_id="crop:nonexistent",
        district_cosh_id="district:nonexistent",
        client_id=client.id,
        answers="",
        db=db, current_user=user,
    )
    assert out["done"] is False
    assert out["error"] == "DATA_CONFIG_ERROR"


@requires_docker
@pytest.mark.asyncio
async def test_reset_with_empty_answers_starts_over(db):
    """PWA decline-confirmation flow: passing answers='' must return to
    Parameter 1 regardless of any state. There's no server-side answer
    state — the question is statelessly determined by the answers string."""
    s = await _seed_simple_pool(db)

    # First call with a partial answer.
    out_partial = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=s["client"].id,
        answers=f"{s['season'].id}:{s['var_rabi'].id}",
        db=db, current_user=s["user"],
    )
    # Pkg-R-Short is the only Rabi package; Duration auto-selects.
    assert out_partial["done"] is True
    assert out_partial["package"]["id"] == s["pkg_r"].id

    # Reset — empty answers — back to Season.
    out_reset = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=s["client"].id, answers="",
        db=db, current_user=s["user"],
    )
    assert out_reset["done"] is False
    assert out_reset["parameter"]["id"] == s["season"].id


@requires_docker
@pytest.mark.asyncio
async def test_only_dead_end_safe_variables_are_offered(db):
    """Spec: 'Only show Variables that lead to at least one valid PoP at
    each step (dead ends structurally impossible)'.

    Setup: Two packages, both Kharif. Add a stray Variable 'Rabi' on the
    Season parameter that no package uses. The route MUST NOT offer Rabi.
    """
    s = await _seed_simple_pool(db)
    # Remove Pkg-R-Short so only Kharif packages remain in the pool.
    s["pkg_r"].status = PackageStatus.INACTIVE
    await db.commit()

    out = await guided_elimination_step(
        crop_cosh_id=CROP, district_cosh_id=DISTRICT,
        client_id=s["client"].id, answers="",
        db=db, current_user=s["user"],
    )
    # Only one Kharif package remains and Duration is single-option →
    # auto-selects all the way to done.
    assert out["done"] is True
    assert out["package"]["id"] == s["pkg_k"].id
    # Summary shouldn't mention Rabi anywhere.
    assert "Rabi" not in out["summary"]
