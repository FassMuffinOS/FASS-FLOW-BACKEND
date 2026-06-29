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
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.config import settings
from app.database import get_supabase, single_data

router = APIRouter(prefix="/bd-partner", tags=["bd-partner"])

ACTIVITY_TYPES = {"alert", "review", "draft", "call", "note", "milestone"}


def _check_admin_secret(x_admin_secret: str | None):
    if not settings.admin_secret:
        raise HTTPException(status_code=503, detail="Admin tools not configured")
    if not x_admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


@router.get("/clients")
async def list_clients(x_admin_secret: str = Header(None)):
    """Founder-only roster — every bd_partner_clients row, newest first,
    joined against profiles for a display name/company so the admin
    console doesn't just show raw UUIDs. Mirrors the rest of this file's
    admin-secret pattern; nothing here is reachable by a regular client."""
    _check_admin_secret(x_admin_secret)
    sb = get_supabase()
    clients = (
        sb.table("bd_partner_clients")
        .select("*")
        .order("started_at", desc=True)
        .execute()
        .data
        or []
    )
    if not clients:
        return {"clients": []}

    user_ids = [c["user_id"] for c in clients]
    profiles = (
        sb.table("profiles")
        .select("id, full_name, company_name")
        .in_("id", user_ids)
        .execute()
        .data
        or []
    )
    profiles_by_id = {p["id"]: p for p in profiles}
    for c in clients:
        p = profiles_by_id.get(c["user_id"])
        c["full_name"] = p.get("full_name") if p else None
        c["company_name"] = p.get("company_name") if p else None
    return {"clients": clients}


@router.get("/status")
async def get_status(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    # 2026-06-29 security fix: previously had no auth check at all — not
    # even the admin shared-secret used elsewhere in this file — so anyone
    # could read whether any user_id was an active BD Partner client.
    require_owner(current_user, user_id, detail="You can only view your own BD Partner status")
    sb = get_supabase()
    client = single_data(
        sb.table("bd_partner_clients").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    return {"active": bool(client and client["status"] == "active"), "client": client}


@router.get("/activity")
async def get_activity(user_id: str, limit: int = 100, current_user: CurrentUser = Depends(get_current_user)):
    # 2026-06-29 security fix: previously had no auth check — anyone could
    # read another client's private BD Partner activity log (alerts,
    # proposal drafts, call notes) by supplying their user_id.
    require_owner(current_user, user_id, detail="You can only view your own BD Partner activity")
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
