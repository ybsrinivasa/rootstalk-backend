"""SMTP credential diagnostic.

Loads the same settings the FastAPI app does (from `.env` via Pydantic
Settings) and attempts an SMTP login + a single test send to the
configured EMAIL_FROM (or EMAIL_SMTP_USER if EMAIL_FROM is blank).

Run after rotating the Gmail app password to confirm the new
credentials work BEFORE relying on the API path:

    venv/bin/python scripts/check_smtp.py

Output is a step-by-step trace — host, port, user, presence of
EMAIL_FROM, password length (NOT the password itself), then the
auth result. On failure, the SMTP exception text printed here is
the same one `_send_email` would log when called from the API.

This script never prints the password. It does print its length
and a short hash hint so you can sanity-check that what's loaded
matches what you pasted — without leaking the value to terminal
scrollback.
"""
import hashlib
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings


def _hint(value: str) -> str:
    if not value:
        return "<empty>"
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"len={len(value)} sha256={digest}"


def main() -> int:
    print("── SMTP settings loaded from app.config.settings ─────────────")
    print(f"  EMAIL_SMTP_HOST: {settings.email_smtp_host!r}")
    print(f"  EMAIL_SMTP_PORT: {settings.email_smtp_port}")
    print(f"  EMAIL_SMTP_USER: {settings.email_smtp_user!r}")
    print(f"  EMAIL_SMTP_PASS: {_hint(settings.email_smtp_pass)}")
    print(f"  EMAIL_FROM:      {settings.email_from!r}")
    print()

    if not settings.email_smtp_user or not settings.email_smtp_pass:
        print("FAIL: EMAIL_SMTP_USER or EMAIL_SMTP_PASS is empty in the loaded settings.")
        print("Check that .env has both keys, no quotes around the values, and that")
        print("you saved the file. If the values look right in .env but show empty")
        print("here, the running process is loading a different .env than you edited.")
        return 1

    sender = settings.email_from or settings.email_smtp_user
    recipient = sender  # send the test mail to ourselves — safe and visible

    print(f"── Attempting SMTP login + test send to {recipient!r} ────────")
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "RootsTalk SMTP diagnostic"
        msg["From"] = sender
        msg["To"] = recipient
        msg.attach(MIMEText("This is a diagnostic test from scripts/check_smtp.py.", "plain"))

        with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port) as s:
            s.set_debuglevel(1)  # prints the full SMTP conversation
            s.ehlo()
            s.starttls()
            s.login(settings.email_smtp_user, settings.email_smtp_pass)
            s.sendmail(sender, recipient, msg.as_string())

        print()
        print(f"OK: test mail sent to {recipient}. Check that inbox.")
        return 0
    except smtplib.SMTPAuthenticationError as e:
        print()
        print(f"FAIL: SMTP authentication rejected. Server response: {e}")
        print()
        print("Most common causes:")
        print("  1. App password was pasted with the spaces Gmail shows (e.g.")
        print("     'abcd efgh ijkl mnop'). Strip spaces — Gmail rejects with")
        print("     a (535, ...) when spaces are present.")
        print("  2. App password is for a Gmail account that has 2FA off — app")
        print("     passwords only work when 2FA is on.")
        print("  3. The EMAIL_SMTP_USER and the account that generated the app")
        print("     password don't match.")
        return 2
    except Exception as e:
        print()
        print(f"FAIL: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
