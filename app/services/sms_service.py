import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)


async def send_otp_sms(phone: str, otp_code: str) -> bool:
    """
    Send OTP via Draft4SMS.
    Phone should be a 10-digit Indian mobile number (without country code).
    The API prepends +91 automatically for Indian numbers.
    Returns True on success, False on failure.
    """
    if not settings.draft_sms_key:
        logger.warning("Draft4SMS key not configured — OTP not sent via SMS")
        return False

    # Normalise phone: strip leading +91 or 0 if present
    number = phone.strip().lstrip("+")
    if number.startswith("91") and len(number) == 12:
        number = number[2:]
    elif number.startswith("0") and len(number) == 11:
        number = number[1:]

    message = f"Your RootsTalk sign-in code is {otp_code}. Valid for 10 minutes. Do not share this code. -EYFARM"

    params = {
        "apikey": settings.draft_sms_key,
        "senderid": settings.draft_sms_sender_id,
        "number": number,
        "message": message,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(settings.draft_sms_base_url, params=params)
            response_text = response.text.strip()
            logger.info(f"Draft4SMS response for {number}: {response_text}")
            # Draft4SMS returns a message ID on success (numeric string)
            if response.status_code == 200 and response_text and not response_text.lower().startswith("error"):
                return True
            logger.error(f"Draft4SMS error: {response_text}")
            return False
    except Exception as e:
        logger.error(f"Draft4SMS request failed: {e}")
        return False
