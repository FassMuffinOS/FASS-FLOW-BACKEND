"""Thin Twilio REST client for Comms Hub — no SDK dependency, just httpx
(already a dependency for the APNs client), matching how this codebase
already prefers hand-rolled HTTP calls over heavier SDKs.

Twilio's Messages API takes HTTP Basic Auth (Account SID as username, Auth
Token as password) and a form-encoded body — not JSON.
"""
import httpx

from app.config import settings


async def send_sms(to_phone: str, body: str) -> tuple[bool, str | None, str | None]:
    """Returns (success, twilio_message_sid, error_message)."""
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_from_number):
        return False, None, "Twilio is not configured (missing account SID, auth token, or from-number)"

    url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                url,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={"To": to_phone, "From": settings.twilio_from_number, "Body": body},
            )
        except httpx.HTTPError as exc:
            return False, None, str(exc)

    if resp.status_code in (200, 201):
        data = resp.json()
        return True, data.get("sid"), None

    try:
        detail = resp.json().get("message", resp.text)
    except Exception:
        detail = resp.text
    return False, None, f"Twilio {resp.status_code}: {detail}"
