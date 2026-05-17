"""Batch 39V (2026-05-17) — CA-side QA write guard.

Extends the 39S/39T/39U guard sweep to the QA (StandardResponse)
pipe. All 10 CA-QA mutations now run `_assert_can_edit_client_advisory`
(ClientUser-or-CM-EDIT). Pre-39V they used `_assert_portal_member`
— stricter: ClientUser-only, CM rejected.

Affected sites:
  - 3 SR CRUD endpoints in farmpundit/router.py (create, update,
    delete StandardResponse) — were ClientUser-only via
    `_assert_portal_member`.
  - 7 timeline / practice / element endpoints in advisory/router.py
    (add_qa_timeline, delete_qa_timeline, add_qa_practice,
    add/update/delete_qa_element, delete_qa_practice) — also
    on the stricter gate via cross-module imports of
    `_assert_portal_member`. Those local imports are gone now.

Behaviour change: CM with EDIT rights now has write access to the
QA library (previously the CM could not even create or edit an SR).
Matches the same widening done for `import_pg_into_sp` in 39U and
the architectural intent of the CA-side advisory guard.

Eligibility / rejection rules are identical to CA-CCA / CA-PG /
CA-SP — covered exhaustively in `test_phase_cca_role_guard.py`
against the helper. This file cross-validates:
  (a) `create_standard_response` rejects a stranger (representative
      farmpundit-module mutation).
  (b) `add_qa_timeline` rejects a stranger (representative advisory-
      module mutation).
  (c) `create_standard_response` now accepts a CM-EDIT user — the
      widening proof.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.advisory.router import add_qa_timeline
from app.modules.advisory.schemas import QATimelineCreate
from app.modules.clients.models import (
    CMClientAssignment, CMRights,
)
from app.modules.farmpundit.models import StandardResponse
from app.modules.farmpundit.router import create_standard_response
from app.modules.platform.models import StatusEnum
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


@requires_docker
@pytest.mark.asyncio
async def test_create_standard_response_rejects_stranger(db):
    """An authenticated user with no link to this client gets 403
    on create_standard_response."""
    client = await make_client(db)
    stranger = await make_user(db, name="Stranger", skip_auto_link=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await create_standard_response(
            client_id=client.id,
            data={"question_text": "Why are my leaves yellow?"},
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "ca_edit_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_add_qa_timeline_rejects_stranger(db):
    """Cross-validate the advisory-module QA gate (was the cross-
    module _assert_portal_member import pre-39V)."""
    client = await make_client(db)
    stranger = await make_user(db, name="Stranger", skip_auto_link=True)
    sr = StandardResponse(
        client_id=client.id,
        question_text="Sample question",
    )
    db.add(sr)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await add_qa_timeline(
            client_id=client.id, sr_id=sr.id,
            request=QATimelineCreate(
                name="Week 1", from_type="DAYS_AFTER_RESPONSE",
                from_value=0, to_value=7,
            ),
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "ca_edit_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_create_standard_response_now_accepts_cm_edit(db):
    """Behaviour change in 39V: a CM with EDIT rights but no
    ClientUser row can create an SR. Pre-39V this raised 403
    because the gate was ClientUser-only."""
    client = await make_client(db)
    cm = await make_user(db, name="Ram-CM", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=cm.id, client_id=client.id,
        rights=CMRights.EDIT, status=StatusEnum.ACTIVE,
    ))
    await db.commit()

    out = await create_standard_response(
        client_id=client.id,
        data={"question_text": "Why are my leaves yellow?"},
        db=db, current_user=cm,
    )
    assert out["question_text"] == "Why are my leaves yellow?"
    assert out["client_id"] == client.id
