"""Access gates for the Client Reports dashboard (Phase 1).

Two role gates + one tier seam:

- ``_assert_client_report_reader`` — allows every principal that should
  be able to read a report: CA, Report User, SA-email bypass, and CM
  with EDIT rights. Mirrors the shape of
  ``clients/training_router.py::_assert_ca_or_fm`` so future audits can
  eyeball both side-by-side.
- ``_assert_ca_for_export`` — CSV export is CA-only. Report User must
  see the button hidden entirely on the frontend; this is the server
  belt-and-braces so a direct URL hit 403s.
- ``client_can_access_reports`` — tier seam that today returns True for
  every client. When a paid analytics add-on lands (Phase 2+), this
  becomes 3 lines and every endpoint that already calls it inherits
  the gate for free. Not SA control — a clean seam so we don't have
  to scatter-hunt across N endpoints later.

Companion memory: `project_rootstalk_client_reports_phase_1.md`.
"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.modules.clients.models import (
    CMClientAssignment, CMRights, ClientUser, ClientUserRole,
)
from app.modules.platform.models import StatusEnum, User


# ── Role gates ────────────────────────────────────────────────────────────────

async def _assert_client_report_reader(
    db: AsyncSession, user: User, client_id: str,
) -> None:
    """Any principal who should see a report renders.

    Allow list (short-circuit in this order):
        1. SA-email bypass (settings.sa_email).
        2. ClientUser row with role CA or REPORT_USER, status ACTIVE.
        3. CMClientAssignment row with rights EDIT, status ACTIVE.

    403 otherwise with stable code ``report_role_required`` so the
    frontend can distinguish this from a generic auth failure.
    """
    if bool(settings.sa_email) and user.email == settings.sa_email:
        return
    role_row = (await db.execute(
        select(ClientUser.role).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
            ClientUser.role.in_([
                ClientUserRole.CA, ClientUserRole.REPORT_USER,
            ]),
        ).limit(1)
    )).scalar_one_or_none()
    if role_row is not None:
        return
    cm_edit = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user.id,
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
            "code": "report_role_required",
            "message": (
                "You need Customer Admin or Report User access at this "
                "client to view reports."
            ),
        },
    )


async def _assert_ca_for_export(
    db: AsyncSession, user: User, client_id: str,
) -> None:
    """CSV export is CA-only.

    Report User can view every report on-screen but cannot download the
    underlying data; the frontend hides the button, this is the
    server-side belt. SA-email bypass + CM(EDIT) fallback follow the
    project convention that CM inside a client has all privileges.
    """
    if bool(settings.sa_email) and user.email == settings.sa_email:
        return
    role_row = (await db.execute(
        select(ClientUser.role).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
            ClientUser.role == ClientUserRole.CA,
        ).limit(1)
    )).scalar_one_or_none()
    if role_row is not None:
        return
    cm_edit = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user.id,
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
            "code": "ca_role_required_for_export",
            "message": (
                "Only the Customer Admin can download report data. "
                "Report Users can view charts on-screen."
            ),
        },
    )


# ── Tier seam (Phase 2+ hook, no-op today) ────────────────────────────────────

def client_can_access_reports(client_id: str, subject_slug: str) -> bool:
    """Return True today. When a paid analytics tier lands, become 3
    lines that check `client.reports_tier` (or an equivalent column)
    against the subject requested. Every report endpoint calls this
    before running the query so the switch flips in one place.

    Deliberately not SA control — client's own data, given back to
    them in a useful shape. This seam is for a future PAID tier
    (per-subject unlock), not for SA gatekeeping.
    """
    return True
