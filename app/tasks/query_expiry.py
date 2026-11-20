"""BL-12b — Query Expiry Background Job (runs hourly).
Closes all queries past their SLA window (see QUERY_EXPIRE_DAYS in
farmpundit/router.py — currently 2 days end-of-IST-Day-2).

On expiry:
  - Flips Query.status to EXPIRED and clears current_holder_id so
    the pundit's PWA active queue drops it (status filter).
  - Writes a QueryRemark(action=REJECTED, "Auto-expired …") so the
    admin UI history row shows the system-closed event.
  - Sends an FCM to the farmer (if they have a PWA token) so they
    aren't left wondering.
  - 2026-11-20 — Emails every ACTIVE CA at the client with the full
    context (farmer / crop / package / query / remarks chain / last
    holder). Replaces the pre-2026-11-20 TODO log.

Applies uniformly across expert roles (PRIMARY / PANEL /
PROMOTER_PUNDIT) — the expiry filter is status-based, not role-
based, so a PP query held past the window expires the same way.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.modules.farmpundit.models import (
    Query, QueryRemark, QueryStatus, QueryRemarkAction, FarmPunditProfile,
)
from app.modules.platform.models import User, StatusEnum
from app.modules.clients.models import Client, ClientUser, ClientUserRole
from app.services.fcm_service import send_fcm

logger = logging.getLogger(__name__)

EXPIRY_FCM_TITLE = "Your query couldn't be answered in time"
EXPIRY_FCM_BODY = (
    "The window for your FarmPundit query has elapsed. The company "
    "has been notified — please raise it again or ask another expert."
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _fmt_ist(dt):
    """UTC datetime → 'DD Mon YYYY, HH:MM IST' string."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_IST).strftime("%d %b %Y, %H:%M IST")


async def _expire_queries_with_session(db, now=None) -> int:
    """Inner sweep: closes every Query past its 7-day expiry window
    that is still in a non-terminal status. Split out so integration
    tests can inject the testcontainer session and assert on the rows
    the task commits."""
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(Query).where(
            Query.status.notin_([
                QueryStatus.RESPONDED,
                QueryStatus.REJECTED,
                QueryStatus.EXPIRED,
            ]),
            Query.expires_at <= now,
        )
    )
    queries = result.scalars().all()

    for query in queries:
        query.status = QueryStatus.EXPIRED
        query.current_holder_id = None

        db.add(QueryRemark(
            query_id=query.id,
            pundit_id=None,
            # The QueryRemarkAction enum has no EXPIRED value — REJECTED
            # is the closest neighbour. The remark text marks this as
            # auto-expired so downstream readers can disambiguate.
            # Adding EXPIRED to the enum requires an Alembic migration
            # and is tracked as a deferred follow-up.
            action=QueryRemarkAction.REJECTED,
            remark="Auto-expired: 7-day resolution window elapsed.",
        ))

        # FCM Batch 3 (2026-05-06): notify farmer that their query
        # auto-expired. Skipped silently if the farmer hasn't
        # registered a token yet (most farmers in V1 until the PWA
        # wires the registration call).
        farmer = (await db.execute(
            select(User).where(User.id == query.farmer_user_id)
        )).scalar_one_or_none()
        if farmer and farmer.fcm_token:
            try:
                await send_fcm(
                    token=farmer.fcm_token,
                    title=EXPIRY_FCM_TITLE,
                    body=EXPIRY_FCM_BODY,
                    data={
                        "type": "QUERY_EXPIRED",
                        "query_id": query.id,
                        "subscription_id": query.subscription_id,
                        "click_action": f"/crop-detail/{query.subscription_id}/queries",
                    },
                )
            except Exception as e:
                # send_fcm catches its own errors and returns False;
                # this guard is belt-and-braces.
                logger.error(f"FCM send raised unexpectedly for farmer {farmer.id}: {e}")
        logger.info(f"Query {query.id} expired. Farmer: {farmer.phone if farmer else 'unknown'}")

        # 2026-11-20 — Email every ACTIVE CA at the client with the
        # full query context. Non-fatal: send failure logs + moves on;
        # the expiry itself has already committed above.
        try:
            await _send_expiry_email(db, query)
        except Exception as e:
            logger.error(
                f"Query-expiry email failed for query {query.id}: {e}"
            )

    if queries:
        await db.commit()
        logger.info(f"BL-12b: Expired {len(queries)} queries")
    return len(queries)


async def _expire_queries():
    """Production entry point: opens its own session and runs the
    inner sweep."""
    async with AsyncSessionLocal() as db:
        return await _expire_queries_with_session(db)


@celery_app.task(name="app.tasks.query_expiry.expire_queries")
def expire_queries():
    """BL-12b: Hourly check for expired queries.

    BL-12 audit (2026-05-06): migrated from
    asyncio.get_event_loop().run_until_complete(...) to asyncio.run.
    Same family as the BL-09 alerts fix — the old form is deprecated
    and raises in Python 3.12+.
    """
    asyncio.run(_expire_queries())


# ── CA email on expiry (2026-11-20) ───────────────────────────────────────────

async def _send_expiry_email(db, query: Query) -> None:
    """Email every ACTIVE CA at the query's client with the full
    expired-query context: farmer, crop, package, subscription,
    severity, title, description, submitted/expired timestamps (IST),
    the remark chain (who held it, when, what action), the last
    holder at the moment of expiry, AND the query's media (photos /
    videos the farmer attached) as email attachments so the CA has
    the full visual context without needing to open the portal.

    Uses the same SMTP transport as the existing _send_email helper
    in app/modules/clients/service.py but with multipart/mixed so
    we can attach binary media. Silent on empty-CA-list; non-fatal
    on individual send or media-download failure.
    """
    from app.modules.advisory.models import Package
    from app.modules.subscriptions.models import Subscription
    from app.modules.farmpundit.models import QueryMedia
    from app.services.i18n_cosh import resolve_name_for_cosh_id

    # Recipients — every ACTIVE CA at this client
    ca_rows = (await db.execute(
        select(User)
        .join(ClientUser, ClientUser.user_id == User.id)
        .where(
            ClientUser.client_id == query.client_id,
            ClientUser.role == ClientUserRole.CA,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    recipients = [u.email for u in ca_rows if u and u.email]
    if not recipients:
        logger.info(
            f"Query {query.id} expired but no ACTIVE CA with email at client "
            f"{query.client_id} — skipping email."
        )
        return

    # Context — load everything needed for the body
    client = (await db.execute(
        select(Client).where(Client.id == query.client_id)
    )).scalar_one_or_none()
    farmer = (await db.execute(
        select(User).where(User.id == query.farmer_user_id)
    )).scalar_one_or_none()
    sub = (await db.execute(
        select(Subscription).where(Subscription.id == query.subscription_id)
    )).scalar_one_or_none()
    pkg = None
    if sub is not None:
        pkg = (await db.execute(
            select(Package).where(Package.id == sub.package_id)
        )).scalar_one_or_none()

    # Crop name — prefer Query.crop_cosh_id; fall back to Package.crop_cosh_id
    crop_cosh_id = query.crop_cosh_id or (pkg.crop_cosh_id if pkg else None)
    crop_name = await resolve_name_for_cosh_id(db, crop_cosh_id, lang="en") or "—"

    # Severity label — enum stored as string; render as-is
    severity = (
        query.severity.value if hasattr(query.severity, "value")
        else str(query.severity or "—")
    )

    # Remarks chain — ordered oldest → newest; resolve pundit names
    remarks = (await db.execute(
        select(QueryRemark)
        .where(QueryRemark.query_id == query.id)
        .order_by(QueryRemark.created_at)
    )).scalars().all()
    pundit_ids = {r.pundit_id for r in remarks if r.pundit_id}
    pundit_ids.update({r.forwarded_to_pundit_id for r in remarks if r.forwarded_to_pundit_id})
    pundit_name_by_id: dict[str, str] = {}
    if pundit_ids:
        prows = (await db.execute(
            select(FarmPunditProfile, User)
            .join(User, User.id == FarmPunditProfile.user_id)
            .where(FarmPunditProfile.id.in_(pundit_ids))
        )).all()
        for profile, user in prows:
            pundit_name_by_id[profile.id] = user.name or user.phone or profile.id[:8]

    # Last holder at the moment of expiry — the holder immediately
    # BEFORE the sweep cleared current_holder_id. The most recent
    # QueryRemark whose pundit_id is set is a reliable anchor; falls
    # back to "—" if no pundit remark exists (rare).
    last_holder_id = next(
        (r.pundit_id for r in reversed(remarks) if r.pundit_id),
        None,
    )
    last_holder_name = pundit_name_by_id.get(last_holder_id, "—") if last_holder_id else "—"

    # Build plain + html bodies
    client_name = (client.display_name or client.full_name) if client else "—"
    farmer_name = farmer.name if farmer else "—"
    farmer_phone = farmer.phone if farmer else "—"
    pkg_name = pkg.name if pkg else "—"
    sub_ref = sub.reference_number if sub else "—"
    submitted = _fmt_ist(query.created_at)
    expired = _fmt_ist(query.expires_at)
    title = query.title or "—"
    description = query.description or "—"

    remarks_lines_plain: list[str] = []
    remarks_lines_html: list[str] = []
    for r in remarks:
        actor = pundit_name_by_id.get(r.pundit_id, "System") if r.pundit_id else "System"
        action = r.action.value if hasattr(r.action, "value") else str(r.action)
        fwd = pundit_name_by_id.get(r.forwarded_to_pundit_id) if r.forwarded_to_pundit_id else None
        text = r.remark or ""
        if fwd:
            text = f"Forwarded to {fwd}. {text}".strip()
        when = _fmt_ist(r.created_at)
        remarks_lines_plain.append(f"  {when} — {actor} [{action}]: {text}")
        remarks_lines_html.append(
            f'<li><strong>{when}</strong> — {actor} [{action}]: {text}</li>'
        )

    plain = f"""A farmer's query auto-expired without a response.

Client: {client_name}

Farmer: {farmer_name} — {farmer_phone}
Crop: {crop_name}
Package: {pkg_name}
Subscription ref: {sub_ref}

Query
  Title: {title}
  Severity: {severity}
  Submitted: {submitted}
  Expired:   {expired}

Description:
{description}

Last holder when expired: {last_holder_name}

Remarks history
{chr(10).join(remarks_lines_plain) if remarks_lines_plain else "  (no remarks)"}

This query has been removed from your experts' active queues. The farmer has been notified and may raise it again.

— RootsTalk
"""

    html = f"""<html><body style="font-family: Arial, sans-serif; color: #333;">
<p>A farmer's query <strong>auto-expired</strong> without a response.</p>

<p><strong>Client:</strong> {client_name}</p>

<table cellpadding="4" style="border-collapse: collapse;">
<tr><td><strong>Farmer</strong></td><td>{farmer_name} — {farmer_phone}</td></tr>
<tr><td><strong>Crop</strong></td><td>{crop_name}</td></tr>
<tr><td><strong>Package</strong></td><td>{pkg_name}</td></tr>
<tr><td><strong>Subscription ref</strong></td><td>{sub_ref}</td></tr>
</table>

<h3 style="margin-top: 20px;">Query</h3>
<table cellpadding="4" style="border-collapse: collapse;">
<tr><td><strong>Title</strong></td><td>{title}</td></tr>
<tr><td><strong>Severity</strong></td><td>{severity}</td></tr>
<tr><td><strong>Submitted</strong></td><td>{submitted}</td></tr>
<tr><td><strong>Expired</strong></td><td>{expired}</td></tr>
</table>

<p><strong>Description:</strong><br>{description.replace(chr(10), "<br>")}</p>

<p><strong>Last holder when expired:</strong> {last_holder_name}</p>

<h3>Remarks history</h3>
<ol>
{chr(10).join(remarks_lines_html) if remarks_lines_html else "<li>(no remarks)</li>"}
</ol>

<p style="color: #666; font-size: 0.9em; margin-top: 20px;">
This query has been removed from your experts' active queues. The farmer has been notified and may raise it again.
</p>
<p style="color: #999; font-size: 0.85em;">— RootsTalk</p>
</body></html>
"""

    # Media attachments — fetch the farmer's uploaded photos / videos
    # for this query. Downloads are best-effort: a single failure
    # (bad URL, network, 404) is logged + skipped; the email still
    # goes with the attachments that did load. Size cap per
    # attachment is 20 MB (SMTP usually rejects beyond ~25 MB total);
    # oversize items are replaced by an inline reference in the body.
    media_rows = (await db.execute(
        select(QueryMedia).where(QueryMedia.query_id == query.id)
    )).scalars().all()
    attachments, skipped_notes = await _fetch_query_media(media_rows, query.id)

    if skipped_notes:
        # Mention skipped-media URLs inline so the CA can click through
        # to the originals via portal / CDN link.
        plain += "\nMedia not attached (fetch failed or oversize):\n"
        plain += "\n".join(f"  - {n}" for n in skipped_notes) + "\n"
        html = html.replace(
            "</body></html>",
            "<p style='color: #666; font-size: 0.85em;'><strong>Media not attached:</strong><br>"
            + "<br>".join(skipped_notes) + "</p></body></html>",
        )

    subject = f"[RootsTalk] Query expired — {farmer_name} / {crop_name}"
    for to in recipients:
        try:
            ok = _send_email_with_attachments(to, subject, html, plain, attachments)
            if ok:
                logger.info(
                    f"Query-expiry email sent to {to} for query {query.id} "
                    f"with {len(attachments)} media attachment(s)"
                )
            else:
                logger.warning(
                    f"Query-expiry email send returned False for {to} / query {query.id}"
                )
        except Exception as e:
            logger.error(
                f"Query-expiry email raised for {to} / query {query.id}: {e}"
            )


# Max per-attachment payload. SMTP servers typically cap total message
# size at ~25 MB; capping each file at 20 MB keeps us safely below
# that even with multiple attachments + a modest body. Oversize media
# gets mentioned by URL in the body instead.
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


async def _fetch_query_media(
    media_rows, query_id: str,
) -> tuple[list[tuple[bytes, str, str]], list[str]]:
    """Download each QueryMedia row's URL into a (bytes, filename,
    mime_type) tuple suitable for email attachment. Returns
    (attachments, skipped_notes). Skipped notes are human-readable
    "URL (reason)" strings for items that couldn't be attached
    (download failed, oversize, etc.) — surfaced in the email body
    so the CA can still click through to the originals."""
    import httpx
    import os
    from urllib.parse import urlparse

    attachments: list[tuple[bytes, str, str]] = []
    skipped: list[str] = []
    if not media_rows:
        return attachments, skipped
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        for i, m in enumerate(media_rows, start=1):
            url = m.url
            if not url:
                continue
            try:
                r = await http.get(url)
                if r.status_code != 200:
                    skipped.append(f"{url} (HTTP {r.status_code})")
                    continue
                data = r.content
                if len(data) > _MAX_ATTACHMENT_BYTES:
                    skipped.append(
                        f"{url} (oversize {len(data) // (1024 * 1024)} MB; max 20 MB)"
                    )
                    continue
                # Filename — prefer the URL's path basename; fall back
                # to a sequential name derived from the media_type.
                path_name = os.path.basename(urlparse(url).path) or ""
                if not path_name or "." not in path_name:
                    ext = _ext_for_mime(r.headers.get("content-type") or m.media_type)
                    path_name = f"query-media-{i}{ext}"
                mime = (
                    r.headers.get("content-type")
                    or _mime_for_media_type(m.media_type)
                )
                attachments.append((data, path_name, mime))
            except Exception as e:
                logger.error(
                    f"Media download failed for query {query_id} / {url}: {e}"
                )
                skipped.append(f"{url} (download error)")
    return attachments, skipped


def _ext_for_mime(mime: str) -> str:
    m = (mime or "").lower().split(";")[0].strip()
    return {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif",
        "video/mp4": ".mp4", "video/quicktime": ".mov",
        "video/webm": ".webm",
    }.get(m, ".bin")


def _mime_for_media_type(media_type: str) -> str:
    # QueryMedia.media_type is a short label like "IMAGE" / "VIDEO".
    # Default to octet-stream when we can't map; the attachment still
    # opens in any client, just without a preview.
    t = (media_type or "").upper()
    if t == "IMAGE":
        return "image/jpeg"
    if t == "VIDEO":
        return "video/mp4"
    return "application/octet-stream"


def _send_email_with_attachments(
    to: str, subject: str, html: str, plain: str,
    attachments: list[tuple[bytes, str, str]],
) -> bool:
    """Send an email with optional binary attachments. Mirrors the
    coaching-local `_send_email_with_pdf_attachments` helper but
    generalised to any MIME type — the three-tuple is
    (bytes, filename, mime_type). Non-fatal, returns False on any
    SMTP / auth / env-config failure."""
    import smtplib
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage
    from email.mime.base import MIMEBase
    from email import encoders
    from app.config import settings

    if not settings.email_smtp_user or not settings.email_smtp_pass:
        logger.error(
            "Query-expiry email to %s skipped: EMAIL_SMTP_USER / _PASS "
            "not configured. Subject: %s", to, subject,
        )
        return False
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = settings.email_from or settings.email_smtp_user
        msg["To"] = to
        body = MIMEMultipart("alternative")
        body.attach(MIMEText(plain, "plain"))
        body.attach(MIMEText(html, "html"))
        msg.attach(body)
        for data, filename, mime in attachments:
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            if maintype == "image":
                att = MIMEImage(data, _subtype=subtype or "jpeg")
            elif maintype == "application":
                att = MIMEApplication(data, _subtype=subtype or "octet-stream")
            else:
                att = MIMEBase(maintype or "application", subtype or "octet-stream")
                att.set_payload(data)
                encoders.encode_base64(att)
            att.add_header(
                "Content-Disposition", "attachment", filename=filename,
            )
            msg.attach(att)
        with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port) as s:
            s.ehlo(); s.starttls()
            s.login(settings.email_smtp_user, settings.email_smtp_pass)
            s.sendmail(msg["From"], to, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Query-expiry email send failed to {to}: {e}")
        return False
