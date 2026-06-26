"""Stripe Connect onboarding — the foundation for letting each business on
FASS Flow receive and cash out its own money, instead of every payment
(today: gift cards) pooling into the platform's single master Stripe
account. This router only handles getting a business linked and verified
with Stripe (an Express account, Stripe's fastest-to-onboard Connect type).
It does NOT yet change where any existing checkout's money goes — that's a
deliberate follow-up once onboarding itself is proven out, since rewiring
gift_cards.py's checkout to use destination charges is a one-line change
once a business has a connect_account_id on file, but is easy to get wrong
in a way that misroutes real money if rushed.

Flow:
  1. Business clicks "Set up payouts" -> POST /connect/start
     - Creates a Stripe Express account for them (once; reused after)
     - Creates a Stripe-hosted "Account Link" (onboarding form) and
       returns its URL for the frontend to redirect to.
  2. Business fills out Stripe's hosted form (identity, bank account).
     Stripe redirects back to our frontend_url regardless of outcome.
  3. The SAME shared webhook endpoint (subscriptions.py's /subscriptions
     /webhook) now also listens for `account.updated` events and flips
     connect_onboarded/connect_payouts_enabled based on the account's
     actual charges_enabled/details_submitted flags reported by Stripe —
     never trust the redirect alone, since the user can close the tab
     mid-flow or Stripe can flag the account for additional review after
     the redirect already happened.
  4. GET /connect/status lets the frontend show current state and, if
     onboarding was abandoned partway, POST /connect/start again returns
     a fresh Account Link to resume rather than creating a duplicate
     Stripe account (Stripe account creation here is itself idempotent
     per business via the stored stripe_connect_account_id).
"""
import stripe
from fastapi import APIRouter, HTTPException, Query
from app.config import settings
from app.database import get_supabase, single_data

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/connect", tags=["stripe-connect"])


def _get_profile(sb, user_id: str) -> dict:
    profile = single_data(
        sb.table("business_profiles").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    return profile or {}


@router.post("/start")
async def start_connect_onboarding(user_id: str = Query(..., min_length=1)):
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Payments are not configured yet")

    sb = get_supabase()
    profile = _get_profile(sb, user_id)
    account_id = profile.get("stripe_connect_account_id")

    if not account_id:
        account = stripe.Account.create(
            type="express",
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
        )
        account_id = account.id
        sb.table("business_profiles").upsert({
            "user_id": user_id,
            "stripe_connect_account_id": account_id,
        }, on_conflict="user_id").execute()

    link = stripe.AccountLink.create(
        account=account_id,
        type="account_onboarding",
        # Stripe redirects here on both abandon and completion — the
        # frontend re-checks GET /connect/status itself rather than
        # trusting which URL it landed on, since a refresh vs a real
        # completion look identical from the URL alone.
        refresh_url=f"{settings.frontend_url}/payouts?refresh=1",
        return_url=f"{settings.frontend_url}/payouts?return=1",
    )
    return {"url": link.url}


@router.get("/status")
async def connect_status(user_id: str = Query(..., min_length=1)):
    sb = get_supabase()
    profile = _get_profile(sb, user_id)
    account_id = profile.get("stripe_connect_account_id")
    return {
        "connected": bool(account_id),
        "onboarded": bool(profile.get("connect_onboarded")),
        "payouts_enabled": bool(profile.get("connect_payouts_enabled")),
    }
