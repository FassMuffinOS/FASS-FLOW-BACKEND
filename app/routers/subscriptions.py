"""Stripe subscription routes."""
import stripe
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from app.config import settings
from app.database import get_supabase

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

PLAN_PRICE_MAP = {
    "starter": settings.stripe_price_starter,
    "pro":     settings.stripe_price_pro,
    "team":    settings.stripe_price_team,
}


class CheckoutRequest(BaseModel):
    plan: str          # "starter" | "pro" | "team"
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
        user_id = session["metadata"]["user_id"]
        plan = session["metadata"]["plan"]
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

    elif event["type"] in (
        "invoice.payment_failed",
        "invoice.payment_succeeded",
    ):
        inv = event["data"]["object"]
        status = "active" if event["type"] == "invoice.payment_succeeded" else "past_due"
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
