"""Pydantic request/response schemas for the Coaching Sandbox
SA-portal-facing endpoints. Student self-registration schemas
(public, token-gated) will land in Phase 3.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


# ── Request bodies ────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    """Body for POST /coaching/sessions."""
    reference_client_id: str


class InviteStudentRequest(BaseModel):
    """Body for POST /coaching/sessions/{id}/invites."""
    email: EmailStr


class AssignPwaRolesRequest(BaseModel):
    """Body for PUT /coaching/sessions/{id}/students/{student_id}/pwa-roles.

    Accepts the four PWA role names farmers/dealers/facilitators/pundits
    hold. Empty list is valid (student holds no PWA role, e.g. they're
    only training as CA and never touch the PWA in this cohort).
    """
    roles: list[str] = Field(default_factory=list)


class CertifyStudentRequest(BaseModel):
    """Body for POST /coaching/sessions/{id}/students/{student_id}/certify."""
    certified: bool


# ── Response bodies ───────────────────────────────────────────────────────

class ReferenceClientMini(BaseModel):
    id: str
    full_name: str
    short_name: str


class CoachMini(BaseModel):
    id: str
    name: Optional[str]
    email: Optional[str]


class SessionListItem(BaseModel):
    """Coach dashboard row — session summary for the sessions-list UI."""
    id: str
    reference_client: ReferenceClientMini
    coach: CoachMini
    status: str
    student_count: int
    approved_student_count: int
    created_at: datetime
    started_at: Optional[datetime]
    closed_at: Optional[datetime]


class InviteDetail(BaseModel):
    id: str
    email: str
    status: str
    submitted_form: Optional[dict]
    created_at: datetime
    submitted_at: Optional[datetime]
    approved_at: Optional[datetime]
    expires_at: datetime


class StudentDetail(BaseModel):
    id: str
    user_id: str
    workspace_client_id: str
    workspace_short_name: str
    student_name: Optional[str]
    student_email: Optional[str]
    approved_phone: str
    assigned_pwa_roles: list[str]
    certified_at: Optional[datetime]
    created_at: datetime


class SessionDetail(BaseModel):
    """Full session view — session + invites + students. Used by the
    session detail page (all three lifecycle states)."""
    id: str
    reference_client: ReferenceClientMini
    coach: CoachMini
    status: str
    created_at: datetime
    started_at: Optional[datetime]
    closed_at: Optional[datetime]
    invites: list[InviteDetail]
    students: list[StudentDetail]


class CreatedInviteResponse(BaseModel):
    """Returned from the create-invite endpoint. The coach's UI uses
    `id` to reference the invite; `invite_link` is what the email
    embedded — surfaced here so the coach can re-copy it manually if
    email delivery is flaky."""
    id: str
    email: str
    status: str
    invite_link: str
    expires_at: datetime


# ── Public student-facing schemas (no auth on these endpoints) ────────────

class InviteContextResponse(BaseModel):
    """Returned by GET /coaching/join/{token} — everything the student's
    self-registration form needs to render context before they submit.

    Deliberately omits sensitive coach details (email, id) and
    reference client internals (id) — just the display info that
    helps the student trust the invite is legit."""
    email: str
    coach_name: Optional[str]
    reference_client_name: str
    status: str
    expires_at: datetime
    already_submitted: bool
    can_submit: bool


class StudentRegistrationForm(BaseModel):
    """Body for POST /coaching/join/{token}/submit — the student's
    self-registration form. All fields required by design; the coach
    needs each of these to make the enrolment decision."""
    name: str = Field(min_length=2, max_length=255)
    year_of_birth: int = Field(ge=1930, le=2015)
    address: str = Field(min_length=5, max_length=1000)
    organization: str = Field(min_length=2, max_length=255)
    phone: str = Field(min_length=10, max_length=20)


class SubmitInviteResponse(BaseModel):
    """Returned after successful submit — student sees a confirmation
    that the coach will review their submission."""
    status: str
    submitted_at: datetime
    message: str
