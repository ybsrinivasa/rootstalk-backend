"""Form-driven Global → Local push + deferred pull (Batch 39N-a,
2026-05-16).

Push is now the CM's authoring step:

  • The Global Package contributes only its content (timelines /
    practices / elements / relations / CQs).
  • Ram (the CM) enters Name, Description, Start Date Label,
    Locations, P-V signature, and Authors at push time. These are
    validated against the target client before any deep-copy runs.
  • Push lands a DRAFT (status=DRAFT, created_via=CM_PUSH). The SE
    publishes when they're ready (legal-review gate).
  • Once-per-Global-lineage, per client: a Global lineage is the set
    of rows sharing (client_id=NULL, crop_cosh_id, name) on the SA
    side. Re-pushing — including from a v_{N+1} Global row in the
    same lineage — returns 409 `package_already_pushed`.

Pull is deferred to V1.x: the endpoint returns 501 `pull_deferred`
so existing CA-portal clients get a clear refusal instead of a 404.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, Package, PackageAuthor, PackageCreatedVia, PackageLocation,
    PackageStatus, PackageType, PackageVariable, Practice, PracticeL0,
    Timeline, TimelineFromType,
)
from app.modules.advisory.router import (
    pull_global_package, push_global_package,
)
from app.modules.advisory.schemas import PackagePushRequest
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_cm_assignment,
    make_push_request_body, make_user,
)


async def _seed_global_pkg_with_content(db, *, name: str = "Global PoP"):
    """Tiny Global Package + 1 timeline + 1 practice + 1 element."""
    pkg = Package(
        client_id=None, name=name, crop_cosh_id="crop:test",
        package_type=PackageType.ANNUAL, duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
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


# ── push: happy path ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_push_creates_draft_with_cm_push_marker_and_form_metadata(db):
    """First push lands a DRAFT row with the form-entered name +
    description + locations + PVs + authors; created_via=CM_PUSH;
    parent_global_id wired to the Global row used at push time;
    content deep-copied."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    body = await make_push_request_body(
        db, client=client, src=gpkg,
        name="Arunodaya Tomato PoP", description="For Arunodaya kitchen gardens",
    )
    await db.commit()
    out = await push_global_package(
        client_id=client.id, pkg_id=gpkg.id,
        request=PackagePushRequest(**body),
        db=db, current_user=cm,
    )
    assert out.status == PackageStatus.DRAFT
    assert out.created_via == PackageCreatedVia.CM_PUSH
    assert out.parent_global_id == gpkg.id
    assert out.client_id == client.id
    assert out.name == "Arunodaya Tomato PoP"
    assert out.description == "For Arunodaya kitchen gardens"

    locs = (await db.execute(
        select(PackageLocation).where(PackageLocation.package_id == out.id)
    )).scalars().all()
    assert len(locs) == 1
    assert (locs[0].state_cosh_id, locs[0].district_cosh_id) == (
        body["locations"][0]["state_cosh_id"],
        body["locations"][0]["district_cosh_id"],
    )

    pvs = (await db.execute(
        select(PackageVariable).where(PackageVariable.package_id == out.id)
    )).scalars().all()
    assert len(pvs) == 1
    assert pvs[0].parameter_id == body["pv_assignments"][0]["parameter_id"]

    authors = (await db.execute(
        select(PackageAuthor).where(PackageAuthor.package_id == out.id)
    )).scalars().all()
    assert len(authors) == 1
    assert authors[0].user_id == body["author_ids"][0]

    # Content deep-copied.
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


# ── push: lineage-aware once-per-package gate ─────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_repush_409s_with_lineage_aware_code(db):
    """Second push of the same Global into the same Client returns
    409 `package_already_pushed`."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    body1 = await make_push_request_body(db, client=client, src=gpkg, name="A")
    await db.commit()
    await push_global_package(
        client_id=client.id, pkg_id=gpkg.id,
        request=PackagePushRequest(**body1),
        db=db, current_user=cm,
    )

    body2 = await make_push_request_body(db, client=client, src=gpkg, name="B")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=gpkg.id,
            request=PackagePushRequest(**body2),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "package_already_pushed"


@requires_docker
@pytest.mark.asyncio
async def test_repush_from_newer_global_version_in_same_lineage_409s(db):
    """Lineage-aware gate (Batch 39N-a). After Ram pushes from
    Global v1, then a new Global v2 is published (same lineage —
    same crop+name, different row id), Ram's attempt to push from
    v2 must STILL refuse: the client already received this logical
    Package once."""
    cm = await make_user(db, name="CM")
    client = await make_client(db)
    await make_cm_assignment(db, user=cm, client=client)
    name = f"GP-{uuid.uuid4().hex[:6]}"
    g_v1 = await _seed_global_pkg_with_content(db, name=name)
    await db.commit()

    body1 = await make_push_request_body(db, client=client, src=g_v1, name="A")
    await db.commit()
    await push_global_package(
        client_id=client.id, pkg_id=g_v1.id,
        request=PackagePushRequest(**body1),
        db=db, current_user=cm,
    )

    # New Global row in the same lineage (same client_id=NULL,
    # crop, name) — represents a v_{N+1} from clone-to-draft +
    # publish. Different row id, same lineage.
    g_v2 = await _seed_global_pkg_with_content(db, name=name)
    await db.commit()

    body2 = await make_push_request_body(db, client=client, src=g_v2, name="B")
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await push_global_package(
            client_id=client.id, pkg_id=g_v2.id,
            request=PackagePushRequest(**body2),
            db=db, current_user=cm,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "package_already_pushed"


# ── pull: deferred to V1.x ────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pull_endpoint_is_deferred_to_v1x(db):
    """Pull endpoint returns 501 `pull_deferred`. Real-world feedback
    on V1 will shape the refresh model in V1.x."""
    se = await make_user(db, name="SE")
    client = await make_client(db)
    await make_client_user(db, user=se, client=client)
    gpkg = await _seed_global_pkg_with_content(db, name=f"GP-{uuid.uuid4().hex[:6]}")
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await pull_global_package(
            client_id=client.id, pkg_id=gpkg.id, db=db, current_user=se,
        )
    assert exc.value.status_code == 501
    assert exc.value.detail["code"] == "pull_deferred"
