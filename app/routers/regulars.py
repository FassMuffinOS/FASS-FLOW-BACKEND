"""Regulars — standalone Wallet/loyalty product for non-govcon local
businesses (coffee shops, salons, gyms, retail — anywhere a paper punch
card or gift certificate already exists). Spun out of the GovCon
platform's existing Wallet system (wallet.py, rewards.py, gift_cards.py,
wallet_campaigns.py, comms.py), which has zero govcon dependency and is
reused here completely unchanged.

Flow: POST /regulars/signup is a single-step "pick a plan, get an account"
form — provisions a real Supabase Auth account via the Admin API
(pre-confirmed, no email-verification wait, same pattern as
affiliates.py's /apply), flags profiles.is_wallet_only so the app shell
renders the stripped Regulars-only chrome instead of the full GovCon
product, creates a business_profiles row, and starts a Stripe Checkout
session for the chosen plan/interval. The webhook branch that actually
activates the subscription lives in subscriptions.py
(kind="regulars_subscription") — same shared-webhook pattern every other
product on this platform uses.
"""
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.database import get_supabase, single_data
from app.routers.subscriptions import REGULARS_PLAN_PRICE_MAP, REGULARS_ANNUAL_PLAN_PRICE_MAP

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/regulars", tags=["regulars"])


@router.get("/prices")
async def list_prices():
    """Public catalog read — live from Stripe, not hardcoded, so a price
    change in Stripe shows up on the signup page with no frontend deploy.
    Returns amount_cents per plan/interval; the frontend computes the
    'save 17%' framing the same way Pricing.jsx does for GovCon plans."""
    out = {}
    for plan, price_id in REGULARS_PLAN_PRICE_MAP.items():
        out.setdefault(plan, {})
        if price_id:
            try:
                price = stripe.Price.retrieve(price_id)
                out[plan]["monthly"] = {"price_id": price_id, "amount_cents": price.get("unit_amount")}
            except Exception:
                pass
    for plan, price_id in REGULARS_ANNUAL_PLAN_PRICE_MAP.items():
        out.setdefault(plan, {})
        if price_id:
            try:
                price = stripe.Price.retrieve(price_id)
                out[plan]["annual"] = {"price_id": price_id, "amount_cents": price.get("unit_amount")}
            except Exception:
                pass
    return {"plans": out}


class SignupRequest(BaseModel):
    email: str
    password: str
    business_name: str
    plan: str  # "starter" | "pro"
    billing_interval: str = "monthly"  # "monthly" | "annual"


@router.post("/signup")
async def signup(body: SignupRequest):
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not body.business_name.strip():
        raise HTTPException(status_code=400, detail="Business name is required")
    if body.plan not in REGULARS_PLAN_PRICE_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")

    price_map = REGULARS_ANNUAL_PLAN_PRICE_MAP if body.billing_interval == "annual" else REGULARS_PLAN_PRICE_MAP
    price_id = price_map.get(body.plan)
    if not price_id:
        raise HTTPException(status_code=503, detail="Regulars isn't configured yet — missing a Stripe price id")

    sb = get_supabase()
    try:
        created = sb.auth.admin.create_user({
            "email": email,
            "password": body.password,
            "email_confirm": True,
            "user_metadata": {"business_name": body.business_name, "is_wallet_only": True},
        })
    except Exception as exc:
        msg = str(exc)
        if "already" in msg.lower() or "registered" in msg.lower() or "exists" in msg.lower():
            raise HTTPException(status_code=409, detail="An account with that email already exists — sign in instead")
        raise HTTPException(status_code=400, detail=f"Could not create account: {msg}")

    user = created.user
    if not user:
        raise HTTPException(status_code=502, detail="Supabase did not return a user")
    user_id = user.id

    sb.table("profiles").upsert({
        "id": user_id,
        "full_name": body.business_name,
        "is_wallet_only": True,
    }).execute()
    sb.table("business_profiles").upsert({
        "user_id": user_id,
        "business_name": body.business_name,
    }).execute()

    # Sign them in immediately so the frontend gets a real session before
    # redirecting to Stripe — same pattern as affiliates.py's /apply.
    session = None
    try:
        signed_in = sb.auth.sign_in_with_password({"email": email, "password": body.password})
        if signed_in.session:
            session = {
                "access_token": signed_in.session.access_token,
                "refresh_token": signed_in.session.refresh_token,
            }
    except Exception:
        pass  # account still created; frontend can fall back to /signin before checkout

    checkout = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email,
        metadata={
            "kind": "regulars_subscription",
            "user_id": user_id,
            "plan": body.plan,
            "billing_interval": body.billing_interval,
        },
        success_url=f"{settings.frontend_url}/regulars/dashboard?checkout=success",
        cancel_url=f"{settings.frontend_url}/regulars/pricing?checkout=cancelled",
        trial_period_days=14,
    )

    return {"user_id": user_id, "session": session, "checkout_url": checkout.url}


@router.get("/status")
async def regulars_status(user_id: str = Query(...)):
    """Lightweight poll for the post-checkout redirect — same idea as
    gift_cards.py's /purchase/status — so the dashboard can show 'activating
    your subscription...' until the webhook lands instead of a confusing
    blank state."""
    sb = get_supabase()
    profile = single_data(
        sb.table("profiles")
        .select("wallet_plan, wallet_subscription_status, wallet_billing_interval")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Account not found")
    return {
        "active": profile.get("wallet_subscription_status") == "active",
        "plan": profile.get("wallet_plan"),
        "billing_interval": profile.get("wallet_billing_interval"),
    }
