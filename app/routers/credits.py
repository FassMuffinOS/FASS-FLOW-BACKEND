"""AI credits — metering + balance for the paid AI drafting.

Every AI proposal draft costs 1 credit (consumed inside ai.py's
/draft-section). New users get a one-time free starter allotment; refills
come from two paths now: an admin grant (/grant, still used for support
corrections/comps) and a real Stripe purchase (/packs + /checkout, granted
by subscriptions.py's webhook on checkout.session.completed). The ledger
records every change either way, so balances stay fully auditable.

consume_credits() is the shared entry point other routers call.
"""
import stripe
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.config import settings
from app.database import get_supabase, single_data

stripe.api_key = settings.stripe_secret_key

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


def grant_purchased_credits(user_id: str, amount: int, external_ref: str, reason: str = "stripe_purchase") -> int | None:
    """Called from subscriptions.py's webhook on checkout.session.completed
    for a metadata.kind == "ai_credits" session. Stripe's webhook delivery
    is at-least-once, so external_ref (the Checkout Session id) is what
    keeps a redelivered event from granting the same pack twice — the
    ledger insert is wrapped against ai_credit_ledger's partial unique index
    on external_ref, same dedupe pattern affiliates.record_conversion() uses
    for invoice.payment_succeeded. Returns the new balance, or None if this
    was a duplicate delivery (no-op, not an error)."""
    sb = get_supabase()
    row = _get_or_create(sb, user_id)
    new_bal = (row.get("balance", 0) or 0) + amount
    try:
        sb.table("ai_credit_ledger").insert({
            "user_id": user_id, "delta": amount, "reason": reason,
            "balance_after": new_bal, "external_ref": external_ref,
        }).execute()
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            return None
        raise
    sb.table("ai_credits").update({"balance": new_bal}).eq("user_id", user_id).execute()
    return new_bal


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


@router.get("/packs")
async def list_packs():
    """The purchasable credit-pack catalog, read live from Stripe rather
    than hardcoded — adding/repricing a pack in Stripe shows up here with no
    deploy. Each pack is a one-time price on the AI Credits product, tagged
    metadata.credits=<n>. Public (no auth) so the picker can render for a
    signed-out visitor previewing pricing; the actual purchase still
    requires a session via /checkout below."""
    if not settings.stripe_product_ai_credits:
        return {"packs": []}
    prices = stripe.Price.list(product=settings.stripe_product_ai_credits, active=True, limit=100)
    packs = []
    for p in prices.data:
        credits = (p.get("metadata") or {}).get("credits")
        if not credits:
            continue  # not a pack price (shouldn't happen on this product, but don't trust blindly)
        packs.append({
            "price_id": p["id"],
            "amount_cents": p["unit_amount"],
            "amount_display": f"${p['unit_amount'] / 100:.2f}",
            "credits": int(credits),
        })
    packs.sort(key=lambda x: x["amount_cents"])
    return {"packs": packs}


class CreditCheckoutRequest(BaseModel):
    price_id: str
    user_id: str
    email: str


@router.post("/checkout")
async def create_credit_checkout(body: CreditCheckoutRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.user_id, detail="You can only buy credits for your own account")
    if not settings.stripe_product_ai_credits:
        raise HTTPException(status_code=503, detail="Credit packs are not configured yet")

    try:
        price = stripe.Price.retrieve(body.price_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Unknown price")
    # Pin the checkout to OUR catalog — never trust a client-supplied price
    # id blindly, or a forged request could check out against an unrelated
    # Stripe price (wrong amount, wrong product) using this endpoint's auth.
    if price.get("product") != settings.stripe_product_ai_credits or not price.get("active"):
        raise HTTPException(status_code=400, detail="That price isn't part of the AI credits catalog")
    credits = (price.get("metadata") or {}).get("credits")
    if not credits:
        raise HTTPException(status_code=400, detail="That price is missing its credits amount")

    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{"price": body.price_id, "quantity": 1}],
        customer_email=body.email,
        metadata={"kind": "ai_credits", "user_id": body.user_id, "credits": credits},
        success_url=f"{settings.frontend_url}/settings?credits=success",
        cancel_url=f"{settings.frontend_url}/settings?credits=cancelled",
    )
    return {"url": session.url}
