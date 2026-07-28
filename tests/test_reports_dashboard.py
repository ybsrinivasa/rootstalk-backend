"""Vertical-slice tests for the Client Reports dashboard (Phase 1).

Covers the ``subs_active`` query end-to-end — the first metric wired
through queries.py + dashboard_router.py + access.py.

The core assertion is the FILTER CONTRACT from
`project_rootstalk_client_reports_phase_1.md`:

- Client scoping (nothing cross-client leaks).
- Training exclusion (Client.is_training=True rows must not count).
- Soft-delete cascade (Subscription.deleted_at IS NULL, auto via the
  session listener).
- Status filter (only SubscriptionStatus.ACTIVE counts).
- DISTINCT farmers (a farmer with 2 active subs on the same client
  counts once in the ``farmers`` number, twice in ``subscriptions``).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)

from app.modules.clients.models import Client
from app.modules.reports.queries import (
    ReportFilters, orders_brand_mix, orders_count, orders_items,
    orders_routing, subs_active, subs_new, subs_total,
)
from app.modules.orders.models import (
    Order, OrderItem, OrderItemStatus, OrderStatus,
)
from app.modules.subscriptions.models import Subscription, SubscriptionStatus


pytestmark = [pytest.mark.asyncio, requires_docker]


async def test_subs_active_filter_contract(db):
    """Exercises every filter in the contract in one setup so a
    regression in any single filter shows up loud.
    """
    # Parent (real) client under audit.
    parent = await make_client(db, full_name="Parent Client")

    # Second real client — sanity that client_id scoping keeps its
    # subscriptions out of parent's report.
    other = await make_client(db, full_name="Other Client")

    # Training shadow child of parent. Its subs must NOT count.
    training_child = Client(
        full_name="Parent Training",
        short_name="pt-" + parent.short_name[:8],
        ca_name="T", ca_phone="+919", ca_email="t@t.local",
        payment_model=parent.payment_model,
        is_training=True,
        parent_client_id=parent.id,
    )
    db.add(training_child)
    await db.flush()

    # Farmers. farmer_a has TWO active subs on parent (dedup test).
    farmer_a = await make_user(db, name="Farmer A")
    farmer_b = await make_user(db, name="Farmer B")
    farmer_c = await make_user(db, name="Farmer C")  # training-only
    farmer_d = await make_user(db, name="Farmer D")  # other-client-only

    pkg_parent_1 = await make_package(db, parent, name="PoP Parent 1")
    pkg_parent_2 = await make_package(db, parent, name="PoP Parent 2")
    pkg_training = await make_package(db, training_child, name="PoP Training")
    pkg_other = await make_package(db, other, name="PoP Other")

    # SHOULD COUNT (parent, ACTIVE): farmer_a x2, farmer_b x1  → subs=3, farmers=2
    await make_subscription(db, farmer=farmer_a, client=parent, package=pkg_parent_1)
    await make_subscription(db, farmer=farmer_a, client=parent, package=pkg_parent_2)
    await make_subscription(db, farmer=farmer_b, client=parent, package=pkg_parent_1)

    # SHOULD NOT COUNT — status != ACTIVE.
    lapsed = await make_subscription(
        db, farmer=farmer_b, client=parent, package=pkg_parent_2,
    )
    lapsed.status = SubscriptionStatus.LAPSED
    cancelled = await make_subscription(
        db, farmer=farmer_b, client=parent, package=pkg_parent_1,
    )
    cancelled.status = SubscriptionStatus.CANCELLED

    # SHOULD NOT COUNT — soft-deleted.
    soft_deleted = await make_subscription(
        db, farmer=farmer_a, client=parent, package=pkg_parent_1,
    )
    soft_deleted.deleted_at = datetime.now(timezone.utc)

    # SHOULD NOT COUNT — training client.
    await make_subscription(
        db, farmer=farmer_c, client=training_child, package=pkg_training,
    )

    # SHOULD NOT COUNT — different client.
    await make_subscription(
        db, farmer=farmer_d, client=other, package=pkg_other,
    )

    await db.flush()

    result = await subs_active(db, parent.id, ReportFilters())

    assert result == {"subscriptions": 3, "farmers": 2}


async def test_subs_active_empty_client_returns_zero(db):
    client = await make_client(db, full_name="Empty Client")

    result = await subs_active(db, client.id, ReportFilters())

    assert result == {"subscriptions": 0, "farmers": 0}


async def test_subs_active_narrows_by_each_filter(db):
    """Each of the four chip filters must scope the result independently.

    The unfiltered baseline is 3 active subs across 2 farmers on 2
    packages / 2 crops / 2 districts. Every filter should carve out
    exactly the expected subset.
    """
    client = await make_client(db, full_name="Filter Test Client")

    farmer_north = await make_user(db, name="Farmer North")
    farmer_north.state_cosh_id = "state:north"
    farmer_north.district_cosh_id = "district:north-a"

    farmer_south = await make_user(db, name="Farmer South")
    farmer_south.state_cosh_id = "state:south"
    farmer_south.district_cosh_id = "district:south-b"

    pkg_maize = await make_package(
        db, client, name="Maize PoP", crop_cosh_id="crop:maize",
    )
    pkg_wheat = await make_package(
        db, client, name="Wheat PoP", crop_cosh_id="crop:wheat",
    )

    # 3 active subs on 2 farmers:
    #   north-a → Maize x1
    #   north-a → Wheat x1     (same farmer, second crop)
    #   south-b → Maize x1
    await make_subscription(db, farmer=farmer_north, client=client, package=pkg_maize)
    await make_subscription(db, farmer=farmer_north, client=client, package=pkg_wheat)
    await make_subscription(db, farmer=farmer_south, client=client, package=pkg_maize)

    await db.flush()

    # Baseline.
    assert await subs_active(db, client.id, ReportFilters()) == {
        "subscriptions": 3, "farmers": 2,
    }

    # Crop chip.
    assert await subs_active(
        db, client.id, ReportFilters(crop_cosh_id="crop:maize"),
    ) == {"subscriptions": 2, "farmers": 2}

    # State chip.
    assert await subs_active(
        db, client.id, ReportFilters(state_cosh_id="state:north"),
    ) == {"subscriptions": 2, "farmers": 1}

    # District chip.
    assert await subs_active(
        db, client.id, ReportFilters(district_cosh_id="district:south-b"),
    ) == {"subscriptions": 1, "farmers": 1}

    # Package chip.
    assert await subs_active(
        db, client.id, ReportFilters(package_id=pkg_wheat.id),
    ) == {"subscriptions": 1, "farmers": 1}

    # Combination — Crop AND State (AND semantics; not OR).
    assert await subs_active(
        db, client.id,
        ReportFilters(crop_cosh_id="crop:maize", state_cosh_id="state:north"),
    ) == {"subscriptions": 1, "farmers": 1}


async def test_subs_total_counts_every_status_but_still_excludes_bad_scopes(db):
    """subs_total = every subscription row on this client, any status.

    Verifies two things at once:
    - Status is NOT filtered (LAPSED / CANCELLED / SUSPENDED /
      UNSUBSCRIBED all count).
    - The base scope still applies: training, other-client, and
      soft-deleted rows do NOT count.
    """
    parent = await make_client(db, full_name="Parent")
    other = await make_client(db, full_name="Other")

    training_child = Client(
        full_name="Parent Training",
        short_name="pt-" + parent.short_name[:8],
        ca_name="T", ca_phone="+919", ca_email="tt@t.local",
        payment_model=parent.payment_model,
        is_training=True,
        parent_client_id=parent.id,
    )
    db.add(training_child)
    await db.flush()

    farmer_a = await make_user(db, name="A")
    farmer_b = await make_user(db, name="B")
    farmer_c = await make_user(db, name="C")

    pkg_parent = await make_package(db, parent, name="PoP Parent")
    pkg_training = await make_package(db, training_child, name="PoP Training")
    pkg_other = await make_package(db, other, name="PoP Other")

    # SHOULD COUNT (any status on parent).
    await make_subscription(db, farmer=farmer_a, client=parent, package=pkg_parent)  # ACTIVE
    lapsed = await make_subscription(db, farmer=farmer_a, client=parent, package=pkg_parent)
    lapsed.status = SubscriptionStatus.LAPSED
    cancelled = await make_subscription(db, farmer=farmer_b, client=parent, package=pkg_parent)
    cancelled.status = SubscriptionStatus.CANCELLED
    unsubbed = await make_subscription(db, farmer=farmer_b, client=parent, package=pkg_parent)
    unsubbed.status = SubscriptionStatus.UNSUBSCRIBED

    # SHOULD NOT COUNT — soft-deleted.
    soft = await make_subscription(db, farmer=farmer_a, client=parent, package=pkg_parent)
    soft.deleted_at = datetime.now(timezone.utc)

    # SHOULD NOT COUNT — training / other-client.
    await make_subscription(db, farmer=farmer_c, client=training_child, package=pkg_training)
    await make_subscription(db, farmer=farmer_c, client=other, package=pkg_other)

    await db.flush()

    # 4 counted (ACTIVE + LAPSED + CANCELLED + UNSUBSCRIBED); 2 distinct farmers.
    assert await subs_total(db, parent.id, ReportFilters()) == {
        "subscriptions": 4, "farmers": 2,
    }


async def test_subs_new_period_boundaries_and_first_ever_dedup(db):
    """subs_new must:
    - Count subs with subscription_date in [period_from, period_to).
    - NOT count NULL subscription_date rows (undatable).
    - Report "farmers" as first-time-in-scope: a returning farmer whose
      earlier sub predates the window does NOT count as a new farmer,
      even though the in-window sub does count as a new relationship.
    """
    from datetime import datetime, timezone

    parent = await make_client(db, full_name="Parent")
    pkg = await make_package(db, parent, name="PoP")

    # Three farmers.
    farmer_new = await make_user(db, name="Truly New")
    farmer_returning = await make_user(db, name="Returning")
    farmer_pre = await make_user(db, name="Pre-window")

    period_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_to   = datetime(2026, 8, 1, tzinfo=timezone.utc)

    # farmer_new: FIRST-EVER sub is 15 Jul → counts as new relationship
    # AND new farmer.
    s_new = await make_subscription(db, farmer=farmer_new, client=parent, package=pkg)
    s_new.subscription_date = datetime(2026, 7, 15, tzinfo=timezone.utc)

    # farmer_returning: had a sub in May (pre-window), plus a NEW sub
    # on 20 Jul (in window). July sub counts as a new relationship; the
    # farmer does NOT count as a new farmer (May sub is earlier).
    s_ret_may = await make_subscription(db, farmer=farmer_returning, client=parent, package=pkg)
    s_ret_may.subscription_date = datetime(2026, 5, 10, tzinfo=timezone.utc)
    s_ret_jul = await make_subscription(db, farmer=farmer_returning, client=parent, package=pkg)
    s_ret_jul.subscription_date = datetime(2026, 7, 20, tzinfo=timezone.utc)

    # farmer_pre: FIRST-EVER sub is 30 Jun — one day before window.
    # NOT counted in relationships (out of window) nor farmers.
    s_pre = await make_subscription(db, farmer=farmer_pre, client=parent, package=pkg)
    s_pre.subscription_date = datetime(2026, 6, 30, tzinfo=timezone.utc)

    # Legacy NULL subscription_date — never dated, always excluded.
    s_null = await make_subscription(db, farmer=farmer_new, client=parent, package=pkg)
    s_null.subscription_date = None

    # Sub on the exclusive right boundary — 1 Aug 00:00 is OUTSIDE.
    s_boundary_out = await make_subscription(db, farmer=farmer_new, client=parent, package=pkg)
    s_boundary_out.subscription_date = datetime(2026, 8, 1, tzinfo=timezone.utc)

    await db.flush()

    # relationships = farmer_new (15 Jul) + farmer_returning (20 Jul) = 2
    # farmers = farmer_new only (returning had May sub earlier) = 1
    result = await subs_new(
        db, parent.id,
        ReportFilters(period_from=period_from, period_to=period_to),
    )
    assert result == {"relationships": 2, "farmers": 1}


async def test_orders_count_status_period_and_scope(db):
    """orders_count must:
    - Only count status in ORDER_COUNTED_STATUSES (SENT and beyond).
      DRAFT / CANCELLED / EXPIRED excluded.
    - Apply period filter on Order.created_at (not subscription_date).
    - Inherit the base scope via the Subscription join — training,
      soft-deleted subs, other-clients don't leak.
    """
    from datetime import datetime, timedelta, timezone

    parent = await make_client(db, full_name="Parent")
    pkg = await make_package(db, parent, name="PoP")
    farmer_a = await make_user(db, name="A")
    farmer_b = await make_user(db, name="B")
    dealer = await make_user(db, name="Dealer")

    sub_a = await make_subscription(db, farmer=farmer_a, client=parent, package=pkg)
    sub_b = await make_subscription(db, farmer=farmer_b, client=parent, package=pkg)

    period_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_to   = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def _order(sub, farmer, status, created):
        return Order(
            subscription_id=sub.id, farmer_user_id=farmer.id,
            client_id=parent.id, dealer_user_id=dealer.id,
            date_from=created, date_to=created + timedelta(days=14),
            status=status,
        )

    # SHOULD COUNT — 3 SENT-or-beyond orders in July on 2 distinct farmers.
    o1 = _order(sub_a, farmer_a, OrderStatus.SENT,      datetime(2026, 7, 5,  tzinfo=timezone.utc))
    o2 = _order(sub_a, farmer_a, OrderStatus.COMPLETED, datetime(2026, 7, 15, tzinfo=timezone.utc))
    o3 = _order(sub_b, farmer_b, OrderStatus.ACCEPTED,  datetime(2026, 7, 20, tzinfo=timezone.utc))
    for o in (o1, o2, o3):
        o.created_at = o.date_from   # explicit — factory would default to utcnow()
        db.add(o)

    # SHOULD NOT COUNT — status excluded.
    for status in (OrderStatus.DRAFT, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
        o = _order(sub_a, farmer_a, status, datetime(2026, 7, 10, tzinfo=timezone.utc))
        o.created_at = o.date_from
        db.add(o)

    # SHOULD NOT COUNT — out of period.
    o_june = _order(sub_a, farmer_a, OrderStatus.SENT, datetime(2026, 6, 25, tzinfo=timezone.utc))
    o_june.created_at = o_june.date_from
    db.add(o_june)
    o_aug = _order(sub_a, farmer_a, OrderStatus.SENT, datetime(2026, 8, 5, tzinfo=timezone.utc))
    o_aug.created_at = o_aug.date_from
    db.add(o_aug)

    await db.flush()

    # 3 orders / 2 distinct farmers within the July window.
    assert await orders_count(
        db, parent.id,
        ReportFilters(period_from=period_from, period_to=period_to),
    ) == {"orders": 3, "farmers": 2}

    # No period — everything counted-status is in scope: 5 (3 July + June + Aug).
    assert await orders_count(db, parent.id, ReportFilters()) == {
        "orders": 5, "farmers": 2,
    }


async def test_orders_routing_direct_vs_via_facilitator(db):
    """orders_routing splits counted-status orders by whether
    Order.facilitator_user_id is NULL (Direct) or NOT NULL (Via
    Facilitator). Status filter + period filter still apply.
    """
    from datetime import datetime, timedelta, timezone

    parent = await make_client(db, full_name="Parent")
    pkg = await make_package(db, parent, name="PoP")
    farmer = await make_user(db, name="F")
    dealer = await make_user(db, name="D")
    facilitator = await make_user(db, name="Fac")
    sub = await make_subscription(db, farmer=farmer, client=parent, package=pkg)

    period_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_to   = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def _order(status, created, via_fac):
        return Order(
            subscription_id=sub.id, farmer_user_id=farmer.id,
            client_id=parent.id, dealer_user_id=dealer.id,
            facilitator_user_id=(facilitator.id if via_fac else None),
            date_from=created, date_to=created + timedelta(days=14),
            status=status,
        )

    # 2 Direct (no facilitator), 3 Via Facilitator — all in period.
    for i, via_fac in enumerate([False, False, True, True, True]):
        o = _order(OrderStatus.SENT, datetime(2026, 7, 5 + i, tzinfo=timezone.utc), via_fac)
        o.created_at = o.date_from
        db.add(o)

    # SHOULD NOT COUNT — DRAFT status.
    o_draft = _order(OrderStatus.DRAFT, datetime(2026, 7, 20, tzinfo=timezone.utc), False)
    o_draft.created_at = o_draft.date_from
    db.add(o_draft)

    # SHOULD NOT COUNT — out of period.
    o_out = _order(OrderStatus.SENT, datetime(2026, 6, 20, tzinfo=timezone.utc), True)
    o_out.created_at = o_out.date_from
    db.add(o_out)

    await db.flush()

    assert await orders_routing(
        db, parent.id,
        ReportFilters(period_from=period_from, period_to=period_to),
    ) == {"direct": 2, "via_facilitator": 3}


async def test_orders_items_excludes_removed_rerouted_and_counts_approved_rejected(db):
    """orders_items totals every OrderItem except REMOVED/REROUTED
    on counted-status orders in period, and reports APPROVED /
    REJECTED as separate counts.
    """
    from datetime import datetime, timedelta, timezone
    from tests.factories import make_practice, make_timeline

    parent = await make_client(db, full_name="Parent")
    pkg = await make_package(db, parent, name="PoP")
    tl = await make_timeline(db, pkg)
    practice = await make_practice(db, tl)
    farmer = await make_user(db, name="F")
    dealer = await make_user(db, name="D")
    sub = await make_subscription(db, farmer=farmer, client=parent, package=pkg)

    order_created = datetime(2026, 7, 10, tzinfo=timezone.utc)
    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=parent.id, dealer_user_id=dealer.id,
        date_from=order_created, date_to=order_created + timedelta(days=14),
        status=OrderStatus.SENT,
    )
    order.created_at = order_created
    db.add(order)
    await db.flush()

    def _item(status):
        db.add(OrderItem(
            order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
            status=status,
        ))

    # Real items: 3 APPROVED, 2 REJECTED, 1 PENDING, 1 SKIPPED = 7 total.
    for _ in range(3): _item(OrderItemStatus.APPROVED)
    for _ in range(2): _item(OrderItemStatus.REJECTED)
    _item(OrderItemStatus.PENDING)
    _item(OrderItemStatus.SKIPPED)

    # Bookkeeping — MUST NOT count in items_total.
    _item(OrderItemStatus.REMOVED)
    _item(OrderItemStatus.REROUTED)

    await db.flush()

    period_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_to   = datetime(2026, 8, 1, tzinfo=timezone.utc)

    assert await orders_items(
        db, parent.id,
        ReportFilters(period_from=period_from, period_to=period_to),
    ) == {"items_total": 7, "items_approved": 3, "items_rejected": 2}


async def test_orders_items_ignores_items_on_draft_orders(db):
    """Parent Order must pass the status filter (SENT and beyond).
    Items on a DRAFT order don't count no matter their status."""
    from datetime import datetime, timedelta, timezone
    from tests.factories import make_practice, make_timeline

    parent = await make_client(db, full_name="Parent")
    pkg = await make_package(db, parent, name="PoP")
    tl = await make_timeline(db, pkg)
    practice = await make_practice(db, tl)
    farmer = await make_user(db, name="F")
    dealer = await make_user(db, name="D")
    sub = await make_subscription(db, farmer=farmer, client=parent, package=pkg)

    order_created = datetime(2026, 7, 10, tzinfo=timezone.utc)
    draft = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=parent.id, dealer_user_id=dealer.id,
        date_from=order_created, date_to=order_created + timedelta(days=14),
        status=OrderStatus.DRAFT,
    )
    draft.created_at = order_created
    db.add(draft)
    await db.flush()

    for _ in range(5):
        db.add(OrderItem(
            order_id=draft.id, practice_id=practice.id, timeline_id=tl.id,
            status=OrderItemStatus.APPROVED,
        ))
    await db.flush()

    assert await orders_items(db, parent.id, ReportFilters()) == {
        "items_total": 0, "items_approved": 0, "items_rejected": 0,
    }


async def test_orders_brand_mix_three_way_by_se_intent(db):
    """orders_brand_mix classifies items by the SE's authoring intent
    on the parent Practice (not by OrderItem.brand_cosh_id — dealers
    always fill that at sale time regardless of authoring state):

      locked      = Practice.is_brand_locked = True
      recommended = not locked, but Practice has a BRAND_NAME element
                    with non-empty cosh_ref
      open        = not locked, no BRAND_NAME element

    REMOVED / REROUTED items excluded. Parent Order status filter
    still applies.
    """
    from datetime import datetime, timedelta, timezone
    from tests.factories import make_practice, make_timeline
    from app.modules.advisory.models import Element

    parent = await make_client(db, full_name="Parent")
    pkg = await make_package(db, parent, name="PoP")
    tl = await make_timeline(db, pkg)

    # Three practices, one per authoring state.
    p_locked = await make_practice(db, tl)
    p_locked.is_brand_locked = True
    db.add(Element(
        practice_id=p_locked.id, element_type="BRAND_NAME",
        cosh_ref="cosh-brand-locked",
    ))

    p_reco = await make_practice(db, tl)
    p_reco.is_brand_locked = False
    db.add(Element(
        practice_id=p_reco.id, element_type="BRAND_NAME",
        cosh_ref="cosh-brand-reco",
    ))

    p_open = await make_practice(db, tl)
    p_open.is_brand_locked = False
    # No BRAND_NAME element on p_open — dealer picks freely.

    farmer = await make_user(db, name="F")
    dealer = await make_user(db, name="D")
    sub = await make_subscription(db, farmer=farmer, client=parent, package=pkg)

    order_created = datetime(2026, 7, 10, tzinfo=timezone.utc)
    order = Order(
        subscription_id=sub.id, farmer_user_id=farmer.id,
        client_id=parent.id, dealer_user_id=dealer.id,
        date_from=order_created, date_to=order_created + timedelta(days=14),
        status=OrderStatus.SENT,
    )
    order.created_at = order_created
    db.add(order)
    await db.flush()

    def _item(practice, status=OrderItemStatus.PENDING):
        # brand_cosh_id/brand_name populated to prove the classifier
        # ignores them — SE-intent from Practice is what counts.
        db.add(OrderItem(
            order_id=order.id, practice_id=practice.id, timeline_id=tl.id,
            brand_cosh_id="cosh-picked-by-dealer",
            brand_name="Whatever dealer picked",
            status=status,
        ))

    for _ in range(4): _item(p_locked)
    for _ in range(2): _item(p_reco)
    for _ in range(3): _item(p_open)

    # REMOVED / REROUTED — MUST NOT count.
    _item(p_locked, OrderItemStatus.REMOVED)
    _item(p_open,   OrderItemStatus.REROUTED)

    await db.flush()

    assert await orders_brand_mix(db, parent.id, ReportFilters()) == {
        "locked": 4, "recommended": 2, "open": 3,
    }


async def test_subs_new_no_period_returns_all_datable(db):
    """When neither period_from nor period_to is set, every datable
    sub counts as a relationship, and every farmer with >=1 datable
    sub counts as a farmer (their first-ever is trivially in-window).
    NULL subscription_date still excluded."""
    from datetime import datetime, timezone

    parent = await make_client(db, full_name="Parent")
    pkg = await make_package(db, parent, name="PoP")
    farmer = await make_user(db, name="F")

    s_dated = await make_subscription(db, farmer=farmer, client=parent, package=pkg)
    s_dated.subscription_date = datetime(2026, 3, 1, tzinfo=timezone.utc)
    s_null = await make_subscription(db, farmer=farmer, client=parent, package=pkg)
    s_null.subscription_date = None

    await db.flush()

    assert await subs_new(db, parent.id, ReportFilters()) == {
        "relationships": 1, "farmers": 1,
    }
