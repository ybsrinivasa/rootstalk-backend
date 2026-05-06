import secrets
import smtplib
import logging
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.modules.clients.models import Client, ClientUser, ClientUserRole, ClientStatus
from app.modules.platform.models import User, StatusEnum
from app.modules.auth.service import hash_password

logger = logging.getLogger(__name__)

ONBOARDING_LINK_HOURS = 24


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _send_email(to: str, subject: str, html: str, plain: str) -> bool:
    """Send an email via SMTP. Returns True on success, False on any
    failure (missing creds, connection, auth, rejected sender, etc.).

    Callers that surface user-visible flows (OTP login, password reset)
    should turn a False return into a 5xx so the user knows something
    went wrong — pre-fix this helper swallowed the exception silently
    and the API returned 200 even when the email never went out.
    """
    if not settings.email_smtp_user or not settings.email_smtp_pass:
        logger.error(
            "Email send to %s skipped: EMAIL_SMTP_USER / EMAIL_SMTP_PASS "
            "not configured. Subject was: %s",
            to, subject,
        )
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.email_from or settings.email_smtp_user
        msg["To"] = to
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port) as s:
            s.ehlo(); s.starttls()
            s.login(settings.email_smtp_user, settings.email_smtp_pass)
            s.sendmail(msg["From"], to, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Email send failed to {to}: {e}")
        return False


async def send_onboarding_email(client: Client, link: str):
    subject = f"Complete your RootsTalk company registration — {client.full_name}"
    plain = f"""Hi {client.ca_name},

You have been invited to register {client.full_name} on RootsTalk.

Complete your registration here: {link}

This link expires in 24 hours.

RootsTalk — Neytiri Eywafarm Agritech"""
    html = f"""
<body style="font-family:sans-serif;padding:32px">
  <h2>Welcome to RootsTalk</h2>
  <p>Hi {client.ca_name},</p>
  <p>You have been invited to complete the registration for <strong>{client.full_name}</strong>.</p>
  <p><a href="{link}" style="background:#1A5C2A;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none">Complete Registration</a></p>
  <p style="color:#666;font-size:12px">This link expires in 24 hours.</p>
</body>"""
    _send_email(client.ca_email, subject, html, plain)


async def send_ca_credentials_email(
    ca_email: str, ca_name: str, login_url: str, password: str,
):
    """Email the CA their post-approval credentials.

    `login_url` is built by the caller (e.g. `f"{_base_url()}/login/{short_name}"`)
    so the env-driven host always matches the deployment. Pre-fix this
    function hardcoded `https://rootstalk.in/{short_name}` — that was
    incorrect once `rootstalk.in` got earmarked for the PWA, and broke
    on testing/dev environments anyway.
    """
    subject = "Your RootsTalk Client Portal access"
    plain = f"""Hi {ca_name},

Your company has been approved on RootsTalk.

Login URL: {login_url}
Email: {ca_email}
Password: {password}

Please change your password after first login.

RootsTalk — Neytiri Eywafarm Agritech"""
    html = f"""
<body style="font-family:sans-serif;padding:32px">
  <h2>Welcome to RootsTalk — Your account is ready</h2>
  <p>Hi {ca_name}, your company registration has been approved.</p>
  <table style="background:#f8fafc;border-radius:8px;padding:16px;margin:16px 0">
    <tr><td><strong>Login URL:</strong></td><td><a href="{login_url}">{login_url}</a></td></tr>
    <tr><td><strong>Email:</strong></td><td>{ca_email}</td></tr>
    <tr><td><strong>Password:</strong></td><td>{password}</td></tr>
  </table>
  <p style="color:#666;font-size:12px">Please change your password after first login.</p>
</body>"""
    _send_email(ca_email, subject, html, plain)


async def get_client_by_token(db: AsyncSession, token: str) -> Client | None:
    result = await db.execute(
        select(Client).where(Client.onboarding_link_token == token)
    )
    client = result.scalar_one_or_none()
    if not client:
        return None
    if client.onboarding_link_expires_at and \
       client.onboarding_link_expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return client


async def create_ca_user(db: AsyncSession, client: Client) -> tuple[User, str]:
    """Create the CA portal user and return (user, plain_password)."""
    plain_password = secrets.token_urlsafe(12)
    user = User(
        email=client.ca_email,
        name=client.ca_name,
        password_hash=hash_password(plain_password),
        language_code="en",
    )
    db.add(user)
    await db.flush()
    db.add(ClientUser(
        client_id=client.id,
        user_id=user.id,
        role=ClientUserRole.CA,
        status=StatusEnum.ACTIVE,
    ))
    return user, plain_password
