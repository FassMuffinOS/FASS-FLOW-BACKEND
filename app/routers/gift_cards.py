"""FASS Gift Cards — prepaid dollar-balance Apple Wallet storeCard passes.

Flow: business issues a gift card for a $ value -> POST /giftcards/issue
creates the row and returns a slug/claim link (same "share a link, customer
adds it to Wallet" pattern as Rewards' join flow) -> customer adds the
storeCard pass, which shows their remaining balance -> customer shows the
card in-store -> staff scans its QR with their phone's normal camera app
(no in-app scanner, same approach as Wallet Messaging's redemption) ->
lands on the frontend's redeem-confirm page, which calls GET
/giftcards/lookup then POST /giftcards/redeem for however much of the
balance is being applied -> the card's balance is decremented and
notify_devices() pushes the new balance straight to the customer's Wallet,
no re-download needed -> GET /giftcards/mine shows the business every card
they've issued with running balances and redemption history.

This ships redemption as QR-scan only. True NFC-tap redemption requires a
separate, selective Apple entitlement application (2-4+ week approval) plus
NFC reader hardware certified for Apple's VAS protocol — see the docstring
in migrations/gift_cards.sql. The balance/ledger model here doesn't change
if that's added later; only the redemption trigger would.

ALSO supports a public, no-login storefront purchase path so a customer can
buy a card for themselves or someone else without the business having to
issue it by hand: POST /purchase/checkout (public) starts a one-time Stripe
Checkout session for whatever $ amount the customer picked (mirrors
wallet.py's mode="payment" unlock pattern, NOT subscriptions.py's price-ID
subscription pattern, since the amount here is customer-chosen, not a fixed
plan). The slug is generated up front and carried through Stripe metadata so
the webhook (subscriptions.py's shared handler, kind="gift_card") can create
the actual gift_cards row only once payment is confirmed — no card exists
in the database before that webhook fires. GET /purchase/status lets the
post-checkout confirmation page poll for that row to appear, same idea as
wallet.py's /purchase-status/{slug}.

If the business has completed Stripe Connect onboarding (stripe_connect.py),
/purchase/checkout routes the payment directly to their own connected
account via a destination charge instead of FASS Flow's master account,
and FASS Flow takes settings.gift_card_platform_fee_pct (default 5%) off
the top via Stripe's application_fee_amount — Stripe splits it
automatically, the fee stays in the platform account and the rest lands
with the business. Businesses who haven't onboarded keep working exactly
as before (full payment in the platform account, no fee since there's
nothing to split yet).
"""
import re
import uuid

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.config import settings
from app.database import get_supabase, single_data
from app.services.apns import notify_devices
from app.services.applewallet import apple_wallet_configured, generate_giftcard_pkpass

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/giftcards", tags=["gift-cards"])


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "giftcard"
    return f"gift-{base}-{uuid.uuid4().hex[:6]}"


def _business_name(sb, business_user_id: str) -> str:
    profile = single_data(
        sb.table("business_profiles").select("business_name").eq("user_id", business_user_id).maybe_single().execute()
    )
    return (profile or {}).get("business_name") or "Your Business"


class IssueGiftCardRequest(BaseModel):
    business_user_id: str
    value: float
    customer_name: str | None = None
    customer_contact: str | None = None


@router.post("/issue")
async def issue_gift_card(body: IssueGiftCardRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.business_user_id, detail="You can only issue gift cards for your own business")
    if body.value <= 0:
        raise HTTPException(status_code=400, detail="Gift card value must be greater than $0")

    sb = get_supabase()
    slug = _slugify(_business_name(sb, body.business_user_id))
    row = {
        "slug": slug,
        "business_user_id": body.business_user_id,
        "customer_name": body.customer_name,
        "customer_contact": body.customer_contact,
        "original_value": body.value,
        "balance": body.value,
    }
    sb.table("gift_cards").insert(row).execute()
    return {"slug": slug}


@router.get("/mine")
async def list_my_gift_cards(user_id: str = Query(..., min_length=1), current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own gift card program")
    sb = get_supabase()
    cards = (
        sb.table("gift_cards")
        .select("*")
        .eq("business_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    outstanding_balance = sum(c.get("balance") or 0 for c in cards if c.get("active", True))
    total_issued = sum(c.get("original_value") or 0 for c in cards)

    return {"cards": cards, "outstanding_balance": outstanding_balance, "total_issued": total_issued}


@router.get("/pass")
async def get_gift_card_pass(slug: str = Query(..., min_length=1)):
    if not apple_wallet_configured():
        raise HTTPException(status_code=503, detail="Apple Wallet not configured")

    sb = get_supabase()
    card = single_data(sb.table("gift_cards").select("*").eq("slug", slug).maybe_single().execute())
    if not card:
        raise HTTPException(status_code=404, detail="No gift card found for that link")

    business_name = _business_name(sb, card["business_user_id"])
    barcode_url = f"https://flow.fass.systems/giftcards/scan/{slug}"

    try:
        pkpass_bytes = generate_giftcard_pkpass(
            business_name=business_name,
            balance=card["balance"],
            original_value=card["original_value"],
            barcode_url=barcode_url,
            serial_number=slug,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    safe_name = "".join(c for c in business_name if c.isalnum() or c in " -_").strip().replace(" ", "-") or "fass-giftcard"

    from fastapi.responses import Response
    return Response(
        content=pkpass_bytes,
        media_type="application/vnd.apple.pkpass",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-giftcard.pkpass"'},
    )


@router.get("/lookup")
async def lookup_gift_card(
    slug: str = Query(..., min_length=1),
    business_user_id: str = Query(..., min_length=1),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Backs the staff redemption confirm page — shown after scanning a
    customer's gift card QR, before any amount is actually redeemed. Staff
    must be logged in as the business that issued the card; this is a
    balance-revealing lookup, not public storefront data."""
    require_owner(current_user, business_user_id, detail="This gift card belongs to a different business")
    sb = get_supabase()
    card = single_data(
        sb.table("gift_cards")
        .select("slug, customer_name, balance, original_value, active, business_user_id")
        .eq("slug", slug)
        .maybe_single()
        .execute()
    )
    if not card:
        raise HTTPException(status_code=404, detail="No gift card found for that link")
    if card["business_user_id"] != business_user_id:
        raise HTTPException(status_code=403, detail="This gift card belongs to a different business")

    return {
        "slug": card["slug"],
        "customer_name": card.get("customer_name"),
        "balance": card["balance"],
        "original_value": card["original_value"],
        "active": card.get("active", True),
    }


class RedeemGiftCardRequest(BaseModel):
    slug: str
    business_user_id: str
    amount: float


@router.post("/redeem")
async def redeem_gift_card(body: RedeemGiftCardRequest, current_user: CurrentUser = Depends(get_current_user)):
    # 2026-06-29 security fix: this previously trusted body.business_user_id
    # with no login check at all — anyone who knew a card's slug + the
    # business's user_id (itself exposed by the public /business lookup)
    # could drain its balance remotely. Now the caller must be logged in
    # as the business that owns the card.
    require_owner(current_user, body.business_user_id, detail="This gift card belongs to a different business")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Redemption amount must be greater than $0")

    sb = get_supabase()
    card = single_data(
        sb.table("gift_cards")
        .select("slug, balance, business_user_id, active")
        .eq("slug", body.slug)
        .maybe_single()
        .execute()
    )
    if not card:
        raise HTTPException(status_code=404, detail="No gift card found for that link")
    if card["business_user_id"] != body.business_user_id:
        raise HTTPException(status_code=403, detail="This gift card belongs to a different business")
    if not card.get("active", True):
        raise HTTPException(status_code=400, detail="This gift card has been deactivated")
    if body.amount > card["balance"]:
        raise HTTPException(status_code=400, detail=f"Amount exceeds remaining balance (${card['balance']:.2f})")

    new_balance = round(card["balance"] - body.amount, 2)
    sb.table("gift_cards").update({
        "balance": new_balance,
        "active": new_balance > 0,
    }).eq("slug", body.slug).execute()

    sb.table("gift_card_redemptions").insert({
        "gift_card_slug": body.slug,
        "business_user_id": body.business_user_id,
        "amount": body.amount,
        "balance_after": new_balance,
    }).execute()

    notify_devices(sb, body.slug)

    return {"slug": body.slug, "amount": body.amount, "balance": new_balance}


@router.get("/history")
async def gift_card_history(
    slug: str = Query(..., min_length=1),
    business_user_id: str = Query(..., min_length=1),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Per-card transaction log for the dashboard — every partial/full
    redemption against this card, newest first. Ownership-checked the same
    way /lookup is, since this exposes how a specific customer has been
    using their balance."""
    require_owner(current_user, business_user_id, detail="This gift card belongs to a different business")
    sb = get_supabase()
    card = single_data(
        sb.table("gift_cards").select("slug, business_user_id").eq("slug", slug).maybe_single().execute()
    )
    if not card:
        raise HTTPException(status_code=404, detail="No gift card found for that link")
    if card["business_user_id"] != business_user_id:
        raise HTTPException(status_code=403, detail="This gift card belongs to a different business")

    rows = (
        sb.table("gift_card_redemptions")
        .select("amount, balance_after, redeemed_at")
        .eq("gift_card_slug", slug)
        .order("redeemed_at", desc=True)
        .execute()
    ).data or []
    return {"history": rows}


# --- Public storefront purchase flow (no login required) ---------------

@router.get("/business")
async def public_business_name(business_user_id: str = Query(..., min_length=1)):
    """Backs the public storefront page's header — just the display name,
    nothing else, so a customer knows who they're buying a card for before
    they hand over payment info."""
    sb = get_supabase()
    return {"business_name": _business_name(sb, business_user_id)}


class CreateGiftCardCheckoutRequest(BaseModel):
    business_user_id: str
    value: float
    customer_email: str
    customer_name: str | None = None


@router.post("/purchase/checkout")
async def create_gift_card_checkout(body: CreateGiftCardCheckoutRequest):
    if body.value < 1:
        raise HTTPException(status_code=400, detail="Gift card value must be at least $1")
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payments are not configured yet")

    sb = get_supabase()
    business_name = _business_name(sb, body.business_user_id)
    # Generated up front and never inserted until the webhook confirms
    # payment — there is no gift_cards row for this slug yet, so a customer
    # who abandons checkout never ends up with a "free" balance.
    slug = _slugify(business_name)

    session_kwargs = dict(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": round(body.value * 100),
                "product_data": {"name": f"{business_name} Gift Card"},
            },
            "quantity": 1,
        }],
        customer_email=body.customer_email,
        metadata={
            "kind": "gift_card",
            "slug": slug,
            "business_user_id": body.business_user_id,
            "value": str(body.value),
            "customer_name": body.customer_name or "",
            "customer_contact": body.customer_email,
        },
        success_url=f"{settings.frontend_url}/giftcards/buy/{body.business_user_id}?success=1&slug={slug}",
        cancel_url=f"{settings.frontend_url}/giftcards/buy/{body.business_user_id}?cancelled=1",
    )

    # If this business has finished Stripe Connect onboarding (see
    # stripe_connect.py), send the payment straight to their own connected
    # account instead of FASS Flow's master account — a "destination
    # charge" — and take FASS Flow's cut off the top via
    # application_fee_amount, which Stripe splits automatically: the fee
    # stays in the platform account, the remainder lands with the business.
    # Businesses who haven't onboarded yet keep working exactly as before,
    # with the full payment landing in the platform account until they do
    # (nothing to split there yet, so no fee applies on that path).
    profile = single_data(
        sb.table("business_profiles")
        .select("stripe_connect_account_id, connect_payouts_enabled")
        .eq("user_id", body.business_user_id)
        .maybe_single()
        .execute()
    ) or {}
    if profile.get("connect_payouts_enabled") and profile.get("stripe_connect_account_id"):
        fee_cents = round(body.value * 100 * (settings.gift_card_platform_fee_pct / 100))
        session_kwargs["payment_intent_data"] = {
            "transfer_data": {"destination": profile["stripe_connect_account_id"]},
            "application_fee_amount": fee_cents,
        }

    session = stripe.checkout.Session.create(**session_kwargs)
    return {"url": session.url, "slug": slug}


@router.get("/purchase/status")
async def gift_card_purchase_status(slug: str = Query(..., min_length=1)):
    """Polled by the post-checkout confirmation page — the gift_cards row
    for this slug only exists once Stripe's webhook has actually landed, so
    'not found yet' just means the webhook hasn't processed the payment
    yet, not that anything went wrong."""
    sb = get_supabase()
    card = single_data(
        sb.table("gift_cards").select("slug, balance, original_value").eq("slug", slug).maybe_single().execute()
    )
    if not card:
        return {"found": False}
    return {"found": True, "slug": card["slug"], "balance": card["balance"], "original_value": card["original_value"]}
