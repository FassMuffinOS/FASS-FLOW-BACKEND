"""Stripe subscription routes."""
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from app.config import settings
from app.database import get_supabase

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

PLAN_PRICE_MAP = {
    "lite":    settings.stripe_price_lite,
    "starter": settings.stripe_price_starter,
    "pro":     settings.stripe_price_pro,
    "team":    settings.stripe_price_team,
}
# Reverse lookup so subscription.created/updated events — which carry a
# Stripe price ID on the subscription item, not our plan name — can be
# mapped back to the plan tier without re-deriving it from checkout
# session metadata (which isn't present on these event types at all).
PRICE_TO_PLAN_MAP = {v: k for k, v in PLAN_PRICE_MAP.items() if v}


def _plan_from_subscription(sub) -> str | None:
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        price_id = (item.get("price") or {}).get("id")
        if price_id in PRICE_TO_PLAN_MAP:
            return PRICE_TO_PLAN_MAP[price_id]
    return None


class CheckoutRequest(BaseModel):
    plan: str          # "lite" | "starter" | "pro" | "team"
    user_id: str
    email: str


@router.post("/checkout")
async def create_checkout_session(body: CheckoutRequest):
    price_id = PLAN_PRICE_MAP.get(body.plan)
    if not price_id:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=body.email,
        metadata={"user_id": body.user_id, "plan": body.plan},
        success_url=f"{settings.frontend_url}/dashboard?checkout=success",
        cancel_url=f"{settings.frontend_url}/pricing?checkout=cancelled",
        trial_period_days=14,
    )
    return {"url": session.url}


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None),
):
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.stripe_webhook_secret
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    sb = get_supabase()

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata") or {}

        if metadata.get("kind") == "wallet_pass":
            # One-time FASS Wallet .pkpass unlock — mode="payment", no
            # subscription/customer record to sync, just flip the purchased
            # flag on the slug's row so GET /wallet/pass will serve the
            # real signed file from here on.
            slug = metadata.get("slug")
            if slug:
                (
                    sb.table("wallet_passes")
                    .update({"purchased": True, "purchased_at": datetime.now(timezone.utc).isoformat()})
                    .eq("slug", slug)
                    .execute()
                )
        elif metadata.get("kind") == "gift_card":
            # Public storefront gift card purchase — mode="payment" with a
            # customer-chosen amount (see gift_cards.py's /purchase/checkout).
            # No gift_cards row exists yet; this webhook is what actually
            # creates it, only now that payment is confirmed. Upserted on
            # slug since Stripe can redeliver the same event more than once.
            slug = metadata.get("slug")
            value = float(metadata.get("value") or 0)
            if slug and value > 0:
                sb.table("gift_cards").upsert({
                    "slug": slug,
                    "business_user_id": metadata.get("business_user_id"),
                    "customer_name": metadata.get("customer_name") or None,
                    "customer_contact": metadata.get("customer_contact") or None,
                    "original_value": value,
                    "balance": value,
                }, on_conflict="slug").execute()
        else:
            user_id = metadata["user_id"]
            plan = metadata["plan"]
            customer_id = session["customer"]
            subscription_id = session["subscription"]

            sb.table("profiles").upsert({
                "id": user_id,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "plan": plan,
                "subscription_status": "active",
            }).execute()

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        (
            sb.table("profiles")
            .update({"subscription_status": "cancelled", "plan": "free"})
            .eq("stripe_subscription_id", sub["id"])
            .execute()
        )

    elif event["type"] in ("customer.subscription.created", "customer.subscription.updated"):
        # Covers plan changes made through the customer billing portal
        # (upgrade/downgrade, pause, reactivation) — these never hit
        # checkout.session.completed and previously left profiles.plan
        # stale until the next invoice event happened to fire.
        sub = event["data"]["object"]
        update = {"subscription_status": sub.get("status", "active")}
        plan = _plan_from_subscription(sub)
        if plan:
            update["plan"] = plan
        (
            sb.table("profiles")
            .update(update)
            .eq("stripe_subscription_id", sub["id"])
            .execute()
        )

    elif event["type"] in (
        "invoice.payment_failed",
        "invoice.payment_succeeded",
        "invoice.paid",
    ):
        inv = event["data"]["object"]
        status = "past_due" if event["type"] == "invoice.payment_failed" else "active"
        (
            sb.table("profiles")
            .update({"subscription_status": status})
            .eq("stripe_customer_id", inv["customer"])
            .execute()
        )

    return {"received": True}


@router.get("/portal/{user_id}")
async def customer_portal(user_id: str):
    sb = get_supabase()
    result = sb.table("profiles").select("stripe_customer_id").eq("id", user_id).single().execute()
    customer_id = result.data["stripe_customer_id"]
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{settings.frontend_url}/dashboard",
    )
    return {"url": session.url}
