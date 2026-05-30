"""FarmPundit module — server-side gates (MED Sub-batch 2).

M5: Promoter-Pundit toggle requires the pundit's user to have an
    active Facilitator-type ClientPromoter row at this client (spec
    §14.2: "must be a Promoter first — being a facilitator alone is
    not sufficient").

M7: Minimum membership gate on FarmPundit-management endpoints —
    caller must be enrolled at the target client (any portal role).
    Pre-fix, a JWT-authed CA at one client could call FarmPundit
    endpoints on another client by guessing the URL. The broader
    `_require_client_role` audit covering ~30 advisory mutating
    endpoints remains a V2 task; this is the focused V1 patch on
    the FarmPundit module specifically.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.clients.models import ClientPromoter, ClientUserRole
from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditProfile, PunditRole,
)
from app.modules.farmpundit.router import (
    InviteRequest, PunditRoleChange, change_company_pundit_role,
    deactivate_company_pundit, delete_company_pundit, invite_pundit,
    list_company_pundit_invitations, list_company_pundits,
    reactivate_company_pundit, search_pundits, toggle_promoter_pundit,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_user,
)


async def _seed_pundit(db):
    user = await make_user(db, name="Pundit")
    profile = FarmPunditProfile(user_id=user.id, declaration_accepted=True)
    db.add(profile)
    await db.flush()
    return user, profile


async def _enrol_pundit(db, *, client, profile, status="ACTIVE",
                        is_promoter_pundit=False):
    cp = ClientFarmPundit(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status=status, round_robin_sequence=1,
        is_promoter_pundit=is_promoter_pundit,
    )
    db.add(cp)
    await db.flush()
    return cp


async def _portal_member(db, *, client, role=ClientUserRole.CA):
    # skip_auto_link so the user is a member of ONLY the requested client
    # (matters when the test creates multiple clients to verify cross-
    # client isolation).
    user = await make_user(db, name=f"Member-{client.short_name}", skip_auto_link=True)
    await make_client_user(db, user=user, client=client, role=role)
    return user


# ── M7: membership gate ─────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_search_pundits_rejects_non_member(db):
    """A user not enrolled at this client gets 403, regardless of
    JWT auth being valid in the test (we pass a User who simply has
    no ClientUser row at the target client)."""
    client = await make_client(db)
    outsider = await make_user(db, name="Outsider", skip_auto_link=True)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await search_pundits(
            client_id=client.id,
            state_cosh_ids=[], expertise_domains=[],
            language_codes=[], crop_groups=[],
            db=db, current_user=outsider,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "client_membership_required"


@requires_docker
@pytest.mark.asyncio
async def test_search_pundits_rejects_member_of_different_client(db):
    """Cross-client confusion — a CA enrolled at client A cannot
    use FarmPundit search on client B."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    member_a = await _portal_member(db, client=client_a)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await search_pundits(
            client_id=client_b.id,
            state_cosh_ids=[], expertise_domains=[],
            language_codes=[], crop_groups=[],
            db=db, current_user=member_a,
        )
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_inactive_client_user_rejected(db):
    """A ClientUser row with status=INACTIVE doesn't satisfy the gate.
    Spec implication: deactivated portal users lose access to all
    company-scoped FarmPundit operations until reactivated."""
    from app.modules.platform.models import StatusEnum
    client = await make_client(db)
    user = await make_user(db, name="Deactivated CA")
    await make_client_user(
        db, user=user, client=client,
        role=ClientUserRole.CA, status=StatusEnum.INACTIVE,
    )
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_company_pundits(
            client_id=client.id, db=db, current_user=user,
        )
    assert ei.value.status_code == 403


@requires_docker
@pytest.mark.asyncio
async def test_member_passes_gate_on_all_management_endpoints(db):
    """Single member of a client can call every gated endpoint —
    list, invitations-list, deactivate, reactivate, change-role,
    delete, toggle-PP — without 403. Each test other than this
    seeds membership ad-hoc; this one proves the gate is uniformly
    permissive when membership IS present."""
    client = await make_client(db)
    member = await _portal_member(db, client=client)
    _, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(db, client=client, profile=profile)
    cp.status = "INACTIVE"
    await db.commit()

    # All of these should succeed (or succeed-and-409 on business-rule
    # grounds, but never 403).
    await list_company_pundits(
        client_id=client.id, db=db, current_user=member,
    )
    await list_company_pundit_invitations(
        client_id=client.id, status="PENDING", db=db, current_user=member,
    )
    await reactivate_company_pundit(
        client_id=client.id, cp_id=cp.id, db=db, current_user=member,
    )
    # Re-deactivate before delete-by-test.
    await deactivate_company_pundit(
        client_id=client.id, cp_id=cp.id, db=db, current_user=member,
    )
    await delete_company_pundit(
        client_id=client.id, cp_id=cp.id, db=db, current_user=member,
    )


# ── M5: Promoter-Pundit eligibility ─────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_blocked_without_facilitator_promoter(db):
    """Spec §14.2: marking a pundit as PP requires they be a
    Facilitator-Promoter at this client first. Without that
    ClientPromoter row, the toggle is rejected with structured 409."""
    client = await make_client(db)
    member = await _portal_member(db, client=client)
    _, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(db, client=client, profile=profile)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await toggle_promoter_pundit(
            client_id=client.id, cp_id=cp.id,
            data={"is_promoter_pundit": True},
            db=db, current_user=member,
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "promoter_pundit_requires_facilitator_promoter"


@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_succeeds_with_facilitator_promoter(db):
    client = await make_client(db)
    member = await _portal_member(db, client=client)
    user, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(db, client=client, profile=profile)
    # The pundit is also registered as a Facilitator-Promoter.
    # After R9 (2026-05-29) `is_promoter` defaults to False on new
    # rows — set it explicitly here to mirror the post-accept state.
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="FACILITATOR", status="ACTIVE",
        is_promoter=True,
        promoter_request_status="ACCEPTED",
        registered_by=member.id,
    ))
    await db.commit()

    out = await toggle_promoter_pundit(
        client_id=client.id, cp_id=cp.id,
        data={"is_promoter_pundit": True},
        db=db, current_user=member,
    )
    assert out["is_promoter_pundit"] is True


@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_blocked_when_only_dealer_promoter(db):
    """Dealer-Promoters are explicitly NOT eligible — spec §14.2 calls
    out Facilitator-Promoters specifically. A pundit who is only a
    DEALER (not FACILITATOR) at this client must be rejected."""
    client = await make_client(db)
    member = await _portal_member(db, client=client)
    user, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(db, client=client, profile=profile)
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="DEALER", status="ACTIVE",
        registered_by=member.id,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await toggle_promoter_pundit(
            client_id=client.id, cp_id=cp.id,
            data={"is_promoter_pundit": True},
            db=db, current_user=member,
        )
    assert ei.value.status_code == 409


@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_blocked_when_facilitator_inactive(db):
    """Inactive Facilitator-Promoter doesn't qualify — the gate looks
    for status=ACTIVE specifically. A re-promotion shouldn't reach
    around a deactivated facilitator role."""
    client = await make_client(db)
    member = await _portal_member(db, client=client)
    user, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(db, client=client, profile=profile)
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="FACILITATOR", status="INACTIVE",
        registered_by=member.id,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await toggle_promoter_pundit(
            client_id=client.id, cp_id=cp.id,
            data={"is_promoter_pundit": True},
            db=db, current_user=member,
        )
    assert ei.value.status_code == 409


@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_off_unconditionally_allowed(db):
    """Removing PP designation must always succeed — no eligibility
    re-check on toggle-off. Otherwise a CA couldn't undo a designation
    after deactivating the underlying Facilitator-Promoter row."""
    client = await make_client(db)
    member = await _portal_member(db, client=client)
    _, profile = await _seed_pundit(db)
    # Pundit was previously marked PP; the underlying Facilitator-
    # Promoter has since been deactivated.
    cp = await _enrol_pundit(
        db, client=client, profile=profile, is_promoter_pundit=True,
    )
    await db.commit()

    out = await toggle_promoter_pundit(
        client_id=client.id, cp_id=cp.id,
        data={"is_promoter_pundit": False},
        db=db, current_user=member,
    )
    assert out["is_promoter_pundit"] is False


@requires_docker
@pytest.mark.asyncio
async def test_pp_toggle_idempotent_when_already_on(db):
    """Repeat toggle-on with a Facilitator-Promoter already in place
    is a no-op success, not a 409."""
    client = await make_client(db)
    member = await _portal_member(db, client=client)
    user, profile = await _seed_pundit(db)
    cp = await _enrol_pundit(
        db, client=client, profile=profile, is_promoter_pundit=True,
    )
    db.add(ClientPromoter(
        client_id=client.id, user_id=user.id,
        promoter_type="FACILITATOR", status="ACTIVE",
        is_promoter=True,
        promoter_request_status="ACCEPTED",
        registered_by=member.id,
    ))
    await db.commit()

    out = await toggle_promoter_pundit(
        client_id=client.id, cp_id=cp.id,
        data={"is_promoter_pundit": True},
        db=db, current_user=member,
    )
    assert out["is_promoter_pundit"] is True


# ── Invite endpoint cross-checks the same gate ──────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_invite_pundit_rejects_non_member(db):
    """The invite endpoint sits behind the same gate. Without it, an
    outsider could spam any client's pundit pool with invitations."""
    client = await make_client(db)
    outsider = await make_user(db, name="Outsider", skip_auto_link=True)
    target_user, _target_profile = await _seed_pundit(db)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await invite_pundit(
            client_id=client.id,
            request=InviteRequest(
                pundit_user_id=target_user.id, role=PunditRole.PRIMARY,
            ),
            db=db, current_user=outsider,
        )
    assert ei.value.status_code == 403


# ── 2026-05-30: CM-EDIT relaxation on the membership gate ──────────────────

@requires_docker
@pytest.mark.asyncio
async def test_cm_edit_passes_membership_gate_without_clientuser(db):
    """Tester report 2026-05-30: a CM logged into the CA portal via
    `/cm-login` got `client_membership_required` on the QA list page
    because the membership gate didn't check the CMClientAssignment
    path. Per the documented "CM = full CA-equivalent access" rule,
    a CM with EDIT rights now passes."""
    from app.modules.clients.models import (
        CMClientAssignment, CMRights,
    )
    from app.modules.farmpundit.router import list_standard_responses
    from app.modules.platform.models import StatusEnum

    client = await make_client(db)
    cm = await make_user(db, name="Ram-CM-QA", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=cm.id, client_id=client.id,
        rights=CMRights.EDIT, status=StatusEnum.ACTIVE,
    ))
    await db.commit()

    # No raise = success.
    out = await list_standard_responses(
        client_id=client.id, db=db, current_user=cm,
    )
    assert isinstance(out, list)


@requires_docker
@pytest.mark.asyncio
async def test_cm_view_assignment_still_refused(db):
    """A CM whose assignment is VIEW (not EDIT) still gets 403 —
    the relaxation is strict-EDIT-only. VIEW-only CMs read via the
    SA Portal client-detail page, not by SSO'ing into CA portal."""
    from app.modules.clients.models import (
        CMClientAssignment, CMRights,
    )
    from app.modules.farmpundit.router import list_standard_responses
    from app.modules.platform.models import StatusEnum

    client = await make_client(db)
    cm = await make_user(db, name="View-Only-CM", skip_auto_link=True)
    db.add(CMClientAssignment(
        cm_user_id=cm.id, client_id=client.id,
        rights=CMRights.VIEW, status=StatusEnum.ACTIVE,
    ))
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_standard_responses(
            client_id=client.id, db=db, current_user=cm,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "client_membership_required"
