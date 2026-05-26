"""FarmPundit profile — state cosh_id → friendly name resolution.

2026-05-26 — the FP Register form used to ask the Pundit to type
raw cosh_ids like "state_karnataka" into a text field. The
register flow now picks from a Cosh-backed state dropdown (PWA
companion change). The profile endpoint must resolve each
support area's `state_cosh_id` to its English `state_name` so
the profile screen never leaks UUIDs / cosh_ids to the Pundit.
"""
from __future__ import annotations

import pytest

from app.modules.farmpundit.models import (
    FarmPunditProfile, FarmPunditSupportArea,
)
from app.modules.farmpundit.router import get_pundit_profile_detail
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import make_user


@requires_docker
@pytest.mark.asyncio
async def test_pundit_profile_resolves_state_cosh_ids_to_names(db):
    user = await make_user(db, name="Pundit Test")
    profile = FarmPunditProfile(
        user_id=user.id, email="p@example.com",
        education="MASTERS", experience_band="FROM_5_TO_10",
        support_method="NON_CHEMICAL", declaration_accepted=True,
    )
    db.add(profile)
    await db.flush()

    # Two states; one districtless (the typical Register-flow row),
    # one with a legacy district present (must still resolve).
    db.add(CoshCoreItem(
        cosh_id="ka", core_type="state_list",
        translations={"en": "Karnataka"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="tn", core_type="state_list",
        translations={"en": "Tamil Nadu"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="mysuru", core_type="district_list",
        translations={"en": "Mysuru"}, status="active",
    ))
    db.add(FarmPunditSupportArea(
        pundit_id=profile.id, state_cosh_id="ka", district_cosh_id=None,
    ))
    db.add(FarmPunditSupportArea(
        pundit_id=profile.id, state_cosh_id="tn", district_cosh_id="mysuru",
    ))
    await db.commit()

    out = await get_pundit_profile_detail(db=db, current_user=user)
    areas = {a["state_cosh_id"]: a for a in out["support_areas"]}

    assert areas["ka"]["state_name"] == "Karnataka"
    assert areas["ka"]["district_cosh_id"] is None
    assert areas["ka"]["district_name"] is None

    assert areas["tn"]["state_name"] == "Tamil Nadu"
    assert areas["tn"]["district_cosh_id"] == "mysuru"
    assert areas["tn"]["district_name"] == "Mysuru"


@requires_docker
@pytest.mark.asyncio
async def test_pundit_profile_handles_unknown_state_cosh_id(db):
    """If the Cosh sync hasn't loaded a state yet, state_name comes
    back null rather than crashing — the UI falls back to the raw
    cosh_id."""
    user = await make_user(db, name="Pundit Unknown")
    profile = FarmPunditProfile(
        user_id=user.id, email="u@example.com",
        education="MASTERS", experience_band="FROM_5_TO_10",
        support_method="NON_CHEMICAL", declaration_accepted=True,
    )
    db.add(profile)
    await db.flush()
    db.add(FarmPunditSupportArea(
        pundit_id=profile.id, state_cosh_id="ghost_state",
        district_cosh_id=None,
    ))
    await db.commit()

    out = await get_pundit_profile_detail(db=db, current_user=user)
    assert out["support_areas"][0]["state_cosh_id"] == "ghost_state"
    assert out["support_areas"][0]["state_name"] is None
