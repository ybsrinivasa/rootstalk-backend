"""Batch 39T (2026-05-17) — CA-side CHA-PG write guard.

Extends Batch 39S's CA-CCA guard sweep to the nine CA-side CHA-PG
mutations (the tenth, `import_global_pg`, is already protected by
`_assert_cm_can_edit_client`, the CM-EDIT-only Global→Local gate).

Eligibility / rejection rules are identical to CA-CCA and tested
exhaustively in `test_phase_cca_role_guard.py` against the helper
directly. This file cross-validates one representative CA-PG mutation
(`create_client_pg`) end-to-end, proving the guard fires before any
PG-specific business logic (the `area_or_plant` and `is_known_problem_group`
validators).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.advisory.router import create_client_pg
from app.modules.advisory.schemas import PGRecommendationCreate
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


@requires_docker
@pytest.mark.asyncio
async def test_create_client_pg_rejects_stranger_before_pg_check(db):
    """Cross-validation: the guard runs BEFORE `is_known_problem_group`.
    Pre-guard a stranger could probe the PG validator with arbitrary
    cosh_ids by getting 422 errors. Now they get 403 upfront."""
    client = await make_client(db)
    stranger = await make_user(db, name="Stranger", skip_auto_link=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await create_client_pg(
            client_id=client.id,
            request=PGRecommendationCreate(
                problem_group_cosh_id="pg:does-not-exist",
                area_or_plant="AREA_WISE",
            ),
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "cca_edit_forbidden"


@requires_docker
@pytest.mark.asyncio
async def test_create_client_pg_rejects_stranger_before_bundle_check(db):
    """The guard also runs BEFORE the `area_or_plant` validator. A
    stranger sending a malformed bundle gets 403, not a 422 that leaks
    the validator's existence."""
    client = await make_client(db)
    stranger = await make_user(db, name="Stranger", skip_auto_link=True)
    await db.commit()
    with pytest.raises(HTTPException) as ei:
        await create_client_pg(
            client_id=client.id,
            request=PGRecommendationCreate(
                problem_group_cosh_id="pg:fungal_diseases",
                area_or_plant=None,
            ),
            db=db, current_user=stranger,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "cca_edit_forbidden"
