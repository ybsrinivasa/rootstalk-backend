"""Enterprise License daily lifecycle sweep (2026-05-30).

For each ACTIVE EnterpriseLicense:

  - If today == to_date: flip the licence to EXPIRED, flip the
    linked Client.status to INACTIVE, send the "your licence has
    closed" email to the CA and SA.
  - Else if (to_date - today).days ∈ {30, 23, 16, 9, 2}: send the
    "your licence expires in N days" reminder to the CA and SA.

Cadence "weekly intervals starting 30 days before" → 30 / 23 / 16 /
9 / 2 days out + closure day = 6 emails total per licence.

Recipients:
  - CA: `Client.ca_email` (denormalised onto the client at approval).
  - SA: `settings.sa_email` — there is one canonical SA per
    instance; the inboxes-with-is_sa-True path is V2.

Scheduled daily at 01:30 UTC (07:00 IST) in the beat schedule.
Inner async function `_sweep_with_session` is the testing seam.
"""
import asyncio
import logging
from datetime import date, datetime, timezone

from celery import shared_task
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import AsyncSessionLocal
from app.modules.clients.models import Client, ClientStatus
from app.modules.clients.service import _send_email
from app.modules.subscriptions.models import EnterpriseLicense

logger = logging.getLogger(__name__)


# Days-out at which a reminder fires. Closure day (0) is handled
# separately because it also flips state.
REMINDER_DAYS = {30, 23, 16, 9, 2}


# ── Email bodies ─────────────────────────────────────────────────────────────

def _reminder_email(*, client_name: str, days_remaining: int, closure_date: date):
    subject = f"Enterprise Licence expires in {days_remaining} days — {client_name}"
    plain = (
        f"This is a reminder that the Enterprise Licence for {client_name} "
        f"on rootsTALK.in expires in {days_remaining} days, on "
        f"{closure_date.strftime('%d %b %Y')}.\n\n"
        f"After expiry the licence stops permitting new farmer subscriptions "
        f"and CA-portal logins; existing farmer subscriptions continue to "
        f"their natural close.\n\n"
        f"If a renewal is in progress no action is needed. Otherwise plan a "
        f"renewal before the expiry date.\n\n"
        f"— rootsTALK.in"
    )
    html = (
        f"<div style=\"font-family:sans-serif;padding:24px;max-width:560px\">"
        f"<h2 style=\"color:#1A5C2A\">Enterprise Licence expires in "
        f"{days_remaining} days</h2>"
        f"<p>The Enterprise Licence for <strong>{client_name}</strong> on "
        f"rootsTALK.in expires on "
        f"<strong>{closure_date.strftime('%d %b %Y')}</strong>.</p>"
        f"<p>After expiry the licence stops permitting new farmer "
        f"subscriptions and CA-portal logins; existing farmer subscriptions "
        f"continue to their natural close.</p>"
        f"<p>If a renewal is in progress no action is needed. Otherwise "
        f"plan a renewal before the expiry date.</p>"
        f"<p style=\"color:#666;font-size:12px;margin-top:24px\">"
        f"This is an automated reminder from rootsTALK.in.</p>"
        f"</div>"
    )
    return subject, plain, html


def _closure_email(*, client_name: str, closure_date: date):
    subject = f"Enterprise Licence has closed — {client_name}"
    plain = (
        f"The Enterprise Licence for {client_name} on rootsTALK.in closed on "
        f"{closure_date.strftime('%d %b %Y')}.\n\n"
        f"The company is now Inactive: CA-portal logins are blocked and no "
        f"new farmer subscriptions can be created. Existing farmer "
        f"subscriptions assigned during the active window continue to their "
        f"natural close — they are not affected.\n\n"
        f"To restore access, grant a new Enterprise Licence or activate "
        f"the standard subscription model from the SA Portal.\n\n"
        f"— rootsTALK.in"
    )
    html = (
        f"<div style=\"font-family:sans-serif;padding:24px;max-width:560px\">"
        f"<h2 style=\"color:#7D4E00\">Enterprise Licence has closed</h2>"
        f"<p>The Enterprise Licence for <strong>{client_name}</strong> on "
        f"rootsTALK.in closed on "
        f"<strong>{closure_date.strftime('%d %b %Y')}</strong>.</p>"
        f"<p>The company is now <strong>Inactive</strong>: CA-portal "
        f"logins are blocked and no new farmer subscriptions can be "
        f"created. Existing farmer subscriptions assigned during the "
        f"active window continue to their natural close — they are not "
        f"affected.</p>"
        f"<p>To restore access, grant a new Enterprise Licence or "
        f"activate the standard subscription model from the SA Portal.</p>"
        f"<p style=\"color:#666;font-size:12px;margin-top:24px\">"
        f"This is an automated message from rootsTALK.in.</p>"
        f"</div>"
    )
    return subject, plain, html


# ── Inner sweep ─────────────────────────────────────────────────────────────

async def _sweep_with_session(db, today=None) -> dict:
    """Inner helper. Returns counts so the Celery wrapper can log.

    Idempotency: closure transitions on a row that's already EXPIRED
    won't fire because the WHERE clause filters status='ACTIVE'.
    Reminders are gated by the exact-day match, so a second run on
    the same day double-sends — beat is configured for one run a
    day so this isn't a real concern; tests document the behaviour.
    """
    today = today or date.today()
    rows = (await db.execute(
        select(EnterpriseLicense, Client)
        .join(Client, Client.id == EnterpriseLicense.client_id)
        .where(EnterpriseLicense.status == "ACTIVE")
    )).all()

    closures = 0
    reminders = 0

    for lic, client in rows:
        days_remaining = (lic.to_date - today).days
        client_name = client.display_name or client.full_name or client.short_name or "Client"

        if days_remaining < 0:
            # Past due — should already have been closed on the
            # original to_date. Catch up.
            days_remaining = 0

        if days_remaining == 0:
            lic.status = "EXPIRED"
            client.status = ClientStatus.INACTIVE
            subject, plain, html = _closure_email(
                client_name=client_name, closure_date=lic.to_date,
            )
            for recipient in _recipients(client):
                try:
                    _send_email(recipient, subject, html, plain)
                except Exception:
                    logger.exception(
                        "EL closure email failed for client=%s recipient=%s",
                        client.id, recipient,
                    )
            closures += 1
        elif days_remaining in REMINDER_DAYS:
            subject, plain, html = _reminder_email(
                client_name=client_name,
                days_remaining=days_remaining,
                closure_date=lic.to_date,
            )
            for recipient in _recipients(client):
                try:
                    _send_email(recipient, subject, html, plain)
                except Exception:
                    logger.exception(
                        "EL reminder email failed for client=%s recipient=%s",
                        client.id, recipient,
                    )
            reminders += 1

    if closures:
        await db.commit()

    return {"closures": closures, "reminders": reminders}


def _recipients(client: Client) -> list[str]:
    """De-duplicated email list — CA inbox + canonical SA inbox."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (client.ca_email, settings.sa_email):
        if not raw:
            continue
        key = raw.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(raw.strip())
    return out


# ── Celery entrypoint ───────────────────────────────────────────────────────

@shared_task(name="app.tasks.enterprise_license_lifecycle.sweep")
def sweep_enterprise_licenses():
    """Daily entrypoint via beat schedule in `app/celery_app.py`."""
    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            return await _sweep_with_session(db)

    counts = asyncio.run(_run())
    if counts["closures"] or counts["reminders"]:
        logger.info(
            "enterprise_license_lifecycle: closures=%d reminders=%d",
            counts["closures"], counts["reminders"],
        )
    return counts
