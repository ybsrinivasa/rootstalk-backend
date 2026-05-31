"""Orders V2 Batch 21 — Perennial CALENDAR support in compute_bundle.

Per the 2026-05-31 BL audit: dedup + ordering rules apply for
both Annual and Perennial packages. compute_bundle handled
DAS/DBS via crop_start_date but skipped CALENDAR — so Perennial
farmers had no order path at all. Fix: CALENDAR timelines resolve
to absolute dates in the current calendar year, then participate
in window-overlap + dedup the same way DAS/DBS do.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.modules.advisory.models import (
    PackageType, PracticeL0, TimelineFromType,
)
from app.services.order_bundle import (
    CATEGORY_PESTICIDE, compute_bundle,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_element, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_perennial_calendar_bundle_includes_in_window_practices(db):
    """A Perennial package with no crop_start_date still resolves
    CALENDAR practices via the current calendar year."""
    user = await make_user(db, name="Farmer Per")
    client = await make_client(db)
    pkg = await make_package(db, client)
    pkg.package_type = PackageType.PERENNIAL
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = None  # Perennial — no sowing event
    await db.commit()

    today = date.today()
    # CALENDAR timeline that includes today's day-of-year ±5 days.
    today_doy = today.timetuple().tm_yday
    tl = await make_timeline(
        db, pkg, name="TL_CAL",
        from_type=TimelineFromType.CALENDAR,
        from_value=max(1, today_doy - 5),
        to_value=min(365, today_doy + 5),
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await make_element(db, p, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:mancozeb")
    await db.commit()

    bundle = await compute_bundle(
        db, subscription=sub, category=CATEGORY_PESTICIDE,
        to_date=today + timedelta(days=10), today=today,
    )
    ids = {row["id"] for row in bundle["practices"]}
    assert p.id in ids


@requires_docker
@pytest.mark.asyncio
async def test_perennial_calendar_bundle_excludes_out_of_window(db):
    """A CALENDAR timeline far from today shouldn't land in the bundle."""
    user = await make_user(db, name="Farmer Per Out")
    client = await make_client(db)
    pkg = await make_package(db, client)
    pkg.package_type = PackageType.PERENNIAL
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = None
    await db.commit()

    today = date.today()
    today_doy = today.timetuple().tm_yday
    # 60 days before today's day-of-year, 50 days before — fully out of window.
    far_from = max(1, today_doy - 60)
    far_to = max(1, today_doy - 50)
    if far_from < far_to:  # well-formed only when wrap math doesn't bite
        tl = await make_timeline(
            db, pkg, name="TL_CAL_FAR",
            from_type=TimelineFromType.CALENDAR,
            from_value=far_from, to_value=far_to,
        )
        p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
        await make_element(db, p, element_type="COMMON_NAME", value=None, unit_cosh_id=None, cosh_ref="cosh:carbendazim")
        await db.commit()

        bundle = await compute_bundle(
            db, subscription=sub, category=CATEGORY_PESTICIDE,
            to_date=today + timedelta(days=10), today=today,
        )
        ids = {row["id"] for row in bundle["practices"]}
        assert p.id not in ids
