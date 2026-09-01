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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.coaching import service as coaching_service
from app.modules.coaching.models import (
    CoachingSession, CoachingSessionStatus, CoachingStudent,
    CoachingStudentInvite,
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

    Auth: caller must be a CoachingStudent in a non-CLOSED session.
    404 for non-students / closed-session students. Bypasses the
    tenant guard cleanly (dedicated endpoint, no path param, so
    the cross_client_forbidden gate doesn't fire).

    Response shape matches GET /client/{cid}/profile so the existing
    profile page's data model can consume this endpoint by swapping
    the URL — no schema changes needed on the frontend.
    """
    from app.modules.clients.models import (
        Client, ClientOrganisationType,
    )
    row = (await db.execute(
        select(CoachingStudent, CoachingSession, Client)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .join(Client, Client.id == CoachingSession.reference_client_id)
        .where(
            CoachingStudent.user_id == current_user.id,
            CoachingSession.status.in_([
                CoachingSessionStatus.DRAFT.value,
                CoachingSessionStatus.ACTIVE.value,
            ]),
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not a coaching student in an active session")
    _cs, _sess, ref_client = row
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
