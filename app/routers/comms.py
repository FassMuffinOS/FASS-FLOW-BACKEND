"""Comms Hub — outbound/inbound message queue for the self-hosted
iMessage/SMS relay ("our own Sendblue").

This backend never talks to Apple or a carrier directly. It only does two
things: hold a queue of outbound messages, and accept inbound ones. The
actual sending happens on a Mac running Messages.app, automated via
AppleScript (see mac-relay/ at the repo root) — that script polls
GET /comms/outbox, sends each message, and reports back via
POST /comms/outbox/{id}/ack. Inbound replies are picked up by a second
relay script watching Messages.app's local chat database and posted to
POST /comms/inbound.

No background workers exist anywhere in this codebase (see other routers —
strictly request/response FastAPI, no Celery/APScheduler), so polling is the
only option here, not a deliberate choice. The Mac relay is expected to poll
every few seconds.

Ownership model matches every other router here: no auth dependency, the
caller's business_user_id is taken at face value and the service-role
client is used for every read/write — this is a backend-trusted contract
between the relay script (which you control) and the API, not a
public-facing endpoint set.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_supabase, single_data

router = APIRouter(prefix="/comms", tags=["comms"])


class SendRequest(BaseModel):
    business_user_id: str
    phone: str
    body: str
    card_slug: str | None = None
    channel: str = "imessage"


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
        "channel": body.channel,
        "direction": "out",
        "status": "queued",
    }
    created = single_data(sb.table("comms_messages").insert(row).execute())
    return created


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


class InboundRequest(BaseModel):
    business_user_id: str
    phone: str
    body: str
    card_slug: str | None = None
    channel: str = "imessage"
    external_id: str | None = None


@router.post("/inbound")
async def receive_inbound(body: InboundRequest):
    """Posted by the relay's reply-watcher script. business_user_id has to
    be supplied by the relay (it knows which Mac/Apple ID — and therefore
    which business — the message landed on); this endpoint doesn't try to
    guess ownership from the phone number alone since one phone could in
    theory message more than one connected business."""
    sb = get_supabase()
    row = {
        "business_user_id": body.business_user_id,
        "phone": body.phone.strip(),
        "body": body.body.strip(),
        "card_slug": body.card_slug,
        "channel": body.channel,
        "direction": "in",
        "status": "received",
        "external_id": body.external_id,
    }
    try:
        created = single_data(sb.table("comms_messages").insert(row).execute())
    except Exception as exc:
        # external_id has a unique index — a relay re-poll re-sending the
        # same reply should be a silent no-op, not a 500.
        if "duplicate key" in str(exc).lower():
            return {"status": "duplicate"}
        raise
    return created


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
