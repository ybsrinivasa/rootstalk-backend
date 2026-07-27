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
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User

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
        if metric_up in {"NEW", "TOTAL"}:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "metric_not_implemented",
                    "message": f"Subscriptions '{metric_up}' is on the "
                               "Phase 1 punch list; not yet wired.",
                },
            )
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
