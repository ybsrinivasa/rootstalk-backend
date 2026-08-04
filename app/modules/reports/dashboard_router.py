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
import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
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
    _assert_ca_for_export,
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
    dealer_user_id: Optional[str] = None,
) -> queries.ReportFilters:
    return queries.ReportFilters(
        period_from=period_from,
        period_to=period_to,
        crop_cosh_id=crop_cosh_id,
        state_cosh_id=state_cosh_id,
        district_cosh_id=district_cosh_id,
        package_id=package_id,
        dealer_user_id=dealer_user_id,
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
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    dealer_user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Options for the five filter chips (Crop / State / District /
    Package / Dealer) on the Reports pages.

    **Cascading behaviour (added 2026-08-04).** When any chip filter is
    passed as a query param, every OTHER chip's options are narrowed
    to only values that are intersectable with the current selection.
    E.g., ``crop_cosh_id=<tomato>`` → the states / districts / packages
    / dealers lists shrink to only those with at least one row in the
    intersection.

    A chip never narrows itself (else you couldn't unset an active
    filter). The frontend refetches on every chip change and reconciles
    any now-invalid selection with a "cleared — no data" toast.

    All queries build on ``_subscription_scope(cid)`` so the filter
    contract (client scoping + training exclusion + soft-delete
    cascade) is enforced at a single place.
    """
    await _assert_client_report_reader(db, current_user, cid)

    from app.modules.clients.models import ClientPromoter
    from app.modules.orders.models import DealerProfile, Order

    def scoped(exclude: str):
        """Subscription-scoped base with Package + User pre-joined; adds
        Order only when Dealer is either filtered or being computed. Then
        applies every chip value EXCEPT the one named ``exclude``.

        Every option-query starts from this so each chip's list is the
        exact intersection of the OTHER chips."""
        stmt = queries._subscription_scope(cid)
        stmt = stmt.join(Package, Package.id == Subscription.package_id)
        stmt = stmt.join(User, User.id == Subscription.farmer_user_id)
        needs_order = (exclude != 'dealer' and dealer_user_id) or exclude == 'dealer'
        if needs_order:
            stmt = stmt.join(Order, Order.subscription_id == Subscription.id)
        if exclude != 'crop' and crop_cosh_id:
            stmt = stmt.where(Package.crop_cosh_id == crop_cosh_id)
        if exclude != 'state' and state_cosh_id:
            stmt = stmt.where(User.state_cosh_id == state_cosh_id)
        if exclude != 'district' and district_cosh_id:
            stmt = stmt.where(User.district_cosh_id == district_cosh_id)
        if exclude != 'package' and package_id:
            stmt = stmt.where(Subscription.package_id == package_id)
        if exclude != 'dealer' and dealer_user_id:
            stmt = stmt.where(Order.dealer_user_id == dealer_user_id)
        return stmt

    crop_ids = [
        r[0] for r in (await db.execute(
            scoped('crop').with_only_columns(Package.crop_cosh_id).distinct()
        )).all() if r[0]
    ]
    state_ids = [
        r[0] for r in (await db.execute(
            scoped('state').with_only_columns(User.state_cosh_id).distinct()
        )).all() if r[0]
    ]
    district_ids = [
        r[0] for r in (await db.execute(
            scoped('district').with_only_columns(User.district_cosh_id).distinct()
        )).all() if r[0]
    ]

    # Packages — sub-scoped (only packages that have at least one sub
    # under the current OTHER-chip filters). Pre-cascade this was a
    # client-wide list including unsubscribed drafts.
    package_ids = [
        r[0] for r in (await db.execute(
            scoped('package').with_only_columns(Subscription.package_id).distinct()
        )).all() if r[0]
    ]
    package_rows = (await db.execute(
        select(Package.id, Package.name).where(
            Package.id.in_(package_ids),
            Package.status != PackageStatus.DRAFT,
        ).order_by(Package.name)
    )).all() if package_ids else []

    # Dealers — the client's onboarded dealers, further narrowed to
    # those who have received at least one order intersectable with
    # the OTHER chips. Zero-order onboarded dealers drop from the list
    # under any active filter (the pure-cascade behaviour). With no
    # other chips set, still requires at least one order — the "onboarded
    # but never used" case is not exposed as a chip option.
    dealer_ids_with_orders = [
        r[0] for r in (await db.execute(
            scoped('dealer').with_only_columns(Order.dealer_user_id).distinct()
        )).all() if r[0]
    ]
    onboarded_dealer_ids = {
        r[0] for r in (await db.execute(
            select(ClientPromoter.user_id).where(
                ClientPromoter.client_id == cid,
                ClientPromoter.promoter_type == "DEALER",
                ClientPromoter.status == "ACTIVE",
            )
        )).all()
    }
    dealer_ids = [d for d in dealer_ids_with_orders if d in onboarded_dealer_ids]
    dealer_rows = (await db.execute(
        select(User.id, User.name, DealerProfile.shop_name)
        .outerjoin(DealerProfile, DealerProfile.user_id == User.id)
        .where(User.id.in_(dealer_ids))
    )).all() if dealer_ids else []
    dealers = sorted(
        [
            {"id": r.id, "name": r.shop_name or r.name or "(unnamed)"}
            for r in dealer_rows
        ],
        key=lambda o: o["name"].lower(),
    )

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
        "dealers":   dealers,
    }


# ── Overview (composed headlines) ─────────────────────────────────────────────

@router.get("/client/{cid}/reports/overview")
async def overview_report(
    cid: str,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    prev_period_from: Optional[datetime] = None,
    prev_period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reports landing payload — headline metrics in one round-trip.

    When ``prev_period_from`` + ``prev_period_to`` are supplied, the
    three period-based metrics also run against the prev window and
    the response carries a ``prev`` block for delta rendering. Chip
    filters (crop / state / district / package) are shared across
    both windows.

    Same auth gate as the drill endpoints.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "overview")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
    )
    prev_filters = None
    if prev_period_from is not None and prev_period_to is not None:
        prev_filters = _build_filters(
            prev_period_from, prev_period_to,
            crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        )
    return await queries.overview_bundle(db, cid, filters, prev_filters)


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

    dim_up = dimension.upper()
    if dim_up not in {"CROP", "SPACE", "PACKAGE", "TIME"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_dimension",
                "message": f"Unknown dimension '{dimension}'.",
            },
        )
    if metric_up == "ACTIVE":
        if dim_up == "TIME":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "dimension_not_applicable",
                    "message": "Active subscriptions is a point-in-time "
                               "snapshot — TIME dimension is meaningless.",
                },
            )
        rows = await queries.subs_active_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "TOTAL":
        rows = await queries.subs_total_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "NEW":
        rows = await queries.subs_new_by_dimension(db, cid, filters, dim_up)
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown subscriptions metric '{metric}'.",
            },
        )
    return await _hydrate_dimension_labels(db, rows, dim_up)


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
        if metric_up == "BRAND_MIX":
            return await queries.orders_brand_mix(db, cid, filters)
        if metric_up == "CONVERSION":
            return await queries.orders_conversion(db, cid, filters)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown orders metric '{metric}'.",
            },
        )

    dim_up = dimension.upper()
    if dim_up not in {"CROP", "SPACE", "PACKAGE", "TIME"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_dimension",
                "message": f"Unknown dimension '{dimension}'.",
            },
        )
    if metric_up == "COUNT":
        rows = await queries.orders_count_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "ROUTING":
        rows = await queries.orders_routing_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "ITEMS":
        rows = await queries.orders_items_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "BRAND_MIX":
        rows = await queries.orders_brand_mix_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "CONVERSION":
        rows = await queries.orders_conversion_by_dimension(db, cid, filters, dim_up)
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown orders metric '{metric}'.",
            },
        )
    return await _hydrate_dimension_labels(db, rows, dim_up)


# ── Sales subject area (Phase 2) ─────────────────────────────────────────────

@router.get("/client/{cid}/reports/sales")
async def sales_report(
    cid: str,
    metric: str = Query(
        ...,
        description=(
            "LOCKED | RECOMMENDED_HONORED | RECOMMENDED_SUBSTITUTED | OPEN | NETWORK_TOTAL"
        ),
    ),
    dimension: Optional[str] = Query(
        None, description="TIME | SPACE | CROP | PACKAGE | DEALER (drill only)",
    ),
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    dealer_user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sales subject area — headline metric OR dimension drill.

    Volume-only (never price), three unit buckets (Litres / Kilograms /
    Numbers). See ``project_rootstalk_client_reports_phase_2.md`` for the
    full metric definitions. Sale marker inherited from Phase 1
    (``PackingList.farmer_received_at IS NOT NULL``).

    First vertical slice ships ``LOCKED`` end-to-end. The other three
    headline metrics + dimension drills + the two pivot matrices fill in
    metric-by-metric per the punch list.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "sales")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        dealer_user_id=dealer_user_id,
    )

    metric_up = metric.upper()

    if dimension is None:
        if metric_up == "LOCKED":
            return await queries.sales_locked_volume(db, cid, filters)
        if metric_up == "RECOMMENDED_HONORED":
            return await queries.sales_recommended_honored_volume(db, cid, filters)
        if metric_up == "RECOMMENDED_SUBSTITUTED":
            return await queries.sales_recommended_substituted_volume(db, cid, filters)
        if metric_up == "OPEN":
            return await queries.sales_open_volume(db, cid, filters)
        if metric_up == "NETWORK_TOTAL":
            return await queries.sales_network_total_volume(db, cid, filters)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown sales metric '{metric}'.",
            },
        )

    dim_up = dimension.upper()
    if dim_up not in {"CROP", "SPACE", "PACKAGE", "TIME", "DEALER"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_dimension",
                "message": f"Unknown dimension '{dimension}'.",
            },
        )
    if metric_up == "LOCKED":
        rows = await queries.sales_locked_volume_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "RECOMMENDED_HONORED":
        rows = await queries.sales_recommended_honored_volume_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "RECOMMENDED_SUBSTITUTED":
        rows = await queries.sales_recommended_substituted_volume_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "OPEN":
        rows = await queries.sales_open_volume_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "NETWORK_TOTAL":
        rows = await queries.sales_network_total_volume_by_dimension(db, cid, filters, dim_up)
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown sales metric '{metric}'.",
            },
        )
    return await _hydrate_dimension_labels(db, rows, dim_up)


# ─── Sales pivot matrices ─────────────────────────────────────────────────

async def _brand_names_for(db: AsyncSession, cosh_ids: list[str]) -> dict[str, str]:
    """Bulk-lookup Cosh tradename → English display name. Falls back
    to raw cosh_id when a row is missing so a rare mismatch still
    renders something. Same treatment as _cosh_names but semantically
    scoped to brand tradenames (they live in the same table today —
    kept as a separate helper in case the schema splits later)."""
    if not cosh_ids:
        return {}
    return await _cosh_names(db, cosh_ids)


@router.get("/client/{cid}/reports/sales/recommended-matrix")
async def sales_recommended_matrix(
    cid: str,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    dealer_user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recommended vs Given pivot — one row per recommended brand,
    each row's `given` array shows what dealers actually sold. Same
    filter chip set as the headline cards."""
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "sales")
    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        dealer_user_id=dealer_user_id,
    )
    rows = await queries.sales_recommended_vs_given_matrix(db, cid, filters)

    # Hydrate brand names — one bulk lookup covering all cosh_ids
    # that appear on either side of the matrix.
    all_brand_ids: set[str] = set()
    for r in rows:
        if r["our_brand_cosh_id"]:
            all_brand_ids.add(r["our_brand_cosh_id"])
        for g in r["given"]:
            if g.get("sold_brand_cosh_id"):
                all_brand_ids.add(g["sold_brand_cosh_id"])
    names = await _brand_names_for(db, list(all_brand_ids))

    for r in rows:
        r["our_brand_name"] = names.get(r["our_brand_cosh_id"]) or r["our_brand_cosh_id"] or "—"
        for g in r["given"]:
            if g.get("sold_brand_cosh_id"):
                g["sold_brand_name"] = names.get(g["sold_brand_cosh_id"]) or g["sold_brand_cosh_id"]
            else:
                g["sold_brand_name"] = None    # frontend renders "(no brand recorded)"
    return {"rows": rows}


@router.get("/client/{cid}/reports/sales/open-matrix")
async def sales_open_matrix(
    cid: str,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    dealer_user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Common Name → Brand Sold pivot — one row per common name (or
    l2_type fallback), each row's `given` array shows which brands
    dealers picked. Same filter chip set as the headline cards."""
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "sales")
    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        dealer_user_id=dealer_user_id,
    )
    rows = await queries.sales_common_name_vs_sold_matrix(db, cid, filters)

    # Bulk hydrate — common_name cosh_ids and sold_brand cosh_ids
    # both live in the CoshCoreItem catalog.
    cosh_ids: set[str] = set()
    for r in rows:
        if r.get("common_name_cosh_id"):
            cosh_ids.add(r["common_name_cosh_id"])
        for g in r["given"]:
            if g.get("sold_brand_cosh_id"):
                cosh_ids.add(g["sold_brand_cosh_id"])
    names = await _cosh_names(db, list(cosh_ids))

    for r in rows:
        # common_name_key is either a cosh_id or a raw l2_type
        # string; if the key is a cosh_id we hydrate, else the
        # verbatim l2_type IS the display label.
        cid_key = r.get("common_name_cosh_id")
        if cid_key:
            r["common_name_label"] = names.get(cid_key) or cid_key
        else:
            r["common_name_label"] = r["common_name_key"] or "(unspecified)"
        for g in r["given"]:
            if g.get("sold_brand_cosh_id"):
                g["sold_brand_name"] = names.get(g["sold_brand_cosh_id"]) or g["sold_brand_cosh_id"]
            else:
                g["sold_brand_name"] = None
    return {"rows": rows}


async def _hydrate_dimension_labels(
    db: AsyncSession, rows: list[dict], dimension: str,
) -> list[dict]:
    """Resolve cosh_id keys to English labels for CROP / SPACE rows.
    PACKAGE rows already carry ``package_name``; TIME rows carry
    ISO datetimes that the frontend formats. DEALER rows resolve the
    user_id → shop_name (fallback to user.name) in one batch."""
    if dimension in {"CROP", "SPACE"}:
        keys = [r["key"] for r in rows if r.get("key")]
        names = await _cosh_names(db, keys) if keys else {}
        return [
            {**r, "label": names.get(r.get("key") or "", r.get("key") or "—")}
            for r in rows
        ]
    if dimension == "PACKAGE":
        return [{**r, "label": r.get("package_name") or "—"} for r in rows]
    if dimension == "TIME":
        # Frontend formats the ISO string per bucket size — we just
        # normalise to isoformat here so the wire shape is stable.
        return [
            {**r, "label": r["key"].isoformat() if r.get("key") else "—"}
            for r in rows
        ]
    if dimension == "DEALER":
        from app.modules.orders.models import DealerProfile
        dealer_ids = [r["key"] for r in rows if r.get("key")]
        if not dealer_ids:
            return [{**r, "label": r.get("key") or "—"} for r in rows]
        dealer_rows = (await db.execute(
            select(
                User.id,
                User.name,
                DealerProfile.shop_name,
            )
            .outerjoin(DealerProfile, DealerProfile.user_id == User.id)
            .where(User.id.in_(dealer_ids))
        )).all()
        names: dict[str, str] = {}
        for r in dealer_rows:
            names[r.id] = r.shop_name or r.name or r.id
        return [
            {**r, "label": names.get(r.get("key") or "", r.get("key") or "—")}
            for r in rows
        ]
    return rows


# ── CSV export ────────────────────────────────────────────────────────────────

def _format_dt(dt) -> str:
    """ISO-8601 formatter that survives None gracefully."""
    return dt.isoformat() if dt is not None else ""


def _stream_csv(header: list[str], rows: list[list[str]]) -> StreamingResponse:
    """Build a streaming CSV response tuned for Excel compatibility.

    Three defences against Excel's habit of "helpfully" mis-parsing
    CSVs, all learned the hard way 2026-07-28:

    1. ``sep=,`` as the first line — Excel-specific directive that
       forces comma as the delimiter regardless of the user's locale
       (some European Excel installs default to semicolon and would
       otherwise auto-detect space as a sub-delimiter, blowing every
       "Farmer Name" cell across two columns).

    2. **UTF-8 BOM** at file start — signals UTF-8 to Excel so it
       stops guessing and reads column boundaries reliably.

    3. **``csv.QUOTE_ALL``** — every field is wrapped in double quotes.
       Removes any ambiguity about where a cell ends. Cheap on wire
       size, huge on parser sanity.

    Phone numbers are separately wrapped as ``="+91…"`` at the call
    site so Excel doesn't convert them to scientific notation; see
    ``_excel_text``.

    StringIO is fine for staging volumes; if row counts grow past
    ~50k the generator can be swapped for a per-row yield without
    touching call sites.
    """
    buf = io.StringIO()
    buf.write("sep=,\r\n")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    body = "﻿" + buf.getvalue()  # UTF-8 BOM prefix
    return StreamingResponse(iter([body]), media_type="text/csv")


def _excel_text(value: str) -> str:
    """Force Excel to treat a numeric-looking string as text.

    Excel converts anything starting with ``+``, ``-`` or all digits
    into a number by default — long phone numbers like ``+919000889927``
    become ``9.19001E+11``. Wrapping as ``="+919000889927"`` makes
    Excel evaluate it as a formula returning the string, which
    preserves the exact display. Other CSV consumers see the literal
    ``="+919000889927"`` which is imperfect but readable; this is the
    least-bad option for a CSV that has to be Excel-safe.
    """
    if not value:
        return ""
    return f'="{value}"'


def _csv_filename(cid: str, subject: str) -> str:
    """`<cid>-<subject>-<YYYYMMDD>.csv` — server sends via
    Content-Disposition; the frontend also builds a fallback if
    the header is stripped by CORS."""
    stamp = datetime.utcnow().strftime("%Y%m%d")
    return f"{cid}-{subject}-{stamp}.csv"


@router.get("/client/{cid}/reports/subscriptions/export.csv")
async def subscriptions_csv(
    cid: str,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Row-per-Subscription CSV. CA-only (Report User gets 403)."""
    await _assert_ca_for_export(db, current_user, cid)
    _assert_subject_enabled(cid, "subscriptions")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
    )
    rows = await queries.subscriptions_rows(db, cid, filters)

    cosh_ids: list[str] = []
    for r in rows:
        for k in ("crop_cosh_id", "state_cosh_id", "district_cosh_id"):
            v = r.get(k)
            if v:
                cosh_ids.append(v)
    cosh_names = await _cosh_names(db, cosh_ids) if cosh_ids else {}

    header = [
        "Subscription Ref", "Farmer Name", "Farmer Phone",
        "Package", "Crop", "State", "District",
        "Subscription Date", "Status", "Type",
    ]
    body = [
        [
            r["subscription_ref"] or "",
            r["farmer_name"] or "",
            _excel_text(r["farmer_phone"] or ""),
            r["package_name"] or "",
            cosh_names.get(r["crop_cosh_id"] or "", r["crop_cosh_id"] or ""),
            cosh_names.get(r["state_cosh_id"] or "", r["state_cosh_id"] or ""),
            cosh_names.get(r["district_cosh_id"] or "", r["district_cosh_id"] or ""),
            _format_dt(r["subscription_date"]),
            r["status"] or "",
            r["subscription_type"] or "",
        ]
        for r in rows
    ]
    resp = _stream_csv(header, body)
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{_csv_filename(cid, "subscriptions")}"'
    )
    return resp


@router.get("/client/{cid}/reports/orders/export.csv")
async def orders_csv(
    cid: str,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Row-per-Order CSV. CA-only."""
    await _assert_ca_for_export(db, current_user, cid)
    _assert_subject_enabled(cid, "orders")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
    )
    rows = await queries.orders_rows(db, cid, filters)

    crop_ids = [r["crop_cosh_id"] for r in rows if r.get("crop_cosh_id")]
    cosh_names = await _cosh_names(db, crop_ids) if crop_ids else {}

    header = [
        "Order Ref", "Order Date", "Status",
        "Farmer Name", "Farmer Phone",
        "Dealer", "Facilitator", "Routing",
        "Package", "Crop",
        "Items Total", "Items Approved", "Items Rejected",
        "Picked Up At",
    ]
    body = [
        [
            r["order_ref"] or "",
            _format_dt(r["order_date"]),
            r["status"] or "",
            r["farmer_name"] or "",
            _excel_text(r["farmer_phone"] or ""),
            r["dealer_name"] or "",
            r["facilitator_name"] or "",
            "Via Facilitator" if r["facilitator_user_id"] else "Direct",
            r["package_name"] or "",
            cosh_names.get(r["crop_cosh_id"] or "", r["crop_cosh_id"] or ""),
            str(r["items_total"] or 0),
            str(r["items_approved"] or 0),
            str(r["items_rejected"] or 0),
            _format_dt(r["picked_up_at"]),
        ]
        for r in rows
    ]
    resp = _stream_csv(header, body)
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{_csv_filename(cid, "orders")}"'
    )
    return resp
