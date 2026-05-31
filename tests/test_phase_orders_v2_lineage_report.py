"""Orders V2 Batch 15 — lineage report endpoints.

Locks in the client-reporting surface the 2026-05-31 narrative
explicitly asked for: "We will need all this information when we
generate reports to our clients." Reads the audit table populated
by every state-change endpoint in Batches 3-12.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.modules.advisory.models import PracticeL0, TimelineFromType
from app.modules.orders.models import (
    Order, OrderItem, OrderItemEvent, OrderItemStatus, OrderStatus,
)
from app.modules.orders.router import (
    accept_order, admin_list_lineages, admin_order_lineage,
    approve_order_item, cancel_order, mark_item_unavailable,
)
from app.services.order_events import record_event
# Memory: feedback_test_lazy_model_import — the lineage endpoints
# import SeedOrderFull lazily; importing it here at module level
# registers the table on Base.metadata BEFORE conftest's create_all
# runs, so teardown's TRUNCATE doesn't trip on the missing relation.
from app.modules.seed_mgmt.models import SeedOrderFull, SeedVariety, VarietyPoP  # noqa: F401
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_onboarded_dealer, make_package, make_practice,
    make_subscription, make_timeline, make_user,
)


async def _make_sa_user(db):
    u = await make_user(db, name="SA-Lineage")
    u.email = settings.sa_email
    await db.flush()
    return u


async def _sent_order_with_item(db):
    user = await make_user(db, name="Farmer Lin")
    client = await make_client(db)
    pkg = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=pkg)
    sub.crop_start_date = datetime.now(timezone.utc)
    await db.commit()

    tl = await make_timeline(
        db, pkg, name="TL_lin",
        from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    p = await make_practice(db, tl, l0=PracticeL0.INPUT, l1="PESTICIDE", l2="CHEMICAL_PESTICIDES")
    await db.commit()

    dealer = await make_onboarded_dealer(db, name="D-Lin")
    await db.commit()
    order = Order(
        subscription_id=sub.id, farmer_user_id=user.id, client_id=client.id,
        category="PESTICIDE",
        date_from=datetime.now(timezone.utc),
        date_to=datetime.now(timezone.utc) + timedelta(days=10),
        status=OrderStatus.SENT,
        dealer_user_id=dealer.id,
    )
    db.add(order)
    await db.flush()
    item = OrderItem(
        order_id=order.id, practice_id=p.id, timeline_id=tl.id,
        status=OrderItemStatus.PENDING,
    )
    db.add(item)
    await db.flush()
    # Emit the CREATED event the production create_order endpoint
    # writes — without it the lineage doesn't show up in
    # /admin/orders/lineages until something else acts on the item.
    await record_event(
        db, lineage_id=item.lineage_id, event_type="CREATED",
        actor_user_id=user.id, actor_role="FARMER",
        order_id=order.id, order_item_id=item.id,
        prev_status=None, new_status=OrderItemStatus.PENDING.value,
    )
    await db.commit()
    await db.refresh(item)
    return user, client, dealer, order, item


# ── /admin/order-lineage/{id} ──────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_lineage_report_walks_a_journey(db):
    user, _, dealer, order, item = await _sent_order_with_item(db)
    sa = await _make_sa_user(db)

    # Drive the item through a small journey: accept → not_available
    # → cancel-migrate. That produces a chain of events keyed on
    # the same lineage_id.
    await accept_order(order_id=order.id, db=db, current_user=dealer)
    await mark_item_unavailable(
        order_id=order.id, item_id=item.id, db=db, current_user=dealer,
    )
    await cancel_order(order_id=order.id, db=db, current_user=user)

    res = await admin_order_lineage(
        lineage_id=item.lineage_id, db=db, current_user=sa,
    )
    assert res["lineage_id"] == item.lineage_id
    types = [e["event_type"] for e in res["events"]]
    # ACCEPTED is order-level (lineage = order.id), not on item
    # lineage. Item lineage should carry MARKED_NOT_AVAILABLE +
    # REROUTED_FROM + REROUTED_TO.
    assert "MARKED_NOT_AVAILABLE" in types
    assert "REROUTED_FROM" in types
    assert "REROUTED_TO" in types

    # Current state: the migrated PENDING item on the new DRAFT.
    cur = res["current"]
    assert cur is not None
    assert cur["status"] == OrderItemStatus.PENDING.value

    # Summary: dealer hops should reflect the original-order leg.
    summary = res["summary"]
    assert summary["dealer_hops"] >= 1
    assert summary["outcome"] == "IN_FLIGHT"


@requires_docker
@pytest.mark.asyncio
async def test_lineage_report_outcome_purchased(db):
    user, _, dealer, order, item = await _sent_order_with_item(db)
    sa = await _make_sa_user(db)

    # Walk to APPROVED via the farmer-side approve.
    item.status = OrderItemStatus.SENT_FOR_APPROVAL
    item.brand_name = "Cheaty-Test"
    item.price = 199
    await db.commit()
    await approve_order_item(
        order_id=order.id, item_id=item.id, db=db, current_user=user,
    )

    res = await admin_order_lineage(
        lineage_id=item.lineage_id, db=db, current_user=sa,
    )
    assert res["summary"]["outcome"] == "PURCHASED"
    assert res["current"]["brand_name"] == "Cheaty-Test"
    assert res["current"]["price"] == 199


@requires_docker
@pytest.mark.asyncio
async def test_lineage_report_404_when_unknown(db):
    sa = await _make_sa_user(db)
    with pytest.raises(HTTPException) as exc:
        await admin_order_lineage(
            lineage_id="00000000-0000-0000-0000-000000000000",
            db=db, current_user=sa,
        )
    assert exc.value.status_code == 404


# ── /admin/orders/lineages ──────────────────────────────────────────


@requires_docker
@pytest.mark.asyncio
async def test_list_lineages_returns_recent(db):
    user, client, dealer, order, item = await _sent_order_with_item(db)
    sa = await _make_sa_user(db)

    await accept_order(order_id=order.id, db=db, current_user=dealer)

    rows = await admin_list_lineages(
        db=db, current_user=sa, limit=50, offset=0,
    )
    lineage_ids = {r["lineage_id"] for r in rows}
    assert item.lineage_id in lineage_ids
    row = next(r for r in rows if r["lineage_id"] == item.lineage_id)
    assert row["client_id"] == client.id
    assert row["kind"] == "input_item"
    assert row["current_status"] == OrderItemStatus.PENDING.value


@requires_docker
@pytest.mark.asyncio
async def test_list_lineages_client_filter(db):
    user_a, client_a, dealer_a, order_a, item_a = await _sent_order_with_item(db)
    user_b, client_b, dealer_b, order_b, item_b = await _sent_order_with_item(db)
    sa = await _make_sa_user(db)
    await accept_order(order_id=order_a.id, db=db, current_user=dealer_a)
    await accept_order(order_id=order_b.id, db=db, current_user=dealer_b)

    rows_a = await admin_list_lineages(
        client_id=client_a.id, db=db, current_user=sa, limit=50, offset=0,
    )
    ids = {r["lineage_id"] for r in rows_a}
    assert item_a.lineage_id in ids
    assert item_b.lineage_id not in ids


@requires_docker
@pytest.mark.asyncio
async def test_list_lineages_outcome_filter(db):
    user, _, dealer, order, item = await _sent_order_with_item(db)
    sa = await _make_sa_user(db)
    await accept_order(order_id=order.id, db=db, current_user=dealer)
    await mark_item_unavailable(
        order_id=order.id, item_id=item.id, db=db, current_user=dealer,
    )

    returned_rows = await admin_list_lineages(
        outcome="RETURNED", db=db, current_user=sa, limit=50, offset=0,
    )
    ids = {r["lineage_id"] for r in returned_rows}
    assert item.lineage_id in ids

    purchased_rows = await admin_list_lineages(
        outcome="PURCHASED", db=db, current_user=sa, limit=50, offset=0,
    )
    ids2 = {r["lineage_id"] for r in purchased_rows}
    assert item.lineage_id not in ids2
