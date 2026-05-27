"""FarmPundit registration — Cosh-reshape coverage (2026-05-26).

Pins three things for the rewritten register form:

1. `GET /cosh/pundit-options?slug=...` returns the Cosh-backed
   dropdown options for an allowed slug; refuses anything else with
   `unknown_pundit_slug`.
2. `POST /pundit/profile` accepts the new payload shape — single-
   select cosh_ids (education / experience), two new multi-select
   junctions (farming_methods, cultivation_types), and the
   employment branch flag.
3. `GET /pundit/profile` returns every cosh_id alongside its
   resolved English name, with the employment branch invariant
   intact (employed → org_type set, non_employed → kind allowed).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.modules.farmpundit.models import FarmPunditProfile
from app.modules.farmpundit.router import (
    PunditProfileCreate, cosh_pundit_options,
    create_pundit_profile, get_pundit_profile_detail,
)
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import make_user


async def _seed_cosh_pundit_cores(db):
    """Minimal Cosh seed for the 8 pundit_* slugs + a state. Real
    values are larger — three per slug is enough to assert sort +
    resolution behaviour."""
    rows = [
        ("ed_doc",       "pundit_education",         "Doctorate"),
        ("ed_mas",       "pundit_education",         "Masters"),
        ("exp_5_10",     "pundit_experience",        "5 to 10 years"),
        ("fm_org",       "pundit_farming_methods",   "Organic Farming"),
        ("fm_conv",      "pundit_farming_methods",   "Conventional Farming"),
        ("ct_open",      "pundit_cultivation_types", "Open cultivation"),
        ("ct_green",     "pundit_cultivation_types", "Polyhouse cultivation"),
        ("de_prot",      "pundit_domain_expertise",  "Plant Protection"),
        ("cg_cereals",   "pundit_crop_groups",       "Cereals"),
        ("lang_kn",      "pundit_languages",         "Kannada"),
        ("org_kvk",      "pundit_organization_types", "Agricultural University / KVK"),
        ("state_ka",     "state_list",               "Karnataka"),
    ]
    for cosh_id, core_type, name_en in rows:
        db.add(CoshCoreItem(
            cosh_id=cosh_id, core_type=core_type,
            translations={"en": name_en}, status="active",
        ))
    await db.flush()


# ── /cosh/pundit-options ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pundit_options_returns_sorted_named_list(db):
    user = await make_user(db)
    await _seed_cosh_pundit_cores(db)
    await db.commit()

    out = await cosh_pundit_options(
        slug="pundit_farming_methods", db=db, current_user=user,
    )
    # Sorted by English name (Conventional < Organic).
    assert [o["cosh_id"] for o in out] == ["fm_conv", "fm_org"]
    assert out[0] == {"cosh_id": "fm_conv", "name": "Conventional Farming"}


@requires_docker
@pytest.mark.asyncio
async def test_pundit_options_refuses_unknown_slug(db):
    user = await make_user(db)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await cosh_pundit_options(
            slug="biological_names", db=db, current_user=user,
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "unknown_pundit_slug"


# ── /pundit/profile POST + GET round-trip ───────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_register_employed_round_trip(db):
    """Employed Pundit: organisation_type_cosh_id is set, non_employed_kind
    is NULL. Every cosh_id comes back with its English name."""
    user = await make_user(db, name="Reg Pundit")
    await _seed_cosh_pundit_cores(db)
    await db.commit()

    payload = PunditProfileCreate(
        email="r@example.com",
        education_cosh_id="ed_doc",
        experience_cosh_id="exp_5_10",
        is_employed_by_organization=True,
        organisation_type_cosh_id="org_kvk",
        non_employed_kind=None,
        declaration_accepted=True,
        farming_methods=["fm_org", "fm_conv"],
        cultivation_types=["ct_green"],
        expertise_domains=["de_prot"],
        crop_groups=["cg_cereals"],
        languages=["lang_kn"],
        support_areas=[{"state_cosh_id": "state_ka"}],
    )
    await create_pundit_profile(request=payload, db=db, current_user=user)

    out = await get_pundit_profile_detail(db=db, current_user=user)
    assert out["phone"] == user.phone
    assert out["education"] == {"cosh_id": "ed_doc", "name": "Doctorate"}
    assert out["experience"] == {"cosh_id": "exp_5_10", "name": "5 to 10 years"}
    assert out["is_employed_by_organization"] is True
    assert out["organisation_type"]["cosh_id"] == "org_kvk"
    assert out["organisation_type"]["name"] == "Agricultural University / KVK"
    assert out["non_employed_kind"] is None

    fm_names = {fm["name"] for fm in out["farming_methods"]}
    assert fm_names == {"Organic Farming", "Conventional Farming"}
    assert out["cultivation_types"][0]["name"] == "Polyhouse cultivation"
    assert out["expertise_domains"][0]["name"] == "Plant Protection"
    assert out["crop_groups"][0]["name"] == "Cereals"
    assert out["languages"][0]["name"] == "Kannada"
    assert out["support_areas"][0]["state_name"] == "Karnataka"
    # No legacy district written from the register flow.
    assert out["support_areas"][0]["district_cosh_id"] is None


@requires_docker
@pytest.mark.asyncio
async def test_register_non_employed_with_retired_kind(db):
    """Non-employed Pundit with non_employed_kind=RETIRED. The
    employment-branch invariant clears organisation_type_cosh_id even
    if the caller mistakenly sent one."""
    user = await make_user(db, name="Retired Pundit")
    await _seed_cosh_pundit_cores(db)
    await db.commit()

    payload = PunditProfileCreate(
        education_cosh_id="ed_doc",
        is_employed_by_organization=False,
        organisation_type_cosh_id="org_kvk",   # should be discarded
        non_employed_kind="RETIRED",
        declaration_accepted=True,
    )
    await create_pundit_profile(request=payload, db=db, current_user=user)
    out = await get_pundit_profile_detail(db=db, current_user=user)
    assert out["is_employed_by_organization"] is False
    assert out["organisation_type"] is None
    assert out["non_employed_kind"] == "RETIRED"


@requires_docker
@pytest.mark.asyncio
async def test_register_accepts_uuid_shaped_language_cosh_id(db):
    """Regression — `farm_pundit_languages.language_code` was originally
    VARCHAR(10) for ISO codes. After the Cosh reshape (2026-05-26) it
    stores a 36-char Cosh UUID. The first real-world register submit
    hit "value too long for type character varying(10)" because tests
    used short codes like 'lang_kn' that fit. Column widened in
    migration b3e4f7a52d11; this test pins the new shape."""
    user = await make_user(db, name="Lang UUID Pundit")
    await _seed_cosh_pundit_cores(db)
    uuid_lang = "11111111-2222-4333-8444-555555555555"
    db.add(CoshCoreItem(
        cosh_id=uuid_lang, core_type="pundit_languages",
        translations={"en": "Tamil"}, status="active",
    ))
    await db.commit()

    payload = PunditProfileCreate(
        education_cosh_id="ed_doc",
        is_employed_by_organization=False,
        declaration_accepted=True,
        languages=[uuid_lang],
    )
    await create_pundit_profile(request=payload, db=db, current_user=user)
    out = await get_pundit_profile_detail(db=db, current_user=user)
    assert out["languages"] == [{"cosh_id": uuid_lang, "name": "Tamil"}]


@requires_docker
@pytest.mark.asyncio
async def test_register_rejects_unknown_non_employed_kind(db):
    user = await make_user(db, name="Bad Kind")
    await _seed_cosh_pundit_cores(db)
    await db.commit()

    payload = PunditProfileCreate(
        education_cosh_id="ed_doc",
        is_employed_by_organization=False,
        non_employed_kind="ALIEN",
        declaration_accepted=True,
    )
    with pytest.raises(HTTPException) as exc:
        await create_pundit_profile(request=payload, db=db, current_user=user)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_non_employed_kind"
