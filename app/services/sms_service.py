import logging
from urllib.parse import urlparse

import httpx
from app.config import settings

logger = logging.getLogger(__name__)


def _normalise_indian_number(phone: str) -> str:
    """Strip leading +91 or 0 so Draft4SMS receives a bare 10-digit number."""
    number = phone.strip().lstrip("+")
    if number.startswith("91") and len(number) == 12:
        return number[2:]
    if number.startswith("0") and len(number) == 11:
        return number[1:]
    return number


async def send_sms(phone: str, message: str) -> bool:
    """Send an arbitrary SMS body via Draft4SMS. Returns True on success.

    Used by BL-09 daily alerts and any other non-OTP transactional SMS.
    OTP messages should keep using `send_otp_sms` so the OTP boilerplate
    and TTL line stay centralised.
    """
    if not settings.draft_sms_key:
        logger.warning("Draft4SMS key not configured — SMS not sent")
        return False

    params = {
        "apikey": settings.draft_sms_key,
        "senderid": settings.draft_sms_sender_id,
        "number": _normalise_indian_number(phone),
        "message": message,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(settings.draft_sms_base_url, params=params)
            response_text = response.text.strip()
            logger.info(f"Draft4SMS response for {params['number']}: {response_text}")
            if response.status_code == 200 and response_text and not response_text.lower().startswith("error"):
                return True
            logger.error(f"Draft4SMS error: {response_text}")
            return False
    except Exception as e:
        logger.error(f"Draft4SMS request failed: {e}")
        return False


def _pwa_origin() -> str:
    """Extract the bare hostname from `settings.pwa_base_url` — used as
    the bound-origin in the WebOTP last line of the OTP SMS. Defaults
    to `rootstalk.in` (prod) when the setting is empty; on staging the
    .env sets it to `https://rstalk-pwa.eywa.farm` and the hostname
    resolves to `rstalk-pwa.eywa.farm`. Each of these hostnames maps
    to a distinct DLT-approved template (both approved 2026-08-31)."""
    base = settings.pwa_base_url or "https://rootstalk.in"
    parsed = urlparse(base)
    return parsed.hostname or "rootstalk.in"


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    """
    Send OTP via Draft4SMS. Wraps `send_sms` with the DLT-registered OTP
    template body. The text below MUST stay byte-identical to the
    template registered with Neytiri Eywafarm Agritech Private Limited
    on the TRAI DLT registry — operators silently drop any message
    whose body diverges from the registered template (single character
    differences are enough to fail the match). The `{#var#}` slot in
    the DLT-registered template is the OTP code (used in two places:
    the human-readable line and the WebOTP bound-origin last line).

    2026-08-31 — Template rewritten to Phase 2 (WebOTP-compliant):
    Chrome on Android auto-fills the OTP into a page whose origin
    matches the `@<origin>` on the last line, so registration on
    prod is now near-tap-free. Two DLT templates approved — one per
    environment — resolved automatically via `_pwa_origin()`.

    Phone should be a 10-digit Indian mobile number (without country
    code). The API prepends +91 automatically for Indian numbers.
    Returns True on success, False on failure.
    """
    origin = _pwa_origin()
    message = (
        f"{otp_code} is your OTP for rootsTALK. Valid for 30 seconds. Do not share.\n"
        f"- NEYTIRI EYWAFARM AGRITECH PRIVATE LIMITED\n"
        f"\n"
        f"@{origin} #{otp_code}"
    )
    return await send_sms(phone, message)
