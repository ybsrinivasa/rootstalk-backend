"""Coaching Sandbox — SA-portal-facing HTTP endpoints. Student
self-registration (public, token-gated) will land in a sibling
`public_router.py` in Phase 3.

Auth pattern: every endpoint requires a bearer token, and the caller
must either be SA (identified by email match against settings.sa_email)
or hold the COACH role. Enforced via `require_coach_or_sa` dependency.
Session-scoped endpoints additionally check that the caller owns the
session (unless SA).
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, or_, select, update

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.coaching import service as coaching_service
from app.modules.coaching.models import (
    CoachingSession, CoachingSessionStatus, CoachingStudent,
    CoachingStudentInvite, StudentCV, StudentPortfolio,
)
from app.modules.coaching.schemas import (
    AssignPwaRolesRequest, CertificatePublicView, CertifiedRecord,
    CertifyStudentRequest, CreateSessionRequest, CreatedInviteResponse,
    InviteContextResponse, InviteStudentRequest, SessionDetail,
    SessionListItem, StudentRegistrationForm, SubmitInviteResponse,
)
from app.modules.platform.models import User


router = APIRouter(prefix="/coaching", tags=["Coaching Sandbox"])


# ── Auth dependency ──────────────────────────────────────────────────────

async def require_coach_or_sa(
    current_user: User = Depends(get_current_user),
) -> User:
    """403 unless the caller holds the COACH role (any active status
    on UserRole) or is the SA. See coaching_service.is_coach_or_sa."""
    if not coaching_service.is_coach_or_sa(current_user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "coach_role_required",
                "message": (
                    "You need the COACH role (or SA privileges) to access "
                    "the coaching sandbox."
                ),
            },
        )
    return current_user


# ── Session endpoints ────────────────────────────────────────────────────

@router.get("/sessions", response_model=list[SessionListItem])
async def list_sessions(
    mine_only: bool = True,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """List coaching sessions visible to the caller.

    - Non-SA coaches always see only their own sessions.
    - SA sees everything by default; can pass `mine_only=true` to
      filter to sessions they own.
    - Optional `status` query filter (DRAFT / ACTIVE / CLOSED_MANUAL
      / CLOSED_AUTO).
    """
    return await coaching_service.list_sessions_for(
        db, current_user, mine_only=mine_only, status_filter=status,
    )


@router.post("/sessions", response_model=SessionDetail, status_code=201)
async def create_session(
    request: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Create a DRAFT coaching session against a real reference client.

    Refuses if the reference client already has an open coaching session
    (409 session_already_open) or if it isn't a real client (422).
    """
    session = await coaching_service.create_session(
        db, coach=current_user, reference_client_id=request.reference_client_id,
    )
    return await coaching_service.load_session_detail(db, session)


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Full session detail (session + invites + students). Used by the
    session detail page in the SA portal — covers all three lifecycle
    states (DRAFT / ACTIVE / CLOSED)."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    return await coaching_service.load_session_detail(db, session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Delete a DRAFT session (only allowed on drafts with zero approved
    students). Cascade-deletes any pending invites via FK."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    await coaching_service.delete_draft_session(db, session)


@router.post("/sessions/{session_id}/start", response_model=SessionDetail)
async def start_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """DRAFT → ACTIVE. Requires at least one approved student. Freezes
    the invite roster and starts the 30-day auto-close clock. Fires
    session-started emails to each approved student."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    session = await coaching_service.start_session(db, session)
    return await coaching_service.load_session_detail(db, session)


@router.post("/sessions/{session_id}/close", response_model=SessionDetail)
async def close_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """ACTIVE → CLOSED_MANUAL. Coaching workspaces remain in place for
    post-close certification review."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    session = await coaching_service.close_session(
        db, session, closed_by=current_user, manual=True,
    )
    return await coaching_service.load_session_detail(db, session)


# ── Invite endpoints ─────────────────────────────────────────────────────

@router.post(
    "/sessions/{session_id}/invites",
    response_model=CreatedInviteResponse, status_code=201,
)
async def create_student_invite(
    session_id: str,
    request: InviteStudentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Coach adds a student by email. Session must be DRAFT. Sends the
    invite email best-effort; the response echoes the invite link back
    so the coach can re-copy it manually if delivery fails."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    invite, link = await coaching_service.create_invite(
        db, session, email=request.email, coach=current_user,
    )
    return CreatedInviteResponse(
        id=invite.id,
        email=invite.email,
        status=invite.status,
        invite_link=link,
        expires_at=invite.expires_at,
    )


@router.post(
    "/sessions/{session_id}/invites/{invite_id}/approve",
    response_model=SessionDetail,
)
async def approve_student_invite(
    session_id: str,
    invite_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Approve a SUBMITTED invite: provisions the student's User,
    workspace Client, ClientUser(CA) row, CoachingStudent record,
    and fires the credentials email. Refuses if the submitted phone
    already belongs to a real user."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    invite = (await db.execute(
        select(CoachingStudentInvite).where(
            CoachingStudentInvite.id == invite_id,
            CoachingStudentInvite.session_id == session.id,
        )
    )).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    await coaching_service.approve_invite(db, invite, coach=current_user)
    return await coaching_service.load_session_detail(db, session)


@router.post(
    "/sessions/{session_id}/invites/{invite_id}/reject",
    response_model=SessionDetail,
)
async def reject_student_invite(
    session_id: str,
    invite_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Reject a SUBMITTED invite. No user or workspace is provisioned."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    invite = (await db.execute(
        select(CoachingStudentInvite).where(
            CoachingStudentInvite.id == invite_id,
            CoachingStudentInvite.session_id == session.id,
        )
    )).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    await coaching_service.reject_invite(db, invite, coach=current_user)
    return await coaching_service.load_session_detail(db, session)


@router.post(
    "/sessions/{session_id}/invites/{invite_id}/regenerate",
    response_model=SessionDetail,
)
async def regenerate_student_invite(
    session_id: str,
    invite_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Void the current invite and create a fresh one with a new
    token for the same email. Pre-approval only — usable while the
    invite is INVITED or SUBMITTED, refused for APPROVED / REJECTED
    / VOID. See `regenerate_invite` service for the full semantics.
    """
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    invite = (await db.execute(
        select(CoachingStudentInvite).where(
            CoachingStudentInvite.id == invite_id,
            CoachingStudentInvite.session_id == session.id,
        )
    )).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    await coaching_service.regenerate_invite(db, invite, coach=current_user)
    return await coaching_service.load_session_detail(db, session)


# ── Student endpoints ────────────────────────────────────────────────────

@router.put(
    "/sessions/{session_id}/students/{student_id}/pwa-roles",
    response_model=SessionDetail,
)
async def assign_pwa_roles(
    session_id: str,
    student_id: str,
    request: AssignPwaRolesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Grant PWA roles to a student. Additive — roles previously
    granted but not in the new list are retained (avoids orphaning
    in-flight PWA activity). DEALER + FACILITATOR can coexist in
    coaching context.
    """
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    await coaching_service.assign_pwa_roles(
        db, session, student, roles=request.roles,
    )
    return await coaching_service.load_session_detail(db, session)


# ── Student reference-client profile (READ-ONLY) ─────────────────────────


@router.get("/my/reference-client-profile")
async def get_my_reference_client_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only view of the reference client's Company Profile,
    scoped to the caller's coaching session. Powers the "Company
    Profile" page inside a coaching workspace — instead of showing
    the workspace's empty shell, we show the reference client's
    fully-configured profile so the student sees what a real
    Company Profile looks like.

    Auth: caller must be a CoachingStudent whose session is in
    ACTIVE or GRACE access state (hybrid-access, 2026-10-15 — GRACE
    students retain writeable workspace access for 30 days post-close
    and must keep seeing this reference profile). 404 for
    non-students and ARCHIVED students (workspace is read-only post-
    grace; UI hides the Company Profile tile).

    Fix 2026-10-28: the original query filtered to `status.in_([DRAFT,
    ACTIVE])` which excluded GRACE (CLOSED_MANUAL / CLOSED_AUTO within
    the 30-day window). GRACE students landed on the Company Profile
    page, got a 404 from this endpoint, and saw a brief flicker before
    the UI rendered the "Could not load profile. Please refresh."
    error block. Same bug class as the earlier `load_coaching_context`
    filter — see feedback_grep_every_session_status_filter_when_adding_
    access_state.md for the audit recipe.

    Bypasses the tenant guard cleanly (dedicated endpoint, no path
    param, so the cross_client_forbidden gate doesn't fire).

    Response shape matches GET /client/{cid}/profile so the existing
    profile page's data model can consume this endpoint by swapping
    the URL — no schema changes needed on the frontend.
    """
    from app.modules.clients.models import (
        Client, ClientOrganisationType,
    )
    from app.modules.coaching.models import CoachingSessionAccessState
    row = (await db.execute(
        select(CoachingStudent, CoachingSession, Client)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .join(Client, Client.id == CoachingSession.reference_client_id)
        .where(CoachingStudent.user_id == current_user.id)
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not a coaching student")
    _cs, _sess, ref_client = row
    if _sess.access_state() not in (
        CoachingSessionAccessState.ACTIVE,
        CoachingSessionAccessState.GRACE,
    ):
        raise HTTPException(
            status_code=404,
            detail="Reference client profile is not available after the coaching grace period ends.",
        )
    org_types = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id)
        .where(ClientOrganisationType.client_id == ref_client.id)
    )).scalars().all()
    return {
        "id": ref_client.id, "short_name": ref_client.short_name,
        "display_name": ref_client.display_name, "tagline": ref_client.tagline,
        "logo_url": ref_client.logo_url,
        "primary_colour": ref_client.primary_colour,
        "secondary_colour": ref_client.secondary_colour,
        "hq_address": ref_client.hq_address,
        "gst_number": ref_client.gst_number, "pan_number": ref_client.pan_number,
        "website": ref_client.website,
        "support_phone": ref_client.support_phone,
        "office_phone": ref_client.office_phone,
        "social_links": ref_client.social_links or {},
        "org_type_cosh_ids": list(org_types),
        "ca_name": ref_client.ca_name, "ca_email": ref_client.ca_email,
        "status": ref_client.status.value,
        "approved_at": ref_client.approved_at,
    }


# ── Reference-client picker (coach/SA) ───────────────────────────────────


@router.get("/reference-clients")
async def list_reference_clients(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Coach-scoped list of clients eligible to be the reference for
    a new coaching session. Only ACTIVE real clients (not training
    children, not coaching workspaces) — those are the only ones
    that make sense as a reference for grooming students to work
    with. Narrow response (id, full_name, short_name) — coach only
    needs to pick, not manage.

    Exists because /admin/clients is SA-only; without this a non-SA
    COACH user gets a 403 + empty picker when trying to create a
    session (SA works fine either way).
    """
    from app.modules.clients.models import Client, ClientStatus
    rows = (await db.execute(
        select(Client.id, Client.full_name, Client.short_name)
        .where(
            Client.status == ClientStatus.ACTIVE,
            Client.is_training.is_(False),
            Client.is_coaching.is_(False),
        )
        .order_by(Client.full_name)
    )).all()
    return [
        {"id": r.id, "full_name": r.full_name, "short_name": r.short_name}
        for r in rows
    ]


# ── Certificate generation + registry (coach/SA auth) ────────────────────


@router.post(
    "/sessions/{session_id}/students/{student_id}/certificate/generate",
    response_model=SessionDetail,
)
async def generate_student_certificate(
    session_id: str,
    student_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Generate the PDF certificate, upload to S3, email it to the
    student. Idempotent on the certificate_number — regeneration
    keeps the same number so verification URLs stay stable across
    grade corrections."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    await coaching_service.generate_certificate(db, session, student)
    return await coaching_service.load_session_detail(db, session)


@router.get("/certified", response_model=list[CertifiedRecord])
async def list_certified_students(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """SA-portal registry: all certified students across sessions.
    SA sees everything; a non-SA coach sees only their own sessions'
    certifications."""
    return await coaching_service.load_certified_students(db, current_user)


# ── Public certificate verification (NO AUTH) ────────────────────────────


@router.get(
    "/certificates/{certificate_number}",
    response_model=CertificatePublicView,
)
async def get_certificate_public(
    certificate_number: str,
    db: AsyncSession = Depends(get_db),
):
    """Public verification endpoint — anyone with the certificate
    number (from the PDF footer or QR code) can query this to confirm
    authenticity. Returns narrow set of fields — no email, phone,
    workspace id, or coach email leaked."""
    data = await coaching_service.load_certificate_public(db, certificate_number)
    if data is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    return data


# ── Public student self-registration endpoints (NO AUTH) ─────────────────
# The student receives the token via emailed invite link. These two
# endpoints DELIBERATELY do NOT depend on require_coach_or_sa or
# get_current_user — the whole point is that the student registers
# BEFORE they have a user account. Token possession is the auth.


@router.get("/join/{token}", response_model=InviteContextResponse)
async def get_invite_context(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Public — the student's self-registration form calls this to
    render context (coach name, reference client name) and to check
    whether the invite is still actionable. Returns 404 for any
    invalid / unknown token (no distinction between never-existed
    and expired-and-scrubbed — prevents token enumeration)."""
    invite, session, ref_client, coach = (
        await coaching_service.load_invite_by_token(db, token)
    )
    return InviteContextResponse(
        email=invite.email,
        coach_name=coach.name,
        reference_client_name=ref_client.full_name,
        status=invite.status,
        expires_at=invite.expires_at,
        already_submitted=invite.status == "SUBMITTED",
        can_submit=coaching_service.can_submit_invite(invite, session),
    )


@router.post("/join/{token}/submit", response_model=SubmitInviteResponse)
async def submit_invite_form(
    token: str,
    form: StudentRegistrationForm,
    db: AsyncSession = Depends(get_db),
):
    """Public — student submits the self-registration form. Fails
    fast with 422 if the phone is already tied to a real user
    (approved-phone exclusivity) so the student can correct on the
    spot. On success, the invite flips to SUBMITTED and the coach
    sees it in their pending-approvals queue."""
    invite = await coaching_service.submit_student_form(
        db, token, form=form.model_dump(),
    )
    return SubmitInviteResponse(
        status=invite.status,
        submitted_at=invite.submitted_at,
        message=(
            "Your details have been submitted. Your coach will review "
            "them and you will receive a confirmation email once you "
            "are enrolled."
        ),
    )


@router.post(
    "/sessions/{session_id}/students/{student_id}/view-workspace",
)
async def view_student_workspace(
    session_id: str,
    student_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Mint a short-lived, read-only JWT the coach can use to open
    the student's workspace in a new tab. Refuses if the coach doesn't
    own the session (require_session_owner conflates
    unauthorised/missing) or if the student isn't in the session.

    Works on ACTIVE and CLOSED sessions — post-close viewing is a
    legitimate certification-review window. The read-only guarantee
    is enforced at request time by `get_current_user`, which refuses
    any non-safe HTTP method on tokens carrying `coach_view=True`.
    """
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    return await coaching_service.mint_coach_view_token(
        db, session, student, coach=current_user,
    )


@router.post(
    "/certified/{coaching_student_id}/view-workspace",
)
async def view_certified_student_workspace(
    coaching_student_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Same as `sessions/{sid}/students/{stid}/view-workspace`, but
    addressable by CoachingStudent id alone — powers the "View" button
    on `/coaching/certified`, which lists across sessions and doesn't
    have a session_id column handy in the row. Ownership check is
    routed through the parent session so a coach can only view
    students in sessions they own (SA sees all)."""
    student = (await db.execute(
        select(CoachingStudent).where(CoachingStudent.id == coaching_student_id)
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    session = await coaching_service.require_session_owner(
        db, student.session_id, current_user,
    )
    return await coaching_service.mint_coach_view_token(
        db, session, student, coach=current_user,
    )


@router.post(
    "/sessions/{session_id}/students/{student_id}/certify",
    response_model=SessionDetail,
)
async def certify_student(
    session_id: str,
    student_id: str,
    request: CertifyStudentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Mark student certified / uncertified. Only meaningful post-close
    (session must be in CLOSED_MANUAL or CLOSED_AUTO state).
    Toggling allowed — the certified_by field always reflects the
    caller of the most recent set."""
    session = await coaching_service.require_session_owner(
        db, session_id, current_user,
    )
    student = (await db.execute(
        select(CoachingStudent).where(
            CoachingStudent.id == student_id,
            CoachingStudent.session_id == session.id,
        )
    )).scalar_one_or_none()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")
    await coaching_service.set_certification(
        db, session, student, coach=current_user,
        certified=request.certified, grade=request.grade,
    )
    return await coaching_service.load_session_detail(db, session)


# ── Student portfolio (2026-11-04) ────────────────────────────────────────
#
# PWA endpoints under /me/portfolio let a CERTIFIED coaching student build
# up two things:
#   - Testimonial profile: photo + short video + written testimonial, each
#     opt-in for marketing use via its own consent flag.
#   - Hiring profile: a versioned CV + employment status + an opt-in to
#     share the CV with partner companies.
#
# SA Portal endpoints under /coaching/portfolios expose the aggregated
# view so the SA / coach can collect testimonials and forward CVs.
#
# Gate for the student side: at least one CoachingStudent row for this
# user_id with certified_at IS NOT NULL. Uncertifying a student re-closes
# the surface without touching their uploaded data.


_PORTFOLIO_UPLOAD_SPECS = {
    # (folder, allowed_types, max_bytes)
    "photo": ("portfolio-photos", "IMAGE", 10 * 1024 * 1024),
    "video": ("portfolio-videos", "VIDEO", 100 * 1024 * 1024),
    "cv":    ("portfolio-cvs",    "PDF",   10 * 1024 * 1024),
}


async def _require_certified_student(
    db: AsyncSession, user_id: str,
) -> None:
    """403 unless the caller has at least one CoachingStudent row where
    certified_at IS NOT NULL. Portfolio surfaces are earned-after-cert;
    uncertifying re-closes them without touching uploads."""
    row = (await db.execute(
        select(CoachingStudent.id).where(
            CoachingStudent.user_id == user_id,
            CoachingStudent.certified_at.isnot(None),
        ).limit(1)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "portfolio_requires_certification",
                "message": (
                    "The portfolio surface is available after you've "
                    "been certified on a coaching session."
                ),
            },
        )


async def _get_or_create_portfolio(
    db: AsyncSession, user_id: str,
) -> StudentPortfolio:
    """Lazy singleton — returns the user's portfolio row, inserting a
    blank one on first access so callers never have to branch on None."""
    row = (await db.execute(
        select(StudentPortfolio).where(StudentPortfolio.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        row = StudentPortfolio(user_id=user_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _portfolio_response(
    portfolio: StudentPortfolio, cvs: list[StudentCV],
) -> dict:
    """Shape shared by GET /me/portfolio and GET /coaching/portfolios/
    {user_id}. Flat so the PWA / SA Portal can bind directly."""
    return {
        "user_id": portfolio.user_id,
        "photo_url": portfolio.photo_url,
        "video_url": portfolio.video_url,
        "testimonial_text": portfolio.testimonial_text,
        "marketing_consent_photo": portfolio.marketing_consent_photo,
        "marketing_consent_video": portfolio.marketing_consent_video,
        "marketing_consent_testimonial": portfolio.marketing_consent_testimonial,
        "is_employed": portfolio.is_employed,
        "employer_name": portfolio.employer_name,
        "employed_since": (
            portfolio.employed_since.isoformat()
            if portfolio.employed_since else None
        ),
        "is_open_to_offers": portfolio.is_open_to_offers,
        "open_to_offers_notes": portfolio.open_to_offers_notes,
        "hiring_consent_share_cv": portfolio.hiring_consent_share_cv,
        "cvs": [
            {
                "id": cv.id,
                "file_url": cv.file_url,
                "filename": cv.filename,
                "is_latest": cv.is_latest,
                "uploaded_at": cv.uploaded_at.isoformat(),
            }
            for cv in cvs
        ],
        "updated_at": portfolio.updated_at.isoformat(),
    }


async def _load_cvs(db: AsyncSession, user_id: str) -> list[StudentCV]:
    return (await db.execute(
        select(StudentCV)
        .where(StudentCV.user_id == user_id)
        .order_by(StudentCV.uploaded_at.desc())
    )).scalars().all()


@router.get("/me/portfolio-eligible")
async def portfolio_eligible(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cheap probe for the PWA Profile page: does this caller have a
    CoachingStudent row with certified_at IS NOT NULL? Drives the
    "My Portfolio" tile's visibility without the PWA having to catch
    403s from the main portfolio endpoint. Returns `{eligible: bool}`."""
    row = (await db.execute(
        select(CoachingStudent.id).where(
            CoachingStudent.user_id == current_user.id,
            CoachingStudent.certified_at.isnot(None),
        ).limit(1)
    )).scalar_one_or_none()
    return {"eligible": row is not None}


@router.get("/me/portfolio")
async def get_my_portfolio(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return this student's portfolio, creating a blank row on first
    read. Gated on certification."""
    await _require_certified_student(db, current_user.id)
    portfolio = await _get_or_create_portfolio(db, current_user.id)
    cvs = await _load_cvs(db, current_user.id)
    return _portfolio_response(portfolio, cvs)


@router.put("/me/portfolio/marketing")
async def update_my_portfolio_marketing(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Patch the marketing-facing fields: photo/video/testimonial URLs +
    the three per-asset consent flags. The PWA uploads a file via
    POST /me/portfolio/upload (which returns a URL), then PUTs the URL
    here — keeps the upload-vs-state concerns separate."""
    await _require_certified_student(db, current_user.id)
    portfolio = await _get_or_create_portfolio(db, current_user.id)
    for field in (
        "photo_url", "video_url", "testimonial_text",
        "marketing_consent_photo", "marketing_consent_video",
        "marketing_consent_testimonial",
    ):
        if field in body:
            setattr(portfolio, field, body[field])
    await db.commit()
    await db.refresh(portfolio)
    cvs = await _load_cvs(db, current_user.id)
    return _portfolio_response(portfolio, cvs)


@router.put("/me/portfolio/employment")
async def update_my_portfolio_employment(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Patch the hiring-facing fields: is_employed + employer_name +
    employed_since, is_open_to_offers + open_to_offers_notes, and the
    hiring_consent_share_cv flag. employed-but-looking is legal — both
    is_employed and is_open_to_offers may be true."""
    await _require_certified_student(db, current_user.id)
    portfolio = await _get_or_create_portfolio(db, current_user.id)
    from datetime import date as _date
    for field in (
        "is_employed", "employer_name", "is_open_to_offers",
        "open_to_offers_notes", "hiring_consent_share_cv",
    ):
        if field in body:
            setattr(portfolio, field, body[field])
    if "employed_since" in body:
        val = body["employed_since"]
        if val is None or val == "":
            portfolio.employed_since = None
        else:
            try:
                portfolio.employed_since = _date.fromisoformat(val)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail="employed_since must be YYYY-MM-DD.",
                )
    await db.commit()
    await db.refresh(portfolio)
    cvs = await _load_cvs(db, current_user.id)
    return _portfolio_response(portfolio, cvs)


@router.post("/me/portfolio/upload")
async def upload_my_portfolio_asset(
    asset_type: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a photo or short video to S3 and return the URL. Caller
    then PUTs the URL into /me/portfolio/marketing. For CV uploads use
    POST /me/portfolio/cv instead — that endpoint also records the
    StudentCV row + flips is_latest."""
    await _require_certified_student(db, current_user.id)
    if asset_type not in ("photo", "video"):
        raise HTTPException(
            status_code=422,
            detail="asset_type must be 'photo' or 'video'. For CVs POST to /me/portfolio/cv.",
        )
    folder, kind, cap = _PORTFOLIO_UPLOAD_SPECS[asset_type]
    from app.modules.media.router import (
        IMAGE_CONTENT_TYPES, VIDEO_CONTENT_TYPES, upload_to_s3,
    )
    allowed = IMAGE_CONTENT_TYPES if kind == "IMAGE" else VIDEO_CONTENT_TYPES
    return await upload_to_s3(
        file, folder, allowed_types=allowed, max_size_bytes=cap,
    )


@router.post("/me/portfolio/cv", status_code=201)
async def upload_my_cv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a new CV PDF. Flips `is_latest=false` on the previous
    latest row and inserts the new row with `is_latest=true`. The
    partial unique index enforces at most one latest row per user — a
    concurrent duplicate insert would bounce at the DB level, so the
    flip-then-insert sequence is race-safe."""
    await _require_certified_student(db, current_user.id)
    folder, _kind, cap = _PORTFOLIO_UPLOAD_SPECS["cv"]
    from app.modules.media.router import DOCUMENT_CONTENT_TYPES, upload_to_s3
    result = await upload_to_s3(
        file, folder,
        allowed_types=DOCUMENT_CONTENT_TYPES,  # PDF-only
        max_size_bytes=cap,
    )
    await db.execute(
        update(StudentCV)
        .where(
            StudentCV.user_id == current_user.id,
            StudentCV.is_latest.is_(True),
        )
        .values(is_latest=False)
    )
    row = StudentCV(
        user_id=current_user.id,
        file_url=result["url"],
        filename=(file.filename or "cv.pdf")[:500],
        is_latest=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": row.id,
        "file_url": row.file_url,
        "filename": row.filename,
        "is_latest": row.is_latest,
        "uploaded_at": row.uploaded_at.isoformat(),
    }


@router.delete("/me/portfolio/cv/{cv_id}", status_code=204)
async def delete_my_cv(
    cv_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete one CV version. If it was the latest, the next-most-
    recent remaining CV is promoted to `is_latest=true` so the SA
    portal still has a current CV to forward. If no CVs remain, the
    student's portfolio simply shows an empty CV list — unchanged
    consent flag stays as the student set it (they may upload a new
    CV later)."""
    await _require_certified_student(db, current_user.id)
    row = (await db.execute(
        select(StudentCV).where(
            StudentCV.id == cv_id,
            StudentCV.user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="CV not found")
    was_latest = row.is_latest
    await db.delete(row)
    await db.flush()
    if was_latest:
        next_latest = (await db.execute(
            select(StudentCV)
            .where(StudentCV.user_id == current_user.id)
            .order_by(StudentCV.uploaded_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if next_latest is not None:
            next_latest.is_latest = True
    await db.commit()


# ── SA Portal — browse / forward portfolios ──────────────────────────────


@router.get("/portfolios")
async def list_student_portfolios(
    has_testimonial: Optional[bool] = None,
    has_cv: Optional[bool] = None,
    shared_cv_consent: Optional[bool] = None,
    is_open_to_offers: Optional[bool] = None,
    is_employed: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """SA/coach view. Lists certified students with their portfolio +
    latest CV, filterable so the SA can shortlist (e.g. "all open-to-
    offers students who have shared their CV")."""
    # Only students who ever got certified are eligible to have a
    # portfolio visible here. If a user had multiple cohorts, dedupe
    # by user_id.
    cert_user_ids_subq = (
        select(CoachingStudent.user_id)
        .where(CoachingStudent.certified_at.isnot(None))
        .distinct()
        .scalar_subquery()
    )
    q = select(StudentPortfolio).where(
        StudentPortfolio.user_id.in_(cert_user_ids_subq)
    )
    if has_testimonial is True:
        q = q.where(StudentPortfolio.testimonial_text.isnot(None))
        q = q.where(StudentPortfolio.testimonial_text != "")
    elif has_testimonial is False:
        q = q.where(
            or_(
                StudentPortfolio.testimonial_text.is_(None),
                StudentPortfolio.testimonial_text == "",
            )
        )
    if shared_cv_consent is not None:
        q = q.where(
            StudentPortfolio.hiring_consent_share_cv == shared_cv_consent
        )
    if is_open_to_offers is not None:
        q = q.where(
            StudentPortfolio.is_open_to_offers == is_open_to_offers
        )
    if is_employed is not None:
        q = q.where(StudentPortfolio.is_employed == is_employed)
    portfolios = (await db.execute(q)).scalars().all()
    # Optionally also require at least one CV on file.
    results: list[dict] = []
    for p in portfolios:
        cvs = await _load_cvs(db, p.user_id)
        if has_cv is True and not cvs:
            continue
        if has_cv is False and cvs:
            continue
        # Include the user's display name for the SA Portal list row.
        user = await db.get(User, p.user_id)
        row = _portfolio_response(p, cvs)
        row["student_name"] = (user.full_name if user else None) or (
            user.email if user else None
        )
        row["student_email"] = user.email if user else None
        results.append(row)
    return results


@router.get("/portfolios/{user_id}")
async def get_student_portfolio(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_coach_or_sa),
):
    """Detailed view of one certified student's portfolio."""
    # Confirm this user_id was ever certified — avoids leaking a
    # portfolio built up by someone who was never a student.
    cert = (await db.execute(
        select(CoachingStudent.id).where(
            CoachingStudent.user_id == user_id,
            CoachingStudent.certified_at.isnot(None),
        ).limit(1)
    )).scalar_one_or_none()
    if cert is None:
        raise HTTPException(status_code=404, detail="Student not found")
    portfolio = (await db.execute(
        select(StudentPortfolio).where(StudentPortfolio.user_id == user_id)
    )).scalar_one_or_none()
    if portfolio is None:
        # Student is certified but has not opened their portfolio yet.
        # Return an empty-but-valid shape so the SA Portal can render
        # without branching.
        portfolio = StudentPortfolio(user_id=user_id)
    cvs = await _load_cvs(db, user_id)
    user = await db.get(User, user_id)
    row = _portfolio_response(portfolio, cvs)
    row["student_name"] = (user.full_name if user else None) or (
        user.email if user else None
    )
    row["student_email"] = user.email if user else None
    return row
