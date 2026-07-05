from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Query as QueryParam  # avoid clash with farmpundit.Query model
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.farmpundit.models import (
    FarmPunditProfile, FarmPunditExpertise, FarmPunditSupportArea,
    FarmPunditLanguage, FarmPunditCropGroup, FarmPunditPreference,
    FarmPunditFarmingMethod, FarmPunditCultivationType,
    ClientFarmPundit, PunditInvitation, PunditRole,
    Query, QueryMedia, QueryRemark, QueryResponse, QueryResponseMedia,
    StandardResponse, QueryStatus, QueryRemarkAction,
)
from app.services.i18n_cosh import pick_translation
from app.services.bl12_query_routing import route_query, ExpertSlot
from app.services.bl12_query_state import (
    PANEL as BL12_PANEL, PRIMARY as BL12_PRIMARY,
    PROMOTER_PUNDIT as BL12_PROMOTER_PUNDIT,
    can_reject as bl12_can_reject,
    validate_transition as validate_query_transition,
)
from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.modules.advisory.router import (
    _assert_can_edit_client_advisory,
    _assert_can_publish_client_advisory,
)

router = APIRouter(tags=["FarmPundit"])


def _raise_query_transition(res, status_code: int = 400) -> None:
    """Convert a TransitionResult.allowed=False into an HTTPException
    carrying the stable error_code in the detail payload."""
    raise HTTPException(
        status_code=status_code,
        detail={"error_code": res.error_code, "message": res.message},
    )


async def _holder_role(
    db: AsyncSession, profile: FarmPunditProfile, query: Query,
) -> str:
    """Return the holder's role on this query's client, as the BL-12
    state-machine vocabulary expects ('PRIMARY' | 'PANEL' |
    'PROMOTER_PUNDIT'). Raises 403 if the pundit isn't holding the
    query at all.

    2026-06-23 — PROMOTER_PUNDIT became a first-class PunditRole
    (migration `b8e4a72f3019`). Pre-fix this function collapsed any
    non-PRIMARY into PANEL, so a PP holding a NEW query failed the
    forward transition (PANEL cannot forward) with a 400. The BL-12
    table now treats PP as a forward-capable role; this lookup
    surfaces it correctly.
    """
    if query.current_holder_id != profile.id:
        raise HTTPException(
            status_code=403,
            detail="You are not the current holder of this query.",
        )
    holder_slot = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == query.client_id,
            ClientFarmPundit.pundit_id == profile.id,
        )
    )).scalar_one_or_none()
    if not holder_slot:
        raise HTTPException(
            status_code=403,
            detail="You are not enrolled with this company.",
        )
    role = holder_slot.role.value if hasattr(holder_slot.role, "value") else str(holder_slot.role)
    if role == "PRIMARY":
        return BL12_PRIMARY
    if role == "PROMOTER_PUNDIT":
        return BL12_PROMOTER_PUNDIT
    return BL12_PANEL

# Expert response window. "2 days, leaving the date of submission"
# per user direction 2026-05-27 — submitted on Day 0, expert has all
# of Day 1 + Day 2 to respond, expires at end of Day 2 (IST). The
# calculation lives in `_compute_query_expiry`.
QUERY_EXPIRE_DAYS = 2

# Per-(farmer, client) free quota. The 7th and later queries cost
# `settings.query_amount_paise` (₹20 prod, ₹1 testing via .env). Single
# source of truth lives in `app.config.settings.query_amount_paise`.
FREE_QUERIES_PER_COMPANY = 6
# Helper to keep call sites tight. Reads settings at call time so an
# env edit + restart picks up cleanly (no module-load capture).
def _query_paid_price_paise() -> int:
    from app.config import settings as _s
    return _s.query_amount_paise

# IST offset for the end-of-Day-2 expiry calc. The farmer's calendar
# day is what matters here, not UTC — a query submitted just before
# midnight IST should still get all of "tomorrow IST" plus the day
# after, not lose an effective day to UTC bucketing.
from datetime import time as _dtime
_IST = timezone(timedelta(hours=5, minutes=30))


def _compute_query_expiry(now_utc: datetime) -> datetime:
    """end-of-Day-2 (IST) for a query submitted at `now_utc`.

    Day 0 = the IST calendar day of submission; the expert has Day 1
    and Day 2 to respond; expires at the last second of Day 2.
    Stored as UTC.
    """
    ist_today = now_utc.astimezone(_IST).date()
    ist_expiry = datetime.combine(
        ist_today + timedelta(days=QUERY_EXPIRE_DAYS),
        _dtime(23, 59, 59),
        tzinfo=_IST,
    )
    return ist_expiry.astimezone(timezone.utc)


# Allowlisted Cosh slugs the /cosh/query-types endpoint will serve.
# Same one-route pattern as /cosh/pundit-options.
QUERY_OPTION_SLUGS: frozenset[str] = frozenset({"query_types"})


async def _assert_portal_member(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Membership gate on FarmPundit-management endpoints — also
    used by SR list, queries list, and Pundit roster CRUD.

    Accepts (either is sufficient):
      1. ACTIVE ClientUser of this client (any role: CA, SE, FM, etc).
      2. ACTIVE CMClientAssignment with EDIT rights on this client.

    The CM-EDIT path was added 2026-05-30 after a tester reported
    the QA pages 403'd on the strict-membership gate. Per the
    documented rule "the CM has all the privileges inside the
    Client — that of the CA, Subject Experts, and all other roles",
    the membership check is now CM-EDIT-permissive. The Global →
    Local pipe still uses its own `cm_assignment_required` shape
    upstream; this only relaxes the CA-portal-facing endpoints in
    this module.

    Stable error code `client_membership_required` unchanged.
    """
    from app.modules.clients.models import (
        CMClientAssignment, CMRights, ClientUser,
    )
    from app.modules.platform.models import StatusEnum

    enrolled = (await db.execute(
        select(ClientUser.id).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if enrolled is not None:
        return

    cm_edit = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user_id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm_edit is not None:
        return

    raise HTTPException(
        status_code=403,
        detail={
            "code": "client_membership_required",
            "message": (
                "Only portal users enrolled at this client may "
                "perform this action."
            ),
        },
    )


# ── FarmPundit Profile ─────────────────────────────────────────────────────────

# Slugs (Cosh `core_type` values) that drive every dropdown on the
# /pundit/register form. Listed here so /cosh/pundit-options can
# refuse anything outside this allowlist (the endpoint is just
# `/cosh/core-items` with an allowlist filter; we never expose the
# generic lookup to PWA users).
PUNDIT_OPTION_SLUGS: frozenset[str] = frozenset({
    "pundit_education",
    "pundit_experience",
    "pundit_farming_methods",
    "pundit_cultivation_types",
    "pundit_domain_expertise",
    "pundit_crop_groups",
    "pundit_languages",
    "pundit_organization_types",
})

NON_EMPLOYED_KINDS: frozenset[str] = frozenset({"RETIRED", "EXPERIENCED_FARMER"})


class PunditProfileCreate(BaseModel):
    email: Optional[str] = None
    # Single-select cosh_id picks
    education_cosh_id: Optional[str] = None
    experience_cosh_id: Optional[str] = None
    # Employment branch
    is_employed_by_organization: bool = False
    organisation_type_cosh_id: Optional[str] = None    # only set when employed
    non_employed_kind: Optional[str] = None             # RETIRED | EXPERIENCED_FARMER, optional
    declaration_accepted: bool = False
    # Multi-select cosh_id lists
    farming_methods: list[str] = []        # pundit_farming_methods cosh_ids
    cultivation_types: list[str] = []      # pundit_cultivation_types cosh_ids
    expertise_domains: list[str] = []      # pundit_domain_expertise cosh_ids
    crop_groups: list[str] = []            # pundit_crop_groups cosh_ids
    languages: list[str] = []              # pundit_languages cosh_ids
    support_areas: list[dict] = []         # [{"state_cosh_id": ...}]


@router.get("/cosh/pundit-options")
async def cosh_pundit_options(
    slug: str = QueryParam(..., description="Cosh core_type slug"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dropdown options for the /pundit/register form.

    Returns `[{cosh_id, name}]` sorted by English name for the
    requested `core_type` slug. The slug must be one of
    PUNDIT_OPTION_SLUGS — we don't expose a generic Cosh-core
    lookup to PWA users.

    Items missing an English translation (Cosh sync partial) are
    skipped silently rather than rendered as "(unnamed)" — the form
    is rendering a picker, not an audit view.
    """
    if slug not in PUNDIT_OPTION_SLUGS:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "unknown_pundit_slug",
                "message": f"slug must be one of {sorted(PUNDIT_OPTION_SLUGS)}",
            },
        )
    from app.modules.sync.models import CoshCoreItem
    rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
            CoshCoreItem.core_type == slug,
            CoshCoreItem.status == "active",
        )
    )).all()
    lang = current_user.language_code or "en"
    out = []
    for cosh_id, translations in rows:
        name = (
            pick_translation(translations, lang, "")
            if isinstance(translations, dict) else None
        )
        if name:
            out.append({"cosh_id": cosh_id, "name": name})
    out.sort(key=lambda x: x["name"].casefold())
    return out


@router.post("/pundit/profile", status_code=201)
async def create_pundit_profile(
    request: PunditProfileCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Regular FarmPundit self-registration.

    Promoter-Pundits never reach this endpoint — they're designated
    through the CA portal Promoter UI and skip these fields entirely.
    """
    existing = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == current_user.id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Profile already exists. Use PUT to update.")

    # Enforce the employment-branch invariant. The Pydantic shape allows
    # both fields nullable so partial drafts validate; the actual rule
    # is "exactly one branch carries data". User direction 2026-05-26:
    # we only ever surface the latest answer to clients — no toggle
    # history — so we just store whichever branch the form filled in.
    if request.is_employed_by_organization:
        org_type = request.organisation_type_cosh_id
        non_emp_kind = None
    else:
        org_type = None
        non_emp_kind = request.non_employed_kind
        if non_emp_kind and non_emp_kind not in NON_EMPLOYED_KINDS:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_non_employed_kind",
                    "message": (
                        f"non_employed_kind must be one of {sorted(NON_EMPLOYED_KINDS)} "
                        f"or null"
                    ),
                },
            )

    profile = FarmPunditProfile(
        user_id=current_user.id,
        email=request.email,
        education_cosh_id=request.education_cosh_id,
        experience_cosh_id=request.experience_cosh_id,
        is_employed_by_organization=request.is_employed_by_organization,
        organisation_type_cosh_id=org_type,
        non_employed_kind=non_emp_kind,
        declaration_accepted=request.declaration_accepted,
    )
    db.add(profile)
    await db.flush()

    for cosh_id in request.farming_methods:
        db.add(FarmPunditFarmingMethod(pundit_id=profile.id, farming_method_cosh_id=cosh_id))
    for cosh_id in request.cultivation_types:
        db.add(FarmPunditCultivationType(pundit_id=profile.id, cultivation_type_cosh_id=cosh_id))
    for domain in request.expertise_domains:
        db.add(FarmPunditExpertise(pundit_id=profile.id, domain=domain))
    for area in request.support_areas:
        # Pundits register at state granularity only — drop any keys the
        # form might still send (e.g. legacy district_cosh_id from older
        # PWA builds), and skip empty rows.
        state = (area or {}).get("state_cosh_id")
        if state:
            db.add(FarmPunditSupportArea(pundit_id=profile.id, state_cosh_id=state))
    for lang in request.languages:
        db.add(FarmPunditLanguage(pundit_id=profile.id, language_code=lang))
    for cg in request.crop_groups:
        db.add(FarmPunditCropGroup(pundit_id=profile.id, crop_group_cosh_id=cg))

    # Add FARM_PUNDIT role to user
    from app.modules.platform.models import UserRole, RoleType
    existing_role = (await db.execute(
        select(UserRole).where(UserRole.user_id == current_user.id, UserRole.role_type == RoleType.FARM_PUNDIT)
    )).scalar_one_or_none()
    if not existing_role:
        db.add(UserRole(user_id=current_user.id, role_type=RoleType.FARM_PUNDIT))

    await db.commit()
    await db.refresh(profile)
    return {"id": profile.id, "user_id": profile.user_id, "declaration_accepted": profile.declaration_accepted}


@router.get("/pundit/profile")
async def get_pundit_profile_detail(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await _get_pundit_profile(db, current_user.id)
    domains = (await db.execute(
        select(FarmPunditExpertise).where(FarmPunditExpertise.pundit_id == profile.id)
    )).scalars().all()
    areas = (await db.execute(
        select(FarmPunditSupportArea).where(FarmPunditSupportArea.pundit_id == profile.id)
    )).scalars().all()
    langs = (await db.execute(
        select(FarmPunditLanguage).where(FarmPunditLanguage.pundit_id == profile.id)
    )).scalars().all()
    crop_groups = (await db.execute(
        select(FarmPunditCropGroup).where(FarmPunditCropGroup.pundit_id == profile.id)
    )).scalars().all()
    farming_methods = (await db.execute(
        select(FarmPunditFarmingMethod).where(FarmPunditFarmingMethod.pundit_id == profile.id)
    )).scalars().all()
    cultivation_types = (await db.execute(
        select(FarmPunditCultivationType).where(FarmPunditCultivationType.pundit_id == profile.id)
    )).scalars().all()
    companies = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.pundit_id == profile.id, ClientFarmPundit.status == "ACTIVE")
    )).scalars().all()

    # One batch lookup against cosh_core_items to resolve every cosh_id
    # (single-select picks + multi-select lists + state list) into an
    # English label. The Pundit's profile screen never sees UUIDs.
    from app.modules.sync.models import CoshCoreItem
    ref_ids: set[str] = set()
    for sid in (
        profile.education_cosh_id, profile.experience_cosh_id,
        profile.organisation_type_cosh_id,
    ):
        if sid:
            ref_ids.add(sid)
    for a in areas:
        if a.state_cosh_id: ref_ids.add(a.state_cosh_id)
        if a.district_cosh_id: ref_ids.add(a.district_cosh_id)
    for fm in farming_methods: ref_ids.add(fm.farming_method_cosh_id)
    for ct in cultivation_types: ref_ids.add(ct.cultivation_type_cosh_id)
    for d in domains: ref_ids.add(d.domain)
    for cg in crop_groups: ref_ids.add(cg.crop_group_cosh_id)
    for l in langs: ref_ids.add(l.language_code)

    name_by_cosh_id: dict[str, str] = {}
    if ref_ids:
        for cosh_id, translations in (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(ref_ids))
        )).all():
            if isinstance(translations, dict):
                label = pick_translation(translations, current_user.language_code or "en", "")
                if label:
                    name_by_cosh_id[cosh_id] = label

    def _named(cosh_id: Optional[str]) -> Optional[dict]:
        if not cosh_id:
            return None
        return {"cosh_id": cosh_id, "name": name_by_cosh_id.get(cosh_id)}

    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "phone": current_user.phone,
        "email": profile.email,
        "education": _named(profile.education_cosh_id),
        "experience": _named(profile.experience_cosh_id),
        "is_employed_by_organization": profile.is_employed_by_organization,
        "organisation_type": _named(profile.organisation_type_cosh_id),
        "non_employed_kind": profile.non_employed_kind,
        "phone_hidden": profile.phone_hidden,
        "declaration_accepted": profile.declaration_accepted,
        "farming_methods": [
            {"cosh_id": fm.farming_method_cosh_id,
             "name": name_by_cosh_id.get(fm.farming_method_cosh_id)}
            for fm in farming_methods
        ],
        "cultivation_types": [
            {"cosh_id": ct.cultivation_type_cosh_id,
             "name": name_by_cosh_id.get(ct.cultivation_type_cosh_id)}
            for ct in cultivation_types
        ],
        "expertise_domains": [
            {"cosh_id": d.domain, "name": name_by_cosh_id.get(d.domain)}
            for d in domains
        ],
        "crop_groups": [
            {"cosh_id": c.crop_group_cosh_id,
             "name": name_by_cosh_id.get(c.crop_group_cosh_id)}
            for c in crop_groups
        ],
        "languages": [
            {"cosh_id": l.language_code,
             "name": name_by_cosh_id.get(l.language_code)}
            for l in langs
        ],
        "support_areas": [{
            "state_cosh_id": a.state_cosh_id,
            "state_name": name_by_cosh_id.get(a.state_cosh_id),
            "district_cosh_id": a.district_cosh_id,
            "district_name": name_by_cosh_id.get(a.district_cosh_id) if a.district_cosh_id else None,
        } for a in areas],
        "companies": [{"client_id": c.client_id, "role": c.role} for c in companies],
    }


@router.put("/pundit/profile/phone-privacy")
async def toggle_phone_privacy(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await _get_pundit_profile(db, current_user.id)
    profile.phone_hidden = data.get("phone_hidden", not profile.phone_hidden)
    await db.commit()
    return {"phone_hidden": profile.phone_hidden}


# ── Company Invitations ────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    pundit_user_id: str
    role: PunditRole = PunditRole.PRIMARY


@router.post("/client/{client_id}/pundit-invitations", status_code=201)
async def invite_pundit(
    client_id: str,
    request: InviteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a PENDING invitation to a FarmPundit.

    Refuses with 409 when the (client, pundit) pair already has:
      - an in-flight PENDING invitation (`invitation_already_pending`)
      - an ACTIVE ClientFarmPundit row (`pundit_already_onboarded`)

    A previously REJECTED invitation does NOT block — the CA can
    re-invite at will (user direction 2026-05-27).
    """
    await _assert_portal_member(db, current_user.id, client_id)
    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == request.pundit_user_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="FarmPundit profile not found")

    # Already onboarded with this client? 409 — onboarding is the
    # terminal accepted state; "Onboarded" is the search-card label.
    existing_active = (await db.execute(
        select(ClientFarmPundit.id).where(
            ClientFarmPundit.client_id == client_id,
            ClientFarmPundit.pundit_id == profile.id,
            ClientFarmPundit.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if existing_active is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pundit_already_onboarded",
                "message": "This FarmPundit is already onboarded with your company.",
            },
        )

    # In-flight PENDING invitation? 409. REJECTED rows are ignored —
    # re-invite is allowed after rejection.
    existing_pending = (await db.execute(
        select(PunditInvitation.id).where(
            PunditInvitation.client_id == client_id,
            PunditInvitation.pundit_id == profile.id,
            PunditInvitation.status == "PENDING",
        ).limit(1)
    )).scalar_one_or_none()
    if existing_pending is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invitation_already_pending",
                "message": (
                    "An invitation is already pending. Wait for the expert "
                    "to accept or decline before re-inviting."
                ),
            },
        )

    invitation = PunditInvitation(
        client_id=client_id,
        pundit_id=profile.id,
        role=request.role,
        status="PENDING",
    )
    db.add(invitation)
    await db.commit()
    return {"invitation_id": invitation.id, "status": "PENDING"}


@router.get("/pundit/invitations")
async def list_invitations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = await _get_pundit_profile(db, current_user.id)
    result = await db.execute(
        select(PunditInvitation).where(
            PunditInvitation.pundit_id == profile.id,
            PunditInvitation.status == "PENDING",
        )
    )
    return result.scalars().all()


@router.put("/pundit/invitations/{invitation_id}/accept")
async def accept_invitation(
    invitation_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inv = await _get_invitation(db, invitation_id)
    inv.status = "ACCEPTED"

    sequence = await _next_round_robin_sequence(db, inv.client_id)
    db.add(ClientFarmPundit(
        client_id=inv.client_id,
        pundit_id=inv.pundit_id,
        role=inv.role,
        status="ACTIVE",
        round_robin_sequence=sequence if inv.role == PunditRole.PRIMARY else None,
    ))
    await db.commit()
    return {"status": "ACCEPTED"}


@router.put("/pundit/invitations/{invitation_id}/reject")
async def reject_invitation(
    invitation_id: str,
    data: dict | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """2026-06-23 — Mandatory reason dropped. A pundit declining a
    company invitation needs a simple confirmation, not a forced
    explanation. The `rejection_reason` column is retained for any
    legacy callers that still send a reason; it stays null when not
    provided.
    """
    inv = await _get_invitation(db, invitation_id)
    inv.status = "REJECTED"
    if data and data.get("reason"):
        inv.rejection_reason = data["reason"]
    await db.commit()
    return {"status": "REJECTED"}


# ── Query Management (Farmer) ──────────────────────────────────────────────────

class QueryMediaItem(BaseModel):
    media_type: str   # IMAGE | AUDIO | VIDEO — VIDEO column-ready but
                      # the PWA UI hides the picker in V1.
    url: str


class QueryCreate(BaseModel):
    subscription_id: str
    client_id: str
    crop_cosh_id: Optional[str] = None
    crop_age: Optional[str] = None
    # Mandatory Cosh-driven nature of the query (replaces the old
    # free-text title that the PWA used to ask for). The title column
    # is auto-populated from the resolved English name so existing
    # Pundit/CA list UIs keep working unchanged.
    query_type_cosh_id: str
    description: Optional[str] = None
    severity: str = "MODERATE"
    # ≥1 IMAGE is mandatory (user direction 2026-05-27). 0-1 AUDIO
    # optional. VIDEO accepted for forward compatibility but the PWA
    # doesn't ship the picker in V1.
    media: list[QueryMediaItem] = []
    # Razorpay payment artefacts. Required when the farmer is over
    # their per-(farmer, client) free quota; rejected otherwise (no
    # silent overpay). All three or none.
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    razorpay_signature: Optional[str] = None
    # Non-production-only flag that skips the Razorpay verification
    # for over-quota queries. Same shape + intent as the
    # /payment/staging-bypass endpoint on subscriptions: Razorpay
    # TEST mode rejects real UPI handles, blocking end-to-end query
    # testing past the 6th free. Server-side `settings.environment
    # == "production"` check refuses the flag on prod.
    staging_bypass: bool = False
    # 2026-06-30 — Optional for plant-wise crops. Propagates into the
    # QA-triggered TriggeredCHAEntry's `affected_plants_count` when the
    # pundit's chosen Standard Response fires a CHA timeline. Optional
    # because the query may not be about a pest at all — we can't know
    # at submit time. Validated 1 ≤ n ≤ subscription.number_of_plants
    # when present.
    affected_plants_count: Optional[int] = None


class QueryPaymentInit(BaseModel):
    client_id: str


@router.post("/farmer/queries/init-payment", status_code=201)
async def init_query_payment(
    request: QueryPaymentInit,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a Razorpay order for a paid (7th+) query.

    Refuses 422 `quota_available_no_payment_needed` when the farmer
    still has free quota — the PWA shouldn't trigger the Razorpay
    sheet in that case. The order is created with a receipt that
    binds the farmer + client; the verify path on POST
    /farmer/queries cross-checks the order amount matches
    settings.query_amount_paise so a tampered front-end can't downgrade.
    """
    used = (await db.execute(
        select(func.count()).select_from(Query).where(
            Query.farmer_user_id == current_user.id,
            Query.client_id == request.client_id,
        )
    )).scalar_one()
    if used < FREE_QUERIES_PER_COMPANY:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "quota_available_no_payment_needed",
                "message": (
                    f"You still have free queries left for this company. "
                    f"Submit directly — no payment needed."
                ),
            },
        )
    from app.services.payment_service import create_query_order
    # Receipt is bounded to 40 chars by Razorpay. Encode farmer+client
    # short prefixes plus a timestamp so receipts are unique enough
    # for audit without exceeding the cap.
    import time as _time
    receipt = f"qy_{current_user.id[:8]}_{request.client_id[:8]}_{int(_time.time())}"
    return create_query_order(receipt[:40])


@router.post("/farmer/queries", status_code=201)
async def submit_query(
    request: QueryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-12a: Submit query. Routes to default expert via round-robin.

    Rules (locked 2026-05-27):
      - `query_type_cosh_id` must resolve to an ACTIVE `query_types`
        Cosh core; refused with `query_type_invalid` (422) otherwise.
      - At least one IMAGE media item is mandatory
        (`image_required`, 422). 0-1 AUDIO is optional. VIDEO accepted
        but the PWA hides the picker in V1.
      - `title` is auto-set from the resolved Cosh translation; the
        farmer no longer types it.
      - Expiry = end-of-Day-2 in IST.
    """
    from app.modules.sync.models import CoshCoreItem

    # Resolve Nature of Query → friendly English name (becomes title).
    qt = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == request.query_type_cosh_id,
            CoshCoreItem.core_type == "query_types",
            CoshCoreItem.status == "active",
        )
    )).scalar_one_or_none()
    if qt is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "query_type_invalid",
                "message": "Nature of Query must be one of the active query_types.",
            },
        )
    title = (qt.translations or {}).get("en") or "Query"

    # Mandatory ≥1 image.
    image_count = sum(1 for m in request.media if m.media_type == "IMAGE")
    if image_count < 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "image_required",
                "message": "At least one photograph is required.",
            },
        )
    if image_count > 4:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_images",
                "message": "At most 4 photographs are allowed.",
            },
        )
    audio_count = sum(1 for m in request.media if m.media_type == "AUDIO")
    if audio_count > 1:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "too_many_audios",
                "message": "At most 1 audio is allowed.",
            },
        )

    # Paywall: 7th+ query for this (farmer, client) costs ₹20 to
    # rootsTALK.in. Verify Razorpay artefacts before insert so we
    # never create a query that's silently unpaid.
    used = (await db.execute(
        select(func.count()).select_from(Query).where(
            Query.farmer_user_id == current_user.id,
            Query.client_id == request.client_id,
        )
    )).scalar_one()
    is_paid = False
    if used >= FREE_QUERIES_PER_COMPANY:
        # Non-production staging bypass — flips the query to is_paid
        # without going through Razorpay. Refused on prod so we never
        # ship a free-of-charge backdoor to live users.
        if request.staging_bypass:
            from app.config import settings
            if settings.environment == "production":
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "bypass_disabled_in_production",
                        "message": "Staging bypass is not available in production.",
                    },
                )
            is_paid = True
        else:
            all_present = all([
                request.razorpay_order_id,
                request.razorpay_payment_id,
                request.razorpay_signature,
            ])
            if not all_present:
                paid_paise = _query_paid_price_paise()
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "payment_required",
                        "message": (
                            f"You've used all {FREE_QUERIES_PER_COMPANY} free queries "
                            f"for this company. Pay ₹{paid_paise // 100} "
                            f"to rootsTALK.in to submit this query."
                        ),
                        "price_paise": paid_paise,
                    },
                )
            from app.services.payment_service import (
                fetch_order_amount_paise, verify_payment_signature,
            )
            if not verify_payment_signature(
                request.razorpay_order_id,
                request.razorpay_payment_id,
                request.razorpay_signature,
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_payment_signature",
                            "message": "Payment signature verification failed."},
                )
            # Defence in depth: the order's amount on Razorpay's side must
            # match our locked price — guards against a tampered front-end
            # that mints a cheaper order id.
            expected_paise = _query_paid_price_paise()
            if fetch_order_amount_paise(request.razorpay_order_id) != expected_paise:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "payment_amount_mismatch",
                            "message": f"Order must be for ₹{expected_paise // 100}."},
                )
            is_paid = True

    # Refuse the submit when the client has no ACTIVE PRIMARY pundit
    # to route the query to. The PWA hides the Ask Expert button in
    # this state (it reads `client_has_primary_expert` from
    # /my-subscriptions), but defence in depth — a tampered client or
    # a race against a deactivation must not orphan a query.
    has_primary = (await db.execute(
        select(ClientFarmPundit.id).where(
            ClientFarmPundit.client_id == request.client_id,
            ClientFarmPundit.role == PunditRole.PRIMARY,
            ClientFarmPundit.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if has_primary is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_primary_expert_available",
                "message": (
                    "This company hasn't onboarded a Primary expert yet. "
                    "Your query cannot be sent until they do."
                ),
            },
        )

    # 2026-06-30 — Validate optional affected_plants_count against the
    # farmer's declared `number_of_plants` when provided. Plant-wise
    # crops only; left blank or area-wise: ignore. We don't enforce
    # PLANT_WISE measure here because the PWA hides the input on
    # area-wise subs, and a stray value sent up should be treated as
    # noise rather than rejected.
    apc: Optional[int] = request.affected_plants_count
    if apc is not None:
        if apc < 1:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "affected_plants_count_min",
                    "message": "Affected-plants count must be at least 1.",
                },
            )
        from app.modules.subscriptions.models import Subscription
        _sub = (await db.execute(
            select(Subscription).where(Subscription.id == request.subscription_id)
        )).scalar_one_or_none()
        if _sub and _sub.number_of_plants and apc > _sub.number_of_plants:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "affected_plants_count_max",
                    "message": (
                        f"Affected-plants count cannot exceed your "
                        f"declared {_sub.number_of_plants} plants."
                    ),
                    "max": _sub.number_of_plants,
                },
            )

    now_utc = datetime.now(timezone.utc)
    expires_at = _compute_query_expiry(now_utc)

    query = Query(
        farmer_user_id=current_user.id,
        subscription_id=request.subscription_id,
        client_id=request.client_id,
        crop_cosh_id=request.crop_cosh_id,
        crop_age=request.crop_age,
        query_type_cosh_id=request.query_type_cosh_id,
        title=title,
        description=request.description,
        severity=request.severity,
        status=QueryStatus.NEW,
        expires_at=expires_at,
        is_paid=is_paid,
        razorpay_payment_id=request.razorpay_payment_id if is_paid else None,
        affected_plants_count=apc,
    )
    db.add(query)
    await db.flush()

    for m in request.media:
        db.add(QueryMedia(query_id=query.id, media_type=m.media_type, url=m.url))

    # BL-12a: Full priority routing (preference → Promoter-Pundit → round-robin)
    next_pundit = await _get_next_pundit_for_query(db, request.client_id, request.subscription_id)
    if next_pundit:
        query.current_holder_id = next_pundit.id
        db.add(QueryRemark(
            query_id=query.id,
            pundit_id=next_pundit.id,
            action=QueryRemarkAction.RECEIVED,
        ))

    await db.commit()
    await db.refresh(query)
    return {"id": query.id, "status": query.status, "expires_at": query.expires_at}


@router.get("/cosh/query-types")
async def cosh_query_types(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cosh `query_types` options for the Ask Expert form.

    Same one-allowlisted-slug shape as `/cosh/pundit-options`. Kept
    on its own route (no `?slug=` param) since query_types is the
    only slug the form needs.
    """
    from app.modules.sync.models import CoshCoreItem
    rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
            CoshCoreItem.core_type == "query_types",
            CoshCoreItem.status == "active",
        )
    )).all()
    lang = current_user.language_code or "en"
    out = []
    for cosh_id, translations in rows:
        name = (
            pick_translation(translations, lang, "")
            if isinstance(translations, dict) else None
        )
        if name:
            out.append({"cosh_id": cosh_id, "name": name})
    out.sort(key=lambda x: x["name"].casefold())
    return out


@router.get("/farmer/queries/quota")
async def get_query_quota(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """How many free queries the farmer has left for this company.

    Free quota is per (farmer, client). Every Query row counts —
    even expired or rejected ones consumed the free slot. The 7th+
    query requires payment (₹20 to RootsTALK.in for software
    infrastructure, NOT to the company).
    """
    used = (await db.execute(
        select(func.count()).select_from(Query).where(
            Query.farmer_user_id == current_user.id,
            Query.client_id == client_id,
        )
    )).scalar_one()
    free_remaining = max(0, FREE_QUERIES_PER_COMPANY - used)
    return {
        "used": used,
        "free_limit": FREE_QUERIES_PER_COMPANY,
        "free_remaining": free_remaining,
        "price_paise": _query_paid_price_paise(),
        "next_query_is_paid": free_remaining == 0,
    }


@router.get("/farmer/queries")
async def list_farmer_queries(
    subscription_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer-facing query list.

    Default (no `subscription_id`) — every query the farmer has
    ever raised. Drives the bottom-nav "Queries" tab at
    `/my-queries` in the PWA.

    With `subscription_id` — queries scoped to ONE subscription.
    Drives the per-sub Queries page at
    `/crop-detail/{subscription_id}/queries` (added 2026-06-18 per
    Option A: "let the farmer see waitlisted / responded / closed
    queries from inside the subscription drill-down").
    """
    # 2026-06-28 — Soft-delete defense: join through Subscription so
    # the auto-listener filters queries on soft-deleted subscriptions
    # out of the farmer's view. Without the join the cascade leak
    # would surface CA-Admin-cleared subs back to the farmer.
    from app.modules.subscriptions.models import Subscription
    where = [Query.farmer_user_id == current_user.id]
    if subscription_id is not None:
        where.append(Query.subscription_id == subscription_id)
    result = await db.execute(
        select(Query)
        .join(Subscription, Subscription.id == Query.subscription_id)
        .where(*where).order_by(Query.created_at.desc())
    )
    queries = result.scalars().all()

    # 2026-06-20 — Auto-clear the dashboard badge by stamping viewed_at
    # on any RESPONDED row that hasn't been viewed yet. Only when the
    # farmer is on the per-sub queries page (subscription_id given) —
    # the global /my-queries view shouldn't mark everything read just
    # because it was rendered.
    if subscription_id is not None:
        now = datetime.now(timezone.utc)
        touched = False
        for q in queries:
            if q.status == QueryStatus.RESPONDED.value and q.viewed_at is None:
                q.viewed_at = now
                touched = True
        if touched:
            await db.commit()

    return [{"id": q.id, "title": q.title, "status": q.status, "severity": q.severity,
             "subscription_id": q.subscription_id,
             "expires_at": q.expires_at, "created_at": q.created_at} for q in queries]


# ── Query Management (FarmPundit) ──────────────────────────────────────────────

async def _serialise_pundit_query_cards(
    db: AsyncSession, queries: list, lang: str,
) -> list[dict]:
    """Bulk-fetch farmer / sub / client / crop_name for a list of
    Query rows and serialise to the card shape consumed by
    /pundit/queries and /pundit/queries/history.

    2026-06-23 — Pulled out of list_pundit_queries so the history
    endpoint can reuse the same enrichment (user direction). One
    query per related table; no N+1.
    """
    if not queries:
        return []

    from app.modules.clients.models import Client
    from app.modules.sync.models import CoshCoreItem

    farmer_ids = {q.farmer_user_id for q in queries if q.farmer_user_id}
    sub_ids = {q.subscription_id for q in queries if q.subscription_id}
    client_ids = {q.client_id for q in queries if q.client_id}
    crop_cosh_ids = {q.crop_cosh_id for q in queries if q.crop_cosh_id}

    farmer_by_id: dict[str, User] = {}
    if farmer_ids:
        rows = (await db.execute(
            select(User).where(User.id.in_(farmer_ids))
        )).scalars().all()
        farmer_by_id = {u.id: u for u in rows}

    sub_by_id: dict[str, Subscription] = {}
    if sub_ids:
        rows = (await db.execute(
            select(Subscription).where(Subscription.id.in_(sub_ids))
        )).scalars().all()
        sub_by_id = {s.id: s for s in rows}

    client_name_by_id: dict[str, str] = {}
    if client_ids:
        rows = (await db.execute(
            select(Client.id, Client.display_name, Client.full_name)
            .where(Client.id.in_(client_ids))
        )).all()
        for cid, disp, full in rows:
            client_name_by_id[cid] = disp or full or ""

    crop_name_by_cosh_id: dict[str, str] = {}
    if crop_cosh_ids:
        rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(crop_cosh_ids))
        )).all()
        for cid, tr in rows:
            if isinstance(tr, dict):
                name = pick_translation(tr, lang, "")
                if name:
                    crop_name_by_cosh_id[cid] = name

    now = datetime.now(timezone.utc)
    out = []
    for q in queries:
        farmer = farmer_by_id.get(q.farmer_user_id)
        sub = sub_by_id.get(q.subscription_id)
        # days_remaining is meaningful for live (non-terminal) queries.
        # For history the expires_at is in the past or moot; we still
        # compute it (could be negative pre-max) so the field shape
        # stays identical across both endpoints.
        days_remaining = max(
            0, (q.expires_at.replace(tzinfo=timezone.utc) - now).days,
        ) if q.expires_at else 0
        out.append({
            "id": q.id,
            "title": q.title,
            "status": q.status,
            "severity": q.severity,
            "client_id": q.client_id,
            "expires_at": q.expires_at,
            "days_remaining": days_remaining,
            "farmer_name": farmer.name if farmer else None,
            "farmer_photo_url": farmer.photo_url if farmer else None,
            "crop_cosh_id": q.crop_cosh_id,
            "crop_name": crop_name_by_cosh_id.get(q.crop_cosh_id) if q.crop_cosh_id else None,
            "crop_start_date": sub.crop_start_date if sub else None,
            "client_name": client_name_by_id.get(q.client_id),
        })
    return out


@router.get("/pundit/queries")
async def list_pundit_queries(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """New, forwarded, returned queries — sorted by urgency (fewest days remaining first).

    2026-06-23 — payload enriched (farmer name + photo, crop name,
    crop start date, company name) via the shared
    `_serialise_pundit_query_cards` helper so the pundit's triage
    card carries enough context to act without drilling in. Same
    shape served on /pundit/queries/history below.
    """
    profile = await _get_pundit_profile(db, current_user.id)
    # 2026-06-28 — Soft-delete defense: join through Subscription so
    # the listener filters queries originating from soft-deleted
    # subscriptions out of the pundit's queue.
    from app.modules.subscriptions.models import Subscription
    result = await db.execute(
        select(Query)
        .join(Subscription, Subscription.id == Query.subscription_id)
        .where(
            Query.current_holder_id == profile.id,
            Query.status.in_([QueryStatus.NEW, QueryStatus.FORWARDED, QueryStatus.RETURNED]),
        ).order_by(Query.expires_at)
    )
    queries = result.scalars().all()
    lang = current_user.language_code or "en"
    return await _serialise_pundit_query_cards(db, list(queries), lang)


@router.put("/pundit/queries/{query_id}/respond")
async def respond_to_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Any expert holding query can respond. Closes query everywhere simultaneously.

    BL-12 audit (2026-05-06): added holder check (pre-fix any pundit
    could respond to any query by guessing the URL) and a transition
    guard so a stale request can't re-respond to an already-closed
    query.
    """
    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)
    role = await _holder_role(db, profile, query)
    res = validate_query_transition(query.status, QueryStatus.RESPONDED.value, role)
    if not res.allowed:
        _raise_query_transition(res)

    has_structured = bool(data.get("problem_cosh_id") or data.get("standard_response_id"))
    media_list = data.get("media", []) or []

    # Text is mandatory only when the Pundit didn't pick a Crop Health
    # problem AND didn't pick a Standard Answer — without one of those,
    # the farmer's only thing to read is the free-form text.
    if not has_structured and not (data.get("text") or "").strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "response_text_required",
                "message": (
                    "Free-form text is required when no Crop Health problem "
                    "or Standard Answer is picked."
                ),
            },
        )

    # Media limits per user direction 2026-05-27: up to 4 IMAGE, 1
    # AUDIO, 1 HYPERLINK. All non-mandatory.
    image_count = sum(1 for m in media_list if m.get("media_type") == "IMAGE")
    audio_count = sum(1 for m in media_list if m.get("media_type") == "AUDIO")
    hyperlink_count = sum(1 for m in media_list if m.get("media_type") == "HYPERLINK")
    if image_count > 4:
        raise HTTPException(status_code=422, detail={
            "code": "too_many_response_images",
            "message": "At most 4 images are allowed in the response.",
        })
    if audio_count > 1:
        raise HTTPException(status_code=422, detail={
            "code": "too_many_response_audios",
            "message": "At most 1 audio is allowed in the response.",
        })
    if hyperlink_count > 1:
        raise HTTPException(status_code=422, detail={
            "code": "too_many_response_hyperlinks",
            "message": "At most 1 hyperlink is allowed in the response.",
        })

    response = QueryResponse(
        query_id=query_id,
        pundit_id=profile.id,
        problem_cosh_id=data.get("problem_cosh_id"),
        text=data.get("text"),
        standard_response_id=data.get("standard_response_id"),
    )
    db.add(response)
    await db.flush()

    # Attach response media if provided
    for media in media_list:
        db.add(QueryResponseMedia(
            response_id=response.id,
            media_type=media.get("media_type", "IMAGE"),
            url=media["url"],
            caption=media.get("caption"),
        ))

    db.add(QueryRemark(query_id=query_id, pundit_id=profile.id, action=QueryRemarkAction.RESPONDED))

    query.status = QueryStatus.RESPONDED
    query.current_holder_id = None

    # The Pundit's response can carry one of three branches per the
    # UCAT three-pipe model (see project_rootstalk_ucat.md):
    #   1. problem_cosh_id set    → CHA pipe (§14.7)
    #   2. standard_response_id   → Q&A pipe (§14.9)
    #   3. text/media only        → degraded fallback (no advisory
    #                               trigger; reaches farmer via
    #                               QueryResponse on a separate page)
    if data.get("problem_cosh_id"):
        await _trigger_cha_for_query(db, query, data["problem_cosh_id"])
    elif data.get("standard_response_id"):
        await _trigger_qa_for_query(db, query, data["standard_response_id"])

    await db.commit()
    return {"status": "RESPONDED", "response_id": response.id}


@router.get("/pundit/queries/{query_id}/forward-candidates")
async def list_forward_candidates(
    query_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Colleagues at this client the current holder can forward TO.

    Returns active FarmPundits at the query's client, excluding the
    current holder themselves. Sorted PRIMARY first (by round-robin
    sequence), then PANEL (by name). Phone follows the profile's
    `phone_hidden` toggle.

    Forward-chain rule (2026-06-23): a Promoter-Pundit may forward
    upward to regular pundits (PRIMARY / PANEL). Regular pundits MAY
    NOT forward back to a Promoter-Pundit — PP rows are excluded from
    the candidate list when the caller is a regular pundit.

    Auth: caller must be the query's current_holder.
    """
    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)
    if query.current_holder_id != profile.id:
        raise HTTPException(
            status_code=403,
            detail="Only the current holder can list forward candidates.",
        )

    caller_cfp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == query.client_id,
            ClientFarmPundit.pundit_id == profile.id,
        )
    )).scalar_one_or_none()
    caller_is_pp = caller_cfp and caller_cfp.role == PunditRole.PROMOTER_PUNDIT

    candidate_filter = [
        ClientFarmPundit.client_id == query.client_id,
        ClientFarmPundit.status == "ACTIVE",
        ClientFarmPundit.pundit_id != profile.id,
    ]
    if not caller_is_pp:
        # Regular pundit forwarding — exclude PPs from the recipient list.
        candidate_filter.append(ClientFarmPundit.role != PunditRole.PROMOTER_PUNDIT)

    rows = (await db.execute(
        select(ClientFarmPundit).where(*candidate_filter)
    )).scalars().all()
    if not rows:
        return []

    pundit_ids = [r.pundit_id for r in rows]
    profiles = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.id.in_(pundit_ids))
    )).scalars().all()
    profile_by_id = {p.id: p for p in profiles}
    user_ids = [p.user_id for p in profiles]
    users = (await db.execute(
        select(User).where(User.id.in_(user_ids))
    )).scalars().all() if user_ids else []
    user_by_id = {u.id: u for u in users}

    out = []
    for r in rows:
        prof = profile_by_id.get(r.pundit_id)
        if prof is None:
            continue
        user = user_by_id.get(prof.user_id)
        role = r.role.value if hasattr(r.role, "value") else str(r.role)
        out.append({
            "pundit_id": r.pundit_id,
            "name": user.name if user else None,
            "phone": (user.phone if (user and not prof.phone_hidden) else None),
            "role": role,
            "round_robin_sequence": r.round_robin_sequence,
        })
    # Primaries first by round-robin sequence; Panels after, by name.
    out.sort(key=lambda p: (
        0 if p["role"] == "PRIMARY" else 1,
        p["round_robin_sequence"] if p["round_robin_sequence"] is not None else 9999,
        (p["name"] or "").casefold(),
    ))
    return out


@router.put("/pundit/queries/{query_id}/forward")
async def forward_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Primary Expert forwards to another expert. Panel Experts cannot forward. Mandatory remarks.

    BL-12 audit (2026-05-06): holder check + transition guard via
    validate_transition (PRIMARY-only edges into FORWARDED). The
    PANEL-cannot-forward rule is now enforced via the table's
    role-set rather than an inline `if`. Chained forwards (already
    in FORWARDED, just rotating holder) short-circuit the transition
    check since the status doesn't change.
    """
    if not data.get("to_pundit_id") or not data.get("remarks"):
        raise HTTPException(status_code=422, detail="to_pundit_id and remarks are mandatory")

    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)
    role = await _holder_role(db, profile, query)

    # Chained forwards (status already FORWARDED) just rotate the
    # holder; only the table-validated transitions need the guard.
    if query.status != QueryStatus.FORWARDED:
        res = validate_query_transition(query.status, QueryStatus.FORWARDED.value, role)
        if not res.allowed:
            _raise_query_transition(res)
    elif role != BL12_PRIMARY:
        # PANEL chained forward — also blocked. Use the same error_code.
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ROLE_NOT_ALLOWED",
                "message": "Panel Experts cannot forward queries. You can only Respond or Return.",
            },
        )

    # Forward-chain rule (2026-06-23): regular pundits (Primary / Panel)
    # cannot forward to a Promoter-Pundit. PP rows receive queries only
    # from farmers (preference / promoter-assigned). Lookup the target's
    # row at this client and reject if it's a PP.
    caller_cfp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == query.client_id,
            ClientFarmPundit.pundit_id == profile.id,
        )
    )).scalar_one_or_none()
    caller_is_pp = caller_cfp and caller_cfp.role == PunditRole.PROMOTER_PUNDIT
    if not caller_is_pp:
        target_cfp = (await db.execute(
            select(ClientFarmPundit).where(
                ClientFarmPundit.client_id == query.client_id,
                ClientFarmPundit.pundit_id == data["to_pundit_id"],
            )
        )).scalar_one_or_none()
        if target_cfp and target_cfp.role == PunditRole.PROMOTER_PUNDIT:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "cannot_forward_to_promoter_pundit",
                    "message": (
                        "A regular pundit cannot forward a query to a "
                        "Promoter-Pundit. Promoter-Pundits receive queries "
                        "directly from farmers."
                    ),
                },
            )

    db.add(QueryRemark(
        query_id=query_id,
        pundit_id=profile.id,
        action=QueryRemarkAction.FORWARDED,
        forwarded_to_pundit_id=data["to_pundit_id"],
        remark=data["remarks"],
    ))

    query.status = QueryStatus.FORWARDED
    query.current_holder_id = data["to_pundit_id"]
    await db.commit()
    return {"status": "FORWARDED"}


@router.put("/pundit/queries/{query_id}/return")
async def return_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recipient returns query to sender. Mandatory remarks.

    BL-12 audit (2026-05-06): added holder check + transition guard.
    """
    if not data.get("remarks"):
        raise HTTPException(status_code=422, detail="Remarks are mandatory when returning")

    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)
    role = await _holder_role(db, profile, query)
    res = validate_query_transition(query.status, QueryStatus.RETURNED.value, role)
    if not res.allowed:
        _raise_query_transition(res)

    # Find original sender
    remarks = (await db.execute(
        select(QueryRemark).where(
            QueryRemark.query_id == query_id,
            QueryRemark.action == QueryRemarkAction.FORWARDED,
        ).order_by(QueryRemark.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    original_sender_id = remarks.pundit_id if remarks else None

    db.add(QueryRemark(
        query_id=query_id,
        pundit_id=profile.id,
        action=QueryRemarkAction.RETURNED,
        remark=data["remarks"],
    ))
    query.status = QueryStatus.RETURNED
    query.current_holder_id = original_sender_id
    await db.commit()
    return {"status": "RETURNED"}


@router.put("/pundit/queries/{query_id}/reject")
async def reject_query(
    query_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Primary Expert only. Mandatory comments.

    BL-12 audit (2026-05-06): the docstring already said 'Primary
    Expert only' but the rule wasn't enforced — a PANEL pundit could
    reject. Now caught by validate_transition (PRIMARY-only role on
    every edge into REJECTED). Holder check also added — pre-fix any
    pundit could reject any query by guessing the URL.
    """
    if not data.get("remarks"):
        raise HTTPException(status_code=422, detail="Remarks are mandatory when rejecting")

    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)
    role = await _holder_role(db, profile, query)
    res = validate_query_transition(query.status, QueryStatus.REJECTED.value, role)
    if not res.allowed:
        _raise_query_transition(res)

    db.add(QueryRemark(query_id=query_id, pundit_id=profile.id,
                       action=QueryRemarkAction.REJECTED, remark=data["remarks"]))
    query.status = QueryStatus.REJECTED
    query.current_holder_id = None
    await db.commit()
    return {"status": "REJECTED"}


@router.get("/pundit/queries/history")
async def query_history(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Queries this Pundit interacted with that have reached a terminal
    state (RESPONDED / REJECTED / EXPIRED).

    Why this isn't a simple `current_holder_id == profile.id` filter:
    `respond_to_query` (and the reject path) nulls `current_holder_id`
    when closing the query — that's correct ("no active holder once
    closed") but it means history is invisible if we only look at the
    live holder column. Source of truth for "I touched this" is the
    remark/response audit trail.

    A query lands in this Pundit's history if either:
      - they wrote a QueryRemark on it (RECEIVED / FORWARDED / RETURNED
        / RESPONDED / REJECTED actions all leave a remark row), OR
      - they were the responder on QueryResponse (defensive — a remark
        is always written alongside, but the union covers it either
        way).
    Plus the query is in a terminal status.
    """
    profile = await _get_pundit_profile(db, current_user.id)

    touched_via_remarks = (await db.execute(
        select(QueryRemark.query_id).where(
            QueryRemark.pundit_id == profile.id,
        ).distinct()
    )).scalars().all()
    touched_via_responses = (await db.execute(
        select(QueryResponse.query_id).where(
            QueryResponse.pundit_id == profile.id,
        )
    )).scalars().all()
    query_ids = set(touched_via_remarks) | set(touched_via_responses)
    if not query_ids:
        return []

    # 2026-06-28 — Soft-delete defense: join through Subscription so
    # the listener filters queries from soft-deleted subscriptions out
    # of the pundit's history tab too (mirror of /pundit/queries).
    from app.modules.subscriptions.models import Subscription
    result = await db.execute(
        select(Query)
        .join(Subscription, Subscription.id == Query.subscription_id)
        .where(
            Query.id.in_(query_ids),
            Query.status.in_([QueryStatus.RESPONDED, QueryStatus.REJECTED, QueryStatus.EXPIRED]),
        ).order_by(Query.created_at.desc())
    )
    # 2026-06-23 — Same enrichment as /pundit/queries so the
    # History tab card carries farmer + crop + start + company.
    # Pre-fix this returned raw Query ORM objects; the PWA
    # gracefully degraded but the new card fields stayed empty.
    lang = current_user.language_code or "en"
    return await _serialise_pundit_query_cards(db, list(result.scalars().all()), lang)


# ── Standard Q&A Library ──────────────────────────────────────────────────────
# Spec §14.9. Subject Experts curate a library of standard
# question/answer pairs; FarmPundits pick from it when responding to
# farmer queries (no edit; can layer additional guidance on top).
# V1 answer body is text + media; Timelines/Practices integration
# deferred to V1.1.


def _serialise_standard_response(sr: StandardResponse) -> dict:
    """Serialise the entry's metadata only. The advisory body
    (Timelines + Practices + Elements) is fetched via the timelines
    endpoints — same shape as PG/SP. Pre-L4-real this serialiser
    also returned answer_text + answer_media; those columns were
    dropped in migration `4b8e2c1a93f5` because they overlapped
    with the QueryResponse-side free-form fallback."""
    return {
        "id": sr.id,
        "client_id": sr.client_id,
        "crop_cosh_id": sr.crop_cosh_id,
        "question_text": sr.question_text,
        "status": sr.status,
        "created_by": sr.created_by,
        "created_at": sr.created_at,
        "updated_at": sr.updated_at,
    }


def _validate_standard_response_payload(data: dict) -> None:
    """Shared input validation for POST + PUT. Question is mandatory;
    crop_cosh_id is optional (null = crop-agnostic per spec §14.9).
    Advisory body lives on the linked timelines, not on this row,
    so there's nothing else to validate here."""
    question = data.get("question_text")
    if not question or not str(question).strip():
        raise HTTPException(status_code=422, detail="question_text is required.")


@router.get("/client/{client_id}/standard-responses")
async def list_standard_responses(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-portal-facing list. Filter by crop (None = crop-agnostic
    only when explicitly passed as the literal string 'AGNOSTIC';
    omitted = no crop filter at all) and/or by free-text search
    against question_text. Returns full payload — answer body
    included — because the SE list page renders both."""
    await _assert_portal_member(db, current_user.id, client_id)
    q = select(StandardResponse).where(
        StandardResponse.client_id == client_id,
    ).order_by(StandardResponse.created_at.desc())
    if crop_cosh_id == "AGNOSTIC":
        q = q.where(StandardResponse.crop_cosh_id.is_(None))
    elif crop_cosh_id:
        q = q.where(StandardResponse.crop_cosh_id == crop_cosh_id)
    if search:
        q = q.where(StandardResponse.question_text.ilike(f"%{search}%"))
    rows = (await db.execute(q)).scalars().all()
    return [_serialise_standard_response(r) for r in rows]


@router.post("/client/{client_id}/standard-responses", status_code=201)
async def create_standard_response(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    _validate_standard_response_payload(data)
    sr = StandardResponse(
        client_id=client_id,
        crop_cosh_id=data.get("crop_cosh_id") or None,
        question_text=str(data["question_text"]).strip(),
        created_by=current_user.id,
    )
    db.add(sr)
    await db.commit()
    await db.refresh(sr)
    _enqueue_sr_question_translation(sr.id)
    return _serialise_standard_response(sr)


@router.put("/client/{client_id}/standard-responses/{sr_id}")
async def update_standard_response(
    client_id: str,
    sr_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit an existing entry. Spec §14.9 says FarmPundits cannot
    modify standard answers — that's a Pundit-side rule on the
    response flow, not an SE-side rule on the library itself. The
    library curator (SE) can refine entries freely."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    _validate_standard_response_payload(data)

    sr = (await db.execute(
        select(StandardResponse).where(
            StandardResponse.id == sr_id,
            StandardResponse.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not sr:
        raise HTTPException(status_code=404, detail="Standard response not found")

    new_q = str(data["question_text"]).strip()
    question_changed = sr.question_text != new_q
    sr.question_text = new_q
    sr.crop_cosh_id = data.get("crop_cosh_id") or None
    await db.commit()
    await db.refresh(sr)
    if question_changed:
        _enqueue_sr_question_translation(sr.id)
    return _serialise_standard_response(sr)


def _enqueue_sr_question_translation(sr_id: str) -> None:
    """Best-effort Celery enqueue — never raise."""
    try:
        from app.tasks.translate_content import translate_field
        from app.modules.translations.models import EntityType
        translate_field.delay(EntityType.STANDARD_RESPONSE_QUESTION, sr_id, "")
    except Exception:
        pass


def _sr_state_error(sr: StandardResponse, expected: str, code: str, action: str) -> HTTPException:
    """Stable 422 shape for state-transition refusals — mirrors the
    `pg_not_draft` / `sp_not_draft` pattern so the frontend can branch
    on `detail.code` programmatically."""
    return HTTPException(
        status_code=422,
        detail={
            "code": code,
            "message": (
                f"Cannot {action} a {sr.status.lower()} question. "
                f"Expected status: {expected}."
            ),
            "current_status": sr.status,
            "expected_status": expected,
        },
    )


async def _load_sr_for_transition(
    db: AsyncSession, sr_id: str, client_id: str,
) -> StandardResponse:
    sr = (await db.execute(
        select(StandardResponse).where(
            StandardResponse.id == sr_id,
            StandardResponse.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not sr:
        raise HTTPException(status_code=404, detail="Standard response not found")
    return sr


@router.post("/client/{client_id}/standard-responses/{sr_id}/publish")
async def publish_standard_response(
    client_id: str,
    sr_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """DRAFT → ACTIVE. One-time gate; CA-side renders a confirmation
    card before calling this. Refuses if not DRAFT."""
    await _assert_can_publish_client_advisory(db, current_user.id, client_id)
    sr = await _load_sr_for_transition(db, sr_id, client_id)
    if sr.status != "DRAFT":
        raise _sr_state_error(sr, "DRAFT", "sr_not_draft", "publish")
    sr.status = "ACTIVE"
    await db.commit()
    await db.refresh(sr)
    return _serialise_standard_response(sr)


@router.post("/client/{client_id}/standard-responses/{sr_id}/deactivate")
async def deactivate_standard_response(
    client_id: str,
    sr_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """ACTIVE → INACTIVE. Hides the question from the Pundit pick list
    without deleting; the curator's escape hatch while rewriting."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    sr = await _load_sr_for_transition(db, sr_id, client_id)
    if sr.status != "ACTIVE":
        raise _sr_state_error(sr, "ACTIVE", "sr_not_active", "deactivate")
    sr.status = "INACTIVE"
    await db.commit()
    await db.refresh(sr)
    return _serialise_standard_response(sr)


@router.post("/client/{client_id}/standard-responses/{sr_id}/activate")
async def activate_standard_response(
    client_id: str,
    sr_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """INACTIVE → ACTIVE. Re-exposes a previously hidden question.
    Separate from publish so the DRAFT-only confirmation copy doesn't
    leak into the re-activate flow."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    sr = await _load_sr_for_transition(db, sr_id, client_id)
    if sr.status != "INACTIVE":
        raise _sr_state_error(sr, "INACTIVE", "sr_not_inactive", "activate")
    sr.status = "ACTIVE"
    await db.commit()
    await db.refresh(sr)
    return _serialise_standard_response(sr)


@router.delete("/client/{client_id}/standard-responses/{sr_id}", status_code=204)
async def delete_standard_response(
    client_id: str,
    sr_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hard delete. Note: a deleted entry that's already been
    referenced by a QueryResponse via standard_response_id remains
    referenced — the FK is nullable on the response side so this
    won't break, but the breadcrumb is broken. Acceptable for V1
    (deletion is a curator action, not a frequent flow)."""
    await _assert_can_edit_client_advisory(db, current_user.id, client_id)
    sr = (await db.execute(
        select(StandardResponse).where(
            StandardResponse.id == sr_id,
            StandardResponse.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not sr:
        raise HTTPException(status_code=404, detail="Standard response not found")
    await db.delete(sr)
    await db.commit()


@router.get("/pundit/standard-responses")
async def search_standard_responses(
    client_id: str,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pundit-side search — used while responding to a farmer's
    query. Same shape as the CA-side list but no auth-membership
    gate (Pundits act on behalf of multiple companies). Only ACTIVE
    questions are returned; DRAFTs and INACTIVE rows are curator-
    only state and must not leak into a Pundit's pick list."""
    q = select(StandardResponse).where(
        StandardResponse.client_id == client_id,
        StandardResponse.status == "ACTIVE",
    )
    if search:
        q = q.where(StandardResponse.question_text.ilike(f"%{search}%"))
    rows = (await db.execute(q)).scalars().all()
    return [_serialise_standard_response(r) for r in rows]


# ── FarmPundit search (Client Portal CA) ──────────────────────────────────────

@router.get("/client/{client_id}/pundit-search")
async def search_pundits(
    client_id: str,
    # Multi-value filters per spec §14.3 Step 1 — every dropdown is now
    # multi-select after the Cosh reshape. Query-param shape unchanged
    # for state/expertise/language/crop-group; farming_methods and
    # cultivation_types are net-new lists.
    state_cosh_ids: list[str] = QueryParam(default=[]),
    expertise_domains: list[str] = QueryParam(default=[]),
    language_codes: list[str] = QueryParam(default=[]),
    crop_groups: list[str] = QueryParam(default=[]),
    farming_methods: list[str] = QueryParam(default=[]),
    cultivation_types: list[str] = QueryParam(default=[]),
    # Single-value filters per spec §14.3 Step 1. These two stayed
    # single after the rewrite; values are Cosh ids now (matched
    # against `education_cosh_id` / `experience_cosh_id`).
    education_cosh_id: Optional[str] = None,
    experience_cosh_id: Optional[str] = None,
    phone: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Multi-filter search across all registered FarmPundits.

    Spec §14.3 Step 1 with the 2026-05-26 Cosh reshape:
      - state / expertise / language / crop_group / farming_methods /
        cultivation_types → multi-select; pundit matches if ANY value
        intersects the filter list
      - education / experience → single-select cosh_id picks
      - phone → independent quick-lookup (NOT a refinement)

    `declaration_accepted=True` is the registration-complete gate.
    Empty filter list / null single-filter = no filter applied.

    Phone is independent: when `phone` is non-empty the other filters
    are ignored entirely and results are everyone whose phone
    contains the query. This matches the "find a specific person by
    number" mental model (user direction 2026-05-27). The other
    filters only matter when phone is blank.
    """
    await _assert_portal_member(db, current_user.id, client_id)

    if phone:
        # Independent quick-lookup path. Every other filter is ignored.
        phone_user_ids = {
            u.id for u in (await db.execute(
                select(User).where(User.phone.like(f"%{phone}%"))
            )).scalars().all()
        }
        if not phone_user_ids:
            profiles = []
        else:
            profiles = (await db.execute(
                select(FarmPunditProfile).where(
                    FarmPunditProfile.declaration_accepted == True,  # noqa: E712
                    FarmPunditProfile.user_id.in_(phone_user_ids),
                )
            )).scalars().all()
    else:
        q = select(FarmPunditProfile).where(FarmPunditProfile.declaration_accepted == True)  # noqa: E712
        if education_cosh_id:
            q = q.where(FarmPunditProfile.education_cosh_id == education_cosh_id)
        if experience_cosh_id:
            q = q.where(FarmPunditProfile.experience_cosh_id == experience_cosh_id)

        profiles = (await db.execute(q)).scalars().all()

        # Multi-value filters for the two new Cosh-backed lists.
        if farming_methods:
            fm_ids = {
                r.pundit_id for r in (await db.execute(
                    select(FarmPunditFarmingMethod).where(
                        FarmPunditFarmingMethod.farming_method_cosh_id.in_(farming_methods)
                    )
                )).scalars().all()
            }
            profiles = [p for p in profiles if p.id in fm_ids]
        if cultivation_types:
            ct_ids = {
                r.pundit_id for r in (await db.execute(
                    select(FarmPunditCultivationType).where(
                        FarmPunditCultivationType.cultivation_type_cosh_id.in_(cultivation_types)
                    )
                )).scalars().all()
            }
            profiles = [p for p in profiles if p.id in ct_ids]

        # Multi-value filters resolved via membership in the joined table.
        # Each multi-filter intersects with the running profile set.
        if state_cosh_ids:
            area_pundit_ids = {
                r.pundit_id for r in (await db.execute(
                    select(FarmPunditSupportArea).where(
                        FarmPunditSupportArea.state_cosh_id.in_(state_cosh_ids)
                    )
                )).scalars().all()
            }
            profiles = [p for p in profiles if p.id in area_pundit_ids]

        if expertise_domains:
            domain_pundit_ids = {
                r.pundit_id for r in (await db.execute(
                    select(FarmPunditExpertise).where(
                        FarmPunditExpertise.domain.in_(expertise_domains)
                    )
                )).scalars().all()
            }
            profiles = [p for p in profiles if p.id in domain_pundit_ids]

        if language_codes:
            lang_pundit_ids = {
                r.pundit_id for r in (await db.execute(
                    select(FarmPunditLanguage).where(
                        FarmPunditLanguage.language_code.in_(language_codes)
                    )
                )).scalars().all()
            }
            profiles = [p for p in profiles if p.id in lang_pundit_ids]

        if crop_groups:
            cg_pundit_ids = {
                r.pundit_id for r in (await db.execute(
                    select(FarmPunditCropGroup).where(
                        FarmPunditCropGroup.crop_group_cosh_id.in_(crop_groups)
                    )
                )).scalars().all()
            }
            profiles = [p for p in profiles if p.id in cg_pundit_ids]

    # Per-pundit invitation state with this client. ACTIVE membership
    # is terminal-accepted; a PENDING invitation is in-flight. REJECTED
    # rows don't surface here — re-invite is allowed after a decline.
    onboarded_ids = {
        r.pundit_id for r in (await db.execute(
            select(ClientFarmPundit).where(
                ClientFarmPundit.client_id == client_id,
                ClientFarmPundit.status == "ACTIVE",
            )
        )).scalars().all()
    }
    pending_invited_ids = {
        r.pundit_id for r in (await db.execute(
            select(PunditInvitation).where(
                PunditInvitation.client_id == client_id,
                PunditInvitation.status == "PENDING",
            )
        )).scalars().all()
    }

    # Resolve the address cosh_ids (state + district from the User's
    # *profile data*, NOT from the FP register form's support_areas).
    # Result cards just need to show where the expert lives — search
    # criteria themselves stay visible on the form.
    from app.modules.sync.models import CoshCoreItem
    users_by_id: dict[str, User] = {}
    if profiles:
        for u in (await db.execute(
            select(User).where(User.id.in_({p.user_id for p in profiles}))
        )).scalars().all():
            users_by_id[u.id] = u

    address_ids: set[str] = set()
    for u in users_by_id.values():
        if u.state_cosh_id: address_ids.add(u.state_cosh_id)
        if u.district_cosh_id: address_ids.add(u.district_cosh_id)
    name_by_cosh_id: dict[str, str] = {}
    if address_ids:
        for cosh_id, translations in (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(
                CoshCoreItem.cosh_id.in_(address_ids),
                CoshCoreItem.core_type.in_(["state_list", "district_list"]),
            )
        )).all():
            if isinstance(translations, dict):
                label = pick_translation(translations, current_user.language_code or "en", "")
                if label:
                    name_by_cosh_id[cosh_id] = label

    def _invitation_status(pundit_id: str) -> str:
        if pundit_id in onboarded_ids:
            return "ONBOARDED"
        if pundit_id in pending_invited_ids:
            return "PENDING"
        return "NONE"

    result_out = []
    for p in profiles:
        user = users_by_id.get(p.user_id)
        result_out.append({
            "id": p.id,
            "user_id": p.user_id,
            "name": user.name if user else None,
            "phone": user.phone if (user and not p.phone_hidden) else None,
            "email": p.email,
            "address": {
                # Address comes from User profile (initial signup),
                # not from the FP register form. Any field may be null
                # if the User hasn't completed their address yet.
                "line": (user.address_line if user else None),
                "locality": (user.locality if user else None),
                "town": (user.town if user else None),
                "pin_code": (user.pin_code if user else None),
                "district": (
                    name_by_cosh_id.get(user.district_cosh_id)
                    if (user and user.district_cosh_id) else None
                ),
                "state": (
                    name_by_cosh_id.get(user.state_cosh_id)
                    if (user and user.state_cosh_id) else None
                ),
            } if user else None,
            "invitation_status": _invitation_status(p.id),
        })
    return result_out


# ── Company Pundit Management (Client Portal) ─────────────────────────────────

@router.get("/client/{client_id}/pundits")
async def list_company_pundits(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_portal_member(db, current_user.id, client_id)
    result = await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.client_id == client_id)
        .order_by(ClientFarmPundit.onboarded_at)
    )
    pundits = result.scalars().all()

    # Pre-fetch the set of user_ids that satisfy the M5 / spec §14.2
    # eligibility for Promoter-Pundit at this client: ACTIVE
    # Facilitator-type ClientPromoter row with is_promoter=True. The
    # CA portal uses this to gate the Mark-PP button — clicking it
    # for an ineligible Pundit otherwise lands a 422 / 409 with the
    # `promoter_pundit_requires_facilitator_promoter` code.
    from app.modules.clients.models import ClientPromoter
    pp_eligible_user_ids = {
        r.user_id for r in (await db.execute(
            select(ClientPromoter).where(
                ClientPromoter.client_id == client_id,
                ClientPromoter.promoter_type == "FACILITATOR",
                ClientPromoter.status == "ACTIVE",
                ClientPromoter.is_promoter == True,  # noqa: E712
            )
        )).scalars().all()
    }

    out = []
    for cp in pundits:
        profile = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.id == cp.pundit_id)
        )).scalar_one_or_none()
        user = (await db.execute(
            select(User).where(User.id == profile.user_id)
        )).scalar_one_or_none() if profile else None
        active_query_count = await _count_active_queries_for_pundit(
            db, client_id=client_id, pundit_id=cp.pundit_id,
        )
        out.append({
            "id": cp.id,
            "pundit_id": cp.pundit_id,
            "name": user.name if user else None,
            "phone": user.phone if user else None,
            "role": cp.role,
            "status": cp.status,
            "can_be_promoter_pundit": (
                user is not None and user.id in pp_eligible_user_ids
            ),
            # 2026-05-31 — distinguishes the two P-P paths so the
            # CA's read-only Promoter-Pundits sub-tab can label rows
            # correctly. `REGISTERED_PUNDIT` = real FarmPundit who
            # was designated; `FM_PROMOTER` = phantom row backing an
            # FM-side ClientPromoter.is_promoter_pundit=True flag.
            # `searchable` is the underlying bit.
            "source": (
                "REGISTERED_PUNDIT" if cp.searchable else "FM_PROMOTER"
            ),
            "round_robin_sequence": cp.round_robin_sequence,
            "active_query_count": active_query_count,
            "onboarded_at": cp.onboarded_at,
        })
    return out


@router.get("/client/{client_id}/pundits/{cp_id}/profile")
async def get_company_pundit_profile(
    client_id: str,
    cp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full Cosh-resolved profile of an onboarded FarmPundit.

    The CA drills in from the My Experts row to see the expert's
    full registration data (latest — no caching, read straight from
    the DB). Scoped to the path-client so a CA can't peek at
    another company's Pundits via id-guessing; `cp_id` must belong
    to `client_id`.

    Response shape mirrors the Pundit's own `GET /pundit/profile`
    so the CA modal can reuse the same render logic the Pundit's
    PWA already uses.
    """
    await _assert_portal_member(db, current_user.id, client_id)

    cp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.id == cp_id,
            ClientFarmPundit.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail={
            "code": "pundit_not_in_client",
            "message": "FarmPundit not found in this company.",
        })

    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.id == cp.pundit_id)
    )).scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="FarmPundit profile not found")

    user = (await db.execute(
        select(User).where(User.id == profile.user_id)
    )).scalar_one_or_none()

    domains = (await db.execute(
        select(FarmPunditExpertise).where(FarmPunditExpertise.pundit_id == profile.id)
    )).scalars().all()
    areas = (await db.execute(
        select(FarmPunditSupportArea).where(FarmPunditSupportArea.pundit_id == profile.id)
    )).scalars().all()
    langs = (await db.execute(
        select(FarmPunditLanguage).where(FarmPunditLanguage.pundit_id == profile.id)
    )).scalars().all()
    crop_groups = (await db.execute(
        select(FarmPunditCropGroup).where(FarmPunditCropGroup.pundit_id == profile.id)
    )).scalars().all()
    farming_methods = (await db.execute(
        select(FarmPunditFarmingMethod).where(FarmPunditFarmingMethod.pundit_id == profile.id)
    )).scalars().all()
    cultivation_types = (await db.execute(
        select(FarmPunditCultivationType).where(FarmPunditCultivationType.pundit_id == profile.id)
    )).scalars().all()

    from app.modules.sync.models import CoshCoreItem
    ref_ids: set[str] = set()
    for sid in (profile.education_cosh_id, profile.experience_cosh_id,
                profile.organisation_type_cosh_id):
        if sid: ref_ids.add(sid)
    for a in areas:
        if a.state_cosh_id: ref_ids.add(a.state_cosh_id)
        if a.district_cosh_id: ref_ids.add(a.district_cosh_id)
    for fm in farming_methods: ref_ids.add(fm.farming_method_cosh_id)
    for ct in cultivation_types: ref_ids.add(ct.cultivation_type_cosh_id)
    for d in domains: ref_ids.add(d.domain)
    for cg in crop_groups: ref_ids.add(cg.crop_group_cosh_id)
    for l in langs: ref_ids.add(l.language_code)
    if user:
        if user.state_cosh_id: ref_ids.add(user.state_cosh_id)
        if user.district_cosh_id: ref_ids.add(user.district_cosh_id)

    name_by_cosh_id: dict[str, str] = {}
    if ref_ids:
        for cosh_id, translations in (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(ref_ids))
        )).all():
            if isinstance(translations, dict):
                label = pick_translation(translations, current_user.language_code or "en", "")
                if label:
                    name_by_cosh_id[cosh_id] = label

    def _named(cosh_id: Optional[str]) -> Optional[dict]:
        if not cosh_id:
            return None
        return {"cosh_id": cosh_id, "name": name_by_cosh_id.get(cosh_id)}

    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "name": user.name if user else None,
        # Phone surfaces only when the Pundit hasn't toggled phone-hidden.
        # CA sees the same privacy state the search results enforce.
        "phone": user.phone if (user and not profile.phone_hidden) else None,
        "email": profile.email,
        "address": {
            "line": user.address_line if user else None,
            "locality": user.locality if user else None,
            "town": user.town if user else None,
            "pin_code": user.pin_code if user else None,
            "district": (
                name_by_cosh_id.get(user.district_cosh_id)
                if (user and user.district_cosh_id) else None
            ),
            "state": (
                name_by_cosh_id.get(user.state_cosh_id)
                if (user and user.state_cosh_id) else None
            ),
        } if user else None,
        "education": _named(profile.education_cosh_id),
        "experience": _named(profile.experience_cosh_id),
        "is_employed_by_organization": profile.is_employed_by_organization,
        "organisation_type": _named(profile.organisation_type_cosh_id),
        "non_employed_kind": profile.non_employed_kind,
        "farming_methods": [
            {"cosh_id": fm.farming_method_cosh_id,
             "name": name_by_cosh_id.get(fm.farming_method_cosh_id)}
            for fm in farming_methods
        ],
        "cultivation_types": [
            {"cosh_id": ct.cultivation_type_cosh_id,
             "name": name_by_cosh_id.get(ct.cultivation_type_cosh_id)}
            for ct in cultivation_types
        ],
        "expertise_domains": [
            {"cosh_id": d.domain, "name": name_by_cosh_id.get(d.domain)}
            for d in domains
        ],
        "crop_groups": [
            {"cosh_id": c.crop_group_cosh_id,
             "name": name_by_cosh_id.get(c.crop_group_cosh_id)}
            for c in crop_groups
        ],
        "languages": [
            {"cosh_id": l.language_code,
             "name": name_by_cosh_id.get(l.language_code)}
            for l in langs
        ],
        "support_areas": [{
            "state_cosh_id": a.state_cosh_id,
            "state_name": name_by_cosh_id.get(a.state_cosh_id),
            "district_cosh_id": a.district_cosh_id,
            "district_name": name_by_cosh_id.get(a.district_cosh_id) if a.district_cosh_id else None,
        } for a in areas],
        # Companion fields the CA may want to see at a glance.
        "role": cp.role,
        "status": cp.status,
        "round_robin_sequence": cp.round_robin_sequence,
        "onboarded_at": cp.onboarded_at,
    }


@router.get("/client/{client_id}/pundit-invitations")
async def list_company_pundit_invitations(
    client_id: str,
    status: str = "PENDING",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Invitations the CA has sent that haven't been accepted yet.

    Spec §14.3 Step 3 — the CA invites; the expert accepts in the PWA.
    Until acceptance, the expert is NOT enrolled — `ClientFarmPundit`
    has no row for them. Without this listing the CA portal can't
    distinguish "invited and waiting" from "never sent" — both look
    like 'no expert' in the My Experts tab.
    """
    await _assert_portal_member(db, current_user.id, client_id)
    invitations = (await db.execute(
        select(PunditInvitation).where(
            PunditInvitation.client_id == client_id,
            PunditInvitation.status == status,
        ).order_by(PunditInvitation.created_at.desc())
    )).scalars().all()

    out = []
    for inv in invitations:
        profile = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.id == inv.pundit_id)
        )).scalar_one_or_none()
        user = (await db.execute(
            select(User).where(User.id == profile.user_id)
        )).scalar_one_or_none() if profile else None
        out.append({
            "id": inv.id,
            "pundit_id": inv.pundit_id,
            "name": user.name if user else None,
            "phone": user.phone if (user and profile and not profile.phone_hidden) else None,
            "email": profile.email if profile else None,
            "role": inv.role.value if hasattr(inv.role, "value") else str(inv.role),
            "status": inv.status,
            "rejection_reason": inv.rejection_reason,
            "created_at": inv.created_at,
        })
    return out


@router.put("/client/{client_id}/pundits/{cp_id}/deactivate")
async def deactivate_company_pundit(
    client_id: str,
    cp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deactivate a FarmPundit from this company. They keep active queries until resolved."""
    await _assert_portal_member(db, current_user.id, client_id)
    cp = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.id == cp_id, ClientFarmPundit.client_id == client_id)
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Company pundit not found")
    cp.status = "INACTIVE"
    await db.commit()
    return {"status": "INACTIVE"}


@router.put("/client/{client_id}/pundits/{cp_id}/reactivate")
async def reactivate_company_pundit(
    client_id: str,
    cp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bring an INACTIVE FarmPundit back into rotation.

    Spec §14.5 covers the *deactivate* flow but is silent on undoing
    a deactivation. In V1 we treat reactivate as the simple inverse:
    flip status back to ACTIVE; the existing round_robin_sequence is
    preserved on deactivate so reactivation slots them back in their
    original position. Only paths through `change_role` clear the
    sequence (Primary→Panel), so the reactivation case is genuinely
    a no-op on routing data."""
    await _assert_portal_member(db, current_user.id, client_id)
    cp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.id == cp_id,
            ClientFarmPundit.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Company pundit not found")
    if cp.status == "ACTIVE":
        raise HTTPException(
            status_code=400,
            detail="This FarmPundit is already active.",
        )
    cp.status = "ACTIVE"
    await db.commit()
    return {"status": "ACTIVE"}


class PunditRoleChange(BaseModel):
    role: PunditRole


@router.put("/client/{client_id}/pundits/{cp_id}/role")
async def change_company_pundit_role(
    client_id: str,
    cp_id: str,
    request: PunditRoleChange,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Spec §14.5: role change Primary↔Panel is allowed only after
    the FarmPundit has been deactivated AND all their active queries
    have been resolved or returned. The deactivate→drain→change-role
    sequence prevents in-flight queries from drifting between role
    semantics (e.g. a Panel pundit suddenly holding what was a
    Primary-routed query).

    Side effect on routing data:
      Primary → Panel: clear round_robin_sequence (Panel pundits don't
                       participate in the round-robin sequence).
      Panel  → Primary: assign the next available sequence so the
                       reactivated pundit lands at the end.
    """
    await _assert_portal_member(db, current_user.id, client_id)
    cp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.id == cp_id,
            ClientFarmPundit.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Company pundit not found")

    if cp.status != "INACTIVE":
        raise HTTPException(
            status_code=409,
            detail="Deactivate this FarmPundit before changing their role. Per spec §14.5, role changes happen only after active queries clear.",
        )

    active_count = await _count_active_queries_for_pundit(
        db, client_id=client_id, pundit_id=cp.pundit_id,
    )
    if active_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"This FarmPundit is still holding {active_count} active query/queries. Wait until all are resolved or returned before changing their role.",
        )

    current_role = cp.role.value if hasattr(cp.role, "value") else str(cp.role)
    new_role = request.role.value if hasattr(request.role, "value") else str(request.role)
    if current_role == new_role:
        raise HTTPException(
            status_code=400,
            detail=f"This FarmPundit is already a {new_role.title()} expert.",
        )

    # Compute next sequence BEFORE flipping role — autoflush would
    # otherwise include this row in the Primary count and yield a
    # sequence one too high.
    if new_role == "PRIMARY":
        next_seq = await _next_round_robin_sequence(db, client_id)
        cp.role = request.role
        cp.round_robin_sequence = next_seq
    else:  # PANEL
        cp.role = request.role
        cp.round_robin_sequence = None

    await db.commit()
    return {"role": new_role, "round_robin_sequence": cp.round_robin_sequence}


@router.delete("/client/{client_id}/pundits/{cp_id}", status_code=204)
async def delete_company_pundit(
    client_id: str,
    cp_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a FarmPundit from this company entirely (spec §14.5
    last bullet: "If deleted: removed from company's list. Their PWA
    profile remains — they can still be invited by other companies").

    Same gate as role-change: must be INACTIVE with zero active
    queries. The PWA-side `FarmPunditProfile` row is deliberately
    untouched — the expert can still be invited by another company,
    and their query/response history under THIS company stays
    intact (those reference `pundit_id` not `client_pundit_id`)."""
    await _assert_portal_member(db, current_user.id, client_id)
    cp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.id == cp_id,
            ClientFarmPundit.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Company pundit not found")

    if cp.status != "INACTIVE":
        raise HTTPException(
            status_code=409,
            detail="Deactivate this FarmPundit before removing them.",
        )

    active_count = await _count_active_queries_for_pundit(
        db, client_id=client_id, pundit_id=cp.pundit_id,
    )
    if active_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"This FarmPundit is still holding {active_count} active query/queries. Wait until all are resolved or returned before removing them.",
        )

    await db.delete(cp)
    await db.commit()


@router.put("/client/{client_id}/pundits/{cp_id}/promoter-pundit")
async def toggle_promoter_pundit(
    client_id: str,
    cp_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Field Manager designates a facilitator FarmPundit as a
    Promoter-Pundit (PP) — or removes that designation.

    2026-06-23 rewrite: PROMOTER_PUNDIT is now a first-class role on
    the `client_farm_pundits.role` column. The historical
    `is_promoter_pundit` flag was dropped. Per user direction:
    - A regular pundit (PRIMARY / PANEL) CANNOT be converted to a PP
      and vice versa. The role designation is exclusive at the row level.
    - A user can hold PROMOTER_PUNDIT at AT MOST ONE client across
      the platform (DB-enforced via partial unique index).

    Toggle ON (set role to PROMOTER_PUNDIT):
      - 409 if the existing row's role is PRIMARY or PANEL
        (regular → PP transition forbidden).
      - 409 eligibility / cross-path checks below (unchanged).
      - 409 if the user already holds PROMOTER_PUNDIT at another client.
    Toggle OFF (clear the designation):
      - We delete the cfp row entirely. Per the user's rule, a PP row
        does not "downgrade" to PRIMARY/PANEL — removing the PP
        designation removes the pundit-at-this-client relationship.
        The user must re-onboard via /pundit/register if they later
        want to act as a regular pundit at this client.
    """
    await _assert_portal_member(db, current_user.id, client_id)
    cp = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.id == cp_id, ClientFarmPundit.client_id == client_id)
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Company pundit not found")

    is_currently_pp = cp.role == PunditRole.PROMOTER_PUNDIT
    new_value = data.get("is_promoter_pundit", not is_currently_pp)

    if new_value and not is_currently_pp:
        # Regular → PP transition is forbidden.
        if cp.role in (PunditRole.PRIMARY, PunditRole.PANEL):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "regular_pundit_cannot_become_promoter_pundit",
                    "message": (
                        "A regular pundit (Primary / Panel) cannot become a "
                        "Promoter-Pundit. Remove this pundit and re-add via "
                        "the Promoter-Pundit designation flow if the role "
                        "needs to change."
                    ),
                },
            )

    if new_value and not is_currently_pp:
        # Eligibility check on enabling — skip on idempotent toggle-on
        # (already PP) and on toggle-off.
        from app.modules.clients.models import ClientPromoter

        profile = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.id == cp.pundit_id)
        )).scalar_one_or_none()
        if profile is None:
            raise HTTPException(status_code=404, detail="FarmPundit profile not found")

        promoter = (await db.execute(
            select(ClientPromoter).where(
                ClientPromoter.client_id == client_id,
                ClientPromoter.user_id == profile.user_id,
                ClientPromoter.promoter_type == "FACILITATOR",
                ClientPromoter.status == "ACTIVE",
                # Onboarded Facilitator alone isn't enough — they
                # must additionally be marked as a Promoter (the
                # is_promoter flag on the ClientPromoter row, added
                # in the Option C separation 2026-05-08).
                ClientPromoter.is_promoter == True,  # noqa: E712
            )
        )).scalar_one_or_none()
        if promoter is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "promoter_pundit_requires_facilitator_promoter",
                    "message": (
                        "Per spec §14.2, a Promoter-Pundit must first be a "
                        "Facilitator-Promoter at this company. The candidate "
                        "must be onboarded as a Facilitator AND marked as a "
                        "Promoter on the Field Manager page before being "
                        "designated as a Promoter-Pundit."
                    ),
                },
            )

        # Mutual-exclusion guard (V1, 2026-05-31): refuse if the same
        # user is already a P-P via the ClientPromoter path at this
        # client. Mirror of the FM-side guard in clients/router.py's
        # fm_toggle_promoter_pundit.
        pp_via_promoter = (await db.execute(
            select(ClientPromoter.id).where(
                ClientPromoter.client_id == client_id,
                ClientPromoter.user_id == profile.user_id,
                ClientPromoter.is_promoter_pundit == True,  # noqa: E712
            ).limit(1)
        )).scalar_one_or_none()
        if pp_via_promoter is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "pp_via_promoter_exists",
                    "message": (
                        "This user is already a Promoter-Pundit via the "
                        "Promoter (Field Manager) path at this client. "
                        "For V1, the two paths are kept mutually "
                        "exclusive — remove the Promoter-side P-P "
                        "designation before switching to the "
                        "FarmPundit-side designation."
                    ),
                },
            )

        # Single-company PP constraint (2026-06-23): refuse if the user
        # already has PROMOTER_PUNDIT at another client. The partial
        # unique index would catch this at commit; raising here gives a
        # cleaner error code to the portal.
        other_pp = (await db.execute(
            select(ClientFarmPundit.client_id).where(
                ClientFarmPundit.pundit_id == cp.pundit_id,
                ClientFarmPundit.role == PunditRole.PROMOTER_PUNDIT,
                ClientFarmPundit.client_id != client_id,
            ).limit(1)
        )).scalar_one_or_none()
        if other_pp is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "promoter_pundit_already_at_another_client",
                    "message": (
                        "This user is already designated as a "
                        "Promoter-Pundit at another company. A "
                        "Promoter-Pundit may serve only one company at a "
                        "time."
                    ),
                },
            )

    if new_value and not is_currently_pp:
        cp.role = PunditRole.PROMOTER_PUNDIT
        await db.commit()
        return {"role": cp.role.value, "is_promoter_pundit": True}

    if not new_value and is_currently_pp:
        # Remove the PP designation by deleting the cfp row. Per the
        # user's rule, a PP row does not "downgrade" — the relationship
        # is removed.
        await db.delete(cp)
        await db.commit()
        return {"role": None, "is_promoter_pundit": False}

    # Idempotent paths: nothing to change.
    return {"role": cp.role.value if hasattr(cp.role, "value") else cp.role,
            "is_promoter_pundit": is_currently_pp}


# ── Promoter-Pundit auto-provision (V1, 2026-05-30) ──────────────────────────
# Designate a Facilitator-Promoter at this client as a Promoter-Pundit
# without forcing them through the /pundit/register flow. Auto-creates
# a phantom FarmPunditProfile + ClientFarmPundit (searchable=False) so
# they exist in the routing tables but never appear in the farmer's
# expert picker. The existing PUT toggle endpoint covers the case
# where a real FarmPundit (someone who DID register) gets promoted to
# PP — and also covers un-designating either kind.

class PromoterPunditAddRequest(BaseModel):
    phone: str


@router.post("/client/{client_id}/promoter-pundits", status_code=201)
async def add_promoter_pundit(
    client_id: str,
    request: PromoterPunditAddRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA designates a Facilitator-Promoter at this client as a P-P.

    Identifies the target by phone (the CA knows the F-P's number);
    enforces §14.2 (must be ACTIVE FACILITATOR + is_promoter=True at
    this client); auto-provisions the FarmPunditProfile and the
    ClientFarmPundit row (searchable=False) on first add. Idempotent
    when re-called for an already-designated PP.
    """
    from app.modules.clients.models import ClientPromoter

    await _assert_portal_member(db, current_user.id, client_id)

    phone = (request.phone or "").strip()
    if not phone:
        raise HTTPException(status_code=422, detail={
            "code": "phone_required",
            "message": "phone is required.",
        })

    target = (await db.execute(
        select(User).where(User.phone == phone)
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=422, detail={
            "code": "user_not_found",
            "message": (
                "No RootsTalk user is registered with this number. "
                "Ask them to register first."
            ),
        })

    # §14.2: must already be a Facilitator-Promoter at this client.
    is_fp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.client_id == client_id,
            ClientPromoter.user_id == target.id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.is_promoter == True,  # noqa: E712
            ClientPromoter.status == "ACTIVE",
        )
    )).scalar_one_or_none() is not None
    if not is_fp:
        raise HTTPException(status_code=409, detail={
            "code": "promoter_pundit_requires_facilitator_promoter",
            "message": (
                "Per §14.2, a Promoter-Pundit must already be an ACTIVE "
                "Facilitator-Promoter at this company. Promote them on "
                "the Field Manager page first."
            ),
        })

    # Phantom FarmPunditProfile — created lazily so the F-P doesn't
    # need to go through /pundit/register.
    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == target.id)
    )).scalar_one_or_none()
    if profile is None:
        profile = FarmPunditProfile(user_id=target.id)
        db.add(profile)
        await db.flush()

    # Single-company PP constraint (2026-06-23): refuse if the user is
    # already a Promoter-Pundit at another client.
    other_pp = (await db.execute(
        select(ClientFarmPundit.client_id).where(
            ClientFarmPundit.pundit_id == profile.id,
            ClientFarmPundit.role == PunditRole.PROMOTER_PUNDIT,
            ClientFarmPundit.client_id != client_id,
        ).limit(1)
    )).scalar_one_or_none()
    if other_pp is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "promoter_pundit_already_at_another_client",
                "message": (
                    "This user is already designated as a Promoter-Pundit "
                    "at another company. A Promoter-Pundit may serve only "
                    "one company at a time."
                ),
            },
        )

    cfp = (await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == client_id,
            ClientFarmPundit.pundit_id == profile.id,
        )
    )).scalar_one_or_none()
    if cfp is None:
        cfp = ClientFarmPundit(
            client_id=client_id,
            pundit_id=profile.id,
            role=PunditRole.PROMOTER_PUNDIT,
            status="ACTIVE",
            # Phantom PP rows are hidden from any farmer-facing list —
            # the farmer can only reach them by typing the phone.
            searchable=False,
        )
        db.add(cfp)
    elif cfp.role == PunditRole.PROMOTER_PUNDIT:
        # Idempotent re-add of an existing PP row — reactivate if
        # needed. Don't flip `searchable` back to False (would erase
        # the CA's earlier decision if this was a real FarmPundit).
        cfp.status = "ACTIVE"
    else:
        # Existing row is a regular pundit (PRIMARY / PANEL). Per the
        # 2026-06-23 rule, regular pundits cannot become Promoter-
        # Pundits at the same client — caller must remove the row
        # first and then re-add as PP.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "regular_pundit_cannot_become_promoter_pundit",
                "message": (
                    "This user is already a regular pundit (Primary / "
                    "Panel) at this company. Remove that designation "
                    "before adding them as a Promoter-Pundit."
                ),
            },
        )

    await db.commit()
    await db.refresh(cfp)
    return {
        "id": cfp.id,
        "client_id": cfp.client_id,
        "pundit_id": cfp.pundit_id,
        "user_id": target.id,
        "name": target.name,
        "phone": target.phone,
        "role": cfp.role.value if hasattr(cfp.role, "value") else cfp.role,
        "status": cfp.status,
        "searchable": cfp.searchable,
    }


# ── Query Detail Routes ────────────────────────────────────────────────────────

@router.get("/pundit/queries/{query_id}")
async def get_query_detail_pundit(
    query_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pundit sees full query with remarks chain and response."""
    profile = await _get_pundit_profile(db, current_user.id)
    query = await _get_query(db, query_id)

    remarks = (await db.execute(
        select(QueryRemark).where(QueryRemark.query_id == query_id).order_by(QueryRemark.created_at)
    )).scalars().all()

    response = (await db.execute(
        select(QueryResponse).where(QueryResponse.query_id == query_id)
    )).scalar_one_or_none()

    media_result = (await db.execute(
        select(QueryMedia).where(QueryMedia.query_id == query_id)
    )).scalars().all()

    response_media = []
    if response:
        rm_result = (await db.execute(
            select(QueryResponseMedia).where(QueryResponseMedia.response_id == response.id)
        )).scalars().all()
        response_media = [{"media_type": m.media_type, "url": m.url, "caption": m.caption} for m in rm_result]

    # Resolve the picked Standard Response (if any) for the
    # response card label. UCAT pipe-3 (commit a6fd376 onwards):
    # the Pundit can pick a standard answer; that fires the QA
    # advisory pipe. The frontend renders a "Q&A — <question>"
    # marker on the response card when this is set.
    standard_response_question = None
    if response is not None and response.standard_response_id:
        sr_row = (await db.execute(
            select(StandardResponse).where(
                StandardResponse.id == response.standard_response_id,
            )
        )).scalar_one_or_none()
        standard_response_question = sr_row.question_text if sr_row else None

    # Resolve the picked Crop Health problem (if any) to its English
    # name so the Pundit's response card reads "Fruit Fly" instead of
    # a UUID. Same resolution path the farmer-side uses.
    response_problem_name = None
    if response is not None and response.problem_cosh_id:
        from app.modules.sync.models import CoshCoreItem
        rp_row = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == response.problem_cosh_id,
            )
        )).scalar_one_or_none()
        if rp_row and isinstance(rp_row.translations, dict):
            response_problem_name = pick_translation(
                rp_row.translations, current_user.language_code or "en", ""
            ) or None

    # Batch resolve every Cosh translation the response needs in
    # one query: query_types (Nature of Query), the crop name, and
    # the farmer's state + district. Cheaper than three separate
    # lookups and the union is small.
    from app.modules.sync.models import CoshCoreItem
    from app.modules.platform.models import User as _User
    from app.modules.subscriptions.models import Subscription as _Sub
    from app.modules.subscriptions.router import _compute_crop_age
    from app.services.cosh_crop_view import get_measure_for_biological_name

    farmer_user = (await db.execute(
        select(_User).where(_User.id == query.farmer_user_id)
    )).scalar_one_or_none()
    sub_row = (await db.execute(
        select(_Sub).where(_Sub.id == query.subscription_id)
    )).scalar_one_or_none()

    ref_ids: set[str] = set()
    if query.query_type_cosh_id: ref_ids.add(query.query_type_cosh_id)
    if query.crop_cosh_id: ref_ids.add(query.crop_cosh_id)
    if farmer_user:
        if farmer_user.state_cosh_id: ref_ids.add(farmer_user.state_cosh_id)
        if farmer_user.district_cosh_id: ref_ids.add(farmer_user.district_cosh_id)
    name_by_cosh_id: dict[str, str] = {}
    if ref_ids:
        lang = current_user.language_code or "en"
        for cid, tr in (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(ref_ids))
        )).all():
            label = (
                pick_translation(tr, lang, "")
                if isinstance(tr, dict) else None
            )
            if label:
                name_by_cosh_id[cid] = label

    query_type_name = (
        name_by_cosh_id.get(query.query_type_cosh_id)
        if query.query_type_cosh_id else None
    )
    crop_name = (
        name_by_cosh_id.get(query.crop_cosh_id)
        if query.crop_cosh_id else None
    )

    # Crop measure + computed age (years for plant-wise, days for
    # area-wise). Same `_compute_crop_age` the Crop Dashboard uses
    # so the Pundit and the farmer see the same number.
    crop_measure = "AREA_WISE"
    if query.crop_cosh_id:
        m = await get_measure_for_biological_name(db, query.crop_cosh_id)
        crop_measure = m or "AREA_WISE"
    computed_crop_age = _compute_crop_age(sub_row, crop_measure) if sub_row else None

    # Farmer card — name, phone (always visible to the Pundit who's
    # responding; phone_hidden on the FarmPundit profile is about
    # the Pundit's own phone, not the farmer's), address from the
    # User profile. Pundit can `tel:` the number when needed.
    farmer_block = None
    if farmer_user:
        farmer_block = {
            "name": farmer_user.name,
            "phone": farmer_user.phone,
            "address": {
                "town": farmer_user.town,
                "district": (
                    name_by_cosh_id.get(farmer_user.district_cosh_id)
                    if farmer_user.district_cosh_id else None
                ),
                "state": (
                    name_by_cosh_id.get(farmer_user.state_cosh_id)
                    if farmer_user.state_cosh_id else None
                ),
            },
        }

    return {
        "id": query.id,
        "title": query.title,
        "query_type_cosh_id": query.query_type_cosh_id,
        "query_type_name": query_type_name,
        "description": query.description,
        "severity": query.severity,
        # client_id surfaces to the response screen so it can search
        # the right company's standard library (the search endpoint
        # is client-scoped per spec §14.9).
        "client_id": query.client_id,
        "crop_cosh_id": query.crop_cosh_id,
        "crop_name": crop_name,
        "crop_measure": crop_measure,
        # `crop_age` is the FARMER-TYPED free-text string (e.g. "45 DAS")
        # — kept for backward compat. `computed_crop_age` is the
        # server-derived envelope ({value, unit, source}) preferred by
        # the new UI.
        "crop_age": query.crop_age,
        "computed_crop_age": computed_crop_age,
        "farmer": farmer_block,
        "status": query.status,
        "created_at": query.created_at,
        "expires_at": query.expires_at,
        "days_remaining": max(0, (query.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days),
        "is_holding": query.current_holder_id == profile.id,
        "media": [{"media_type": m.media_type, "url": m.url} for m in media_result],
        "remarks": [
            {
                "action": r.action, "pundit_id": r.pundit_id,
                "forwarded_to_pundit_id": r.forwarded_to_pundit_id,
                "remark": r.remark, "created_at": r.created_at,
            }
            for r in remarks
        ],
        "response": {
            "problem_cosh_id": response.problem_cosh_id,
            "problem_name": response_problem_name,
            "standard_response_id": response.standard_response_id,
            "standard_response_question": standard_response_question,
            "text": response.text,
            "media": response_media,
            "created_at": response.created_at,
        } if response else None,
    }


@router.get("/farmer/queries/{query_id}")
async def get_query_detail_farmer(
    query_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer sees their query with the pundit's response (if responded)."""
    query = (await db.execute(
        select(Query).where(Query.id == query_id, Query.farmer_user_id == current_user.id)
    )).scalar_one_or_none()
    if not query:
        raise HTTPException(status_code=404, detail="Query not found")

    response = (await db.execute(
        select(QueryResponse).where(QueryResponse.query_id == query_id)
    )).scalar_one_or_none()

    response_media = []
    if response:
        rm_result = (await db.execute(
            select(QueryResponseMedia).where(QueryResponseMedia.response_id == response.id)
        )).scalars().all()
        response_media = [{"media_type": m.media_type, "url": m.url, "caption": m.caption} for m in rm_result]

    media_result = (await db.execute(
        select(QueryMedia).where(QueryMedia.query_id == query_id)
    )).scalars().all()

    # Resolve problem_cosh_id to a friendly English name so the
    # farmer doesn't see "sp_blast_rice" / a UUID on the response card.
    problem_name = None
    if response and response.problem_cosh_id:
        from app.modules.sync.models import CoshCoreItem
        prow = (await db.execute(
            select(CoshCoreItem).where(CoshCoreItem.cosh_id == response.problem_cosh_id)
        )).scalar_one_or_none()
        if prow and isinstance(prow.translations, dict):
            problem_name = pick_translation(
                prow.translations, current_user.language_code or "en", ""
            ) or None

    return {
        "id": query.id,
        "title": query.title,
        "description": query.description,
        "severity": query.severity,
        "crop_cosh_id": query.crop_cosh_id,
        "crop_age": query.crop_age,
        "status": query.status,
        "created_at": query.created_at,
        "expires_at": query.expires_at,
        "media": [{"media_type": m.media_type, "url": m.url} for m in media_result],
        "response": {
            "text": response.text,
            "problem_cosh_id": response.problem_cosh_id,
            "problem_name": problem_name,
            "media": response_media,
            "created_at": response.created_at,
            "has_cha_recommendation": bool(response.problem_cosh_id),
        } if response else None,
    }


# ── Farmer: Set preferred FarmPundit ─────────────────────────────────────────

@router.post("/farmer/subscriptions/{subscription_id}/pundit-preference")
async def set_pundit_preference(
    subscription_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer sets their preferred expert for this subscription.

    BL-12 audit (2026-05-06): added subscription ownership check.

    PP V1 (2026-05-30): the contract is **phone-based** — the farmer
    types a phone number (informed by their promoter or RM), and the
    server validates that it belongs to an ACTIVE Promoter-Pundit at
    this subscription's Client. The legacy `pundit_id` path stays
    accepted for back-compat callers but the canonical input now is
    `{phone}`. Refuses with:
      - 422 phone_or_pundit_id_required  → neither given
      - 422 user_not_found               → no User has that phone
      - 422 not_a_promoter_pundit        → exists but no ACTIVE PP at
                                           this Client (the farmer
                                           was told the wrong number)
    """
    phone = (data.get("phone") or "").strip() if isinstance(data.get("phone"), str) else None
    pundit_id = data.get("pundit_id")
    if not phone and not pundit_id:
        raise HTTPException(status_code=422, detail={
            "code": "phone_or_pundit_id_required",
            "message": "phone (or legacy pundit_id) required.",
        })

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    # PP V1: resolve a typed phone to a Promoter-Pundit pundit_id at
    # this sub's Client. Same idiom as alerts B — refuse early with a
    # clear code so the PWA can surface the right message inline.
    if phone:
        from app.modules.clients.models import ClientPromoter
        target = (await db.execute(
            select(User).where(User.phone == phone)
        )).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=422, detail={
                "code": "user_not_found",
                "message": (
                    "No RootsTalk user is registered with this number. "
                    "Check the digits with your promoter or RM."
                ),
            })

        pp_profile = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.user_id == target.id)
        )).scalar_one_or_none()
        cfp = None
        if pp_profile is not None:
            cfp = (await db.execute(
                select(ClientFarmPundit).where(
                    ClientFarmPundit.client_id == sub.client_id,
                    ClientFarmPundit.pundit_id == pp_profile.id,
                    ClientFarmPundit.role == PunditRole.PROMOTER_PUNDIT,
                    ClientFarmPundit.status == "ACTIVE",
                )
            )).scalar_one_or_none()
        # Read-time eligibility — even if the ClientFarmPundit says
        # PP, the underlying ClientPromoter must still be ACTIVE +
        # is_promoter=True. Catches the "F-P stepped down but stale
        # PP row exists" case.
        is_fp_promoter = (await db.execute(
            select(ClientPromoter).where(
                ClientPromoter.client_id == sub.client_id,
                ClientPromoter.user_id == target.id,
                ClientPromoter.promoter_type == "FACILITATOR",
                ClientPromoter.is_promoter == True,  # noqa: E712
                ClientPromoter.status == "ACTIVE",
            )
        )).scalar_one_or_none() is not None

        if not (cfp and is_fp_promoter):
            raise HTTPException(status_code=422, detail={
                "code": "not_a_promoter_pundit",
                "message": (
                    "This number doesn't belong to a Promoter-Pundit for "
                    "this company. Your queries will go to the company's "
                    "expert team. Ask your promoter or RM for the correct "
                    "number."
                ),
            })
        pundit_id = pp_profile.id

    existing = (await db.execute(
        select(FarmPunditPreference).where(FarmPunditPreference.subscription_id == subscription_id)
    )).scalar_one_or_none()

    if existing:
        existing.pundit_id = pundit_id
        existing.set_at = datetime.now(timezone.utc)
    else:
        db.add(FarmPunditPreference(
            subscription_id=subscription_id,
            pundit_id=pundit_id,
        ))
    await db.commit()
    return {"detail": "Preference set", "pundit_id": pundit_id}


@router.delete("/farmer/subscriptions/{subscription_id}/pundit-preference")
async def clear_pundit_preference(
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer reverts to default expert routing for this subscription.

    BL-12 audit (2026-05-06): subscription ownership check added,
    matching set_pundit_preference.
    """
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    pref = (await db.execute(
        select(FarmPunditPreference).where(FarmPunditPreference.subscription_id == subscription_id)
    )).scalar_one_or_none()
    if pref:
        await db.delete(pref)
        await db.commit()
    return {"detail": "Reverted to default expert"}


# ── Company Queries Monitoring ────────────────────────────────────────────────

@router.get("/client/{client_id}/queries")
async def list_company_queries(
    client_id: str,
    status_filter: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_portal_member(db, current_user.id, client_id)
    q = select(Query).where(Query.client_id == client_id).order_by(Query.created_at.desc())
    if status_filter:
        q = q.where(Query.status == status_filter)
    queries = (await db.execute(q)).scalars().all()
    return [
        {
            "id": query.id, "title": query.title, "status": query.status,
            "severity": query.severity, "created_at": query.created_at,
            "expires_at": query.expires_at, "farmer_user_id": query.farmer_user_id,
            "current_holder_id": query.current_holder_id,
        }
        for query in queries
    ]


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_pundit_profile(db: AsyncSession, user_id: str) -> FarmPunditProfile:
    result = await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == user_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="FarmPundit profile not found. Please register first.")
    return profile


async def _get_query(db: AsyncSession, query_id: str) -> Query:
    result = await db.execute(select(Query).where(Query.id == query_id))
    q = result.scalar_one_or_none()
    if not q:
        raise HTTPException(status_code=404, detail="Query not found")
    return q


async def _get_invitation(db: AsyncSession, invitation_id: str) -> PunditInvitation:
    result = await db.execute(select(PunditInvitation).where(PunditInvitation.id == invitation_id))
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found")
    return inv


async def _get_next_pundit_for_query(
    db: AsyncSession,
    client_id: str,
    subscription_id: str,
) -> Optional[FarmPunditProfile]:
    """BL-12a: Full priority routing — preference → Promoter-Pundit → round-robin."""
    # Load all company pundits
    all_cp = (await db.execute(
        select(ClientFarmPundit).where(ClientFarmPundit.client_id == client_id)
    )).scalars().all()

    experts = [
        ExpertSlot(
            pundit_id=cp.pundit_id,
            role=cp.role.value if hasattr(cp.role, 'value') else str(cp.role),
            status=cp.status,
            round_robin_sequence=cp.round_robin_sequence or 0,
            onboarded_at=cp.onboarded_at,
        )
        for cp in all_cp
    ]

    # Priority 1: Farmer preference
    pref = (await db.execute(
        select(FarmPunditPreference).where(FarmPunditPreference.subscription_id == subscription_id)
    )).scalar_one_or_none()
    farmer_preferred = pref.pundit_id if pref else None

    # Priority 2: Promoter-Pundit (from promoter_assignments)
    from app.modules.subscriptions.models import PromoterAssignment
    assignment = (await db.execute(
        select(PromoterAssignment).where(
            PromoterAssignment.subscription_id == subscription_id,
            PromoterAssignment.status == "ACTIVE",
        ).order_by(PromoterAssignment.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()

    promoter_pundit_id = None
    if assignment:
        # Check if promoter is also a Promoter-Pundit for this client
        promoter_user = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.user_id == assignment.promoter_user_id)
        )).scalar_one_or_none()
        if promoter_user:
            pp_slot = next(
                (e for e in experts
                 if e.pundit_id == promoter_user.id and e.role == "PROMOTER_PUNDIT"),
                None,
            )
            if pp_slot:
                promoter_pundit_id = promoter_user.id

    # Last received pundit (for round-robin)
    last_remark = (await db.execute(
        select(QueryRemark)
        .join(Query, Query.id == QueryRemark.query_id)
        .where(Query.client_id == client_id, QueryRemark.action == QueryRemarkAction.RECEIVED)
        .order_by(QueryRemark.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    last_received_id = last_remark.pundit_id if last_remark else None

    # Run BL-12a service
    result = route_query(experts, farmer_preferred, promoter_pundit_id, last_received_id)
    if not result.pundit_id:
        return None

    return (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.id == result.pundit_id)
    )).scalar_one_or_none()


async def _next_round_robin_sequence(db: AsyncSession, client_id: str) -> int:
    result = await db.execute(
        select(ClientFarmPundit).where(
            ClientFarmPundit.client_id == client_id,
            ClientFarmPundit.role == PunditRole.PRIMARY,
        )
    )
    existing = result.scalars().all()
    return len(existing) + 1


async def _count_active_queries_for_pundit(
    db: AsyncSession, *, client_id: str, pundit_id: str,
) -> int:
    """Active = NEW / FORWARDED / RETURNED, currently held by this
    pundit. Used to gate role-change and delete in spec §14.5."""
    from sqlalchemy import func
    result = await db.execute(
        select(func.count(Query.id)).where(
            Query.client_id == client_id,
            Query.current_holder_id == pundit_id,
            Query.status.in_([
                QueryStatus.NEW,
                QueryStatus.FORWARDED,
                QueryStatus.RETURNED,
            ]),
        )
    )
    return result.scalar() or 0


async def _trigger_cha_for_query(db: AsyncSession, query: Query, problem_cosh_id: str):
    """
    §14.7/14.8: When pundit identifies a problem, deliver the corresponding
    CHA recommendation using the full SP→PG hierarchy:
    1. SP (client-specific for exact specific_problem_cosh_id)
    2. PG (client-specific for parent problem_group)
    3. PG (global for parent problem_group)
    """
    from app.modules.subscriptions.models import TriggeredCHAEntry
    from app.services.cha_hierarchy import resolve_cha_recommendation

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == query.farmer_user_id,
            Subscription.client_id == query.client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        ).order_by(Subscription.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sub:
        return

    # Pass the subscription's crop so the resolver scopes PG to the
    # right area-wise / plant-wise bundle (post-CHA-PG-Round-1).
    from app.modules.advisory.models import Package as _Pkg
    package = (await db.execute(
        select(_Pkg).where(_Pkg.id == sub.package_id)
    )).scalar_one_or_none()
    sub_crop = package.crop_cosh_id if package else None

    farmer = (await db.execute(
        select(User).where(User.id == query.farmer_user_id)
    )).scalar_one_or_none()
    resolved = await resolve_cha_recommendation(
        db, query.client_id, problem_cosh_id, crop_cosh_id=sub_crop,
        lang=(farmer.language_code if farmer else None) or "en",
    )
    if not resolved:
        return

    db.add(TriggeredCHAEntry(
        subscription_id=sub.id,
        farmer_user_id=query.farmer_user_id,
        client_id=query.client_id,
        problem_cosh_id=problem_cosh_id,
        recommendation_type=resolved.recommendation_type,
        recommendation_id=resolved.recommendation_id,
        triggered_by="QUERY",
        problem_name=resolved.problem_name,
        parent_pg_cosh_id=resolved.parent_pg_cosh_id,
    ))


async def _trigger_qa_for_query(
    db: AsyncSession, query: Query, standard_response_id: str,
):
    """§14.9: When a Pundit picks a Standard Response, deliver the
    advisory's Timelines into the farmer's plan. Mirrors
    `_trigger_cha_for_query` — same TriggeredCHAEntry table — but
    `recommendation_type='QA'` and `recommendation_id` points at the
    Standard Response. The PWA-side render branches on
    recommendation_type to walk the right Timeline source: PG/SP go
    via pg_recommendations.id / sp_recommendations.id, QA goes via
    standard_responses.id (which after Sub-batch 1 is a valid
    parent of pg_timelines).

    Silent no-op (matches CHA path) when:
      - the Pundit picked a SR that doesn't belong to the query's
        client (cross-client guess);
      - the farmer has no active subscription with this client.
    These are diagnostic-only edge cases; logging not warranted.
    """
    from app.modules.farmpundit.models import StandardResponse
    from app.modules.subscriptions.models import TriggeredCHAEntry

    sr = (await db.execute(
        select(StandardResponse).where(
            StandardResponse.id == standard_response_id,
            StandardResponse.client_id == query.client_id,
            StandardResponse.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if not sr:
        return

    sub = (await db.execute(
        select(Subscription).where(
            Subscription.farmer_user_id == query.farmer_user_id,
            Subscription.client_id == query.client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        ).order_by(Subscription.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not sub:
        return

    # problem_name ≈ what farmer-side cards show. For QA, the
    # question is the most useful label. Truncate to fit the column.
    label = (sr.question_text or "")[:500]

    db.add(TriggeredCHAEntry(
        subscription_id=sub.id,
        farmer_user_id=query.farmer_user_id,
        client_id=query.client_id,
        problem_cosh_id=None,           # not a CHA problem
        recommendation_type="QA",
        recommendation_id=sr.id,
        triggered_by="QUERY",
        problem_name=label,
        parent_pg_cosh_id=None,
        # 2026-06-30 — Carry the optional count the farmer entered at
        # query submission. NULL when blank — dealer surface renders
        # "Please check with the farmer" and the volume estimate skips.
        affected_plants_count=query.affected_plants_count,
    ))
