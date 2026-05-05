import logging
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


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    """
    Send OTP via Draft4SMS. Wraps `send_sms` with the standard OTP body.
    Phone should be a 10-digit Indian mobile number (without country code).
    The API prepends +91 automatically for Indian numbers.
    Returns True on success, False on failure.
    """
    message = f"Your RootsTalk sign-in code is {otp_code}. Valid for 10 minutes. Do not share this code. -EYFARM"
    return await send_sms(phone, message)
