"""Brand Lock authoring (Batch 39I-a, 2026-05-16).

Practice.is_brand_locked is a per-Practice flag the SE opts into when
they want a specific Trade Name to be the only fulfilment for that
input row. Backend validation: the flag is only valid when the
Practice carries a BRAND_NAME element with a non-empty cosh_ref —
otherwise the request is rejected with `brand_lock_requires_brand_name`.

BL-07's brand-options resolution consults `practice.is_brand_locked`
(superseding the legacy `element_type='brand'` inference) — covered in
`test_phase_bl07_brand_strict.py` / `test_phase_locked_brand_routing.py`
as far as the brand-picker contract is concerned. This file pins the
authoring-time contract: create / update accept the flag, validation
fires when BRAND_NAME is missing, and the flag round-trips through the
practice index endpoint.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, Package, PackageStatus, PackageType, Practice,
    Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    create_global_practice, list_global_practices,
    update_global_practice,
)
from app.modules.advisory.schemas import (
    ElementIn, PracticeCreate,
)
from app.modules.advisory.models import PracticeL0
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_global_pkg_and_tl(db) -> tuple[Package, Timeline, "User"]:
    user = await make_user(db, name="SA")
    pkg = Package(
        client_id=None, name="GP",
        crop_cosh_id="crop:tomato",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    tl = Timeline(
        package_id=pkg.id, name="TL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    db.add(tl)
    await db.flush()
    return pkg, tl, user


def _practice_request_with_brand(
    *, l2: str = "CHEMICAL_PESTICIDES",
    brand_cosh_id: str | None = "tn:confidor",
    common_name_cosh_id: str = "cn:imid",
    is_brand_locked: bool = False,
) -> PracticeCreate:
    elements = [
        ElementIn(element_type="COMMON_NAME", cosh_ref=common_name_cosh_id),
    ]
    if brand_cosh_id is not None:
        elements.append(ElementIn(element_type="BRAND_NAME", cosh_ref=brand_cosh_id))
    return PracticeCreate(
        l0_type=PracticeL0.INPUT,
        l1_type="PESTICIDE",
        l2_type=l2,
        display_order=0,
        is_special_input=False,
        is_brand_locked=is_brand_locked,
        elements=elements,
    )


# ── Happy path ───────────────────────────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_create_practice_brand_locked_with_trade_name_succeeds(db, monkeypatch):
    """The SE checks Lock Brand AND has picked a BRAND_NAME — accepted."""
    # Bypass the L2 element validator / interval-fit gate for this
    # narrow authoring test; they're exhaustively covered elsewhere
    # and aren't the subject of this batch.
    from app.modules.advisory import router as adv_router
    async def _noop(*a, **kw): return None
    monkeypatch.setattr(adv_router, "assert_l2_elements_valid", _noop)
    monkeypatch.setattr(adv_router, "_assert_interval_fits_timeline", _noop)

    pkg, tl, user = await _seed_global_pkg_and_tl(db)
    await db.commit()
    out = await create_global_practice(
        pkg_id=pkg.id, tl_id=tl.id,
        request=_practice_request_with_brand(is_brand_locked=True),
        db=db, current_user=user,
    )
    assert out.is_brand_locked is True


# ── Validation: lock without brand 422 ───────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_create_practice_brand_locked_without_brand_name_422(db, monkeypatch):
    """Lock Brand without a BRAND_NAME element — rejected."""
    from app.modules.advisory import router as adv_router
    async def _noop(*a, **kw): return None
    monkeypatch.setattr(adv_router, "assert_l2_elements_valid", _noop)
    monkeypatch.setattr(adv_router, "_assert_interval_fits_timeline", _noop)

    pkg, tl, user = await _seed_global_pkg_and_tl(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_practice(
            pkg_id=pkg.id, tl_id=tl.id,
            request=_practice_request_with_brand(
                brand_cosh_id=None, is_brand_locked=True,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "brand_lock_requires_brand_name"


@requires_docker
@pytest.mark.asyncio
async def test_create_practice_brand_locked_with_blank_brand_422(db, monkeypatch):
    """A BRAND_NAME element with an empty cosh_ref doesn't count as a
    Trade Name pick — Lock Brand still rejected."""
    from app.modules.advisory import router as adv_router
    async def _noop(*a, **kw): return None
    monkeypatch.setattr(adv_router, "assert_l2_elements_valid", _noop)
    monkeypatch.setattr(adv_router, "_assert_interval_fits_timeline", _noop)

    pkg, tl, user = await _seed_global_pkg_and_tl(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await create_global_practice(
            pkg_id=pkg.id, tl_id=tl.id,
            request=_practice_request_with_brand(
                brand_cosh_id="", is_brand_locked=True,
            ),
            db=db, current_user=user,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "brand_lock_requires_brand_name"


# ── Update preserves / clears the flag ───────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_update_practice_can_clear_brand_lock(db, monkeypatch):
    """Edit the Practice with is_brand_locked=False — flag clears."""
    from app.modules.advisory import router as adv_router
    async def _noop(*a, **kw): return None
    monkeypatch.setattr(adv_router, "assert_l2_elements_valid", _noop)
    monkeypatch.setattr(adv_router, "_assert_interval_fits_timeline", _noop)

    pkg, tl, user = await _seed_global_pkg_and_tl(db)
    await db.commit()
    p = await create_global_practice(
        pkg_id=pkg.id, tl_id=tl.id,
        request=_practice_request_with_brand(is_brand_locked=True),
        db=db, current_user=user,
    )
    assert p.is_brand_locked is True

    await update_global_practice(
        pkg_id=pkg.id, tl_id=tl.id, practice_id=p.id,
        request=_practice_request_with_brand(is_brand_locked=False),
        db=db, current_user=user,
    )
    refreshed = (await db.execute(
        select(Practice).where(Practice.id == p.id)
    )).scalar_one()
    assert refreshed.is_brand_locked is False


# ── Round-trip through the list endpoint ─────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_list_global_practices_surfaces_brand_locked_flag(db, monkeypatch):
    """The practice index endpoint bundles is_brand_locked alongside
    the existing per-practice fields."""
    from app.modules.advisory import router as adv_router
    async def _noop(*a, **kw): return None
    monkeypatch.setattr(adv_router, "assert_l2_elements_valid", _noop)
    monkeypatch.setattr(adv_router, "_assert_interval_fits_timeline", _noop)

    pkg, tl, user = await _seed_global_pkg_and_tl(db)
    await db.commit()
    await create_global_practice(
        pkg_id=pkg.id, tl_id=tl.id,
        request=_practice_request_with_brand(
            is_brand_locked=True, common_name_cosh_id="cn:imid",
        ),
        db=db, current_user=user,
    )
    # Distinct Common Name on the second practice — Rule 1 forbids
    # duplicate CN per Timeline for PESTICIDE/FERTILIZER (2026-05-22).
    await create_global_practice(
        pkg_id=pkg.id, tl_id=tl.id,
        request=_practice_request_with_brand(
            is_brand_locked=False, common_name_cosh_id="cn:other",
        ),
        db=db, current_user=user,
    )
    listed = await list_global_practices(
        pkg_id=pkg.id, tl_id=tl.id, db=db, current_user=user,
    )
    locks = sorted(row["is_brand_locked"] for row in listed)
    assert locks == [False, True]
