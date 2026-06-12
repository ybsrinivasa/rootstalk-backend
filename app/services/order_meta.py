"""Resolve package-anchor metadata for farmer order lists.

Phase 1 of the farmer-side Orders restructure (2026-06-02): every
order card needs to show the crop name, the company name, and the
crop start date so the farmer can tell at a glance which crop /
which company an order belongs to — without having to mentally
re-attach context from the order ID.

This helper batch-loads the join chain once per request:

  Order.subscription_id → Subscription → Package → crop_cosh_id
                                       → Client  → display_name
  Subscription.crop_start_date / planting_year

so individual order rows don't fan out into N+1 queries. Cosh crop
name resolution piggybacks on the same single IN-query the
facilitator payment-requests view uses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Package
from app.modules.clients.models import Client
from app.modules.subscriptions.models import Subscription
from app.modules.sync.models import CoshCoreItem


@dataclass(frozen=True)
class OrderPackageMeta:
    subscription_id: Optional[str]
    package_id: Optional[str]
    package_name: Optional[str]
    crop_cosh_id: Optional[str]
    crop_name: Optional[str]
    client_id: Optional[str]
    company_name: Optional[str]
    company_short_name: Optional[str]
    crop_start_date: Optional[datetime]
    planting_year: Optional[int]

    def to_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "package_id": self.package_id,
            "package_name": self.package_name,
            "crop_cosh_id": self.crop_cosh_id,
            "crop_name": self.crop_name,
            "client_id": self.client_id,
            "company_name": self.company_name,
            "company_short_name": self.company_short_name,
            "crop_start_date": self.crop_start_date,
            "planting_year": self.planting_year,
        }


@dataclass(frozen=True)
class RecipientInfo:
    """Dealer / Facilitator surface fields the farmer's Order cards
    need: who's holding the order right now + how to reach them."""
    user_id: str
    name: Optional[str]
    phone: Optional[str]
    shop_name: Optional[str]  # dealer-only — facilitator stays None
    role: str                  # "DEALER" | "FACILITATOR"

    def to_dict(self) -> dict:
        return {
            "recipient_user_id": self.user_id,
            "recipient_name": self.name,
            "recipient_phone": self.phone,
            "recipient_shop_name": self.shop_name,
            "recipient_role": self.role,
        }


async def load_recipients(
    db: AsyncSession,
    dealer_user_ids: Iterable[str],
    facilitator_user_ids: Iterable[str],
) -> dict[str, RecipientInfo]:
    """Batch-load recipient info by user_id. Two ID lists in (dealer
    + facilitator) because the same user_id never plays both roles
    for a given order, and the join shape differs (shop_name only
    applies to dealers via DealerProfile)."""
    from app.modules.orders.models import DealerProfile
    from app.modules.platform.models import User

    dealer_ids = {d for d in dealer_user_ids if d}
    facil_ids = {f for f in facilitator_user_ids if f}
    all_ids = dealer_ids | facil_ids
    if not all_ids:
        return {}

    users = (await db.execute(
        select(User).where(User.id.in_(all_ids))
    )).scalars().all()
    user_by_id = {u.id: u for u in users}

    shop_by_id: dict[str, str] = {}
    if dealer_ids:
        profiles = (await db.execute(
            select(DealerProfile).where(
                DealerProfile.user_id.in_(dealer_ids),
            )
        )).scalars().all()
        shop_by_id = {p.user_id: p.shop_name for p in profiles if p.shop_name}

    out: dict[str, RecipientInfo] = {}
    for uid in dealer_ids:
        u = user_by_id.get(uid)
        if u is None:
            continue
        out[uid] = RecipientInfo(
            user_id=uid, name=u.name, phone=u.phone,
            shop_name=shop_by_id.get(uid), role="DEALER",
        )
    for uid in facil_ids:
        u = user_by_id.get(uid)
        if u is None:
            continue
        out[uid] = RecipientInfo(
            user_id=uid, name=u.name, phone=u.phone,
            shop_name=None, role="FACILITATOR",
        )
    return out


async def load_meta_for_subscription_ids(
    db: AsyncSession, subscription_ids: Iterable[str],
    *, lang: str = "en",
) -> dict[str, OrderPackageMeta]:
    """Return {subscription_id → OrderPackageMeta} for the given ids.

    Subscription ids absent from the map either don't exist or have
    no joined package; callers should treat them as no-meta orders
    rather than 500.
    """
    ids = {s for s in subscription_ids if s}
    if not ids:
        return {}

    rows = (await db.execute(
        select(Subscription, Package, Client)
        .join(Package, Package.id == Subscription.package_id)
        .join(Client, Client.id == Subscription.client_id)
        .where(Subscription.id.in_(ids))
    )).all()

    crop_ids = {pkg.crop_cosh_id for _, pkg, _ in rows if pkg.crop_cosh_id}
    crop_name_by_id: dict[str, str] = {}
    if crop_ids:
        from app.services.i18n_cosh import pick_translation
        cores = (await db.execute(
            select(CoshCoreItem).where(CoshCoreItem.cosh_id.in_(crop_ids))
        )).scalars().all()
        for c in cores:
            tr = c.translations or {}
            crop_name_by_id[c.cosh_id] = pick_translation(tr, lang, c.cosh_id)

    out: dict[str, OrderPackageMeta] = {}
    for sub, pkg, client in rows:
        out[sub.id] = OrderPackageMeta(
            subscription_id=sub.id,
            package_id=pkg.id,
            package_name=pkg.name,
            crop_cosh_id=pkg.crop_cosh_id,
            crop_name=crop_name_by_id.get(pkg.crop_cosh_id) if pkg.crop_cosh_id else None,
            client_id=client.id,
            # display_name (UI-facing) wins; full_name is the legal name
            # which can be long. short_name is the 12-char code for the
            # company avatar.
            company_name=client.display_name or client.full_name,
            company_short_name=client.short_name,
            crop_start_date=sub.crop_start_date,
            planting_year=sub.planting_year,
        )
    return out
