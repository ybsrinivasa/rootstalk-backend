"""Coaching Sandbox — email templates for invite + credentials.
Two audiences:
  - Prospective student: invite link → self-registration form
  - Approved student: portal login credentials + PWA phone reminder
"""
import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import settings
from app.modules.clients.service import _send_email

logger = logging.getLogger(__name__)


def _send_email_with_pdf_attachment(
    to: str, subject: str, html: str, plain: str,
    pdf_bytes: bytes, pdf_filename: str,
) -> bool:
    """Coaching certificate delivery — same SMTP path as `_send_email`
    but with a PDF attached. Kept coaching-local because the base
    `_send_email` intentionally doesn't grow attachment support (the
    other callers — onboarding, reset, welcome — never need it)."""
    if not settings.email_smtp_user or not settings.email_smtp_pass:
        logger.error(
            "Certificate email to %s skipped: EMAIL_SMTP_USER / _PASS not "
            "configured. Subject was: %s", to, subject,
        )
        return False
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = settings.email_from or settings.email_smtp_user
        msg["To"] = to
        # Body as an alternative-part so both plain + html render
        body = MIMEMultipart("alternative")
        body.attach(MIMEText(plain, "plain"))
        body.attach(MIMEText(html, "html"))
        msg.attach(body)
        # PDF attachment
        att = MIMEApplication(pdf_bytes, _subtype="pdf")
        att.add_header(
            "Content-Disposition", "attachment", filename=pdf_filename,
        )
        msg.attach(att)
        with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port) as s:
            s.ehlo(); s.starttls()
            s.login(settings.email_smtp_user, settings.email_smtp_pass)
            s.sendmail(msg["From"], to, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Certificate email send failed to {to}: {e}")
        return False


def send_certificate_email(
    to_email: str,
    student_name: str,
    coach_name: str,
    reference_client_name: str,
    grade: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    verification_url: str,
) -> bool:
    """Emails the freshly-generated certificate PDF to the student.
    Body mentions grade + coach + reference client + verification URL
    so the certificate context is captured in the email even before
    the recipient opens the attachment."""
    grade_labels = {
        "SATISFACTORY": "Satisfactory",
        "GOOD": "Good",
        "EXCELLENT": "Excellent",
    }
    grade_label = grade_labels.get(grade, grade)
    subject = (
        f"Your rootsTALK coaching certificate — {grade_label}"
    )
    plain = f"""Congratulations, {student_name}!

Your rootsTALK Coaching Program certificate is attached to this email.

Grade: {grade_label}
Coached by: {coach_name}
Context: {reference_client_name}

You can verify the authenticity of this certificate at any time via:
{verification_url}

RootsTalk — Neytiri Eywafarm Agritech"""
    html = f"""
<body style="font-family:sans-serif;padding:32px;background:#F7F5F0">
  <div style="max-width:540px;margin:0 auto;background:#fff;padding:32px;border-radius:12px;border:1px solid #DDD0B8">
    <h2 style="color:#7D4196;margin:0 0 8px">Congratulations, {student_name}!</h2>
    <p style="color:#333">Your rootsTALK Coaching Program certificate is attached to this email.</p>
    <table style="background:#f8fafc;border-radius:8px;padding:16px;margin:16px 0;border-collapse:collapse;width:100%">
      <tr><td style="padding:4px 8px;color:#666"><strong>Grade:</strong></td><td style="padding:4px 8px">{grade_label}</td></tr>
      <tr><td style="padding:4px 8px;color:#666"><strong>Coached by:</strong></td><td style="padding:4px 8px">{coach_name}</td></tr>
      <tr><td style="padding:4px 8px;color:#666"><strong>Context:</strong></td><td style="padding:4px 8px">{reference_client_name}</td></tr>
    </table>
    <p style="color:#666;font-size:13px">You can verify the authenticity of this certificate at any time:</p>
    <p><a href="{verification_url}" style="color:#7D4196;word-break:break-all">{verification_url}</a></p>
    <p style="color:#999;font-size:11px;margin-top:24px">RootsTalk — Neytiri Eywafarm Agritech</p>
  </div>
</body>"""
    return _send_email_with_pdf_attachment(
        to_email, subject, html, plain, pdf_bytes, pdf_filename,
    )


def send_student_invite_email(
    to_email: str, invite_link: str, coach_name: str, reference_client_name: str,
) -> bool:
    """Fires on POST /coaching/sessions/{id}/invites — the student
    clicks the link, lands on the self-registration form, and their
    submission comes back to the coach for approval."""
    subject = "You are invited to a rootsTALK coaching session"
    plain = f"""Hi,

{coach_name} has invited you to a rootsTALK coaching session in the context of {reference_client_name}.

To join, please open this link and complete the short self-registration form:
{invite_link}

Once you submit, {coach_name} will review your details and confirm your enrolment.

If you did not expect this invitation, you can safely ignore this email.

RootsTalk — Neytiri Eywafarm Agritech"""
    html = f"""
<body style="font-family:sans-serif;padding:32px">
  <h2 style="color:#1A5C2A">You are invited to a rootsTALK coaching session</h2>
  <p><strong>{coach_name}</strong> has invited you to a coaching session in the context of <strong>{reference_client_name}</strong>.</p>
  <p>Please complete the short self-registration form to join:</p>
  <p><a href="{invite_link}" style="background:#1A5C2A;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none">Complete Registration</a></p>
  <p style="color:#666;font-size:12px">Once you submit, {coach_name} will review your details and confirm your enrolment.</p>
  <p style="color:#666;font-size:12px">If you did not expect this invitation, you can safely ignore this email.</p>
</body>"""
    return _send_email(to_email, subject, html, plain)


def send_student_credentials_email(
    to_email: str,
    student_name: str,
    portal_url: str,
    portal_password: str,
    approved_phone: str,
    coach_name: str,
    reference_client_name: str,
    workspace_short_name: str,
) -> bool:
    """Fires on invite approval. Gives the student both logins:
    portal (email + password) for their coaching workspace, and PWA
    (approved phone + OTP) for role-play interactions.

    The `approved_phone` note is critical — the student may forget
    which number they registered with, and only that number will be
    accepted by OTP request while the session is open."""
    subject = "Your rootsTALK coaching enrolment is confirmed"
    plain = f"""Hi {student_name},

Your enrolment in {coach_name}'s coaching session (context: {reference_client_name}) is confirmed.

You now have two ways to log in — both belong to you for the duration of this coaching session.

1. Portal login (for creating packages, advisories, and team members)
   URL: {portal_url}
   Email: {to_email}
   Password: {portal_password}
   Your workspace: {workspace_short_name}

2. PWA login (for playing farmer / dealer / facilitator / pundit roles)
   Registered phone: {approved_phone}
   Login by entering this phone number on the rootsTALK PWA — you'll get an OTP.
   Only this phone number will be accepted while the coaching session is active.

The session is currently in draft. You will only be able to log in after {coach_name} starts the session — you will be notified separately when that happens.

Please change your portal password after first login.

RootsTalk — Neytiri Eywafarm Agritech"""
    html = f"""
<body style="font-family:sans-serif;padding:32px">
  <h2 style="color:#1A5C2A">Your rootsTALK coaching enrolment is confirmed</h2>
  <p>Hi {student_name},</p>
  <p>Your enrolment in <strong>{coach_name}</strong>'s coaching session (context: <strong>{reference_client_name}</strong>) is confirmed.</p>
  <h3 style="margin-top:24px">1. Portal login</h3>
  <p style="color:#555">For creating packages, advisories, and team members in your workspace.</p>
  <table style="background:#f8fafc;border-radius:8px;padding:16px;margin:16px 0;border-collapse:collapse">
    <tr><td style="padding:4px"><strong>URL:</strong></td><td style="padding:4px"><a href="{portal_url}">{portal_url}</a></td></tr>
    <tr><td style="padding:4px"><strong>Email:</strong></td><td style="padding:4px">{to_email}</td></tr>
    <tr><td style="padding:4px"><strong>Password:</strong></td><td style="padding:4px"><code>{portal_password}</code></td></tr>
    <tr><td style="padding:4px"><strong>Workspace:</strong></td><td style="padding:4px">{workspace_short_name}</td></tr>
  </table>
  <h3 style="margin-top:24px">2. PWA login</h3>
  <p style="color:#555">For playing farmer / dealer / facilitator / pundit roles.</p>
  <table style="background:#f8fafc;border-radius:8px;padding:16px;margin:16px 0;border-collapse:collapse">
    <tr><td style="padding:4px"><strong>Registered phone:</strong></td><td style="padding:4px"><code>{approved_phone}</code></td></tr>
  </table>
  <p style="color:#555;font-size:13px">Only this phone will be accepted for OTP while the coaching session is active.</p>
  <p style="color:#B45309;background:#FEF3C7;padding:12px;border-radius:6px;font-size:13px;margin-top:16px">
    The session is currently in <strong>draft</strong>. You will only be able to log in after {coach_name} starts the session — you will be notified separately when that happens.
  </p>
  <p style="color:#666;font-size:12px;margin-top:24px">Please change your portal password after first login.</p>
</body>"""
    return _send_email(to_email, subject, html, plain)


def send_session_started_email(
    to_email: str,
    student_name: str,
    coach_name: str,
    reference_client_name: str,
    portal_url: str,
) -> bool:
    """Fires on session Start → notifies each approved student that
    they can now log in. Complements the credentials email which was
    sent at approval time."""
    subject = "Your rootsTALK coaching session has started"
    plain = f"""Hi {student_name},

{coach_name} has started the coaching session (context: {reference_client_name}).

You can now log in to your workspace: {portal_url}

Use the credentials you received in the earlier confirmation email. Your registered phone number is also active for PWA login now.

RootsTalk — Neytiri Eywafarm Agritech"""
    html = f"""
<body style="font-family:sans-serif;padding:32px">
  <h2 style="color:#1A5C2A">Your rootsTALK coaching session has started</h2>
  <p>Hi {student_name},</p>
  <p><strong>{coach_name}</strong> has started the coaching session (context: <strong>{reference_client_name}</strong>).</p>
  <p>You can now log in to your workspace: <a href="{portal_url}">{portal_url}</a></p>
  <p style="color:#555">Use the credentials you received in the earlier confirmation email. Your registered phone number is also active for PWA login now.</p>
</body>"""
    return _send_email(to_email, subject, html, plain)
