"""Comms Hub — outbound/inbound messaging, sent via Twilio SMS.

Originally designed around a self-hosted Mac/Messages.app relay for real
iMessage delivery (see mac-relay/ at the repo root — still there, still
usable later) — but that requires owning a Mac with a dedicated Apple ID
running 24/7, which isn't available yet. Twilio SMS needs none of that: the
backend calls Twilio's API directly on send, no relay, no polling, works
today. Outbound messages go out as green-bubble SMS, not blue-bubble
iMessage; the table/schema still distinguishes channel ('imessage' vs
'sms') so a future Mac relay can be wired back in as an alternative send
path without a schema change — see app/services/twilio_sms.py and the
mac-relay scripts for that path.

Ownership model matches every other router here: no auth dependency, the
caller's business_user_id is taken at face value and the service-role
client is used for every read/write.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import settings
from app.database import get_supabase, single_data
from app.services.twilio_sms import send_sms

router = APIRouter(prefix="/comms", tags=["comms"])


class SendRequest(BaseModel):
    business_user_id: str
    phone: str
    body: str
    card_slug: str | None = None
    channel: str = "sms"


@router.post("/send")
async def send_message(body: SendRequest):
    if not body.phone.strip() or not body.body.strip():
        raise HTTPException(status_code=400, detail="phone and body are required")

    sb = get_supabase()
    row = {
        "business_user_id": body.business_user_id,
        "phone": body.phone.strip(),
        "body": body.body.strip(),
        "card_slug": body.card_slug,
        "channel": "sms",
        "direction": "out",
        "status": "queued",
    }
    created = single_data(sb.table("comms_messages").insert(row).execute())

    ok, sid, error = await send_sms(row["phone"], row["body"])
    update = {"status": "sent" if ok else "failed"}
    if sid:
        update["external_id"] = sid
    if error:
        update["error"] = error
    if ok:
        update["sent_at"] = "now()"
    sb.table("comms_messages").update(update).eq("id", created["id"]).execute()

    if not ok:
        # The row exists either way (so it shows up in the thread as
        # failed) but surface the Twilio error to the caller too, since a
        # silent failed-send is worse than a loud one here.
        raise HTTPException(status_code=502, detail=f"Message queued but send failed: {error}")

    return {**created, **update}


@router.get("/outbox")
async def get_outbox(limit: int = 25):
    """Polled by the Mac relay. Returns the oldest queued messages first so
    nothing waits forever behind a burst of newer sends."""
    sb = get_supabase()
    rows = (
        sb.table("comms_messages")
        .select("*")
        .eq("status", "queued")
        .eq("direction", "out")
        .order("created_at")
        .limit(limit)
        .execute()
        .data
        or []
    )
    return {"messages": rows}


class AckRequest(BaseModel):
    status: str  # "sent" or "failed"
    external_id: str | None = None
    error: str | None = None


@router.post("/outbox/{message_id}/ack")
async def ack_outbox(message_id: str, body: AckRequest):
    if body.status not in ("sent", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'sent' or 'failed'")

    sb = get_supabase()
    update = {"status": body.status}
    if body.external_id:
        update["external_id"] = body.external_id
    if body.error:
        update["error"] = body.error
    if body.status == "sent":
        update["sent_at"] = "now()"
    sb.table("comms_messages").update(update).eq("id", message_id).execute()
    return {"status": body.status}


@router.post("/twilio/inbound")
async def receive_twilio_inbound(request: Request):
    """Webhook target you configure on the Twilio number itself: Twilio
    Console -> Phone Numbers -> your number -> "A message comes in" ->
    Webhook -> https://<your-backend>/api/v1/comms/twilio/inbound.

    Twilio posts application/x-www-form-urlencoded, not JSON — From/To/Body
    are Twilio's own field names. Single-tenant MVP: there's no
    per-business phone-number table yet, so every inbound message is
    attributed to settings.twilio_inbound_business_user_id regardless of
    which "To" number it came in on (there's currently only one). Must
    return 200 with empty/TwiML body or Twilio will retry and eventually
    mark the webhook unhealthy.
    """
    form = await request.form()
    from_phone = form.get("From", "")
    text = form.get("Body", "")
    message_sid = form.get("MessageSid")

    if not settings.twilio_inbound_business_user_id:
        # Misconfigured — still 200 so Twilio doesn't retry forever, but
        # don't pretend we stored anything.
        return PlainTextResponse("", status_code=200)

    sb = get_supabase()
    row = {
        "business_user_id": settings.twilio_inbound_business_user_id,
        "phone": from_phone,
        "body": text,
        "channel": "sms",
        "direction": "in",
        "status": "received",
        "external_id": message_sid,
    }
    try:
        sb.table("comms_messages").insert(row).execute()
    except Exception as exc:
        # external_id has a unique index — Twilio occasionally retries the
        # same webhook; that should be a silent no-op, not a 500.
        if "duplicate key" not in str(exc).lower():
            raise
    return PlainTextResponse("", status_code=200)


@router.get("/threads")
async def list_threads(business_user_id: str):
    """One row per phone number, most recently active first — the contact
    list view. Pulled in one query and folded down in Python since Supabase
    doesn't have a clean "latest row per group" without a view/RPC, and
    standing up a SQL view here would be the first one in this codebase."""
    sb = get_supabase()
    rows = (
        sb.table("comms_messages")
        .select("*")
        .eq("business_user_id", business_user_id)
        .order("created_at", desc=True)
        .limit(500)
        .execute()
        .data
        or []
    )
    threads = {}
    for r in rows:
        phone = r["phone"]
        if phone not in threads:
            threads[phone] = {
                "phone": phone,
                "card_slug": r.get("card_slug"),
                "last_body": r["body"],
                "last_direction": r["direction"],
                "last_status": r["status"],
                "last_at": r["created_at"],
                "unread": 0,
            }
        if r["direction"] == "in" and r["status"] == "received":
            threads[phone]["unread"] += 1
    return {"threads": list(threads.values())}


@router.get("/thread")
async def get_thread(business_user_id: str, phone: str):
    sb = get_supabase()
    rows = (
        sb.table("comms_messages")
        .select("*")
        .eq("business_user_id", business_user_id)
        .eq("phone", phone)
        .order("created_at")
        .execute()
        .data
        or []
    )
    return {"messages": rows}
