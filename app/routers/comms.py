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

2026-06-29 security fix: this previously had no auth dependency anywhere —
caller-supplied business_user_id was taken at face value, so anyone could
read or send SMS as any business. User-scoped endpoints (/send, /threads,
/contact, /thread, /contact/dismiss-nudge) now require the caller to be
logged in as that business_user_id. /outbox and /outbox/{id}/ack are the
Mac relay's internal polling endpoints (no specific business context —
they see the global send queue), so they're gated behind the same
shared-secret pattern admin.py uses instead of a user session. /twilio/inbound
stays open with no auth — it's Twilio's own webhook target and Twilio
doesn't send a bearer token.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.config import settings
from app.database import get_supabase, single_data
from app.services.twilio_sms import send_sms

router = APIRouter(prefix="/comms", tags=["comms"])


def _check_relay_secret(x_admin_secret: str | None):
    """Same fail-closed pattern as admin.py: if ADMIN_SECRET isn't set in
    this environment, the relay endpoints are disabled rather than open."""
    if not settings.admin_secret or not x_admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=403, detail="Not authorized")

NUDGE_QUIET_DAYS = 30


class SendRequest(BaseModel):
    business_user_id: str
    phone: str
    body: str
    card_slug: str | None = None
    channel: str = "sms"


@router.post("/send")
async def send_message(body: SendRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.business_user_id, detail="You can only send messages for your own business")
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
async def get_outbox(limit: int = 25, x_admin_secret: str | None = Header(None)):
    """Polled by the Mac relay. Returns the oldest queued messages first so
    nothing waits forever behind a burst of newer sends.

    2026-06-29 security fix: this returns ALL businesses' queued outbound
    messages with zero scoping — there's no per-business filter here by
    design (the relay polls one global queue). Previously had no auth at
    all; now gated behind the same shared admin-secret header admin.py
    uses, since the caller is the relay script, not a logged-in user."""
    _check_relay_secret(x_admin_secret)
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
async def ack_outbox(message_id: str, body: AckRequest, x_admin_secret: str | None = Header(None)):
    _check_relay_secret(x_admin_secret)
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
async def list_threads(business_user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    """One row per phone number, most recently active first — the contact
    list view. Pulled in one query and folded down in Python since Supabase
    doesn't have a clean "latest row per group" without a view/RPC, and
    standing up a SQL view here would be the first one in this codebase.

    Also folds in the Nudge signal (days since last activity, either
    direction, vs NUDGE_QUIET_DAYS) and the saved Contact Identity, if any —
    both computed/joined here rather than in the frontend so a relationship
    that's gone quiet but already covered (awarded, declined, off-cycle) can
    be dismissed server-side via nudge_dismissed_until and stay dismissed
    across devices."""
    require_owner(current_user, business_user_id, detail="You can only view your own message threads")
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

    contacts_by_phone = {
        c["phone"]: c
        for c in (
            sb.table("comms_contacts")
            .select("*")
            .eq("business_user_id", business_user_id)
            .execute()
            .data
            or []
        )
    }

    now = datetime.now(timezone.utc)
    result = []
    for t in threads.values():
        contact = contacts_by_phone.get(t["phone"])
        last_at = datetime.fromisoformat(t["last_at"].replace("Z", "+00:00"))
        days_quiet = (now - last_at).days
        dismissed_until = contact.get("nudge_dismissed_until") if contact else None
        nudge_dismissed = bool(
            dismissed_until and datetime.fromisoformat(dismissed_until.replace("Z", "+00:00")) > now
        )
        t["days_quiet"] = days_quiet
        t["show_nudge"] = days_quiet >= NUDGE_QUIET_DAYS and not nudge_dismissed
        t["contact_name"] = contact.get("name") if contact else None
        t["contact_company"] = contact.get("company") if contact else None
        result.append(t)
    return {"threads": result}


class ContactRequest(BaseModel):
    business_user_id: str
    phone: str
    name: str | None = None
    company: str | None = None
    naics: str | None = None
    last_award_date: str | None = None
    notes: str | None = None


@router.get("/contact")
async def get_contact(business_user_id: str, phone: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, business_user_id, detail="You can only view your own contacts")
    sb = get_supabase()
    contact = single_data(
        sb.table("comms_contacts")
        .select("*")
        .eq("business_user_id", business_user_id)
        .eq("phone", phone)
        .maybe_single()
        .execute()
    )
    return {"contact": contact}


@router.put("/contact")
async def upsert_contact(body: ContactRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.business_user_id, detail="You can only edit your own contacts")
    sb = get_supabase()
    row = {
        "business_user_id": body.business_user_id,
        "phone": body.phone,
        "name": body.name or None,
        "company": body.company or None,
        "naics": body.naics or None,
        "last_award_date": body.last_award_date or None,
        "notes": body.notes or None,
        "updated_at": "now()",
    }
    result = sb.table("comms_contacts").upsert(row).execute()
    return {"contact": single_data(result)}


class DismissNudgeRequest(BaseModel):
    business_user_id: str
    phone: str
    days: int = 14


@router.post("/contact/dismiss-nudge")
async def dismiss_nudge(body: DismissNudgeRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Soft-snooze a quiet-contact nudge — used when the relationship is
    intentionally dormant (already awarded, declined, off-cycle) so it stops
    resurfacing without requiring you to message someone just to silence it."""
    require_owner(current_user, body.business_user_id, detail="You can only manage your own contacts")
    sb = get_supabase()
    from datetime import timedelta

    until = (datetime.now(timezone.utc) + timedelta(days=body.days)).isoformat()
    row = {
        "business_user_id": body.business_user_id,
        "phone": body.phone,
        "nudge_dismissed_until": until,
        "updated_at": "now()",
    }
    sb.table("comms_contacts").upsert(row).execute()
    return {"dismissed_until": until}


@router.get("/thread")
async def get_thread(business_user_id: str, phone: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, business_user_id, detail="You can only view your own message threads")
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
