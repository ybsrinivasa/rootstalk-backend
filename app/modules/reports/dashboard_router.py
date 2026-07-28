"""FastAPI router for the Client Reports dashboard (Phase 1).

Thin adapter: parse query params → gate access → build ``ReportFilters``
→ call a pure function from ``queries.py`` → return JSON. No SQL, no
business logic. Swapping to a rollup table or a read-replica session
later means editing ``queries.py``, not this file.

Kept in a SEPARATE file from the legacy ``reports/router.py`` (which
holds unrelated Support / RM code); registered as its own router in
``main.py`` with tag ``Client Reports``.

Endpoints (all under ``/client/{cid}/reports/*``):

    GET  /client/{cid}/reports/overview                    (WIP)
    GET  /client/{cid}/reports/subscriptions               (partial)
    GET  /client/{cid}/reports/orders                      (WIP)
    GET  /client/{cid}/reports/{subject}/export.csv        (WIP)

Vertical-slice status (2026-07-27): only ``subscriptions?metric=ACTIVE``
is wired end-to-end. Every other metric returns 501 until its query
body lands.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.advisory.models import Package, PackageStatus
from app.modules.clients.models import Client
from app.modules.platform.models import User
from app.modules.subscriptions.models import Subscription
from app.modules.sync.models import CoshCoreItem

from app.modules.reports import queries
from app.modules.reports.access import (
    _assert_client_report_reader,
    client_can_access_reports,
)


router = APIRouter(tags=["Client Reports"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _assert_subject_enabled(client_id: str, subject: str) -> None:
    """Tier seam — 403 with a stable code if this client can't see the
    subject. Today the seam always returns True; when a paid analytics
    tier lands, only this call site needs to grow claws."""
    if not client_can_access_reports(client_id, subject):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "report_subject_not_enabled",
                "message": (
                    f"Reports for '{subject}' are not enabled on this "
                    "client's plan."
                ),
            },
        )


def _build_filters(
    period_from: Optional[datetime],
    period_to: Optional[datetime],
    crop_cosh_id: Optional[str],
    state_cosh_id: Optional[str],
    district_cosh_id: Optional[str],
    package_id: Optional[str],
) -> queries.ReportFilters:
    return queries.ReportFilters(
        period_from=period_from,
        period_to=period_to,
        crop_cosh_id=crop_cosh_id,
        state_cosh_id=state_cosh_id,
        district_cosh_id=district_cosh_id,
        package_id=package_id,
    )


# ── Filter chip options (shared across all subject areas) ────────────────────

async def _cosh_names(
    db: AsyncSession, cosh_ids: list[str],
) -> dict[str, str]:
    """Bulk-lookup English display names for a list of cosh_ids.

    Falls back to the raw cosh_id when a row is missing or has no English
    translation — the frontend chip still renders something sensible
    ("crop:xyz-abc") rather than a blank pill. Not scoped by core_type
    because states/districts/crops live in different Cores and callers
    would need to split the batch otherwise.
    """
    if not cosh_ids:
        return {}
    rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
            CoshCoreItem.cosh_id.in_(cosh_ids),
        )
    )).all()
    out: dict[str, str] = {}
    for cid, translations in rows:
        en = (translations or {}).get("en")
        if en:
            out[cid] = en
    for cid in cosh_ids:
        out.setdefault(cid, cid)
    return out


@router.get("/client/{cid}/reports/filter-options")
async def filter_options(
    cid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Options for the four filter chips on the Reports pages.

    One round-trip so the frontend can render every chip on first
    paint. Values are:

    - ``crops``    — distinct Package.crop_cosh_id on this client's
                     subscriptions, with English names.
    - ``states``   — distinct farmer states seen on this client's
                     subscriptions, with English names.
    - ``districts``— same shape as states, at district resolution.
    - ``packages`` — this client's ACTIVE + INACTIVE packages
                     (DRAFT excluded — nothing subscribes to it).

    All four queries reuse ``_subscription_scope(cid)`` so the pickers
    inherit the filter contract (client scoping + training exclusion
    + soft-delete cascade) automatically. That means the pickers can
    never surface a value that has no matching data — a state that
    only appears on training subs won't be pickable.
    """
    await _assert_client_report_reader(db, current_user, cid)

    scope = queries._subscription_scope(cid)

    crop_ids = [
        r[0] for r in (await db.execute(
            scope.join(Package, Package.id == Subscription.package_id)
                 .with_only_columns(Package.crop_cosh_id)
                 .distinct()
        )).all() if r[0]
    ]
    state_ids = [
        r[0] for r in (await db.execute(
            scope.join(User, User.id == Subscription.farmer_user_id)
                 .with_only_columns(User.state_cosh_id)
                 .distinct()
        )).all() if r[0]
    ]
    district_ids = [
        r[0] for r in (await db.execute(
            scope.join(User, User.id == Subscription.farmer_user_id)
                 .with_only_columns(User.district_cosh_id)
                 .distinct()
        )).all() if r[0]
    ]
    package_rows = (await db.execute(
        select(Package.id, Package.name).where(
            Package.client_id == cid,
            Package.status != PackageStatus.DRAFT,
        ).order_by(Package.name)
    )).all()

    cosh_names = await _cosh_names(db, crop_ids + state_ids + district_ids)

    def _pack(ids: list[str]) -> list[dict]:
        return sorted(
            [{"id": i, "name": cosh_names.get(i, i)} for i in ids],
            key=lambda o: o["name"].lower(),
        )

    return {
        "crops":     _pack(crop_ids),
        "states":    _pack(state_ids),
        "districts": _pack(district_ids),
        "packages":  [{"id": p.id, "name": p.name} for p in package_rows],
    }


# ── Subscriptions subject area ────────────────────────────────────────────────

@router.get("/client/{cid}/reports/subscriptions")
async def subscriptions_report(
    cid: str,
    metric: str = Query(..., description="NEW | ACTIVE | TOTAL"),
    dimension: Optional[str] = Query(
        None, description="TIME | SPACE | CROP | PACKAGE (drill only)",
    ),
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Subscriptions subject area — headline metric OR dimension drill.

    Without ``dimension``: returns the headline shape from the matching
    ``subs_*`` query (e.g. ``{"subscriptions": 2847, "farmers": 1912}``
    for ACTIVE).

    With ``dimension``: returns a list of ``{label, ...}`` rows from
    the matching ``subs_*_by_dimension`` query for a drill chart.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "subscriptions")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
    )

    metric_up = metric.upper()

    if dimension is None:
        if metric_up == "ACTIVE":
            return await queries.subs_active(db, cid, filters)
        if metric_up == "TOTAL":
            return await queries.subs_total(db, cid, filters)
        if metric_up == "NEW":
            return await queries.subs_new(db, cid, filters)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown subscriptions metric '{metric}'.",
            },
        )

    raise HTTPException(
        status_code=501,
        detail={
            "code": "dimension_not_implemented",
            "message": "Dimension drills are on the Phase 1 punch list; "
                       "not yet wired.",
        },
    )


# ── Orders subject area ───────────────────────────────────────────────────────

@router.get("/client/{cid}/reports/orders")
async def orders_report(
    cid: str,
    metric: str = Query(
        ..., description="COUNT | ITEMS | BRAND_MIX | ROUTING | CONVERSION",
    ),
    dimension: Optional[str] = Query(
        None, description="TIME | SPACE | CROP | PACKAGE (drill only)",
    ),
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Orders subject area — headline metric OR dimension drill.

    Same auth gate + filter shape as ``subscriptions_report``. Period
    is meaningful for every Order metric (they're time-bounded event
    counts, not point-in-time snapshots), so unlike Subscriptions,
    Period always narrows the result.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "orders")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
    )

    metric_up = metric.upper()

    if dimension is None:
        if metric_up == "COUNT":
            return await queries.orders_count(db, cid, filters)
        if metric_up == "ROUTING":
            return await queries.orders_routing(db, cid, filters)
        if metric_up == "ITEMS":
            return await queries.orders_items(db, cid, filters)
        if metric_up in {"BRAND_MIX", "CONVERSION"}:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "metric_not_implemented",
                    "message": f"Orders '{metric_up}' is on the "
                               "Phase 1 punch list; not yet wired.",
                },
            )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown orders metric '{metric}'.",
            },
        )

    raise HTTPException(
        status_code=501,
        detail={
            "code": "dimension_not_implemented",
            "message": "Dimension drills are on the Phase 1 punch list; "
                       "not yet wired.",
        },
    )
