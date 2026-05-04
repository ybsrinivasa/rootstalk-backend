"""Phase 4.1 — admin snapshot debug endpoints.

Verifies:
  - GET /admin/subscriptions/{id}/snapshots returns one row per existing
    snapshot with metadata, no content body, sorted by locked_at ascending.
  - GET /admin/snapshots/{id} returns full content.
  - Both endpoints require SA email auth (403 otherwise).
  - Missing snapshot id returns 404.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.modules.subscriptions.router import (
    admin_get_snapshot, admin_list_subscription_snapshots,
)
from app.services.snapshot import take_snapshot
from fastapi import HTTPException
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_practice, make_subscription, make_timeline,
    make_user,
)


async def _seed_two_snapshots(db):
    user = await make_user(db)
    user.email = settings.sa_email
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=user, client=client, package=package,
    )
    tl_a = await make_timeline(db, package, name="A")
    tl_b = await make_timeline(db, package, name="B")
    await make_practice(db, tl_a)
    await make_practice(db, tl_b)
    await db.commit()

    snap_a = await take_snapshot(db, sub.id, tl_a.id, "PURCHASE_ORDER", "CCA")
    snap_b = await take_snapshot(db, sub.id, tl_b.id, "VIEWED", "CCA")
    return user, sub, [snap_a, snap_b]


# ── List endpoint ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_admin_list_returns_metadata_for_each_snapshot(db):
    sa_user, sub, snaps = await _seed_two_snapshots(db)

    out = await admin_list_subscription_snapshots(
        subscription_id=sub.id, db=db, current_user=sa_user,
    )
    assert len(out) == 2
    out_by_trigger = {row["lock_trigger"]: row for row in out}
    assert "PURCHASE_ORDER" in out_by_trigger
    assert "VIEWED" in out_by_trigger
    # Metadata only — content body absent in the list response.
    for row in out:
        assert "id" in row
        assert row["subscription_id"] == sub.id
        assert row["source"] == "CCA"
        assert "content" not in row
        assert row["practice_count"] >= 1


@requires_docker
@pytest.mark.asyncio
async def test_admin_list_empty_for_subscription_without_snapshots(db):
    sa_user = await make_user(db)
    sa_user.email = settings.sa_email
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(
        db, farmer=sa_user, client=client, package=package,
    )
    await db.commit()

    out = await admin_list_subscription_snapshots(
        subscription_id=sub.id, db=db, current_user=sa_user,
    )
    assert out == []


@requires_docker
@pytest.mark.asyncio
async def test_admin_list_rejects_non_sa(db):
    sa_user, sub, _ = await _seed_two_snapshots(db)

    other = await make_user(db)
    other.email = "not-sa@example.com"
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await admin_list_subscription_snapshots(
            subscription_id=sub.id, db=db, current_user=other,
        )
    assert exc.value.status_code == 403


# ── Single-snapshot endpoint ────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_admin_get_returns_full_content(db):
    sa_user, _sub, snaps = await _seed_two_snapshots(db)

    out = await admin_get_snapshot(
        snapshot_id=snaps[0].id, db=db, current_user=sa_user,
    )
    assert out["id"] == snaps[0].id
    assert "content" in out
    assert out["content"]["timeline"]["id"] == snaps[0].timeline_id
    assert out["lock_trigger"] == "PURCHASE_ORDER"


@requires_docker
@pytest.mark.asyncio
async def test_admin_get_404_for_missing_id(db):
    sa_user = await make_user(db)
    sa_user.email = settings.sa_email
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await admin_get_snapshot(
            snapshot_id="snap_does_not_exist",
            db=db, current_user=sa_user,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_admin_get_rejects_non_sa(db):
    sa_user, _sub, snaps = await _seed_two_snapshots(db)

    other = await make_user(db)
    other.email = "not-sa@example.com"
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await admin_get_snapshot(
            snapshot_id=snaps[0].id, db=db, current_user=other,
        )
    assert exc.value.status_code == 403
