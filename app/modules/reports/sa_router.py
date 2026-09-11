"""Super-Admin cross-platform Reports.

One dashboard endpoint returning 13 headline metrics with an optional
prior-window comparison. Filters: date window, location (state +
district on the *relevant* user's own address), client multi-select,
and an escape-hatch toggle for sandbox clients.

Sandbox (is_training / is_coaching) clients are excluded from every
count by default — SA can flip the toggle to include them when
sanity-checking QA activity.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import distinct, func, select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user

from app.modules.platform.models import User, UserRole, RoleType
from app.modules.clients.models import Client, ClientPromoter
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, PromoterAssignment,
)
from app.modules.orders.models import Order
from app.modules.farmpundit.models import (
    Query as FarmerQuery, QueryResponse, ClientFarmPundit,
    PunditRole, FarmPunditProfile,
)
from app.modules.diagnosis.models import DiagnosisSession


router = APIRouter(tags=["SA Reports"])


def _require_sa(current_user: User) -> None:
    if current_user.email != settings.sa_email:
        raise HTTPException(
            status_code=403, detail="Super Admin access required",
        )


def _parse_ids_csv(csv: Optional[str]) -> Optional[list[str]]:
    if not csv:
        return None
    ids = [i.strip() for i in csv.split(",") if i.strip()]
    return ids or None


async def _resolved_client_ids(
    db: AsyncSession,
    requested: Optional[list[str]],
    include_sandboxes: bool,
) -> list[str]:
    """Return the client-id set the counts should be scoped to.

    When `requested` is empty → all clients (subject to sandbox filter).
    When it's supplied → intersect with the sandbox filter so a caller
    can't accidentally pull sandbox data via an explicit id list.
    """
    q = select(Client.id)
    if not include_sandboxes:
        q = q.where(
            Client.is_training.is_(False),
            Client.is_coaching.is_(False),
        )
    if requested:
        q = q.where(Client.id.in_(requested))
    return [row[0] for row in (await db.execute(q)).all()]


async def _compute_window(
    db: AsyncSession,
    *,
    period_from: datetime,
    period_to: datetime,
    client_ids: list[str],
    client_filter_active: bool,
    state_cosh_id: Optional[str],
    district_cosh_id: Optional[str],
) -> dict:
    """Run every count for one window and return the payload dict.

    `client_ids` is the pre-resolved allowed set (empty list means
    "no clients match filter" → every client-scoped count returns 0).
    `client_filter_active` = True when the URL explicitly narrowed
    clients (partial subset OR explicit zero via `__none__`). When True,
    `farmers_newly_registered` also honours the client filter — a
    farmer counts only if they ended up subscribed to at least one of
    the filtered clients. When False (default all-clients view), the
    metric stays client-agnostic so newly-registered farmers who
    haven't subscribed to anything yet still appear.
    """
    nr_client_filter = client_ids if client_filter_active else None

    if not client_ids:
        # No clients survive the filter → every client-scoped count is 0.
        # `farmers_newly_registered` is client-agnostic only when the
        # user hasn't narrowed by client at all; here the empty list
        # comes from an explicit narrow, so it too returns 0.
        empty = {k: 0 for k in _METRIC_KEYS}
        empty["farmers_newly_registered"] = await _count_farmers_newly_registered(
            db,
            period_from=period_from, period_to=period_to,
            state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
            client_ids=nr_client_filter,
        )
        return empty

    cids = client_ids

    # Farmer-address filter alias for reuse.
    _in_win = lambda col: and_(col >= period_from, col < period_to)  # noqa: E731

    # ── Farmer / subscription joins ──────────────────────────────────
    _farmer_loc_join = (
        Subscription.__table__.join(User, User.id == Subscription.farmer_user_id)
    )

    def _farmer_loc_filter(query):
        if state_cosh_id:
            query = query.where(User.state_cosh_id == state_cosh_id)
        if district_cosh_id:
            query = query.where(User.district_cosh_id == district_cosh_id)
        return query

    def _team_loc_filter(query, user_id_col):
        if not (state_cosh_id or district_cosh_id):
            return query
        # Join User for the team member's own address (dealer /
        # facilitator / promoter / pundit).
        query = query.join(User, User.id == user_id_col)
        if state_cosh_id:
            query = query.where(User.state_cosh_id == state_cosh_id)
        if district_cosh_id:
            query = query.where(User.district_cosh_id == district_cosh_id)
        return query

    # ── 1. Farmers — active (any ACTIVE subscription as of period_to) ──
    q = (
        select(func.count(distinct(Subscription.farmer_user_id)))
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.client_id.in_(cids),
            Subscription.created_at < period_to,
            or_(Subscription.lapsed_at.is_(None), Subscription.lapsed_at >= period_to),
        )
    )
    q = _farmer_loc_filter(q)
    farmers_active = int((await db.execute(q)).scalar_one() or 0)

    # ── 2. Farmers — newly registered (User.self_registered_at in window) ──
    farmers_newly_registered = await _count_farmers_newly_registered(
        db,
        period_from=period_from, period_to=period_to,
        state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
        client_ids=nr_client_filter,
    )

    # ── 3. Active subscriptions (point-in-time at period_to) ────────
    q = (
        select(func.count(Subscription.id))
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.client_id.in_(cids),
            Subscription.created_at < period_to,
            or_(Subscription.lapsed_at.is_(None), Subscription.lapsed_at >= period_to),
        )
    )
    q = _farmer_loc_filter(q)
    active_subscriptions = int((await db.execute(q)).scalar_one() or 0)

    # ── 4. Total subscriptions created in window ────────────────────
    q = (
        select(func.count(Subscription.id))
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            _in_win(Subscription.created_at),
            Subscription.client_id.in_(cids),
        )
    )
    q = _farmer_loc_filter(q)
    total_subscriptions = int((await db.execute(q)).scalar_one() or 0)

    # ── 5 / 7. Dealers + Facilitators onboarded (ClientPromoter) ───
    async def _cp_count(promoter_type: str) -> int:
        q = (
            select(func.count(ClientPromoter.id))
            .where(
                ClientPromoter.promoter_type == promoter_type,
                ClientPromoter.status == "ACTIVE",
                ClientPromoter.client_id.in_(cids),
                _in_win(ClientPromoter.registered_at),
            )
        )
        q = _team_loc_filter(q, ClientPromoter.user_id)
        return int((await db.execute(q)).scalar_one() or 0)

    dealers_onboarded = await _cp_count("DEALER")
    facilitators_onboarded = await _cp_count("FACILITATOR")

    # ── 6. Purchase orders created in window ────────────────────────
    q = (
        select(func.count(Order.id))
        .join(User, User.id == Order.farmer_user_id)
        .where(
            _in_win(Order.created_at),
            Order.client_id.in_(cids),
        )
    )
    q = _farmer_loc_filter(q)
    purchase_orders_generated = int((await db.execute(q)).scalar_one() or 0)

    # ── 8. Promoters designated (PromoterAssignment.assigned_at in win) ──
    q = (
        select(func.count(distinct(PromoterAssignment.promoter_user_id)))
        .join(Subscription, Subscription.id == PromoterAssignment.subscription_id)
        .where(
            _in_win(PromoterAssignment.assigned_at),
            Subscription.client_id.in_(cids),
        )
    )
    q = _team_loc_filter(q, PromoterAssignment.promoter_user_id)
    promoters_designated = int((await db.execute(q)).scalar_one() or 0)

    # ── 9. Promoter-Pundits / Primary / Panel Experts (ClientFarmPundit) ──
    async def _pundit_count(role: PunditRole) -> int:
        q = (
            select(func.count(ClientFarmPundit.id))
            .where(
                ClientFarmPundit.role == role,
                ClientFarmPundit.status == "ACTIVE",
                ClientFarmPundit.client_id.in_(cids),
                _in_win(ClientFarmPundit.onboarded_at),
            )
        )
        if state_cosh_id or district_cosh_id:
            q = (
                q.join(FarmPunditProfile, FarmPunditProfile.id == ClientFarmPundit.pundit_id)
                 .join(User, User.id == FarmPunditProfile.user_id)
            )
            if state_cosh_id:
                q = q.where(User.state_cosh_id == state_cosh_id)
            if district_cosh_id:
                q = q.where(User.district_cosh_id == district_cosh_id)
        return int((await db.execute(q)).scalar_one() or 0)

    promoter_pundits = await _pundit_count(PunditRole.PROMOTER_PUNDIT)
    primary_experts = await _pundit_count(PunditRole.PRIMARY)
    panel_experts = await _pundit_count(PunditRole.PANEL)

    # ── 10. Queries raised in window ────────────────────────────────
    q = (
        select(func.count(FarmerQuery.id))
        .join(User, User.id == FarmerQuery.farmer_user_id)
        .where(
            _in_win(FarmerQuery.created_at),
            FarmerQuery.client_id.in_(cids),
        )
    )
    q = _farmer_loc_filter(q)
    queries_raised = int((await db.execute(q)).scalar_one() or 0)

    # ── 11. Queries responded to (QueryResponse.created_at in win) ──
    q = (
        select(func.count(distinct(QueryResponse.query_id)))
        .join(FarmerQuery, FarmerQuery.id == QueryResponse.query_id)
        .join(User, User.id == FarmerQuery.farmer_user_id)
        .where(
            _in_win(QueryResponse.created_at),
            FarmerQuery.client_id.in_(cids),
        )
    )
    q = _farmer_loc_filter(q)
    queries_responded = int((await db.execute(q)).scalar_one() or 0)

    # ── 12. Queries pending response (raised ≤ period_to, no response) ──
    # Point-in-time "still open" — surfaces the SLA gap the SA cares
    # about. Correlated NOT EXISTS keeps this cheap on current volumes.
    q = (
        select(func.count(FarmerQuery.id))
        .join(User, User.id == FarmerQuery.farmer_user_id)
        .where(
            FarmerQuery.created_at < period_to,
            FarmerQuery.client_id.in_(cids),
            ~select(QueryResponse.id).where(
                QueryResponse.query_id == FarmerQuery.id,
            ).exists(),
        )
    )
    q = _farmer_loc_filter(q)
    queries_pending = int((await db.execute(q)).scalar_one() or 0)

    # ── 13. Pests diagnosed (DiagnosisSession reached DIAGNOSED in win) ──
    q = (
        select(func.count(DiagnosisSession.id))
        .join(Subscription, Subscription.id == DiagnosisSession.subscription_id)
        .join(User, User.id == DiagnosisSession.farmer_user_id)
        .where(
            DiagnosisSession.status == "DIAGNOSED",
            _in_win(DiagnosisSession.updated_at),
            Subscription.client_id.in_(cids),
        )
    )
    q = _farmer_loc_filter(q)
    pests_diagnosed = int((await db.execute(q)).scalar_one() or 0)

    return {
        "farmers_active": farmers_active,
        "farmers_newly_registered": farmers_newly_registered,
        "active_subscriptions": active_subscriptions,
        "total_subscriptions": total_subscriptions,
        "dealers_onboarded": dealers_onboarded,
        "facilitators_onboarded": facilitators_onboarded,
        "purchase_orders_generated": purchase_orders_generated,
        "promoters_designated": promoters_designated,
        "promoter_pundits": promoter_pundits,
        "primary_experts": primary_experts,
        "panel_experts": panel_experts,
        "queries_raised": queries_raised,
        "queries_responded": queries_responded,
        "queries_pending": queries_pending,
        "pests_diagnosed": pests_diagnosed,
    }


_METRIC_KEYS = (
    "farmers_active", "farmers_newly_registered", "active_subscriptions",
    "total_subscriptions", "dealers_onboarded", "facilitators_onboarded",
    "purchase_orders_generated", "promoters_designated",
    "promoter_pundits", "primary_experts", "panel_experts",
    "queries_raised", "queries_responded", "queries_pending",
    "pests_diagnosed",
)


async def _count_farmers_newly_registered(
    db: AsyncSession, *,
    period_from: datetime, period_to: datetime,
    state_cosh_id: Optional[str], district_cosh_id: Optional[str],
    client_ids: Optional[list[str]] = None,
) -> int:
    """Count farmers who self-registered in the window.

    `client_ids` semantics:
      None → no client filter (platform-wide count; the default view).
      list → count only farmers who ended up subscribed to at least one
             of the given clients (any subscription status, any time).
             An empty list correctly returns 0.
    """
    q = (
        select(func.count(distinct(User.id)))
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            UserRole.role_type == RoleType.FARMER,
            User.self_registered_at.is_not(None),
            User.self_registered_at >= period_from,
            User.self_registered_at < period_to,
        )
    )
    if state_cosh_id:
        q = q.where(User.state_cosh_id == state_cosh_id)
    if district_cosh_id:
        q = q.where(User.district_cosh_id == district_cosh_id)
    if client_ids is not None:
        q = q.where(
            select(Subscription.id).where(
                Subscription.farmer_user_id == User.id,
                Subscription.client_id.in_(client_ids),
            ).exists()
        )
    return int((await db.execute(q)).scalar_one() or 0)


@router.get("/admin/sa/reports/clients")
async def sa_reports_clients(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Real (non-sandbox) clients for the SA reports multi-select."""
    _require_sa(current_user)
    rows = (await db.execute(
        select(Client.id, Client.full_name, Client.short_name)
        .where(Client.is_training.is_(False), Client.is_coaching.is_(False))
        .order_by(Client.full_name.asc())
    )).all()
    return [
        {"id": r[0], "name": r[1], "short_name": r[2]}
        for r in rows
    ]


@router.get("/admin/sa/reports/summary")
async def sa_reports_summary(
    period_from: Optional[datetime] = Query(None),
    period_to: Optional[datetime] = Query(None),
    state_cosh_id: Optional[str] = Query(None),
    district_cosh_id: Optional[str] = Query(None),
    client_ids: Optional[str] = Query(
        None, description="Comma-separated client ids; empty = all real clients",
    ),
    include_sandboxes: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Headline SA metrics with a prior-period comparison.

    Both windows are inclusive of `period_from`, exclusive of `period_to`
    so the prior window can be computed as
    `[period_from - delta, period_from)` without off-by-one issues.
    """
    _require_sa(current_user)

    # Default window: last 30 days ending now (UTC).
    now_utc = datetime.now(timezone.utc)
    if period_to is None:
        period_to = now_utc
    if period_from is None:
        period_from = period_to - timedelta(days=30)
    if period_from >= period_to:
        raise HTTPException(
            status_code=422,
            detail="period_from must be strictly before period_to",
        )

    # "Explicit client filter" = the URL had ?client_ids= at all
    # (including the __none__ sentinel). Absent → default view (all
    # real clients), where farmers_newly_registered stays platform-wide
    # so we don't hide farmers who registered but haven't subscribed yet.
    client_filter_active = client_ids is not None
    requested_ids = _parse_ids_csv(client_ids)
    resolved_ids = await _resolved_client_ids(
        db, requested_ids, include_sandboxes,
    )

    current = await _compute_window(
        db,
        period_from=period_from, period_to=period_to,
        client_ids=resolved_ids,
        client_filter_active=client_filter_active,
        state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
    )

    # Prior window of equal length, immediately preceding.
    delta = period_to - period_from
    prior_to = period_from
    prior_from = period_from - delta
    prior = await _compute_window(
        db,
        period_from=prior_from, period_to=prior_to,
        client_ids=resolved_ids,
        client_filter_active=client_filter_active,
        state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
    )

    return {
        "generated_at": now_utc.isoformat(),
        "filters_applied": {
            "period_from": period_from.isoformat(),
            "period_to": period_to.isoformat(),
            "state_cosh_id": state_cosh_id,
            "district_cosh_id": district_cosh_id,
            "client_ids": resolved_ids,
            "include_sandboxes": include_sandboxes,
        },
        "prior_window": {
            "period_from": prior_from.isoformat(),
            "period_to": prior_to.isoformat(),
        },
        "current": current,
        "prior": prior,
    }
