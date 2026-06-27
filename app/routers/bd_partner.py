"""BD Partner — backend for the $500/mo white-glove service.

This is the same class of fix as Masterclass's sales-page-vs-real-tool
split: a signed-in client clicking "BD Partner" in their sidebar shouldn't
land back on the page that sold them the service. Unlike Masterclass,
there was no backend state to switch to — BD Partner has always been pure
marketing copy paid for through a raw out-of-band Stripe Payment Link, so
this router exists to give an active client an actual log of the work
being done for them (alerts surfaced, bids reviewed, proposals drafted,
calls, milestones), not just a "thanks for paying" screen.

Client status and activity entries are written by hand via the
admin-secret-gated endpoints below, mirroring admin.py's pattern exactly
(this is a one-person white-glove service, not something that needs a
full admin-role system yet).
"""
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from app.config import settings
from app.database import get_supabase, single_data

router = APIRouter(prefix="/bd-partner", tags=["bd-partner"])

ACTIVITY_TYPES = {"alert", "review", "draft", "call", "note", "milestone"}


def _check_admin_secret(x_admin_secret: str | None):
    if not settings.admin_secret:
        raise HTTPException(status_code=503, detail="Admin tools not configured")
    if not x_admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


@router.get("/status")
async def get_status(user_id: str):
    sb = get_supabase()
    client = single_data(
        sb.table("bd_partner_clients").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    return {"active": bool(client and client["status"] == "active"), "client": client}


@router.get("/activity")
async def get_activity(user_id: str, limit: int = 100):
    sb = get_supabase()
    rows = (
        sb.table("bd_partner_activity")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    return {"activity": rows}


class UpsertClientRequest(BaseModel):
    user_id: str
    status: str = "active"
    plan_note: str = ""


@router.post("/clients")
async def upsert_client(body: UpsertClientRequest, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    if body.status not in ("active", "paused", "ended"):
        raise HTTPException(status_code=400, detail=f"Unknown status: {body.status}")

    sb = get_supabase()
    row = {
        "user_id": body.user_id,
        "status": body.status,
        "plan_note": body.plan_note or None,
        "updated_at": "now()",
    }
    result = sb.table("bd_partner_clients").upsert(row).execute()
    return {"client": result.data}


class LogActivityRequest(BaseModel):
    user_id: str
    type: str
    title: str
    detail: str = ""


@router.post("/activity")
async def log_activity(body: LogActivityRequest, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    if body.type not in ACTIVITY_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {sorted(ACTIVITY_TYPES)}")
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")

    sb = get_supabase()
    row = {
        "user_id": body.user_id,
        "type": body.type,
        "title": body.title.strip(),
        "detail": body.detail.strip() or None,
    }
    created = single_data(sb.table("bd_partner_activity").insert(row).execute())
    return created
