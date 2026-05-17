"""Batch 39U (2026-05-17) — CA-side CHA-SP write guard.

Extends the 39S CCA / 39T CHA-PG guard sweep to CHA-SP. All 10
CA-side SP mutations now run `_assert_can_edit_client_advisory`
(ClientUser-or-CM-EDIT). The 10th — `import_pg_into_sp` — was
previously CM-EDIT-only; widened in this batch because it's a
within-client copy (cross-client is 404'd inside the handler),
not a cross-tenant Global→Local propagation.

Eligibility / rejection rules are identical to CA-CCA / CA-PG and
covered exhaustively in `test_phase_cca_role_guard.py` against the
helper. This file cross-validates:
  (a) `create_client_sp` — stranger gets 403 before any business
      logic (representative happy-path mutation).
  (b) `import_pg_into_sp` — a ClientUser SE (without any CM
      assignment) can now successfully import. Pre-39U this
      raised 403; post-39U it succeeds.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, PGRecommendation, Practice, SPRecommendation, Timeline,
)
from app.modules.advisory.router import (
    create_client_sp, import_pg_into_sp,
)
from app.modules.advisory.schemas import SPRecommendationCreate
from app.modules.clients.models import ClientUser, ClientUserRole
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


@requires_docker
@pytest.mark.asyncio
async def test_create_client_sp_rejects_stranger(db):
    """An authenticated user with no link to this client gets 403
    on create_client_sp, not 201."""
    client = await make_client(db)
    stranger = await make_user(db, name="Stranger", skip_auto_link=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await create_client_sp(
            client_id=client.id,
            request=SPRecommendationCreate(
                specific_problem_cosh_id="sp:powdery",
                crop_cosh_id="crop:test",
            ),
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "ca_edit_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_import_pg_into_sp_now_accepts_clientuser_se(db):
    """Behavior change in 39U: a ClientUser SE (no CM assignment) can
    seed an SP from a PG they authored on the same client. Pre-39U
    this raised 403 because the gate was CM-EDIT-only."""
    client = await make_client(db)
    se = await make_user(db, name="SE-no-CM", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )

    pg = PGRecommendation(
        problem_group_cosh_id="pg:fungal",
        client_id=client.id,
        parent_id=None,
        area_or_plant="AREA_WISE",
    )
    db.add(pg)
    await db.flush()
    tl = Timeline(
        pg_recommendation_id=pg.id, name="src",
        from_type="DAYS_AFTER_DETECTION", from_value=0, to_value=7,
    )
    db.add(tl)
    await db.flush()
    p = Practice(
        timeline_id=tl.id, l0_type="INPUT", l1_type="FERTILIZER",
        l2_type="MANURES", display_order=1, is_special_input=False,
    )
    db.add(p)
    await db.flush()
    db.add(Element(
        practice_id=p.id, element_type="COMMON_NAME",
        cosh_ref="cn:fym", value=None, display_order=1,
    ))

    sp = SPRecommendation(
        specific_problem_cosh_id="sp:powdery",
        client_id=client.id, crop_cosh_id="crop:test",
    )
    db.add(sp)
    await db.commit()

    out = await import_pg_into_sp(
        client_id=client.id, sp_id=sp.id, local_pg_id=pg.id,
        db=db, current_user=se,
    )
    assert out.id == sp.id

    tls = (await db.execute(
        select(Timeline).where(Timeline.sp_recommendation_id == sp.id)
    )).scalars().all()
    assert len(tls) == 1
    assert tls[0].name == "src"


@requires_docker
@pytest.mark.asyncio
async def test_import_pg_into_sp_still_rejects_stranger(db):
    """The widening only added ClientUser as an eligible identity —
    a complete stranger still gets 403."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    stranger = await make_user(db, name="Stranger", skip_auto_link=True)
    await make_client_user(
        db, user=stranger, client=client_a,
        role=ClientUserRole.SUBJECT_EXPERT,
    )

    pg = PGRecommendation(
        problem_group_cosh_id="pg:fungal", client_id=client_b.id,
        parent_id=None, area_or_plant="AREA_WISE",
    )
    db.add(pg)
    await db.flush()
    sp = SPRecommendation(
        specific_problem_cosh_id="sp:powdery",
        client_id=client_b.id, crop_cosh_id="crop:test",
    )
    db.add(sp)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await import_pg_into_sp(
            client_id=client_b.id, sp_id=sp.id, local_pg_id=pg.id,
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "ca_edit_forbidden"
