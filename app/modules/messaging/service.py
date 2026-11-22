"""Messaging — shared logic.

Audience computation is here (not in the router) because Phase 2B's
send flow and Phase 3's test-send both call the same function with
different outcomes (preview count vs frozen snapshot write).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import ClientPromoter
from app.modules.platform.models import User
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus,
)
from app.modules.messaging.models import (
    RecipientRole,
    SUBSCRIPTION_STATUS_FILTER_VALUES,
)


# Status buckets per the spec addendum (§4.3). Values in each list
# are the actual SubscriptionStatus enum values that belong to that
# bucket for a (farmer, client) pair.
_ACTIVE_STATUSES = [SubscriptionStatus.ACTIVE]
_WAITLISTED_STATUSES = [SubscriptionStatus.WAITLISTED]
_TERMINAL_STATUSES = [
    SubscriptionStatus.CANCELLED,
    SubscriptionStatus.UNSUBSCRIBED,
    SubscriptionStatus.LAPSED,
    SubscriptionStatus.SUSPENDED,
]


def _apply_location_filter(stmt, filters: dict):
    if filters.get("state_cosh_id"):
        stmt = stmt.where(User.state_cosh_id == filters["state_cosh_id"])
    if filters.get("district_cosh_id"):
        stmt = stmt.where(User.district_cosh_id == filters["district_cosh_id"])
    return stmt


async def compute_farmer_audience(
    db: AsyncSession, client_id: str, filters: dict,
) -> list[str]:
    """Return the list of farmer user_ids that match the current
    filter set for this client.

    Filter semantics per spec §4.3:
      - subscription_status: ALL | CURRENTLY_ACTIVE | WAITLISTED | PAST
        | NEVER_SUBSCRIBED. Default ALL.
      - state_cosh_id / district_cosh_id: narrow by farmer location.
      - crop_cosh_id: narrow to farmers with ≥1 subscription in that
        crop under this client (only meaningful for status buckets
        that include subscription rows).
      - promoter_user_id: narrow to farmers whose subscription for
        this client was assigned by the given promoter.

    The "Never Subscribed" bucket returns EMPTY in v1 (user-confirmed
    semantic 2026-11-22: a farmer with no subscription to any crop
    of this client is not meaningfully linked → correctly excluded).
    Kept as a valid filter value so the UI can offer it; just returns
    0 rows.
    """
    sub_status = (filters.get("subscription_status") or "ALL").upper()
    if sub_status not in SUBSCRIPTION_STATUS_FILTER_VALUES:
        return []

    # NEVER_SUBSCRIBED — per user design, empty audience (no shadow
    # table that identifies a farmer-client link without a subscription).
    if sub_status == "NEVER_SUBSCRIBED":
        return []

    # Base: farmers with any (non-deleted) subscription for this client.
    base = (
        select(User.id)
        .distinct()
        .join(Subscription, Subscription.farmer_user_id == User.id)
        .where(
            Subscription.client_id == client_id,
            Subscription.deleted_at.is_(None),
        )
    )

    # Status filter — ALL keeps all buckets; the others restrict to
    # pairs where the user has (at least one) sub in the chosen bucket
    # AND does NOT have a higher-priority bucket, so a farmer with both
    # ACTIVE and CANCELLED subs only counts as ACTIVE.
    if sub_status == "CURRENTLY_ACTIVE":
        base = base.where(Subscription.status.in_(_ACTIVE_STATUSES))
    elif sub_status == "WAITLISTED":
        # Farmer has ≥1 waitlisted sub and NO active sub for this client.
        has_active = select(Subscription.id).where(
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == User.id,
            Subscription.status.in_(_ACTIVE_STATUSES),
            Subscription.deleted_at.is_(None),
        ).exists()
        base = base.where(
            Subscription.status.in_(_WAITLISTED_STATUSES),
            ~has_active,
        )
    elif sub_status == "PAST":
        # Farmer has ≥1 terminal sub and NO active/waitlisted sub.
        has_active_or_waitlisted = select(Subscription.id).where(
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == User.id,
            Subscription.status.in_(_ACTIVE_STATUSES + _WAITLISTED_STATUSES),
            Subscription.deleted_at.is_(None),
        ).exists()
        base = base.where(
            Subscription.status.in_(_TERMINAL_STATUSES),
            ~has_active_or_waitlisted,
        )
    # ALL falls through — any sub counts.

    # Crop filter.
    if filters.get("crop_cosh_id"):
        from app.modules.advisory.models import Package
        base = base.join(Package, Package.id == Subscription.package_id).where(
            Package.crop_cosh_id == filters["crop_cosh_id"],
        )

    # Promoter filter — current effective promoter on the subscription
    # (not the signup_promoter, since a CA targeting "Promoter X's
    # farmers" means "who is following up with them now").
    if filters.get("promoter_user_id"):
        base = base.where(
            Subscription.promoter_user_id == filters["promoter_user_id"],
        )

    base = _apply_location_filter(base, filters)
    rows = (await db.execute(base)).scalars().all()
    return list(rows)


async def compute_promoter_audience(
    db: AsyncSession, client_id: str, filters: dict, promoter_type: str,
) -> list[str]:
    """Return the list of user_ids for Dealer/Facilitator audiences.
    `promoter_type` is "DEALER" or "FACILITATOR".

    Only location filters apply here — Dealer / Facilitator rows live
    in `client_promoters`, not `subscriptions`.
    """
    stmt = (
        select(User.id)
        .distinct()
        .join(ClientPromoter, ClientPromoter.user_id == User.id)
        .where(
            ClientPromoter.client_id == client_id,
            ClientPromoter.status == "ACTIVE",
            ClientPromoter.promoter_type == promoter_type,
        )
    )
    stmt = _apply_location_filter(stmt, filters)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def compute_audience_user_ids(
    db: AsyncSession, client_id: str, recipient_role: str, filters: dict,
) -> list[str]:
    """Dispatch to the role-specific audience resolver. Returns the
    list of user_ids that match the filter set.
    """
    if recipient_role == RecipientRole.FARMER.value:
        return await compute_farmer_audience(db, client_id, filters)
    if recipient_role == RecipientRole.DEALER.value:
        return await compute_promoter_audience(db, client_id, filters, "DEALER")
    if recipient_role == RecipientRole.FACILITATOR.value:
        return await compute_promoter_audience(db, client_id, filters, "FACILITATOR")
    return []


async def audience_count_with_exclusions(
    db: AsyncSession, client_id: str, recipient_role: str, filters: dict,
) -> dict:
    """Return the audience count plus a small breakdown of how many
    recipients would be excluded due to missing data when a given
    filter needs it. Used by the Preview step per spec §4.4.

    Exclusions reported:
      - missing_district: count of matching users with
        district_cosh_id = NULL when a district filter is applied.
        (If no district filter is applied, this is reported as 0 — the
        user isn't trying to filter by district so they're not excluded.)

    `matched` is the count that WOULD actually be delivered to; the
    excluded counts are informational and additive.
    """
    user_ids = await compute_audience_user_ids(
        db, client_id, recipient_role, filters,
    )
    matched = len(user_ids)

    missing_district = 0
    if filters.get("district_cosh_id") is None and user_ids:
        # Only report when no district filter is applied — in that case
        # the UI can still show "N farmers; M have no district on file"
        # so the CA knows the data-quality shape. Narrowing by district
        # later would drop those M silently.
        stmt = select(User.id).where(
            User.id.in_(user_ids),
            User.district_cosh_id.is_(None),
        )
        missing_district = len((await db.execute(stmt)).scalars().all())

    return {
        "matched": matched,
        "excluded_missing_district": missing_district,
    }
