"""CCA Global → Local push + pull (multi-row versioning).

Batch 2 of the work locked 2026-05-11 in
`project_rootstalk_global_to_local_pipe.md`. Verifies:

  • push (CM first-contact) creates a DRAFT row with
    created_via=CM_PUSH and parent_global_id linked.
  • push is once per (client, parent_global_id) — re-push 409s
    with code `package_already_pushed`.
  • pull (SE refresh) requires a prior push — refuses with
    `package_not_pushed_yet` otherwise.
  • pull creates a new DRAFT row with created_via=SE_PULL_DRAFT
    alongside the existing PUBLISHED (single-DRAFT invariant
    flips any prior DRAFT to INACTIVE first).
  • pull's auth gate accepts ACTIVE ClientUser; refuses
    unrelated users with `client_user_required`.

Subscription migration on publish is Batch 3 and tested
separately.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Package, PackageCreatedVia, PackageStatus, PackageType, Timeline,
    TimelineFromType, Practice, PracticeL0, Element,
)
from app.modules.advisory.router import (
    pull_global_package, push_global_package,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_cm_assignment, make_user,
)


async def _seed_global_pkg_with_content(db, *, name: str = "Global PoP"):
    """Tiny Global Package + 1 timeline + 1 practice + 1 element."""
    pkg = Package(
        client_id=None, name=name, crop_cosh_id="crop:test",
        package_type=PackageType.ANNUAL, duration_days=120,
        status=PackageStatus.ACTIVE,
    )
    db.add(pkg)
    await db.flush()
    tl = Timeline(
        package_id=pkg.id, name="GTL",
        from_type=TimelineFromType.DAS, from_value=0, to_value=15,
    )
    db.add(tl)
    await db.flush()
    p = Practice(
        timeline_id=tl.id, l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER", l2_type="UREA", display_order=0,
    )
    db.add(p)
    await db.flush()
    db.add(Element(
        practice_id=p.id, element_type="DOSAGE", value="50",
        unit_cosh_id="kg_per_acre",
    ))
    await db.flush()
    return pkg


# ── push: first-contact happy path ──────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_creates_draft_with_cm_push_marker(db):
    """First push lands a DRAFT row with created_via=CM_PUSH and
    parent_global_id wired up. Content is deep-copied."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    out = await push_global_package(
        client_id=client.id, pkg_id=gpkg.id,
        db=db, current_user=cm,
    )
    assert out.status == PackageStatus.DRAFT
    assert out.created_via == PackageCreatedVia.CM_PUSH
    assert out.parent_global_id == gpkg.id
    assert out.client_id == client.id

    # Content deep-copied: one timeline, one practice, one element.
    tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == out.id)
    )).scalars().all()
    assert len(tls) == 1
    pracs = (await db.execute(
        select(Practice).where(Practice.timeline_id == tls[0].id)
    )).scalars().all()
    assert len(pracs) == 1
    els = (await db.execute(
        select(Element).where(Element.practice_id == pracs[0].id)
    )).scalars().all()
    assert len(els) == 1


@requires_docker
@pytest.mark.asyncio
async def test_repush_409s_with_new_code(db):
    """Second push of the same Global into the same Client returns
    409 with stable code `package_already_pushed` (renamed from
    the old `package_already_forked`)."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )

    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "package_already_pushed"


# ── pull: gating ──────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pull_requires_prior_push(db):
    """SE pull on a Global that has never been pushed to this
    client → 422 `package_not_pushed_yet`."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await pull_global_package(
            client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "package_not_pushed_yet"


@requires_docker
@pytest.mark.asyncio
async def test_pull_rejected_for_non_client_user(db):
    """Caller without an active ClientUser at this client → 403
    `client_user_required`, even if Global is ACTIVE."""
    cm = await make_user(db, name="CM")
    se = await make_user(db, name="Rando")  # not a client user
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()
    await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )

    with pytest.raises(HTTPException) as exc:
        await pull_global_package(
            client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "client_user_required"


# ── pull: success + single-DRAFT invariant ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pull_creates_new_draft_alongside_existing_published(db):
    """After a CM push + SE publish (simulated by flipping push'd
    DRAFT to ACTIVE), the SE pulls a refresh. Result: a new DRAFT
    row with created_via=SE_PULL_DRAFT, alongside the ACTIVE
    row from the prior cycle. Both share parent_global_id."""
    cm = await make_user(db, name="CM")
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    await make_client_user(db, user=se, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    pushed = await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )
    # Simulate the legacy in-place publish (Batch 3 reworks this).
    pushed.status = PackageStatus.ACTIVE
    await db.commit()

    pulled = await pull_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
    )
    assert pulled.status == PackageStatus.DRAFT
    assert pulled.created_via == PackageCreatedVia.SE_PULL_DRAFT
    assert pulled.parent_global_id == gpkg.id
    assert pulled.id != pushed.id

    # Predecessor is still ACTIVE — farmers continue on it until SE
    # publishes the pulled DRAFT.
    await db.refresh(pushed)
    assert pushed.status == PackageStatus.ACTIVE


@requires_docker
@pytest.mark.asyncio
async def test_pull_flips_prior_draft_to_inactive(db):
    """Single-DRAFT invariant: if an abandoned DRAFT already exists
    for this (client, parent_global_id), a new pull flips it to
    INACTIVE before creating the new DRAFT."""
    cm = await make_user(db, name="CM")
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    await make_client_user(db, user=se, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    pushed = await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )
    pushed.status = PackageStatus.ACTIVE
    await db.commit()

    first_pull = await pull_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
    )
    assert first_pull.status == PackageStatus.DRAFT

    # Second pull without first publishing the prior DRAFT.
    second_pull = await pull_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
    )
    assert second_pull.status == PackageStatus.DRAFT
    assert second_pull.id != first_pull.id

    await db.refresh(first_pull)
    assert first_pull.status == PackageStatus.INACTIVE, (
        "Prior DRAFT must auto-flip to INACTIVE on a fresh pull"
    )


@requires_docker
@pytest.mark.asyncio
async def test_pull_refuses_global_not_published(db):
    """Pull on a DRAFT Global → 422 `global_package_not_published`."""
    cm = await make_user(db, name="CM")
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    await make_client_user(db, user=se, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    # First push to make pull eligible…
    await db.commit()
    await push_global_package(
        client_id=client.id, pkg_id=gpkg.id, db=db, current_user=cm,
    )
    # …then drop the Global out of ACTIVE.
    gpkg.status = PackageStatus.DRAFT
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await pull_global_package(
            client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "global_package_not_published"
