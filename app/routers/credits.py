"""AI credits — metering + balance for the paid AI drafting.

Every AI proposal draft costs 1 credit (consumed inside ai.py's
/draft-section). New users get a one-time free starter allotment; refills
are granted by an admin (honor-system for beta — pay via the Support page's
Cash App/Venmo, email us, we top you up). The ledger records every change,
so swapping in Stripe auto-purchase later is just another grant row.

consume_credits() is the shared entry point other routers call.
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.config import settings
from app.database import get_supabase, single_data

router = APIRouter(prefix="/credits", tags=["credits"])

FREE_STARTER = 25  # credits granted once on first use


def _get_or_create(sb, user_id: str) -> dict:
    """Fetch the user's credit row, creating it with the free starter grant
    on first touch (idempotent via free_granted)."""
    row = single_data(
        sb.table("ai_credits").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    if row:
        return row
    created = single_data(
        sb.table("ai_credits").insert({"user_id": user_id, "balance": FREE_STARTER, "free_granted": True}).execute()
    )
    sb.table("ai_credit_ledger").insert({
        "user_id": user_id, "delta": FREE_STARTER, "reason": "free_starter", "balance_after": FREE_STARTER,
    }).execute()
    return created


def consume_credits(user_id: str, n: int = 1, reason: str = "ai_draft") -> tuple[bool, int]:
    """Decrement n credits if the balance covers it. Returns (ok, balance).
    ok=False means insufficient — caller should 402. Records the ledger."""
    sb = get_supabase()
    row = _get_or_create(sb, user_id)
    bal = row.get("balance", 0) or 0
    if bal < n:
        return False, bal
    new_bal = bal - n
    sb.table("ai_credits").update({"balance": new_bal}).eq("user_id", user_id).execute()
    sb.table("ai_credit_ledger").insert({
        "user_id": user_id, "delta": -n, "reason": reason, "balance_after": new_bal,
    }).execute()
    return True, new_bal


@router.get("/balance")
async def get_balance(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own credits")
    sb = get_supabase()
    row = _get_or_create(sb, user_id)
    return {"balance": row.get("balance", 0) or 0}


class GrantRequest(BaseModel):
    user_id: str
    amount: int
    reason: str = "refill"


@router.post("/grant")
async def grant_credits(req: GrantRequest, x_admin_secret: str | None = Header(default=None)):
    """Admin-secret refill (beta honor-system). Same shared-secret pattern as
    admin.py / feed.py. Negative amounts allowed for corrections."""
    if not settings.admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    sb = get_supabase()
    row = _get_or_create(sb, req.user_id)
    new_bal = max(0, (row.get("balance", 0) or 0) + req.amount)
    sb.table("ai_credits").update({"balance": new_bal}).eq("user_id", req.user_id).execute()
    sb.table("ai_credit_ledger").insert({
        "user_id": req.user_id, "delta": req.amount, "reason": req.reason, "balance_after": new_bal,
    }).execute()
    return {"balance": new_bal}
