"""FarmPundit CA-portal search — multi-value filters + pending invitations.

Covers HIGH-batch findings H3 + H5 (search-filter completeness, multi-
select semantics) and H2 (pending invitations visible to the CA).

Spec §14.3 Step 1 lists 8 filter fields:
- Multi: Support Area (state), Expertise Domain, Crop Group, Language
- Single: Education, Years of Experience, Support Method, Cultivation Type

Multi-value semantics: a pundit matches if ANY of their tagged values
intersects the query — not all. Empty list = filter not applied.

Spec §14.3 Step 3: invitation must be accepted by the expert before
they're enrolled. Until then, `ClientFarmPundit` has no row — the
listing endpoint surfaces these "in flight" invitations so the CA
isn't blind.
"""
from __future__ import annotations

import pytest

from app.modules.farmpundit.models import (
    ClientFarmPundit, FarmPunditCropGroup, FarmPunditCultivationType,
    FarmPunditExpertise, FarmPunditFarmingMethod, FarmPunditLanguage,
    FarmPunditProfile, FarmPunditSupportArea,
    PunditInvitation, PunditRole,
)
from app.modules.farmpundit.router import (
    list_company_pundit_invitations, search_pundits,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_client_user, make_self_registered_user, make_user,
)


async def _ca_user_for(db, *, client):
    """Seed a CA portal user enrolled at the given client so the
    FarmPundit endpoints' `_assert_portal_member` gate accepts."""
    user = await make_user(db, name=f"CA-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


async def _make_full_pundit(
    db, *, name="Pundit",
    states=(), domains=(), languages=(), crop_groups=(),
    farming_methods=(), cultivation_types=(),
    education_cosh_id=None, experience_cosh_id=None,
):
    """Build a profile + linked rows in a single helper. Every list-arg
    is the cosh_id (or code) string list to attach. Post 2026-05-26
    Cosh reshape: education / experience are single cosh_ids; the
    former single-select support_method + cultivation_type are now
    multi-select junction tables."""
    user = await make_user(db, name=name)
    profile = FarmPunditProfile(
        user_id=user.id, declaration_accepted=True,
        education_cosh_id=education_cosh_id,
        experience_cosh_id=experience_cosh_id,
    )
    db.add(profile)
    await db.flush()

    for s in states:
        db.add(FarmPunditSupportArea(pundit_id=profile.id, state_cosh_id=s))
    for d in domains:
        db.add(FarmPunditExpertise(pundit_id=profile.id, domain=d))
    for lang in languages:
        db.add(FarmPunditLanguage(pundit_id=profile.id, language_code=lang))
    for cg in crop_groups:
        db.add(FarmPunditCropGroup(pundit_id=profile.id, crop_group_cosh_id=cg))
    for fm in farming_methods:
        db.add(FarmPunditFarmingMethod(pundit_id=profile.id, farming_method_cosh_id=fm))
    for ct in cultivation_types:
        db.add(FarmPunditCultivationType(pundit_id=profile.id, cultivation_type_cosh_id=ct))
    await db.flush()
    return user, profile


# ── H3 / H5 — search filter completeness + multi-select ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_search_multi_state_returns_union(db):
    """state_cosh_ids=[A, B] → matches experts who support EITHER A OR B
    (not both)."""
    client = await make_client(db)
    await _make_full_pundit(db, name="K-only",   states=["state_karnataka"])
    await _make_full_pundit(db, name="TN-only",  states=["state_tamil_nadu"])
    await _make_full_pundit(db, name="MH-only",  states=["state_maharashtra"])
    await db.commit()

    results = await search_pundits(
        client_id=client.id,
        state_cosh_ids=["state_karnataka", "state_tamil_nadu"],
        expertise_domains=[], language_codes=[], crop_groups=[],
        farming_methods=[], cultivation_types=[],
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    names = {r["name"] for r in results}
    assert names == {"K-only", "TN-only"}


@requires_docker
@pytest.mark.asyncio
async def test_search_multi_expertise_returns_union(db):
    """expertise_domains multi-select."""
    client = await make_client(db)
    await _make_full_pundit(db, name="Protection",  domains=["plant_protection"])
    await _make_full_pundit(db, name="Nutrition",    domains=["plant_nutrition"])
    await _make_full_pundit(db, name="Agronomy",     domains=["overall_agronomy"])
    await db.commit()

    results = await search_pundits(
        client_id=client.id,
        state_cosh_ids=[],
        expertise_domains=["plant_protection", "plant_nutrition"],
        language_codes=[], crop_groups=[],
        farming_methods=[], cultivation_types=[],
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert {r["name"] for r in results} == {"Protection", "Nutrition"}


@requires_docker
@pytest.mark.asyncio
async def test_search_multi_language_returns_union(db):
    client = await make_client(db)
    await _make_full_pundit(db, name="Kannada-only", languages=["kn"])
    await _make_full_pundit(db, name="Tamil-only",   languages=["ta"])
    await _make_full_pundit(db, name="Hindi-only",   languages=["hi"])
    await db.commit()

    results = await search_pundits(
        client_id=client.id,
        state_cosh_ids=[], expertise_domains=[],
        language_codes=["kn", "ta"],
        crop_groups=[],
        farming_methods=[], cultivation_types=[],
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert {r["name"] for r in results} == {"Kannada-only", "Tamil-only"}


@requires_docker
@pytest.mark.asyncio
async def test_search_multi_crop_group_returns_union(db):
    client = await make_client(db)
    await _make_full_pundit(db, name="Cereals",  crop_groups=["cereals"])
    await _make_full_pundit(db, name="Fruits",   crop_groups=["fruit_trees"])
    await _make_full_pundit(db, name="Oilseeds", crop_groups=["oilseeds"])
    await db.commit()

    results = await search_pundits(
        client_id=client.id,
        state_cosh_ids=[], expertise_domains=[], language_codes=[],
        crop_groups=["cereals", "fruit_trees"],
        farming_methods=[], cultivation_types=[],
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert {r["name"] for r in results} == {"Cereals", "Fruits"}


@requires_docker
@pytest.mark.asyncio
async def test_search_cultivation_type_filter(db):
    """cultivation_types is now multi-select (post-Cosh reshape).
    Filtering by one value returns the pundit who has it tagged."""
    client = await make_client(db)
    await _make_full_pundit(db, name="Open Field", cultivation_types=["ct_open"])
    await _make_full_pundit(db, name="Greenhouse", cultivation_types=["ct_green"])
    await db.commit()

    results = await search_pundits(
        client_id=client.id,
        state_cosh_ids=[], expertise_domains=[], language_codes=[], crop_groups=[],
        farming_methods=[], cultivation_types=["ct_green"],
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert {r["name"] for r in results} == {"Greenhouse"}


@requires_docker
@pytest.mark.asyncio
async def test_search_combines_multi_and_single_filters(db):
    """Multi (state) + single (education) = AND across the two filter
    types, OR within the multi set. Pundit must support Karnataka AND
    have a Doctorate."""
    client = await make_client(db)
    await _make_full_pundit(
        db, name="K + Doc",
        states=["state_karnataka"], education_cosh_id="ed_doctorate",
    )
    await _make_full_pundit(
        db, name="K + Masters",
        states=["state_karnataka"], education_cosh_id="ed_masters",
    )
    await _make_full_pundit(
        db, name="TN + Doc",
        states=["state_tamil_nadu"], education_cosh_id="ed_doctorate",
    )
    await db.commit()

    results = await search_pundits(
        client_id=client.id,
        state_cosh_ids=["state_karnataka"],
        expertise_domains=[], language_codes=[], crop_groups=[],
        farming_methods=[], cultivation_types=[],
        education_cosh_id="ed_doctorate",
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert {r["name"] for r in results} == {"K + Doc"}


@requires_docker
@pytest.mark.asyncio
async def test_search_empty_filters_returns_all_with_declaration(db):
    """No filters at all → everyone with declaration_accepted=True."""
    client = await make_client(db)
    await _make_full_pundit(db, name="A")
    await _make_full_pundit(db, name="B")

    # A profile with declaration_accepted=False is excluded.
    user_c = await make_user(db, name="C")
    db.add(FarmPunditProfile(user_id=user_c.id, declaration_accepted=False))
    await db.commit()

    results = await search_pundits(
        client_id=client.id,
        state_cosh_ids=[], expertise_domains=[], language_codes=[], crop_groups=[],
        farming_methods=[], cultivation_types=[],
        db=db, current_user=await _ca_user_for(db, client=client),
    )
    names = {r["name"] for r in results}
    assert "A" in names and "B" in names and "C" not in names


# ── H2 — pending invitations visible to CA ──────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pending_invitations_listed_with_profile_info(db):
    """CA sees a pending invitation with name/phone/email so the My
    Experts tab can show 'Invitation sent · Pending acceptance'."""
    client = await make_client(db)
    user, profile = await _make_full_pundit(db, name="Invited Expert")
    profile.email = "expert@example.com"
    await db.flush()

    inv = PunditInvitation(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PRIMARY, status="PENDING",
    )
    db.add(inv)
    await db.commit()

    out = await list_company_pundit_invitations(
        client_id=client.id, status="PENDING", db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert len(out) == 1
    assert out[0]["name"] == "Invited Expert"
    assert out[0]["email"] == "expert@example.com"
    assert out[0]["status"] == "PENDING"
    assert out[0]["role"] == "PRIMARY"


@requires_docker
@pytest.mark.asyncio
async def test_pending_invitations_respect_phone_privacy(db):
    """If the expert toggled phone-hidden, the CA-side listing must
    not leak the phone — same rule as the search results."""
    client = await make_client(db)
    user, profile = await _make_full_pundit(db, name="Private Expert")
    profile.phone_hidden = True
    await db.flush()

    db.add(PunditInvitation(
        client_id=client.id, pundit_id=profile.id,
        role=PunditRole.PANEL, status="PENDING",
    ))
    await db.commit()

    out = await list_company_pundit_invitations(
        client_id=client.id, status="PENDING", db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert out[0]["phone"] is None


@requires_docker
@pytest.mark.asyncio
async def test_pending_invitations_excludes_accepted(db):
    """Accepted invitations should NOT appear in the PENDING list —
    the expert is now in `ClientFarmPundit` and surfaces via
    list_company_pundits instead."""
    client = await make_client(db)
    user_a, prof_a = await _make_full_pundit(db, name="Pending")
    user_b, prof_b = await _make_full_pundit(db, name="Accepted")

    db.add(PunditInvitation(
        client_id=client.id, pundit_id=prof_a.id,
        role=PunditRole.PRIMARY, status="PENDING",
    ))
    db.add(PunditInvitation(
        client_id=client.id, pundit_id=prof_b.id,
        role=PunditRole.PRIMARY, status="ACCEPTED",
    ))
    await db.commit()

    out = await list_company_pundit_invitations(
        client_id=client.id, status="PENDING", db=db, current_user=await _ca_user_for(db, client=client),
    )
    assert {r["name"] for r in out} == {"Pending"}


# ── H1 — promoter f-string ──────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_promoter_duplicate_error_renders_promoter_type(db):
    """Re-registering the same person as DEALER for the same client
    must surface 'DEALER' / 'FACILITATOR' in the message, not the
    literal {promoter_type} placeholder."""
    from app.modules.clients.router import register_promoter

    client = await make_client(db)
    sa_user = await make_user(db, name="SA")
    await make_self_registered_user(db, phone="+919900000000", role="DEALER")
    await db.commit()

    payload = {
        "phone": "+919900000000",
        "promoter_type": "DEALER", "territory_notes": None,
    }
    await register_promoter(
        client_id=client.id, request=payload,
        db=db, current_user=sa_user,
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        await register_promoter(
            client_id=client.id, request=payload,
            db=db, current_user=sa_user,
        )
    assert ei.value.status_code == 409
    assert "Dealer" in ei.value.detail


# ── V1.1 Item 3 — FM cannot create users; must self-register first ──────────

@requires_docker
@pytest.mark.asyncio
async def test_register_promoter_rejects_unknown_phone(db):
    """V1.1 Item 3 (2026-05-09): FM no longer creates Users. A phone
    that doesn't match an existing User → structured 422 telling the
    FM to ask the person to self-register first."""
    from app.modules.clients.router import register_promoter
    from fastapi import HTTPException

    client = await make_client(db)
    sa = await make_user(db, name="SA")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await register_promoter(
            client_id=client.id,
            request={
                "phone": "+919999911111",
                "promoter_type": "DEALER",
                "territory_notes": None,
            },
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "user_not_self_registered"


@requires_docker
@pytest.mark.asyncio
async def test_register_promoter_rejects_user_without_role(db):
    """The phone matches an existing User but they haven't claimed
    the DEALER / FACILITATOR role. FM cannot give the role; user
    must do it themselves on the PWA."""
    from app.modules.clients.router import register_promoter
    from fastapi import HTTPException

    client = await make_client(db)
    sa = await make_user(db, name="SA")
    plain_user = await make_user(db, name="Just a Farmer")
    plain_user.phone = "+919900099009"
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await register_promoter(
            client_id=client.id,
            request={
                "phone": "+919900099009",
                "promoter_type": "DEALER",
                "territory_notes": None,
            },
            db=db, current_user=sa,
        )
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "user_lacks_self_claimed_role"
