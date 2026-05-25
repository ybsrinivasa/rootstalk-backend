"""Regression guard for tester report 2026-05-25.

The CA-side CCA `delete_practice` endpoint silently failed when the
Practice had Elements — Element.practice_id is NOT NULL with no
ON DELETE CASCADE, so a bare `db.delete(practice)` tripped a FK
violation. The frontend's handleDeletePractice had no try/catch, so
the UI just did nothing.

This test reproduces the failing path: seed a Practice with at least
one Element, then call delete_practice via the production endpoint
handler. Pre-fix it raised IntegrityError; post-fix the Practice and
its Elements are gone.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.advisory.models import (
    Element, Practice, PracticeL0, TimelineFromType,
)
from app.modules.advisory.router import delete_practice
from app.modules.clients.models import ClientUserRole
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_package, make_timeline, make_user,
)


@requires_docker
@pytest.mark.asyncio
async def test_cca_delete_practice_drops_elements(db):
    client = await make_client(db)
    se = await make_user(db, name="SE", skip_auto_link=True)
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    pkg = await make_package(db, client, crop_cosh_id="crop:test")
    tl = await make_timeline(db, pkg, from_type=TimelineFromType.DAS)
    practice = Practice(
        timeline_id=tl.id,
        l0_type=PracticeL0.INPUT,
        l1_type="FERTILIZER",
        l2_type="CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS",
        display_order=0,
    )
    db.add(practice)
    await db.flush()
    db.add(Element(
        practice_id=practice.id, element_type="COMMON_NAME",
        cosh_ref="cn:urea", display_order=0,
    ))
    db.add(Element(
        practice_id=practice.id, element_type="DOSAGE",
        value="2", unit_cosh_id="kg/ha", display_order=1,
    ))
    await db.commit()
    practice_id = practice.id

    # Pre-fix: this raised sqlalchemy.exc.IntegrityError on FK
    # violation; post-fix it returns cleanly.
    await delete_practice(
        client_id=client.id, timeline_id=tl.id, practice_id=practice_id,
        db=db, current_user=se,
    )

    assert (await db.execute(
        select(Practice).where(Practice.id == practice_id)
    )).scalar_one_or_none() is None
    assert (await db.execute(
        select(Element).where(Element.practice_id == practice_id)
    )).scalars().all() == []
