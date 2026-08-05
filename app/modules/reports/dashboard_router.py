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
    promoter_user_id: Optional[str] = None,
    severity: Optional[str] = None,
    pundit_id: Optional[str] = None,
) -> queries.ReportFilters:
    return queries.ReportFilters(
        period_from=period_from,
        period_to=period_to,
        crop_cosh_id=crop_cosh_id,
        state_cosh_id=state_cosh_id,
        district_cosh_id=district_cosh_id,
        package_id=package_id,
        dealer_user_id=dealer_user_id,
        promoter_user_id=promoter_user_id,
        severity=severity,
        pundit_id=pundit_id,
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
    promoter_user_id: Optional[str] = None,
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
        if exclude != 'promoter' and promoter_user_id:
            stmt = stmt.where(Subscription.promoter_user_id == promoter_user_id)
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
    #
    # Seeds path — seed orders live in seed_orders_full, not orders.
    # Union both so a client whose network only handles seeds still
    # sees dealer options.
    from app.modules.seed_mgmt.models import SeedOrderFull as _SOF
    dealer_ids_with_pf_orders = {
        r[0] for r in (await db.execute(
            scoped('dealer').with_only_columns(Order.dealer_user_id).distinct()
        )).all() if r[0]
    }
    # Rebuild a seed-scoped query similar shape to scoped('dealer'),
    # applying the same OTHER-chip filters.
    seed_scope = queries._subscription_scope(cid)
    seed_scope = seed_scope.join(_SOF, _SOF.subscription_id == Subscription.id)
    seed_scope = seed_scope.join(Package, Package.id == Subscription.package_id)
    seed_scope = seed_scope.join(User, User.id == Subscription.farmer_user_id)
    seed_scope = seed_scope.where(_SOF.status == "PURCHASED")
    if crop_cosh_id:
        seed_scope = seed_scope.where(Package.crop_cosh_id == crop_cosh_id)
    if state_cosh_id:
        seed_scope = seed_scope.where(User.state_cosh_id == state_cosh_id)
    if district_cosh_id:
        seed_scope = seed_scope.where(User.district_cosh_id == district_cosh_id)
    if package_id:
        seed_scope = seed_scope.where(Subscription.package_id == package_id)
    dealer_ids_with_seed_orders = {
        r[0] for r in (await db.execute(
            seed_scope.with_only_columns(_SOF.dealer_user_id).distinct()
        )).all() if r[0]
    }
    dealer_ids_with_orders = list(dealer_ids_with_pf_orders | dealer_ids_with_seed_orders)
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

    # Promoters — every distinct promoter_user_id on a subscription in
    # the intersection of the OTHER chips. NULL (no promoter) drops out.
    # Names come from users.name; the CA UI shows the plain name.
    promoter_ids = [
        r[0] for r in (await db.execute(
            scoped('promoter').with_only_columns(Subscription.promoter_user_id).distinct()
        )).all() if r[0]
    ]
    promoter_rows = (await db.execute(
        select(User.id, User.name).where(User.id.in_(promoter_ids))
    )).all() if promoter_ids else []
    promoters = sorted(
        [{"id": r.id, "name": r.name or "(unnamed)"} for r in promoter_rows],
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
        "promoters": promoters,
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
            "LOCKED | RECOMMENDED | OPEN | NETWORK_TOTAL — leads/conversion shape"
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

    2026-08-04 reframing: report speaks in leads/conversion terms
    (matches how a manufacturer's leadership scans the story). Volumes
    still live in the matrices — see /sales/*-matrix endpoints.

    Metric shapes:
      LOCKED / OPEN / NETWORK_TOTAL   → {leads, converted}
      RECOMMENDED (formerly _HONORED + _SUBSTITUTED) → {leads, honored,
        substituted, pending}  (converted = honored + substituted;
        headline % = honored / leads)

    LOCKED + NETWORK_TOTAL combine pesticide/fertilizer + seed leads
    via `_merge_leads_totals`. RECOMMENDED + OPEN are pesticide/fert
    only (seeds have no brand-authoring flow).
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
            pf = await queries.sales_locked_leads(db, cid, filters)
            seed = await queries.sales_seed_locked_leads(db, cid, filters)
            return queries._merge_leads_totals(pf, seed)
        if metric_up == "RECOMMENDED":
            return await queries.sales_recommended_leads(db, cid, filters)
        if metric_up == "OPEN":
            return await queries.sales_open_leads(db, cid, filters)
        if metric_up == "NETWORK_TOTAL":
            pf = await queries.sales_network_total_leads(db, cid, filters)
            seed = await queries.sales_seed_network_total_leads(db, cid, filters)
            return queries._merge_leads_totals(pf, seed)
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
        pf = await queries.sales_locked_leads_by_dimension(db, cid, filters, dim_up)
        seed = await queries.sales_seed_leads_by_dimension(
            db, cid, filters, dim_up, locked_only=True,
        )
        rows = queries._merge_leads_dim_rows(pf, seed, dim_up)
    elif metric_up == "RECOMMENDED":
        rows = await queries.sales_recommended_leads_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "OPEN":
        rows = await queries.sales_open_leads_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "NETWORK_TOTAL":
        pf = await queries.sales_network_total_leads_by_dimension(db, cid, filters, dim_up)
        seed = await queries.sales_seed_leads_by_dimension(
            db, cid, filters, dim_up, locked_only=False,
        )
        rows = queries._merge_leads_dim_rows(pf, seed, dim_up)
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


@router.get("/client/{cid}/reports/sales/locked-matrix")
async def sales_locked_matrix(
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
    """Locked-Brand matrix — per-brand sales through our onboarded
    network. By enforcement, every row's `given` should be a single
    honored entry; substitutions or NULL brands surfacing here are
    data-integrity anomalies. Same nested-row + brand-name hydration
    treatment as the recommended matrix."""
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "sales")
    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        dealer_user_id=dealer_user_id,
    )
    # Pesticide/fertilizer path — brand cosh_ids need Cosh tradename
    # hydration. Seed path — variety rows already carry their own
    # names (SeedVariety.name), no hydration needed. We concat, then
    # hydrate only the cosh_id-bearing rows.
    pf_rows = await queries.sales_locked_brand_matrix(db, cid, filters)
    seed_rows = await queries.sales_seed_locked_matrix(db, cid, filters)

    all_brand_ids: set[str] = set()
    for r in pf_rows:
        if r["our_brand_cosh_id"]:
            all_brand_ids.add(r["our_brand_cosh_id"])
        for g in r["given"]:
            if g.get("sold_brand_cosh_id"):
                all_brand_ids.add(g["sold_brand_cosh_id"])
    names = await _brand_names_for(db, list(all_brand_ids))

    for r in pf_rows:
        r["our_brand_name"] = names.get(r["our_brand_cosh_id"]) or r["our_brand_cosh_id"] or "—"
        for g in r["given"]:
            if g.get("sold_brand_cosh_id"):
                g["sold_brand_name"] = names.get(g["sold_brand_cosh_id"]) or g["sold_brand_cosh_id"]
            else:
                g["sold_brand_name"] = None
    # Seed rows already carry variety names.
    combined = pf_rows + seed_rows

    # Attach leads/converted per row via two lookups. Row key is
    # OrderItem.brand_cosh_id for pesticide/fert rows and
    # SeedVariety.id for seed rows — both stored under
    # `our_brand_cosh_id` on the row dict.
    pf_row_leads = await queries._matrix_locked_row_leads(db, cid, filters)
    seed_row_leads = await queries._matrix_seed_row_leads(db, cid, filters)
    for r in combined:
        key = r.get("our_brand_cosh_id")
        entry = pf_row_leads.get(key) or seed_row_leads.get(key)
        if entry:
            r["leads"] = entry["leads"]
            r["converted"] = entry["converted"]
        else:
            r["leads"] = 0
            r["converted"] = 0

    combined.sort(key=lambda r: -r["leads"])
    return {"rows": combined}


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

    row_leads = await queries._matrix_recommended_row_leads(db, cid, filters)
    for r in rows:
        entry = row_leads.get(r.get("our_brand_cosh_id"))
        r["leads"] = entry["leads"] if entry else 0
        r["converted"] = entry["converted"] if entry else 0
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

    row_leads = await queries._matrix_open_row_leads(db, cid, filters)
    for r in rows:
        entry = row_leads.get(r.get("common_name_key"))
        r["leads"] = entry["leads"] if entry else 0
        r["converted"] = entry["converted"] if entry else 0
    return {"rows": rows}


@router.get("/client/{cid}/reports/sales/dealer-scorecard")
async def sales_dealer_scorecard(
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
    """Per-dealer leads/conversion summary + pooled totals row. Rows
    hydrated with shop_name (falls back to user.name)."""
    from app.modules.orders.models import DealerProfile
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "sales")
    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        dealer_user_id=dealer_user_id,
    )
    rows = await queries.sales_dealer_scorecard(db, cid, filters)

    dealer_ids = [r["dealer_user_id"] for r in rows]
    if dealer_ids:
        dealer_rows = (await db.execute(
            select(User.id, User.name, DealerProfile.shop_name)
            .outerjoin(DealerProfile, DealerProfile.user_id == User.id)
            .where(User.id.in_(dealer_ids))
        )).all()
        names = {r.id: (r.shop_name or r.name or r.id) for r in dealer_rows}
    else:
        names = {}
    for r in rows:
        r["dealer_name"] = names.get(r["dealer_user_id"], r["dealer_user_id"])

    # Synthetic pooled row appended so the frontend can render a
    # totals footer without re-summing client-side.
    pooled = {
        "dealer_user_id": None,
        "dealer_name": f"Pooled ({len(rows)} dealer{'s' if len(rows) != 1 else ''})",
        "leads": sum(r["leads"] for r in rows),
        "converted": sum(r["converted"] for r in rows),
        "pooled": True,
    }
    return {"rows": rows, "pooled": pooled}


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


# ═══════════════════════════════════════════════════════════════════════════
# Promoters subject area (Phase 3, 2026-08-05)
# ═══════════════════════════════════════════════════════════════════════════

_PROMOTER_METRICS = {"ACTIVE", "SUBSCRIPTIONS", "ACRES", "LEADS"}
_PROMOTER_DIMENSIONS = {"TIME", "SPACE", "CROP"}


@router.get("/client/{cid}/reports/promoters")
async def promoters_report(
    cid: str,
    metric: str = Query(
        ...,
        description=(
            "ACTIVE | SUBSCRIPTIONS | ACRES | LEADS — headline OR dimension drill"
        ),
    ),
    dimension: Optional[str] = Query(
        None, description="TIME | SPACE | CROP (drill only)",
    ),
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    promoter_user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promoters subject area — headline metric OR TIME/SPACE/CROP drill.

    Metric shapes:
      ACTIVE / SUBSCRIPTIONS / LEADS  → {"count"|"leads": <int>}
      ACRES                           → {"acres": <float>}

    Attribution: Subscription.promoter_user_id (nullable) — subs with
    NULL promoter drop out of every promoter metric. Leads follow the
    Sales lead definition (ORDER_LEAD_STATUSES + ITEM_EXCLUDED_STATUSES)
    so cross-report comparisons stay honest.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "promoters")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        promoter_user_id=promoter_user_id,
    )

    metric_up = metric.upper()
    if metric_up not in _PROMOTER_METRICS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_metric",
                "message": f"Unknown promoters metric '{metric}'.",
            },
        )

    if dimension is None:
        if metric_up == "ACTIVE":
            return await queries.promoters_active(db, cid, filters)
        if metric_up == "SUBSCRIPTIONS":
            return await queries.subscriptions_promoted(db, cid, filters)
        if metric_up == "ACRES":
            return await queries.acres_promoted(db, cid, filters)
        return await queries.promoters_leads(db, cid, filters)

    dim_up = dimension.upper()
    if dim_up not in _PROMOTER_DIMENSIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_dimension",
                "message": f"Unknown dimension '{dimension}'.",
            },
        )
    if metric_up == "ACTIVE":
        rows = await queries.promoters_active_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "SUBSCRIPTIONS":
        rows = await queries.subscriptions_promoted_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "ACRES":
        rows = await queries.acres_promoted_by_dimension(db, cid, filters, dim_up)
    else:
        rows = await queries.promoters_leads_by_dimension(db, cid, filters, dim_up)
    return await _hydrate_dimension_labels(db, rows, dim_up)


@router.get("/client/{cid}/reports/promoters/scorecard")
async def promoters_scorecard(
    cid: str,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    package_id: Optional[str] = None,
    promoter_user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-promoter scorecard rows + pooled totals footer.

    Row shape: {promoter_user_id, name, subscriptions, acres, leads}.
    Pooled row uses promoter_user_id = None and name = "All promoters"
    so the frontend can render it separately without a special flag.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "promoters")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id, package_id,
        promoter_user_id=promoter_user_id,
    )
    rows = await queries.promoter_scorecard(db, cid, filters)
    if not rows:
        return {"rows": [], "pooled": None}

    # Hydrate names in one batch.
    ids = [r["promoter_user_id"] for r in rows if r.get("promoter_user_id")]
    name_rows = (await db.execute(
        select(User.id, User.name).where(User.id.in_(ids))
    )).all() if ids else []
    names = {r.id: (r.name or "(unnamed)") for r in name_rows}
    hydrated = [
        {**r, "name": names.get(r["promoter_user_id"], "(unnamed)")}
        for r in rows
    ]

    pooled = {
        "promoter_user_id": None,
        "name":             "All promoters",
        "subscriptions":    sum(r["subscriptions"] for r in hydrated),
        "acres":            round(sum(r["acres"] for r in hydrated), 2),
        "leads":            sum(r["leads"] for r in hydrated),
    }
    return {"rows": hydrated, "pooled": pooled}


# ═══════════════════════════════════════════════════════════════════════════
# Queries subject area (Phase 4, 2026-08-05)
# ═══════════════════════════════════════════════════════════════════════════

_QUERY_METRICS = {"COUNT", "RESPONDED", "AVG_RESPONSE", "SLA_24H", "EXPIRED", "SEVERITY"}
_QUERY_DIMENSIONS = {"TIME", "SPACE", "CROP"}
_QUERY_DRILL_METRICS = {"COUNT", "RESPONDED", "EXPIRED", "AVG_RESPONSE"}


@router.get("/client/{cid}/reports/queries")
async def queries_report(
    cid: str,
    metric: str = Query(
        ...,
        description="COUNT | RESPONDED | AVG_RESPONSE | SLA_24H | EXPIRED | SEVERITY",
    ),
    dimension: Optional[str] = Query(
        None, description="TIME | SPACE | CROP (drill only, for the four countable metrics)",
    ),
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    severity: Optional[str] = None,
    pundit_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Queries subject area — headline metric OR dimension drill.

    Metric shapes (headline, no dimension):
      COUNT / RESPONDED / EXPIRED   → {"count": int}
      AVG_RESPONSE                  → {"avg_seconds": float, "responded": int}
      SLA_24H                       → {"within": int, "total": int, "sla_hours": 24}
      SEVERITY                      → {"severe": N, "moderate": N, "low": N, "total": N}

    With ``dimension`` set (TIME | SPACE | CROP), only the four countable
    metrics are valid: COUNT, RESPONDED, EXPIRED, AVG_RESPONSE. Each
    returns ``[{key, value, label}, ...]``. SLA_24H and SEVERITY are
    headline-only.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "queries")

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id,
        None,  # package_id — not used in queries area
        severity=severity, pundit_id=pundit_id,
    )

    metric_up = metric.upper()
    if metric_up not in _QUERY_METRICS:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_metric", "message": f"Unknown queries metric '{metric}'."},
        )

    if dimension is None:
        if metric_up == "COUNT":
            return await queries.queries_count(db, cid, filters)
        if metric_up == "RESPONDED":
            return await queries.queries_responded(db, cid, filters)
        if metric_up == "AVG_RESPONSE":
            return await queries.queries_avg_response_seconds(db, cid, filters)
        if metric_up == "SLA_24H":
            return await queries.queries_sla_24h(db, cid, filters)
        if metric_up == "EXPIRED":
            return await queries.queries_expired(db, cid, filters)
        return await queries.queries_severity_split(db, cid, filters)

    dim_up = dimension.upper()
    if dim_up not in _QUERY_DIMENSIONS:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_dimension", "message": f"Unknown dimension '{dimension}'."},
        )
    if metric_up not in _QUERY_DRILL_METRICS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "metric_no_dimension",
                "message": f"Metric '{metric}' does not support dimension drill.",
            },
        )
    if metric_up == "COUNT":
        rows = await queries.queries_count_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "RESPONDED":
        rows = await queries.queries_responded_by_dimension(db, cid, filters, dim_up)
    elif metric_up == "EXPIRED":
        rows = await queries.queries_expired_by_dimension(db, cid, filters, dim_up)
    else:
        rows = await queries.queries_avg_response_by_dimension(db, cid, filters, dim_up)
    return await _hydrate_dimension_labels(db, rows, dim_up)


@router.get("/client/{cid}/reports/queries/pundit-scorecard")
async def queries_pundit_scorecard(
    cid: str,
    period_from: Optional[datetime] = None,
    period_to: Optional[datetime] = None,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    severity: Optional[str] = None,
    pundit_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-pundit row + pooled totals footer.

    Row shape: ``{pundit_id, name, role, direct, forwarded_in,
    responded, forwarded_out, returned, expired, avg_response_seconds}``.
    ``role`` is the client-scoped role from ClientFarmPundit — one of
    PRIMARY / PANEL / PROMOTER_PUNDIT.

    Reception sums (direct + forwarded_in) across rows will NOT equal
    the total-queries card in the Queries area — a query forwarded is
    counted on both pundits. Caption on the client caveats this.
    """
    await _assert_client_report_reader(db, current_user, cid)
    _assert_subject_enabled(cid, "queries")

    from app.modules.farmpundit.models import FarmPunditProfile, ClientFarmPundit

    filters = _build_filters(
        period_from, period_to,
        crop_cosh_id, state_cosh_id, district_cosh_id,
        None,
        severity=severity, pundit_id=pundit_id,
    )
    rows = await queries.pundit_scorecard(db, cid, filters)
    if not rows:
        return {"rows": [], "pooled": None}

    # Hydrate pundit_id → (user_id, name, role) in one batch.
    pundit_ids = [r["pundit_id"] for r in rows if r.get("pundit_id")]
    hydrated: list[dict] = []
    if pundit_ids:
        info_rows = (await db.execute(
            select(
                FarmPunditProfile.id.label("profile_id"),
                User.name.label("name"),
                ClientFarmPundit.role.label("role"),
            )
            .join(User, User.id == FarmPunditProfile.user_id)
            .outerjoin(
                ClientFarmPundit,
                (ClientFarmPundit.pundit_id == FarmPunditProfile.id)
                & (ClientFarmPundit.client_id == cid),
            )
            .where(FarmPunditProfile.id.in_(pundit_ids))
        )).all()
        info = {
            i.profile_id: {"name": i.name or "(unnamed)", "role": i.role or None}
            for i in info_rows
        }
        for r in rows:
            meta = info.get(r["pundit_id"], {"name": "(unknown)", "role": None})
            hydrated.append({
                **r,
                "name": meta["name"],
                "role": meta["role"],
                "avg_response_seconds": round(r["avg_response_seconds"], 1),
            })

    # Pooled row — reception sums honestly may exceed distinct-queries
    # totals (that's the whole point of the caption on the frontend).
    pooled = {
        "pundit_id": None,
        "name":              "All pundits",
        "role":              None,
        "direct":            sum(r["direct"] for r in hydrated),
        "forwarded_in":      sum(r["forwarded_in"] for r in hydrated),
        "responded":         sum(r["responded"] for r in hydrated),
        "forwarded_out":     sum(r["forwarded_out"] for r in hydrated),
        "returned":          sum(r["returned"] for r in hydrated),
        "expired":           sum(r["expired"] for r in hydrated),
        # Pooled avg response — mean of per-pundit means weighted by
        # responded count. Falls back to 0 when no responses.
        "avg_response_seconds": round(
            (sum(r["avg_response_seconds"] * r["responded"] for r in hydrated)
             / max(1, sum(r["responded"] for r in hydrated))),
            1,
        ),
    }
    return {"rows": hydrated, "pooled": pooled}


@router.get("/client/{cid}/reports/queries/filter-options")
async def queries_filter_options(
    cid: str,
    crop_cosh_id: Optional[str] = None,
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    severity: Optional[str] = None,
    pundit_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cascading chip options for the Queries page.

    Same cascade behaviour as the shared /filter-options: every chip's
    list is the intersection of the OTHER chips' current values. A chip
    never narrows itself.

    Scoped to Query rows (not Subscription rows) — so ``crops`` here
    means "crops that have at least one query in this client", which
    won't match the shared endpoint's Subscription-based crops list.
    """
    await _assert_client_report_reader(db, current_user, cid)

    from app.modules.farmpundit.models import (
        Query as QueryModel, QueryRemark, FarmPunditProfile,
    )

    def scoped(exclude: str):
        stmt = queries._query_scope(cid)
        stmt = stmt.join(User, User.id == QueryModel.farmer_user_id, isouter=False)
        if exclude != 'crop' and crop_cosh_id:
            stmt = stmt.where(QueryModel.crop_cosh_id == crop_cosh_id)
        if exclude != 'state' and state_cosh_id:
            stmt = stmt.where(User.state_cosh_id == state_cosh_id)
        if exclude != 'district' and district_cosh_id:
            stmt = stmt.where(User.district_cosh_id == district_cosh_id)
        if exclude != 'severity' and severity:
            stmt = stmt.where(QueryModel.severity == severity)
        if exclude != 'pundit' and pundit_id:
            stmt = stmt.where(
                select(QueryRemark.id).where(
                    QueryRemark.query_id == QueryModel.id,
                    QueryRemark.pundit_id == pundit_id,
                ).exists()
            )
        return stmt

    crop_ids = [
        r[0] for r in (await db.execute(
            scoped('crop').with_only_columns(QueryModel.crop_cosh_id).distinct()
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

    # Pundits — distinct pundits who ever touched a query in the
    # intersection of the OTHER chips. Cascades naturally.
    scoped_pundit = scoped('pundit').join(
        QueryRemark, QueryRemark.query_id == QueryModel.id,
    )
    pundit_profile_ids = [
        r[0] for r in (await db.execute(
            scoped_pundit.with_only_columns(QueryRemark.pundit_id).distinct()
        )).all() if r[0]
    ]
    pundit_rows = (await db.execute(
        select(FarmPunditProfile.id, User.name)
        .join(User, User.id == FarmPunditProfile.user_id)
        .where(FarmPunditProfile.id.in_(pundit_profile_ids))
    )).all() if pundit_profile_ids else []
    pundits = sorted(
        [{"id": r.id, "name": r.name or "(unnamed)"} for r in pundit_rows],
        key=lambda o: o["name"].lower(),
    )

    # Severity — static three (only the standard values are exposed as
    # chip options; legacy rows with other values just don't get a chip
    # bucket). No cascade needed for the values themselves; we still
    # optionally intersect to "severities that have at least one row"
    # so an empty-scope client doesn't see all three when only some
    # are used.
    scoped_sev = scoped('severity').with_only_columns(QueryModel.severity).distinct()
    present = {r[0] for r in (await db.execute(scoped_sev)).all() if r[0]}
    severities = [
        {"id": s, "name": s.title()}
        for s in QUERY_SEVERITIES_ORDERED
        if s in present or not present   # empty scope → show all three
    ]

    cosh_names = await _cosh_names(db, crop_ids + state_ids + district_ids)

    def _pack(ids: list[str]) -> list[dict]:
        return sorted(
            [{"id": i, "name": cosh_names.get(i, i)} for i in ids],
            key=lambda o: o["name"].lower(),
        )

    return {
        "crops":      _pack(crop_ids),
        "states":     _pack(state_ids),
        "districts":  _pack(district_ids),
        "severities": severities,
        "pundits":    pundits,
    }


QUERY_SEVERITIES_ORDERED = ["SEVERE", "MODERATE", "LOW"]
