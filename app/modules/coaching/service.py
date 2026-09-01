"""Coaching Sandbox service layer — session lifecycle, invite
processing, student provisioning. Called from the SA-portal router in
`app/modules/coaching/router.py`.

Design principles:
  - **Isolation invariant**: every entity a student creates
    (packages, CHA, orders, etc.) foreign-keys to their workspace
    Client. Since the workspace is `is_coaching=true`, it's hidden
    from the `v_real_clients` view and from any read path that
    filters `is_coaching=false`. Nothing student-created can leak
    into real-farmer surfaces.
  - **Approved-phone exclusivity**: at invite-approval time, we
    refuse if the student-provided phone is already tied to any
    real user. Enforced in `_ensure_phone_available_for_student`.
  - **No cross-workspace login**: the student's provisioned User is
    scoped exclusively to their workspace via `ClientUser(CA)`. Only
    the student can log in as CA of their own workspace.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.modules.auth.service import hash_password
from app.modules.clients.models import (
    Client, ClientStatus, ClientUser, ClientUserRole, PaymentModel,
)
from app.modules.coaching.emails import (
    send_session_started_email, send_student_credentials_email,
    send_student_invite_email,
)
from app.modules.coaching.models import (
    CoachingInviteStatus, CoachingSession, CoachingSessionStatus,
    CoachingStudent, CoachingStudentInvite, INVITE_EXPIRY_DAYS,
    OPEN_SESSION_STATUSES, SESSION_DURATION_DAYS, new_invite_token,
    new_uuid, utcnow,
)
from app.modules.platform.models import RoleType, StatusEnum, User


# ── Login / OTP gates for coaching students ──────────────────────────────

async def guard_coaching_student_login(
    db: AsyncSession, user_id: str,
) -> None:
    """Refuse login (portal or PWA) if this user is a coaching student
    whose session isn't ACTIVE. Session lifecycle for the student:

      - Coach approves → CoachingStudent created + session still DRAFT →
        student MUST NOT be able to log in yet (coach hasn't clicked
        Start; wait for the session-started email).
      - Coach clicks Start → session ACTIVE → login allowed.
      - Coach clicks Close (or 30-day auto-close) → session CLOSED →
        student loses login access; coach reviews their workspace
        read-only for certification.

    Non-coaching users pass through unchanged. Same 403 for both
    "session not started" and "session closed" — students already
    know the state from the emails they've received, and the
    generic message avoids leaking session mechanics to non-students.
    """
    row = (await db.execute(
        select(CoachingStudent, CoachingSession)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .where(CoachingStudent.user_id == user_id)
    )).first()
    if row is None:
        return  # not a coaching student
    _cs, session = row
    if session.status == CoachingSessionStatus.ACTIVE.value:
        return
    if session.status == CoachingSessionStatus.DRAFT.value:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "coaching_session_not_started",
                "message": (
                    "Your coaching session has not started yet. Your coach "
                    "will notify you by email when you can log in."
                ),
            },
        )
    # CLOSED_MANUAL / CLOSED_AUTO
    raise HTTPException(
        status_code=403,
        detail={
            "code": "coaching_session_closed",
            "message": (
                "Your coaching session has ended. If you need access, "
                "contact your coach."
            ),
        },
    )


async def guard_otp_request_for_coaching_phone(
    db: AsyncSession, phone: str,
) -> None:
    """Refuse OTP send if the phone belongs to a coaching student
    whose session isn't ACTIVE. Guards the PWA `/auth/request-otp`
    entry so students in DRAFT / CLOSED sessions can't even trigger
    an SMS they wouldn't be able to complete login with.

    Phone comparison uses the same normalisation the platform-lookup
    endpoint uses, matching what we stored at approval time.
    """
    try:
        normalised = normalise_phone(phone)
    except HTTPException:
        return  # bad phone shape → let the OTP endpoint's own validation fire
    row = (await db.execute(
        select(CoachingStudent, CoachingSession)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .where(CoachingStudent.approved_phone == normalised)
    )).first()
    if row is None:
        return
    _cs, session = row
    if session.status == CoachingSessionStatus.ACTIVE.value:
        return
    if session.status == CoachingSessionStatus.DRAFT.value:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "coaching_session_not_started",
                "message": (
                    "This phone is registered for a coaching session that "
                    "has not started yet. Your coach will notify you when "
                    "you can log in."
                ),
            },
        )
    raise HTTPException(
        status_code=403,
        detail={
            "code": "coaching_session_closed",
            "message": (
                "This phone is registered for a coaching session that has "
                "ended. If you need access, contact your coach."
            ),
        },
    )


# ── Auth helpers ──────────────────────────────────────────────────────────

def is_sa_user(user: User) -> bool:
    """SA is identified by email match against settings.sa_email —
    same convention used elsewhere in the codebase."""
    if not user.email or not settings.sa_email:
        return False
    return user.email.lower() == settings.sa_email.lower()


def is_coach_or_sa(user: User) -> bool:
    """A user can create/manage coaching sessions if they hold the
    COACH role OR are the SA (implicit coach). Called from the
    router's dependency; raises 403 at the caller."""
    if is_sa_user(user):
        return True
    return any(
        r.role_type == RoleType.COACH and r.status == StatusEnum.ACTIVE
        for r in user.roles
    )


async def require_session_owner(
    db: AsyncSession, session_id: str, current_user: User,
) -> CoachingSession:
    """Load session, refuse if the caller isn't the coach who created
    it (SA can act on any session). Returns the session row on success.
    404s on missing session — same-shape response as unauthorised so
    a wrong-session probe can't enumerate other coaches' sessions."""
    session = (await db.execute(
        select(CoachingSession).where(CoachingSession.id == session_id)
    )).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Coaching session not found")
    if session.coach_user_id != current_user.id and not is_sa_user(current_user):
        # Conflate ownership failure with not-found to avoid leaking
        # the existence of other coaches' sessions.
        raise HTTPException(status_code=404, detail="Coaching session not found")
    return session


# ── Session lifecycle ─────────────────────────────────────────────────────

async def create_session(
    db: AsyncSession, coach: User, reference_client_id: str,
) -> CoachingSession:
    """POST /coaching/sessions — creates a DRAFT session bound to a
    real reference client. Enforces one open session per reference
    client (DB partial unique index gives a hard belt; we check first
    for a clean 409 message).
    """
    ref_client = (await db.execute(
        select(Client).where(Client.id == reference_client_id)
    )).scalar_one_or_none()
    if ref_client is None:
        raise HTTPException(status_code=404, detail="Reference client not found")
    if ref_client.is_training or ref_client.is_coaching:
        # A coaching session can only be run against a REAL client —
        # not against a training-sandbox row or another coaching
        # workspace. Both are shadow entities that shouldn't seed
        # nested sandboxes.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "reference_must_be_real_client",
                "message": (
                    "Coaching sessions can only be created against a real "
                    "onboarded client — not against a training sandbox or "
                    "another coaching workspace."
                ),
            },
        )

    open_statuses = [s.value for s in OPEN_SESSION_STATUSES]
    existing = (await db.execute(
        select(CoachingSession).where(
            CoachingSession.reference_client_id == reference_client_id,
            CoachingSession.status.in_(open_statuses),
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_already_open",
                "message": (
                    f"{ref_client.full_name} already has an open coaching "
                    "session. Close it before starting a new one."
                ),
            },
        )

    session = CoachingSession(
        id=new_uuid(),
        coach_user_id=coach.id,
        reference_client_id=reference_client_id,
        status=CoachingSessionStatus.DRAFT.value,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def start_session(
    db: AsyncSession, session: CoachingSession,
) -> CoachingSession:
    """DRAFT → ACTIVE. Requires at least one approved student (else
    what's the point). Freezes the roster — no new invites can be
    created after start. Sends the session-started email to each
    approved student so they know they can log in now."""
    if session.status != CoachingSessionStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_not_draft",
                "message": "Only draft sessions can be started.",
            },
        )
    students_count = (await db.execute(
        select(func.count(CoachingStudent.id)).where(
            CoachingStudent.session_id == session.id,
        )
    )).scalar()
    if not students_count:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "no_approved_students",
                "message": (
                    "Cannot start a session with zero approved students. "
                    "Approve at least one invited student first."
                ),
            },
        )

    session.status = CoachingSessionStatus.ACTIVE.value
    session.started_at = utcnow()
    await db.commit()
    await db.refresh(session)

    # Fire session-started emails best-effort — an email failure must
    # not roll back the start (same pattern as onboarding emails).
    ref_client = (await db.execute(
        select(Client).where(Client.id == session.reference_client_id)
    )).scalar_one()
    coach = (await db.execute(
        select(User).where(User.id == session.coach_user_id)
    )).scalar_one()
    # Students + their workspaces so the session-started email can
    # link each student directly to their tenant-branded login.
    students_rows = (await db.execute(
        select(CoachingStudent, User, Client)
        .join(User, User.id == CoachingStudent.user_id)
        .join(Client, Client.id == CoachingStudent.workspace_client_id)
        .where(CoachingStudent.session_id == session.id)
    )).all()
    base = (settings.frontend_base_url or "https://rootstalk.in").rstrip("/")
    for _cs, u, workspace in students_rows:
        if u.email:
            send_session_started_email(
                to_email=u.email,
                student_name=u.name or "Student",
                coach_name=coach.name or "your coach",
                reference_client_name=ref_client.full_name,
                portal_url=f"{base}/login/{workspace.short_name.lower()}",
            )
    return session


async def close_session(
    db: AsyncSession, session: CoachingSession, closed_by: User,
    manual: bool = True,
) -> CoachingSession:
    """ACTIVE → CLOSED_MANUAL (or CLOSED_AUTO via the celery task).
    Coaching workspaces stay in place (RESTRICT FK) so the coach can
    still review student work for certification post-close."""
    if session.status != CoachingSessionStatus.ACTIVE.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_not_active",
                "message": "Only active sessions can be closed.",
            },
        )
    session.status = (
        CoachingSessionStatus.CLOSED_MANUAL.value if manual
        else CoachingSessionStatus.CLOSED_AUTO.value
    )
    session.closed_at = utcnow()
    session.closed_by_user_id = closed_by.id if closed_by else None
    await db.commit()
    await db.refresh(session)
    return session


async def delete_draft_session(
    db: AsyncSession, session: CoachingSession,
) -> None:
    """DELETE /coaching/sessions/{id} — only allowed on DRAFT sessions
    with zero approved students. Cascade-deletes invites via FK."""
    if session.status != CoachingSessionStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail="Only draft sessions can be deleted. Close active sessions instead.",
        )
    students_count = (await db.execute(
        select(func.count(CoachingStudent.id)).where(
            CoachingStudent.session_id == session.id,
        )
    )).scalar()
    if students_count:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot delete a draft session with approved students. "
                "Remove the students first or close the session."
            ),
        )
    await db.delete(session)
    await db.commit()


# ── Invites ───────────────────────────────────────────────────────────────

async def create_invite(
    db: AsyncSession, session: CoachingSession, email: str, coach: User,
) -> tuple[CoachingStudentInvite, str]:
    """POST /coaching/sessions/{id}/invites — coach adds a student by
    email. Session must be DRAFT (roster freezes at Start). Refuses
    duplicate emails per session (DB unique constraint).

    Returns (invite, full_invite_link) so the router can echo the
    link back for manual re-copy in case email delivery flakes."""
    if session.status != CoachingSessionStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_not_draft",
                "message": (
                    "Cannot add students after the session has started. "
                    "New students must wait for the next coaching session."
                ),
            },
        )
    email_lower = email.strip().lower()
    if not email_lower:
        raise HTTPException(status_code=422, detail="Email is required")

    existing = (await db.execute(
        select(CoachingStudentInvite).where(
            CoachingStudentInvite.session_id == session.id,
            func.lower(CoachingStudentInvite.email) == email_lower,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "invite_already_exists",
                "message": (
                    f"{email_lower} has already been invited to this session "
                    f"(status: {existing.status})."
                ),
            },
        )

    invite = CoachingStudentInvite(
        id=new_uuid(),
        session_id=session.id,
        email=email_lower,
        invite_token=new_invite_token(),
        status=CoachingInviteStatus.INVITED.value,
        expires_at=utcnow() + timedelta(days=INVITE_EXPIRY_DAYS),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)

    ref_client = (await db.execute(
        select(Client).where(Client.id == session.reference_client_id)
    )).scalar_one()
    invite_link = build_invite_link(invite.invite_token)

    # Best-effort — coach can re-copy the link from the response if
    # the email delivery fails.
    send_student_invite_email(
        to_email=email_lower,
        invite_link=invite_link,
        coach_name=coach.name or "Your coach",
        reference_client_name=ref_client.full_name,
    )
    return invite, invite_link


def build_invite_link(token: str) -> str:
    base = settings.frontend_base_url or "https://rootstalk.in"
    return f"{base.rstrip('/')}/coaching/join/{token}"


async def approve_invite(
    db: AsyncSession, invite: CoachingStudentInvite, coach: User,
) -> CoachingStudent:
    """POST /coaching/sessions/{id}/invites/{invite_id}/approve —
    provisions the student's User, their isolated workspace Client,
    the ClientUser(CA) row, and the CoachingStudent record. Sends
    the credentials email.

    Preconditions:
      - Invite is SUBMITTED (student has filled the form)
      - Session is still DRAFT
      - `submitted_form.phone` is not already tied to any real user
        (approved-phone exclusivity)
    """
    if invite.status != CoachingInviteStatus.SUBMITTED.value:
        raise HTTPException(
            status_code=409,
            detail=(
                "This invite is not awaiting approval "
                f"(current status: {invite.status})."
            ),
        )
    session = (await db.execute(
        select(CoachingSession).where(CoachingSession.id == invite.session_id)
    )).scalar_one()
    if session.status != CoachingSessionStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail="Session is no longer in draft — cannot approve new students.",
        )

    form = invite.submitted_form or {}
    student_name = (form.get("name") or "").strip()
    phone_raw = (form.get("phone") or "").strip()
    if not student_name or not phone_raw:
        raise HTTPException(
            status_code=422,
            detail="Student's submitted form is missing name or phone.",
        )
    approved_phone = normalise_phone(phone_raw)
    await _ensure_phone_available_for_student(db, approved_phone)

    # Provision the student's User (portal login = email + password).
    plain_password = secrets.token_urlsafe(12)
    student_user = User(
        id=new_uuid(),
        email=invite.email,
        name=student_name,
        phone=approved_phone,
        password_hash=hash_password(plain_password),
        language_code="en",
    )
    db.add(student_user)
    await db.flush()

    # Provision the workspace Client (is_coaching=true; parent_client_id
    # = reference client; parent_session_id = this session).
    workspace = _build_workspace_client(session=session, student_user=student_user)
    db.add(workspace)
    await db.flush()

    # Register the student as CA of their own workspace so the portal
    # login lands them in the right tenant.
    db.add(ClientUser(
        client_id=workspace.id,
        user_id=student_user.id,
        role=ClientUserRole.CA,
        status=StatusEnum.ACTIVE,
    ))

    # Finally, the CoachingStudent tie-row + flip the invite state.
    coaching_student = CoachingStudent(
        id=new_uuid(),
        session_id=session.id,
        user_id=student_user.id,
        workspace_client_id=workspace.id,
        approved_phone=approved_phone,
        assigned_pwa_roles=[],
    )
    db.add(coaching_student)

    invite.status = CoachingInviteStatus.APPROVED.value
    invite.approved_at = utcnow()
    invite.approved_by_user_id = coach.id

    await db.commit()
    await db.refresh(coaching_student)

    # Fire credentials email best-effort.
    ref_client = (await db.execute(
        select(Client).where(Client.id == session.reference_client_id)
    )).scalar_one()
    # Portal URL routes the student directly to their tenant-branded
    # login page (LoginForm skips the company-name step when it has
    # a short_name in the URL). short_name is uppercase in the DB but
    # per-tenant route params are lowercased — see
    # rootstalk-client-portal/app/login/[shortName]/page.tsx.
    base = (settings.frontend_base_url or "https://rootstalk.in").rstrip("/")
    portal_url = f"{base}/login/{workspace.short_name.lower()}"
    send_student_credentials_email(
        to_email=invite.email,
        student_name=student_name,
        portal_url=portal_url,
        portal_password=plain_password,
        approved_phone=approved_phone,
        coach_name=coach.name or "your coach",
        reference_client_name=ref_client.full_name,
        workspace_short_name=workspace.short_name,
    )
    return coaching_student


async def reject_invite(
    db: AsyncSession, invite: CoachingStudentInvite, coach: User,
) -> CoachingStudentInvite:
    """Mark invite REJECTED. No user or workspace is provisioned."""
    if invite.status != CoachingInviteStatus.SUBMITTED.value:
        raise HTTPException(
            status_code=409,
            detail="This invite is not awaiting approval.",
        )
    invite.status = CoachingInviteStatus.REJECTED.value
    invite.approved_at = utcnow()
    invite.approved_by_user_id = coach.id
    await db.commit()
    await db.refresh(invite)
    return invite


def _build_workspace_client(
    session: CoachingSession, student_user: User,
) -> Client:
    """Compose the Client row that represents the student's workspace.

    Naming: workspace `full_name` includes the student's name for
    easy identification in coach's roster; `short_name` is a
    deterministic slug (co<sessionid_short><studentid_short>) so
    it fits the 12-char column limit AND is unique per (session,
    student) — the DB unique constraint on short_name enforces this.
    LOWERCASE because the login lookup does `Client.short_name ==
    short_name.lower()` — real clients also store lowercase per
    clients/router.py (line 344)."""
    session_short = session.id.replace("-", "")[:4]
    student_short = student_user.id.replace("-", "")[:4]
    short_name = f"co{session_short}{student_short}"[:12]

    return Client(
        id=new_uuid(),
        full_name=f"[Coaching] {student_user.name}",
        short_name=short_name,
        display_name=f"{student_user.name}'s workspace",
        is_coaching=True,
        parent_client_id=session.reference_client_id,
        parent_session_id=session.id,
        payment_model=PaymentModel.COMPANY_PAYS,
        status=ClientStatus.ACTIVE,
        ca_name=student_user.name or "Coaching Student",
        ca_phone=student_user.phone or "",
        ca_email=student_user.email or "",
    )


async def _ensure_phone_available_for_student(
    db: AsyncSession, phone: str,
) -> None:
    """Approved-phone exclusivity: the phone the student registered
    with must not already belong to any real user. If it does, the
    coach must reject the invite and ask the student to use a
    different number.

    Non-coaching users only — a phone tied to another CoachingStudent
    is caught separately by the unique User.phone constraint at
    insert time, but real-user detection is the important defensive
    layer since the entire "student phone is exclusively theirs
    during the session" promise depends on it.
    """
    from sqlalchemy import exists
    from app.modules.subscriptions.models import Subscription  # noqa: F401 (registry)
    # Check any User row currently holding this phone that ISN'T
    # itself a student in an OPEN session.
    open_statuses = [s.value for s in OPEN_SESSION_STATUSES]
    student_user_subq = select(CoachingStudent.user_id).join(
        CoachingSession, CoachingSession.id == CoachingStudent.session_id,
    ).where(CoachingSession.status.in_(open_statuses))
    conflict = (await db.execute(
        select(User).where(
            User.phone == phone,
            or_(User.deleted_at.is_(None), User.deleted_at > utcnow() - timedelta(days=30)),
            User.id.notin_(student_user_subq),
        )
    )).scalar_one_or_none()
    if conflict is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "phone_already_a_real_user",
                "message": (
                    f"The phone {phone} is already registered on rootsTALK "
                    "as a real user. Ask the student to provide a different "
                    "number before you can approve this invite."
                ),
            },
        )


def normalise_phone(input_phone: str) -> str:
    """Same normalisation the platform lookup + alerts endpoints use:
    take last 10 digits, prefix '+91'. Anchor
    feedback_identity_lookup_normalisation_parity.md."""
    digits = "".join(ch for ch in (input_phone or "") if ch.isdigit())
    if len(digits) < 10:
        raise HTTPException(status_code=422, detail="Phone must be at least 10 digits")
    return "+91" + digits[-10:]


# ── Public student self-registration ─────────────────────────────────────

async def load_invite_by_token(
    db: AsyncSession, token: str,
) -> tuple[CoachingStudentInvite, CoachingSession, Client, User]:
    """Look up an invite by its emailed token, along with the context
    the student's form needs to render (coach + reference client).
    404 for missing token — same shape as expired/consumed to avoid
    token-enumeration attacks.
    """
    invite = (await db.execute(
        select(CoachingStudentInvite).where(
            CoachingStudentInvite.invite_token == token,
        )
    )).scalar_one_or_none()
    if invite is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    session = (await db.execute(
        select(CoachingSession).where(CoachingSession.id == invite.session_id)
    )).scalar_one()
    ref_client = (await db.execute(
        select(Client).where(Client.id == session.reference_client_id)
    )).scalar_one()
    coach = (await db.execute(
        select(User).where(User.id == session.coach_user_id)
    )).scalar_one()
    return invite, session, ref_client, coach


def can_submit_invite(
    invite: CoachingStudentInvite, session: CoachingSession,
) -> bool:
    """Student is allowed to submit / re-submit only while the invite
    is INVITED or SUBMITTED (re-submit lets them fix a typo before
    coach reviews), the invite hasn't expired, and the session is
    still DRAFT (invites become inert once the coach clicks Start).
    """
    if invite.is_expired():
        return False
    if session.status != CoachingSessionStatus.DRAFT.value:
        return False
    return invite.status in (
        CoachingInviteStatus.INVITED.value,
        CoachingInviteStatus.SUBMITTED.value,
    )


async def submit_student_form(
    db: AsyncSession, token: str, form: dict,
) -> CoachingStudentInvite:
    """Public endpoint's write side. Stores the form JSON, flips
    invite to SUBMITTED, stamps submitted_at. Rejects with 422 if
    the phone is already a real user (approved-phone exclusivity,
    caught here as a UX win instead of waiting for the coach to
    reject the invite)."""
    invite, session, _ref, _coach = await load_invite_by_token(db, token)
    if not can_submit_invite(invite, session):
        # Message tuned per specific failure so the student knows why.
        if invite.is_expired():
            msg = "This invite has expired. Please ask the coach to send a fresh one."
        elif session.status != CoachingSessionStatus.DRAFT.value:
            msg = "This coaching session has already started; new students cannot join it."
        else:
            msg = "This invite is no longer active."
        raise HTTPException(status_code=409, detail=msg)

    # Fail-fast on phone availability so student can correct on the spot
    # instead of getting a rejection email later.
    approved_phone = normalise_phone(form.get("phone", ""))
    await _ensure_phone_available_for_student(db, approved_phone)

    invite.submitted_form = {
        "name": form.get("name", "").strip(),
        "year_of_birth": form.get("year_of_birth"),
        "address": form.get("address", "").strip(),
        "organization": form.get("organization", "").strip(),
        "phone": approved_phone,
    }
    invite.status = CoachingInviteStatus.SUBMITTED.value
    invite.submitted_at = utcnow()
    await db.commit()
    await db.refresh(invite)
    return invite


# ── PWA role assignment ──────────────────────────────────────────────────

_VALID_PWA_ROLES = {
    RoleType.FARMER.value,
    RoleType.DEALER.value,
    RoleType.FACILITATOR.value,
    RoleType.FARM_PUNDIT.value,
}


async def assign_pwa_roles(
    db: AsyncSession, session: CoachingSession, student: CoachingStudent,
    roles: list[str],
) -> CoachingStudent:
    """Coach grants PWA roles to a student. Only meaningful during
    DRAFT or ACTIVE sessions. DEALER + FACILITATOR can coexist on a
    single student — the exclusion is relaxed inside coaching."""
    if session.status not in (CoachingSessionStatus.DRAFT.value,
                              CoachingSessionStatus.ACTIVE.value):
        raise HTTPException(
            status_code=409,
            detail="PWA roles can only be assigned while the session is draft or active.",
        )
    for r in roles:
        if r not in _VALID_PWA_ROLES:
            raise HTTPException(status_code=422, detail=f"Unknown PWA role: {r}")
    student.assigned_pwa_roles = list(dict.fromkeys(roles))  # dedup, preserve order

    # Also reflect on the student's UserRole rows so PWA gates that
    # check UserRole behave naturally — the student is a real
    # FARMER/DEALER/etc. inside the coaching context. Additive:
    # roles previously assigned but not in the new list stay, so any
    # in-flight PWA-side activity doesn't get orphaned mid-session.
    # The DEALER/FACILITATOR exclusion lives on the PWA self-claim
    # path (auth/router.py:425) — this coach-driven path bypasses
    # that gate, which is the intended coaching-context relaxation.
    from app.modules.platform.models import UserRole
    existing_urs = (await db.execute(
        select(UserRole).where(UserRole.user_id == student.user_id)
    )).scalars().all()
    existing_role_types = {ur.role_type.value for ur in existing_urs}
    for r in roles:
        if r not in existing_role_types:
            db.add(UserRole(
                id=new_uuid(),
                user_id=student.user_id,
                role_type=RoleType(r),
                status=StatusEnum.ACTIVE,
            ))
    await db.commit()
    await db.refresh(student)
    return student


# ── Certification ────────────────────────────────────────────────────────

_VALID_GRADES = {"SATISFACTORY", "GOOD", "EXCELLENT"}


async def set_certification(
    db: AsyncSession, session: CoachingSession, student: CoachingStudent,
    coach: User, certified: bool, grade: Optional[str] = None,
) -> CoachingStudent:
    """Certification lands post-close per plan. Coach reviews student
    work in the (now read-only) workspace and marks a grade:
      - certified=True  + grade=SATISFACTORY/GOOD/EXCELLENT → certified
      - certified=False → not certified (clears grade + certified_at)
    Toggling is allowed (correct a mistake); the certified_by field
    always reflects the caller of the most recent set."""
    if session.status not in (
        CoachingSessionStatus.CLOSED_MANUAL.value,
        CoachingSessionStatus.CLOSED_AUTO.value,
    ):
        raise HTTPException(
            status_code=409,
            detail="Certification is only meaningful after the session has closed.",
        )
    if certified:
        if not grade or grade not in _VALID_GRADES:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "grade_required",
                    "message": "Grade must be one of SATISFACTORY, GOOD, or EXCELLENT.",
                },
            )
        student.certified_at = utcnow()
        student.certified_by_user_id = coach.id
        student.grade = grade
    else:
        student.certified_at = None
        student.certified_by_user_id = None
        student.grade = None
    await db.commit()
    await db.refresh(student)
    return student


# ── Digital certificate ──────────────────────────────────────────────────

async def generate_certificate(
    db: AsyncSession, session: CoachingSession, student: CoachingStudent,
) -> CoachingStudent:
    """Generate a certificate PDF, upload to S3, email to the student,
    persist the certificate_number + generated_at + pdf_url. Idempotent
    on the certificate_number — regeneration (e.g. grade updated)
    keeps the same number so verification URLs stay stable across
    regenerations.

    Preconditions:
      - Student must be certified (grade + certified_at set)
      - Session must be CLOSED (grading happens post-close, so this
        holds by the certification precondition)

    Failure modes:
      - S3 not configured → PDF is still generated + emailed, but no
        pdf_url stored. Caller can retry.
      - Email delivery fails → PDF url stays, generated_at bumps,
        student can re-download from the SA portal.
    """
    from app.modules.coaching.certificate import render_certificate_pdf
    from app.modules.coaching.emails import send_certificate_email
    from app.modules.media.router import upload_to_s3

    if not (student.certified_at and student.grade):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_certified",
                "message": "Student must be certified with a grade before generating a certificate.",
            },
        )
    if session.closed_at is None or session.started_at is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_not_closed",
                "message": "Certificates can only be generated after the session has closed.",
            },
        )

    # Compose the certificate context
    ref_client = (await db.execute(
        select(Client).where(Client.id == session.reference_client_id)
    )).scalar_one()
    coach = (await db.execute(
        select(User).where(User.id == session.coach_user_id)
    )).scalar_one()
    student_user = (await db.execute(
        select(User).where(User.id == student.user_id)
    )).scalar_one()

    # Preserve certificate_number across regenerations so verification
    # URLs stay stable if the coach corrects a grade.
    if not student.certificate_number:
        student.certificate_number = new_uuid()

    verification_url = build_verification_url(student.certificate_number)
    pdf_bytes = render_certificate_pdf(
        student_name=student_user.name or "Coaching Student",
        reference_client_name=ref_client.full_name,
        coach_name=coach.name or coach.email or "rootsTALK Coach",
        session_started_at=session.started_at,
        session_closed_at=session.closed_at,
        grade=student.grade,
        certificate_number=student.certificate_number,
        verification_url=verification_url,
    )

    # Upload to S3 for a stable public URL. If S3 isn't configured
    # (dev), upload_to_s3 returns a placeholder URL — good enough
    # for local testing but the coach will see the placeholder in
    # the SA portal.
    pdf_filename = f"rootsTALK-certificate-{student.certificate_number}.pdf"
    try:
        from fastapi import UploadFile
        import io
        # upload_to_s3 expects an UploadFile; build one from our bytes.
        upload_file = UploadFile(
            filename=pdf_filename,
            file=io.BytesIO(pdf_bytes),
        )
        # UploadFile's content_type comes from the header on real
        # uploads; set it explicitly here.
        upload_file.headers = {"content-type": "application/pdf"}  # type: ignore[attr-defined]
        # Route through upload_to_s3 with the coaching-certs folder.
        # Allowed types filter — reuse the module's default which
        # accepts application/pdf as document.
        s3_result = await upload_to_s3(upload_file, folder="coaching-certificates")
        student.certificate_pdf_url = s3_result.get("url")
    except Exception as e:
        # PDF generation succeeded; S3 upload failed. Don't lose the
        # certification progress — log + continue, coach can retry
        # the generate button which uploads again.
        import logging
        logging.getLogger(__name__).warning(
            f"Certificate S3 upload failed for student {student.id}: {e}"
        )

    student.certificate_generated_at = utcnow()
    await db.commit()
    await db.refresh(student)

    # Email delivery is best-effort — student can re-download via the
    # SA-portal "resend certificate" button (Phase 6c if needed).
    if student_user.email:
        send_certificate_email(
            to_email=student_user.email,
            student_name=student_user.name or "Coaching Student",
            coach_name=coach.name or coach.email or "rootsTALK Coach",
            reference_client_name=ref_client.full_name,
            grade=student.grade,
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
            verification_url=verification_url,
        )
    return student


def build_verification_url(cert_number: str) -> str:
    """Public verification URL. On prod resolves to the client-portal
    domain per `settings.frontend_base_url` (same host that serves the
    student self-registration form)."""
    base = settings.frontend_base_url or "https://rootstalk.in"
    return f"{base.rstrip('/')}/verify/{cert_number}"


async def load_certified_students(
    db: AsyncSession, current_user: User,
) -> list[dict]:
    """Registry query for the SA-portal `/coaching/certified` page.
    Returns all certified students across all sessions (SA scope); a
    non-SA coach sees only certifications from their own sessions."""
    q = (
        select(CoachingStudent, User, CoachingSession, Client, User)
        .join(User, User.id == CoachingStudent.user_id)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .join(Client, Client.id == CoachingSession.reference_client_id)
        .where(CoachingStudent.certified_at.is_not(None))
    )
    # SQLAlchemy alias for the second User join (coach). Trick: use
    # a distinct alias so the ORM knows which User row to bind to
    # the coach column.
    from sqlalchemy.orm import aliased
    CoachUser = aliased(User)
    q = (
        select(CoachingStudent, User, CoachingSession, Client, CoachUser)
        .join(User, User.id == CoachingStudent.user_id)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .join(Client, Client.id == CoachingSession.reference_client_id)
        .join(CoachUser, CoachUser.id == CoachingSession.coach_user_id)
        .where(CoachingStudent.certified_at.is_not(None))
        .order_by(CoachingStudent.certified_at.desc())
    )
    if not is_sa_user(current_user):
        q = q.where(CoachingSession.coach_user_id == current_user.id)

    rows = (await db.execute(q)).all()
    return [
        {
            "id": cs.id,
            "certificate_number": cs.certificate_number,
            "student_name": student.name,
            "student_email": student.email,
            "reference_client_name": ref_client.full_name,
            "reference_client_short_name": ref_client.short_name,
            "coach_name": coach.name or coach.email,
            "session_id": sess.id,
            "session_started_at": sess.started_at,
            "session_closed_at": sess.closed_at,
            "grade": cs.grade,
            "certified_at": cs.certified_at,
            "certificate_generated_at": cs.certificate_generated_at,
            "certificate_pdf_url": cs.certificate_pdf_url,
        }
        for cs, student, sess, ref_client, coach in rows
    ]


async def load_certificate_public(
    db: AsyncSession, certificate_number: str,
) -> Optional[dict]:
    """Public verification lookup — no auth. Returns the certificate
    context if the number resolves, or None. Deliberately narrow —
    doesn't leak email / phone / workspace ids to the public verifier."""
    from sqlalchemy.orm import aliased
    CoachUser = aliased(User)
    row = (await db.execute(
        select(CoachingStudent, User, CoachingSession, Client, CoachUser)
        .join(User, User.id == CoachingStudent.user_id)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .join(Client, Client.id == CoachingSession.reference_client_id)
        .join(CoachUser, CoachUser.id == CoachingSession.coach_user_id)
        .where(
            CoachingStudent.certificate_number == certificate_number,
            CoachingStudent.certified_at.is_not(None),
        )
    )).first()
    if row is None:
        return None
    cs, student, sess, ref_client, coach = row
    return {
        "certificate_number": cs.certificate_number,
        "student_name": student.name,
        "reference_client_name": ref_client.full_name,
        "coach_name": coach.name or coach.email,
        "session_started_at": sess.started_at,
        "session_closed_at": sess.closed_at,
        "grade": cs.grade,
        "certified_at": cs.certified_at,
        "certificate_generated_at": cs.certificate_generated_at,
    }


async def load_coaching_context(
    db: AsyncSession, user: User,
) -> Optional[dict]:
    """Return the PWA/portal context for a coaching student user, or
    None if the user isn't one. Used by /auth/me so the PWA can
    render a persistent 'You're in a coaching session — this is
    practice' banner across every screen.

    Only returns context for the current OPEN session — if the
    student's session has closed and they somehow still hold a
    valid token, context is None (login gates would already have
    kicked them out).
    """
    open_statuses = [s.value for s in OPEN_SESSION_STATUSES]
    row = (await db.execute(
        select(CoachingStudent, CoachingSession, Client, User)
        .join(CoachingSession, CoachingSession.id == CoachingStudent.session_id)
        .join(Client, Client.id == CoachingSession.reference_client_id)
        .join(User, User.id == CoachingSession.coach_user_id)
        .where(
            CoachingStudent.user_id == user.id,
            CoachingSession.status.in_(open_statuses),
        )
    )).first()
    if row is None:
        return None
    student, session, ref_client, coach = row
    return {
        "session_id": session.id,
        "session_status": session.status,
        "coach_name": coach.name or coach.email,
        "reference_client_name": ref_client.full_name,
        "workspace_client_id": student.workspace_client_id,
        "assigned_pwa_roles": student.assigned_pwa_roles or [],
    }


async def _load_activity_counts(
    db: AsyncSession, workspace_client_ids: list[str],
) -> dict[str, dict[str, int]]:
    """Batch-fetch per-workspace activity counts for the session
    detail view. One query per counted entity, GROUP BY client_id,
    keyed back by workspace_client_id. Zero counts fill in for
    workspaces that have no rows in a given table.
    """
    if not workspace_client_ids:
        return {}
    from app.modules.advisory.models import Package, Practice, Timeline
    from app.modules.subscriptions.models import Subscription
    from app.modules.orders.models import Order
    from app.modules.farmpundit.models import Query

    result: dict[str, dict[str, int]] = {
        cid: {"packages": 0, "practices": 0, "subscriptions": 0,
              "orders": 0, "queries": 0}
        for cid in workspace_client_ids
    }

    # Packages authored inside the workspace
    for cid, cnt in (await db.execute(
        select(Package.client_id, func.count(Package.id))
        .where(Package.client_id.in_(workspace_client_ids))
        .group_by(Package.client_id)
    )).all():
        if cid in result:
            result[cid]["packages"] = cnt

    # Practices authored inside the workspace. Practice → Timeline →
    # Package chain (Practice doesn't hold client_id directly, and
    # Timeline is polymorphic across CCA/PG/SP/QA — for coaching
    # counts we scope via Package's client_id which covers the CCA
    # authoring path students exercise).
    for cid, cnt in (await db.execute(
        select(Package.client_id, func.count(Practice.id))
        .join(Timeline, Timeline.package_id == Package.id)
        .join(Practice, Practice.timeline_id == Timeline.id)
        .where(Package.client_id.in_(workspace_client_ids))
        .group_by(Package.client_id)
    )).all():
        if cid in result:
            result[cid]["practices"] = cnt

    # Subscriptions (each subscription is a farmer in the workspace)
    for cid, cnt in (await db.execute(
        select(Subscription.client_id, func.count(Subscription.id))
        .where(Subscription.client_id.in_(workspace_client_ids))
        .group_by(Subscription.client_id)
    )).all():
        if cid in result:
            result[cid]["subscriptions"] = cnt

    # Orders placed inside the workspace
    for cid, cnt in (await db.execute(
        select(Order.client_id, func.count(Order.id))
        .where(Order.client_id.in_(workspace_client_ids))
        .group_by(Order.client_id)
    )).all():
        if cid in result:
            result[cid]["orders"] = cnt

    # Queries submitted inside the workspace
    for cid, cnt in (await db.execute(
        select(Query.client_id, func.count(Query.id))
        .where(Query.client_id.in_(workspace_client_ids))
        .group_by(Query.client_id)
    )).all():
        if cid in result:
            result[cid]["queries"] = cnt

    return result


# ── Listing + detail loaders ─────────────────────────────────────────────

async def list_sessions_for(
    db: AsyncSession, current_user: User, mine_only: bool,
    status_filter: Optional[str],
) -> list[dict]:
    """Coach dashboard list. SA sees everything; a non-SA coach sees
    only sessions they own (mine_only=True always for them)."""
    q = select(CoachingSession)
    if mine_only or not is_sa_user(current_user):
        q = q.where(CoachingSession.coach_user_id == current_user.id)
    if status_filter:
        q = q.where(CoachingSession.status == status_filter)
    q = q.order_by(CoachingSession.created_at.desc())
    sessions = (await db.execute(q)).scalars().all()
    if not sessions:
        return []

    # Batch-fetch reference clients + coaches to avoid N+1.
    ref_ids = {s.reference_client_id for s in sessions}
    coach_ids = {s.coach_user_id for s in sessions}
    session_ids = [s.id for s in sessions]
    refs = {c.id: c for c in (await db.execute(
        select(Client).where(Client.id.in_(ref_ids))
    )).scalars().all()}
    coaches = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_(coach_ids))
    )).scalars().all()}
    student_counts = {
        sid: cnt
        for sid, cnt in (await db.execute(
            select(CoachingStudent.session_id, func.count(CoachingStudent.id))
            .where(CoachingStudent.session_id.in_(session_ids))
            .group_by(CoachingStudent.session_id)
        )).all()
    }

    return [
        {
            "id": s.id,
            "reference_client": {
                "id": s.reference_client_id,
                "full_name": refs[s.reference_client_id].full_name if s.reference_client_id in refs else "(unknown)",
                "short_name": refs[s.reference_client_id].short_name if s.reference_client_id in refs else "",
            },
            "coach": {
                "id": s.coach_user_id,
                "name": coaches[s.coach_user_id].name if s.coach_user_id in coaches else None,
                "email": coaches[s.coach_user_id].email if s.coach_user_id in coaches else None,
            },
            "status": s.status,
            "student_count": student_counts.get(s.id, 0),
            # In v1 approved_student_count == student_count (student is only
            # created on approval). Kept separately in the schema so future
            # states (pending on-hold, revoked) can diverge.
            "approved_student_count": student_counts.get(s.id, 0),
            "created_at": s.created_at,
            "started_at": s.started_at,
            "closed_at": s.closed_at,
        }
        for s in sessions
    ]


async def load_session_detail(
    db: AsyncSession, session: CoachingSession,
) -> dict:
    """Full session view for the detail page — session + invites + students."""
    ref_client = (await db.execute(
        select(Client).where(Client.id == session.reference_client_id)
    )).scalar_one()
    coach = (await db.execute(
        select(User).where(User.id == session.coach_user_id)
    )).scalar_one()

    invites = (await db.execute(
        select(CoachingStudentInvite)
        .where(CoachingStudentInvite.session_id == session.id)
        .order_by(CoachingStudentInvite.created_at)
    )).scalars().all()

    students_rows = (await db.execute(
        select(CoachingStudent, User, Client)
        .join(User, User.id == CoachingStudent.user_id)
        .join(Client, Client.id == CoachingStudent.workspace_client_id)
        .where(CoachingStudent.session_id == session.id)
        .order_by(CoachingStudent.created_at)
    )).all()

    # Batch-fetch per-workspace activity counts for the coach's
    # certification review. Zero-fills workspaces with no activity.
    workspace_ids = [wc.id for _cs, _u, wc in students_rows]
    counts_by_workspace = await _load_activity_counts(db, workspace_ids)

    return {
        "id": session.id,
        "reference_client": {
            "id": ref_client.id,
            "full_name": ref_client.full_name,
            "short_name": ref_client.short_name,
        },
        "coach": {
            "id": coach.id,
            "name": coach.name,
            "email": coach.email,
        },
        "status": session.status,
        "created_at": session.created_at,
        "started_at": session.started_at,
        "closed_at": session.closed_at,
        "invites": [
            {
                "id": i.id,
                "email": i.email,
                "status": i.status,
                "submitted_form": i.submitted_form,
                "created_at": i.created_at,
                "submitted_at": i.submitted_at,
                "approved_at": i.approved_at,
                "expires_at": i.expires_at,
            }
            for i in invites
        ],
        "students": [
            {
                "id": cs.id,
                "user_id": cs.user_id,
                "workspace_client_id": cs.workspace_client_id,
                "workspace_short_name": wc.short_name,
                "student_name": u.name,
                "student_email": u.email,
                "approved_phone": cs.approved_phone,
                "assigned_pwa_roles": cs.assigned_pwa_roles or [],
                "certified_at": cs.certified_at,
                "grade": cs.grade,
                "created_at": cs.created_at,
                "counts": counts_by_workspace.get(cs.workspace_client_id, {
                    "packages": 0, "practices": 0, "subscriptions": 0,
                    "orders": 0, "queries": 0,
                }),
                "certificate_number": cs.certificate_number,
                "certificate_generated_at": cs.certificate_generated_at,
                "certificate_pdf_url": cs.certificate_pdf_url,
            }
            for cs, u, wc in students_rows
        ],
    }
