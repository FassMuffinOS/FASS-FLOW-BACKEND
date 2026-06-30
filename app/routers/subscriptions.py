"""Stripe subscription routes."""
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from pydantic import BaseModel
from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.config import settings
from app.database import get_supabase
from app.routers.affiliates import record_conversion

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

PLAN_PRICE_MAP = {
    "lite":       settings.stripe_price_lite,
    "starter":    settings.stripe_price_starter,
    "pro":        settings.stripe_price_pro,
    "team":       settings.stripe_price_team,
    "enterprise": settings.stripe_price_enterprise,
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
    plan: str          # "lite" | "starter" | "pro" | "team" | "enterprise"
    user_id: str
    email: str


@router.post("/checkout")
async def create_checkout_session(body: CheckoutRequest, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, body.user_id, detail="You can only start checkout for your own account")
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
            wallet_user_id = metadata.get("user_id")
            if wallet_user_id:
                amount = (session.get("amount_total") or 0) / 100
                if amount > 0:
                    record_conversion(wallet_user_id, "wallet_pass", amount)
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
            # Defensive: a subscription checkout.session.completed we didn't
            # initiate (or one missing our metadata) has nothing safe to sync.
            # Bracket access here would KeyError -> 500 -> Stripe retries the
            # event forever. Ack and move on instead.
            user_id = metadata.get("user_id")
            plan = metadata.get("plan")
            if not user_id or not plan:
                return {"received": True, "skipped": "subscription checkout missing user_id/plan metadata"}
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")

            sb.table("profiles").upsert({
                "id": user_id,
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "plan": plan,
                "subscription_status": "active",
            }).execute()

            # No commission fired here on purpose — Stripe always creates a
            # real invoice for the first subscription cycle too, which will
            # land as invoice.payment_succeeded below moments after this
            # event. Commissioning HERE as well as there would double-pay
            # the first month. invoice.payment_succeeded is the single
            # source of truth for subscription commission — first invoice
            # and every renewal alike — which is what makes "recurring for
            # 12 months" (gated by record_conversion()'s commission-window
            # check against profiles.referred_at) actually work.

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

    elif event["type"] == "account.updated":
        # Fired by Stripe as a Connect Express account moves through
        # onboarding/verification (see stripe_connect.py). Never trust the
        # frontend redirect alone for this — Stripe can flag an account for
        # additional review or the user can abandon the tab mid-flow, so
        # this event is the only reliable source of truth for whether a
        # business can actually receive payouts yet.
        account = event["data"]["object"]
        (
            sb.table("business_profiles")
            .update({
                "connect_onboarded": bool(account.get("details_submitted")),
                "connect_payouts_enabled": bool(account.get("payouts_enabled")),
                "connect_updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("stripe_connect_account_id", account["id"])
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

        # Subscription commission lives here, and only here — see the long
        # comment up in checkout.session.completed for why. Fires on the
        # first invoice AND every renewal; record_conversion() itself caps
        # it to a 12-month window from profiles.referred_at, and the
        # external_ref=inv["id"] dedupes against Stripe's at-least-once
        # webhook delivery via affiliate_conversions' partial unique index.
        # Subscription-mode invoices only — skip one-time invoices (e.g.
        # any non-subscription invoice item) by requiring a subscription id.
        if event["type"] == "invoice.payment_succeeded" and inv.get("subscription"):
            amount = (inv.get("amount_paid") or 0) / 100
            if amount > 0:
                profile = (
                    sb.table("profiles")
                    .select("id")
                    .eq("stripe_customer_id", inv["customer"])
                    .maybe_single()
                    .execute()
                )
                user_id = profile.data["id"] if profile and profile.data else None
                if user_id:
                    record_conversion(user_id, "subscription", amount, external_ref=inv["id"])

    return {"received": True}


# Mirrors Pricing.jsx's advertised monthly prices (USD). Kept here as an
# INDEPENDENT assertion so the pre-launch check below can flag any drift
# between what the pricing page promises and what the Stripe price actually
# charges. Update both together if a price changes. 'free' has no Stripe
# price; 'lite' is intentionally absent (no longer offered on the page).
EXPECTED_PRICE_CENTS = {
    "starter": 9900,      # Core — $99/mo
    "pro": 20000,         # Pro — $200/mo
    "team": 49900,        # Team — $499/mo (sales-assisted, but still needs a
                          # price id set so portal/manual subscriptions map back)
    "enterprise": 150000, # Enterprise — $1,500/mo
}


@router.get("/admin/pricing-check")
async def pricing_check(x_admin_secret: str = Header(None)):
    """Pre-launch pricing sweep — run before flipping payments live and any
    time a price changes. For every plan it confirms the Stripe price env is
    set, points to a real ACTIVE recurring USD price, charges exactly what the
    pricing page advertises, and that no two plans share a price id (which
    would corrupt the webhook's PRICE_TO_PLAN reverse lookup). Read-only.

        curl -s https://<backend>/api/v1/subscriptions/admin/pricing-check \\
             -H "X-Admin-Secret: $ADMIN_SECRET" | python3 -m json.tool
    """
    if not settings.admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")

    results = []
    seen_price_ids: dict[str, str] = {}
    for plan, price_id in PLAN_PRICE_MAP.items():
        offered = plan in EXPECTED_PRICE_CENTS
        row = {"plan": plan, "offered": offered, "price_id": price_id or None, "issues": []}

        if not price_id:
            if offered:
                row["issues"].append("price id env var is blank — checkout/webhook will fail for this plan")
            else:
                row["issues"].append("price id not set (plan not offered on pricing page — OK)")
            row["ok"] = not offered
            results.append(row)
            continue

        if price_id in seen_price_ids:
            row["issues"].append(
                f"shares its Stripe price id with '{seen_price_ids[price_id]}' — "
                "webhook plan mapping will be wrong for one of them"
            )
        seen_price_ids[price_id] = plan

        try:
            price = stripe.Price.retrieve(price_id)
        except Exception as exc:
            row["issues"].append(f"Stripe could not retrieve this price id: {exc}")
            row["ok"] = False
            results.append(row)
            continue

        recurring = price.get("recurring") or {}
        row["amount_cents"] = price.get("unit_amount")
        row["amount_display"] = f"${(price.get('unit_amount') or 0) / 100:.2f}"
        row["currency"] = price.get("currency")
        row["interval"] = recurring.get("interval")
        row["active"] = bool(price.get("active"))

        if not price.get("active"):
            row["issues"].append("price is archived/inactive in Stripe")
        if price.get("currency") != "usd":
            row["issues"].append(f"currency is '{price.get('currency')}', expected 'usd'")
        if recurring.get("interval") != "month":
            row["issues"].append(f"billing interval is '{recurring.get('interval')}', expected 'month'")
        expected = EXPECTED_PRICE_CENTS.get(plan)
        if expected is not None and price.get("unit_amount") != expected:
            row["issues"].append(
                f"charges ${ (price.get('unit_amount') or 0) / 100:.2f} but the pricing page "
                f"advertises ${expected / 100:.2f}"
            )

        row["ok"] = not row["issues"]
        results.append(row)

    offered_ok = all(r["ok"] for r in results if r["offered"])
    return {
        "all_offered_plans_ok": offered_ok,
        "stripe_secret_key_set": bool(settings.stripe_secret_key),
        "stripe_webhook_secret_set": bool(settings.stripe_webhook_secret),
        "frontend_url": settings.frontend_url,
        "plans": results,
    }


@router.get("/portal/{user_id}")
async def customer_portal(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only manage your own billing")
    sb = get_supabase()
    result = sb.table("profiles").select("stripe_customer_id").eq("id", user_id).single().execute()
    customer_id = result.data["stripe_customer_id"]
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{settings.frontend_url}/dashboard",
    )
    return {"url": session.url}
