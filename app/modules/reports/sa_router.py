"""Super-Admin cross-platform Reports.

Response shape:

    {
      "generated_at": "...",
      "filters_applied": {...},
      "prior_window": {...},
      "platform_totals": { ...5 tiles snapshot-as-of-today... },
      "current":  { ...windowed + client-scoped tiles... },
      "prior":    { ...same shape as current... },
    }

**Platform Totals** live above the client filter on the UI. They ignore
the date + client filter and are always a snapshot as-of-now; only
Location applies. Sandbox toggle applies (excludes is_training + is_coaching
clients unless flipped on).

**Current / Prior** honour the full filter set (date + location + client).
The prior window is the immediately-preceding equal-length window used
for the per-tile delta.
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


async def _all_real_client_ids(
    db: AsyncSession, include_sandboxes: bool,
) -> list[str]:
    """The 'all real clients' set used for Platform Totals scope."""
    q = select(Client.id)
    if not include_sandboxes:
        q = q.where(
            Client.is_training.is_(False),
            Client.is_coaching.is_(False),
        )
    return [row[0] for row in (await db.execute(q)).all()]


async def _resolved_client_ids(
    db: AsyncSession,
    requested: Optional[list[str]],
    include_sandboxes: bool,
) -> list[str]:
    """Return the client-id set that windowed counts should be scoped to.

    When `requested` is None → all clients (subject to sandbox filter).
    When supplied → intersect with the sandbox filter so a caller can't
    smuggle sandbox data through an explicit id list.
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


# ── Platform Totals (snapshot as-of-now, Location-filtered) ─────────

async def _compute_platform_totals(
    db: AsyncSession, *,
    real_cids: list[str],
    state_cosh_id: Optional[str],
    district_cosh_id: Optional[str],
) -> dict:
    """5 tiles rendered above the client filter. Snapshot as-of-now.
    Location applies (against the relevant entity's own User row).
    Client filter deliberately does NOT apply — these are the
    "how big are we?" numbers scoped only to real (non-sandbox) clients.
    """
    def _user_loc(query):
        if state_cosh_id:
            query = query.where(User.state_cosh_id == state_cosh_id)
        if district_cosh_id:
            query = query.where(User.district_cosh_id == district_cosh_id)
        return query

    # ── Total Registered Farmers (has FARMER role, self_registered_at set) ──
    q = (
        select(func.count(distinct(User.id)))
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            UserRole.role_type == RoleType.FARMER,
            User.self_registered_at.is_not(None),
        )
    )
    total_registered_farmers = int((await db.execute(_user_loc(q))).scalar_one() or 0)

    if not real_cids:
        # Nothing else to count (every remaining tile joins on real clients).
        return {
            "total_registered_farmers": total_registered_farmers,
            "total_subscribed_farmers": 0,
            "total_active_subscriptions": 0,
            "total_active_dealers": 0,
            "total_active_facilitators": 0,
        }

    # ── Total Subscribed Farmers (any subscription to any real client, ever) ──
    q = (
        select(func.count(distinct(Subscription.farmer_user_id)))
        .join(User, User.id == Subscription.farmer_user_id)
        .where(Subscription.client_id.in_(real_cids))
    )
    total_subscribed_farmers = int((await db.execute(_user_loc(q))).scalar_one() or 0)

    # ── Total Active Subscriptions (currently ACTIVE, real clients) ──
    q = (
        select(func.count(Subscription.id))
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.client_id.in_(real_cids),
        )
    )
    total_active_subscriptions = int((await db.execute(_user_loc(q))).scalar_one() or 0)

    # ── Total Active Dealers (distinct users on ≥1 real client, ACTIVE link) ──
    async def _active_team_count(promoter_type: str) -> int:
        q = (
            select(func.count(distinct(ClientPromoter.user_id)))
            .join(User, User.id == ClientPromoter.user_id)
            .where(
                ClientPromoter.promoter_type == promoter_type,
                ClientPromoter.status == "ACTIVE",
                ClientPromoter.client_id.in_(real_cids),
            )
        )
        return int((await db.execute(_user_loc(q))).scalar_one() or 0)

    total_active_dealers = await _active_team_count("DEALER")
    total_active_facilitators = await _active_team_count("FACILITATOR")

    return {
        "total_registered_farmers": total_registered_farmers,
        "total_subscribed_farmers": total_subscribed_farmers,
        "total_active_subscriptions": total_active_subscriptions,
        "total_active_dealers": total_active_dealers,
        "total_active_facilitators": total_active_facilitators,
    }


# ── Windowed / Client-scoped tiles ──────────────────────────────────

_METRIC_KEYS = (
    "farmers_active", "farmers_newly_registered",
    "active_subscriptions_in_window", "subscriptions_created",
    "purchase_orders_generated",
    "dealers_onboarded", "facilitators_onboarded",
    "facilitator_promoters_designated", "dealer_promoters_designated",
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
    """Farmers who self-registered in the window.

    `client_ids`:
      None → no client filter (default all-clients view).
      list → count only farmers who ended up subscribed to at least one
             of the given clients (any status, any time). Empty → 0.
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


async def _compute_window(
    db: AsyncSession, *,
    period_from: datetime, period_to: datetime,
    client_ids: list[str],
    client_filter_active: bool,
    state_cosh_id: Optional[str], district_cosh_id: Optional[str],
) -> dict:
    """Run every windowed / client-scoped count and return the payload dict.

    `client_ids` is the pre-resolved allowed set (empty list means no
    clients survive the filter → every client-scoped count returns 0).
    `client_filter_active` = the URL had an explicit ?client_ids= (any
    value, including the __none__ sentinel). Controls whether
    farmers_newly_registered honours the client filter.
    """
    nr_client_filter = client_ids if client_filter_active else None

    if not client_ids:
        empty = {k: 0 for k in _METRIC_KEYS}
        empty["farmers_newly_registered"] = await _count_farmers_newly_registered(
            db,
            period_from=period_from, period_to=period_to,
            state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
            client_ids=nr_client_filter,
        )
        return empty

    cids = client_ids
    _in_win = lambda col: and_(col >= period_from, col < period_to)  # noqa: E731

    def _farmer_loc_filter(query):
        if state_cosh_id:
            query = query.where(User.state_cosh_id == state_cosh_id)
        if district_cosh_id:
            query = query.where(User.district_cosh_id == district_cosh_id)
        return query

    def _team_loc_filter(query, user_id_col):
        if not (state_cosh_id or district_cosh_id):
            return query
        query = query.join(User, User.id == user_id_col)
        if state_cosh_id:
            query = query.where(User.state_cosh_id == state_cosh_id)
        if district_cosh_id:
            query = query.where(User.district_cosh_id == district_cosh_id)
        return query

    # ── 1. Farmers — active as-of period_to ──
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

    # ── 2. Farmers — newly registered in window ──
    farmers_newly_registered = await _count_farmers_newly_registered(
        db,
        period_from=period_from, period_to=period_to,
        state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
        client_ids=nr_client_filter,
    )

    # ── 3. Active subscriptions in window (active for at least some part) ──
    # A subscription counts if it became ACTIVE (subscription_date set)
    # before period_to AND wasn't already lapsed before period_from.
    q = (
        select(func.count(Subscription.id))
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.client_id.in_(cids),
            Subscription.subscription_date.is_not(None),
            Subscription.subscription_date < period_to,
            or_(Subscription.lapsed_at.is_(None), Subscription.lapsed_at > period_from),
        )
    )
    q = _farmer_loc_filter(q)
    active_subscriptions_in_window = int((await db.execute(q)).scalar_one() or 0)

    # ── 4. Subscriptions created in window ──
    q = (
        select(func.count(Subscription.id))
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            _in_win(Subscription.created_at),
            Subscription.client_id.in_(cids),
        )
    )
    q = _farmer_loc_filter(q)
    subscriptions_created = int((await db.execute(q)).scalar_one() or 0)

    # ── 5 / 6. Dealers + Facilitators onboarded (in window) ──
    async def _cp_onboarded(promoter_type: str) -> int:
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

    dealers_onboarded = await _cp_onboarded("DEALER")
    facilitators_onboarded = await _cp_onboarded("FACILITATOR")

    # ── 7. Purchase orders generated in window ──
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

    # ── 8 / 9. Promoters designated in window, split by type ──
    async def _promoters_designated(promoter_type: str) -> int:
        q = (
            select(func.count(distinct(PromoterAssignment.promoter_user_id)))
            .join(Subscription, Subscription.id == PromoterAssignment.subscription_id)
            .where(
                _in_win(PromoterAssignment.assigned_at),
                PromoterAssignment.promoter_type == promoter_type,
                Subscription.client_id.in_(cids),
            )
        )
        q = _team_loc_filter(q, PromoterAssignment.promoter_user_id)
        return int((await db.execute(q)).scalar_one() or 0)

    facilitator_promoters_designated = await _promoters_designated("FACILITATOR")
    dealer_promoters_designated = await _promoters_designated("DEALER")

    # ── 10 / 11 / 12. Pundit roles onboarded in window ──
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

    # ── 13. Queries raised in window ──
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

    # ── 14. Queries responded (first response in window) ──
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

    # ── 15. Queries pending as-of period_to (raised, no response) ──
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

    # ── 16. Pests diagnosed in window ──
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
        "active_subscriptions_in_window": active_subscriptions_in_window,
        "subscriptions_created": subscriptions_created,
        "purchase_orders_generated": purchase_orders_generated,
        "dealers_onboarded": dealers_onboarded,
        "facilitators_onboarded": facilitators_onboarded,
        "facilitator_promoters_designated": facilitator_promoters_designated,
        "dealer_promoters_designated": dealer_promoters_designated,
        "promoter_pundits": promoter_pundits,
        "primary_experts": primary_experts,
        "panel_experts": panel_experts,
        "queries_raised": queries_raised,
        "queries_responded": queries_responded,
        "queries_pending": queries_pending,
        "pests_diagnosed": pests_diagnosed,
    }


# ── Endpoints ────────────────────────────────────────────────────────

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
        None, description="Comma-separated client ids; empty = all real clients; __none__ = explicit zero",
    ),
    include_sandboxes: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA headline metrics: platform_totals (snapshot, above client
    filter on the UI) + current / prior windowed sections (below)."""
    _require_sa(current_user)

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

    client_filter_active = client_ids is not None
    requested_ids = _parse_ids_csv(client_ids)
    resolved_ids = await _resolved_client_ids(
        db, requested_ids, include_sandboxes,
    )

    # Platform totals scope: all real clients (or +sandbox if toggled).
    # NOT narrowed by the user's client selection — see module docstring.
    real_cids = await _all_real_client_ids(db, include_sandboxes)
    platform_totals = await _compute_platform_totals(
        db,
        real_cids=real_cids,
        state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
    )

    current = await _compute_window(
        db,
        period_from=period_from, period_to=period_to,
        client_ids=resolved_ids,
        client_filter_active=client_filter_active,
        state_cosh_id=state_cosh_id, district_cosh_id=district_cosh_id,
    )

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
        "platform_totals": platform_totals,
        "current": current,
        "prior": prior,
    }
