"""FASS Data API — external, B2B programmatic access to data FASS creates.

v1 exposes exactly one product: WARDOG Intel's incumbent/award-history pull
(the same USASpending.gov synthesis already sold à la carte to FASS Flow
users inside intelligence.py). Auth is a `fk_live_`/`fk_test_` API key
(app.auth_deps.require_api_key), not a Supabase session — callers here are
outside companies, not FASS Flow users.

Metering: every call tries the customer's monthly plan quota first (if
they're subscribed), then falls back to their pay-per-call balance. Key
issuance and credit/plan grants are admin-secret gated for now — real
self-serve checkout (Stripe) is the next piece, see subscriptions.py's
pattern for how that gets wired once prices exist.
"""
from datetime import datetime, timedelta, timezone

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from app.auth_deps import DataAPICustomer, generate_api_key, require_api_key
from app.cache import cache_get, cache_set
from app.config import settings
from app.database import get_supabase, single_data
from app.routers.intelligence import fetch_incumbent_awards

stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/data", tags=["data-api"])


def _get_or_create_credit_row(sb, customer_id: str) -> dict:
    row = single_data(
        sb.table("data_api_credits").select("*").eq("customer_id", customer_id).maybe_single().execute()
    )
    if row:
        return row
    return single_data(
        sb.table("data_api_credits").insert({"customer_id": customer_id, "balance": 0}).execute()
    )


def _consume_call(customer_id: str, endpoint: str) -> dict:
    """Debits one call — plan quota first, then pay-per-call balance.
    Raises 402 if the customer has neither. Returns the resulting credit
    row so callers can surface remaining balance/quota in the response."""
    sb = get_supabase()
    row = _get_or_create_credit_row(sb, customer_id)

    quota = row.get("plan_quota")
    used = row.get("plan_used", 0) or 0
    period_end = row.get("plan_period_end")
    plan_active = quota and (not period_end or datetime.fromisoformat(period_end.replace("Z", "+00:00")) > datetime.now(timezone.utc))

    if plan_active and used < quota:
        sb.table("data_api_credits").update({"plan_used": used + 1}).eq("customer_id", customer_id).execute()
        row["plan_used"] = used + 1
        return row

    balance = row.get("balance", 0) or 0
    if balance < 1:
        raise HTTPException(
            status_code=402,
            detail="Out of API calls. This key's monthly quota (if any) is used up and its pay-per-call "
            "balance is empty — contact FASS to add credits or upgrade the plan.",
        )
    new_balance = balance - 1
    sb.table("data_api_credits").update({"balance": new_balance}).eq("customer_id", customer_id).execute()
    sb.table("data_api_credit_ledger").insert({
        "customer_id": customer_id, "delta": -1, "reason": endpoint, "balance_after": new_balance,
    }).execute()
    row["balance"] = new_balance
    return row


def _log_usage(customer_id: str, key_id: str, endpoint: str, status_code: int) -> None:
    try:
        get_supabase().table("data_api_usage_log").insert({
            "customer_id": customer_id, "api_key_id": key_id, "endpoint": endpoint, "status_code": status_code,
        }).execute()
    except Exception:
        pass  # usage log is best-effort, never blocks the actual response


# --- Self-serve checkout — called by external buyers, no FASS login -------
# Mirrors credits.py's /packs + /checkout pattern (catalog read live from
# Stripe, price pinned server-side) but for a NEW customer who doesn't have
# a FASS account at all yet. subscriptions.py's webhook does the actual
# granting on payment success by calling the helpers below; the raw API key
# it mints can't be handed back synchronously (webhooks have no browser to
# respond to), so it's cached under the checkout session id for a short
# window and /checkout/session/{id} below is what the success page polls.

PENDING_KEY_TTL = 3600  # 1 hour — plenty of time for the success page to load and fetch it


def get_or_create_customer_by_email(email: str, company_name: str) -> dict:
    """Looks up a Data API customer by contact_email, creating one if this
    is their first purchase. Email is the natural key here since external
    buyers have no FASS user_id — same role email plays for Stripe's own
    Customer object."""
    sb = get_supabase()
    row = single_data(
        sb.table("data_api_customers").select("*").eq("contact_email", email).maybe_single().execute()
    )
    if row:
        return row
    return single_data(
        sb.table("data_api_customers").insert({"company_name": company_name, "contact_email": email}).execute()
    )


def grant_purchased_calls(customer_id: str, amount: int, external_ref: str) -> int | None:
    """Pay-per-call pack purchase — mirrors credits.py's
    grant_purchased_credits, including the same external_ref dedupe against
    Stripe's at-least-once webhook delivery. Returns the new balance, or
    None if this was a duplicate delivery (no-op, not an error)."""
    sb = get_supabase()
    row = _get_or_create_credit_row(sb, customer_id)
    new_balance = (row.get("balance", 0) or 0) + amount
    try:
        sb.table("data_api_credit_ledger").insert({
            "customer_id": customer_id, "delta": amount, "reason": "stripe_purchase",
            "balance_after": new_balance, "external_ref": external_ref,
        }).execute()
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            return None
        raise
    sb.table("data_api_credits").update({"balance": new_balance}).eq("customer_id", customer_id).execute()
    return new_balance


def apply_subscription_plan(customer_id: str, plan: str, monthly_quota: int, period_days: int = 31) -> None:
    """Sets/refreshes the monthly plan quota — called on both the initial
    checkout.session.completed AND every invoice.paid renewal, so plan_used
    resets to 0 each billing cycle exactly like a real quota should."""
    sb = get_supabase()
    _get_or_create_credit_row(sb, customer_id)
    now = datetime.now(timezone.utc)
    sb.table("data_api_credits").update({
        "plan": plan, "plan_quota": monthly_quota, "plan_used": 0,
        "plan_period_start": now.isoformat(),
        "plan_period_end": (now + timedelta(days=period_days)).isoformat(),
    }).eq("customer_id", customer_id).execute()


async def issue_first_key_if_needed(customer_id: str, checkout_session_id: str) -> None:
    """Mints a live key for a customer's FIRST successful purchase only —
    repeat credit-pack top-ups or plan changes for an existing customer
    don't get a new key, they keep using the one they already have. The raw
    value is cached (not persisted — only the hash is ever stored, per
    generate_api_key's design) under the checkout session id so the
    post-checkout success page (GET /checkout/session/{id} below) can
    retrieve it exactly once."""
    sb = get_supabase()
    existing = (
        sb.table("data_api_keys")
        .select("id")
        .eq("customer_id", customer_id)
        .is_("revoked_at", "null")
        .limit(1)
        .execute()
    )
    if existing.data:
        return
    raw_key, key_hash, key_prefix = generate_api_key("live")
    sb.table("data_api_keys").insert({
        "customer_id": customer_id, "key_hash": key_hash, "key_prefix": key_prefix,
        "environment": "live", "name": "First key (self-serve checkout)",
    }).execute()
    await cache_set(f"data_api_pending_key:{checkout_session_id}", {"key": raw_key}, ex=PENDING_KEY_TTL)


@router.get("/plans")
async def list_data_api_plans():
    """The purchasable catalog, read live from Stripe — public (no auth) so
    a pricing page can render for a signed-out visitor. Split into
    subscription plans (recurring) and pay-per-call packs (one-time),
    mirroring how the prices themselves were created."""
    if not settings.stripe_product_data_api:
        return {"plans": [], "packs": []}
    prices = stripe.Price.list(product=settings.stripe_product_data_api, active=True, limit=100)
    plans, packs = [], []
    for p in prices.data:
        meta = p.get("metadata") or {}
        if p.get("recurring") and meta.get("monthly_quota"):
            plans.append({
                "price_id": p["id"], "plan": meta.get("plan"),
                "amount_cents": p["unit_amount"], "amount_display": f"${p['unit_amount'] / 100:.2f}/mo",
                "monthly_quota": int(meta["monthly_quota"]),
            })
        elif meta.get("credits"):
            packs.append({
                "price_id": p["id"], "amount_cents": p["unit_amount"],
                "amount_display": f"${p['unit_amount'] / 100:.2f}", "credits": int(meta["credits"]),
            })
    plans.sort(key=lambda x: x["amount_cents"])
    packs.sort(key=lambda x: x["amount_cents"])
    return {"plans": plans, "packs": packs}


class DataAPICheckoutRequest(BaseModel):
    price_id: str
    company_name: str
    email: str


@router.post("/checkout/subscription")
async def checkout_subscription(body: DataAPICheckoutRequest):
    """Starts a Stripe Checkout session for one of the 3 monthly plans.
    No FASS login required — company_name/email are exactly what's needed
    to stand up a new Data API customer."""
    try:
        price = stripe.Price.retrieve(body.price_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Unknown price")
    if price.get("product") != settings.stripe_product_data_api or not price.get("active") or not price.get("recurring"):
        raise HTTPException(status_code=400, detail="That price isn't an active Data API plan")
    meta = price.get("metadata") or {}
    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": body.price_id, "quantity": 1}],
        customer_email=body.email,
        metadata={
            "kind": "data_api_subscription", "company_name": body.company_name, "email": body.email,
            "plan": meta.get("plan", ""), "monthly_quota": meta.get("monthly_quota", ""),
        },
        success_url=f"{settings.frontend_url}/data-api/welcome?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.frontend_url}/data-api?checkout=cancelled",
    )
    return {"url": session.url}


@router.post("/checkout/credits")
async def checkout_credits(body: DataAPICheckoutRequest):
    """Starts a Stripe Checkout session for a pay-per-call credit pack."""
    try:
        price = stripe.Price.retrieve(body.price_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Unknown price")
    if price.get("product") != settings.stripe_product_data_api or not price.get("active") or price.get("recurring"):
        raise HTTPException(status_code=400, detail="That price isn't an active Data API credit pack")
    meta = price.get("metadata") or {}
    credits = meta.get("credits")
    if not credits:
        raise HTTPException(status_code=400, detail="That price is missing its credits amount")
    session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{"price": body.price_id, "quantity": 1}],
        customer_email=body.email,
        metadata={
            "kind": "data_api_credits", "company_name": body.company_name, "email": body.email, "credits": credits,
        },
        success_url=f"{settings.frontend_url}/data-api/welcome?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.frontend_url}/data-api?checkout=cancelled",
    )
    return {"url": session.url}


@router.get("/checkout/session/{session_id}")
async def get_checkout_result(session_id: str):
    """The post-checkout success page calls this to (a) confirm payment
    went through and (b) retrieve a freshly-minted API key exactly once, if
    this was the customer's first purchase. The webhook (subscriptions.py)
    is what actually grants credits/plan/key — this endpoint only ever
    reads, and the cached raw key is popped so a page refresh can't leak it
    twice."""
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Unknown checkout session")
    if session.get("payment_status") != "paid":
        return {"paid": False}

    cache_key = f"data_api_pending_key:{session_id}"
    pending = await cache_get(cache_key)
    api_key = None
    if pending:
        api_key = pending.get("key")
        await cache_set(cache_key, {}, ex=1)  # effectively clears it — shown once
    return {"paid": True, "api_key": api_key}


@router.get("/wardog/incumbent")
async def data_api_incumbent(
    naics: str = Query(..., description="NAICS code, e.g. 561720"),
    agency: str = Query("", description="Top-tier awarding agency name, blank = all agencies"),
    customer: DataAPICustomer = Depends(require_api_key),
):
    """Recent federal prime-award history for a NAICS (+ optional agency),
    synthesized from USASpending.gov — the same data WARDOG Intel sells to
    FASS Flow users à la carte, now available to external buyers. Metered:
    1 call = 1 unit against the key's plan quota or pay-per-call balance."""
    credit_row = _consume_call(customer.customer_id, "wardog_incumbent")
    try:
        result = await fetch_incumbent_awards(naics, agency)
    except HTTPException as e:
        _log_usage(customer.customer_id, customer.key_id, "wardog_incumbent", e.status_code)
        raise
    result["balance_remaining"] = credit_row.get("balance", 0)
    if credit_row.get("plan_quota"):
        result["plan_calls_remaining"] = max(0, credit_row["plan_quota"] - credit_row.get("plan_used", 0))
    _log_usage(customer.customer_id, customer.key_id, "wardog_incumbent", 200)
    return result


@router.get("/usage")
async def data_api_usage(customer: DataAPICustomer = Depends(require_api_key)):
    """Lets a customer check their own remaining balance/quota without
    hitting a billable endpoint."""
    sb = get_supabase()
    row = _get_or_create_credit_row(sb, customer.customer_id)
    out = {"company_name": customer.company_name, "balance": row.get("balance", 0)}
    if row.get("plan_quota"):
        out["plan"] = row.get("plan")
        out["plan_quota"] = row["plan_quota"]
        out["plan_used"] = row.get("plan_used", 0)
        out["plan_period_end"] = row.get("plan_period_end")
    return out


# --- Admin — manual customer/key/credit provisioning ------------------
# Same shared-secret pattern as credits.py's /grant and admin.py, kept until
# real self-serve Stripe checkout exists (see task: "Wire billing + checkout
# for Data API customers"). Lets support onboard an external buyer today
# without waiting on that.

def _require_admin(x_admin_secret: str | None) -> None:
    if not settings.admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


class CreateCustomerRequest(BaseModel):
    company_name: str
    contact_email: str


@router.post("/admin/customers")
async def create_customer(body: CreateCustomerRequest, x_admin_secret: str | None = Header(default=None)):
    _require_admin(x_admin_secret)
    sb = get_supabase()
    row = single_data(
        sb.table("data_api_customers")
        .insert({"company_name": body.company_name, "contact_email": body.contact_email})
        .execute()
    )
    return row


class IssueKeyRequest(BaseModel):
    customer_id: str
    environment: str = "live"
    name: str = ""


@router.post("/admin/keys")
async def issue_key(body: IssueKeyRequest, x_admin_secret: str | None = Header(default=None)):
    """Mints a new key and returns the raw value exactly once — same
    once-only disclosure pattern the docs site already promises."""
    _require_admin(x_admin_secret)
    if body.environment not in ("live", "test"):
        raise HTTPException(status_code=400, detail="environment must be 'live' or 'test'")
    raw_key, key_hash, key_prefix = generate_api_key(body.environment)
    sb = get_supabase()
    row = single_data(
        sb.table("data_api_keys")
        .insert({
            "customer_id": body.customer_id, "key_hash": key_hash, "key_prefix": key_prefix,
            "environment": body.environment, "name": body.name or None,
        })
        .execute()
    )
    return {"key_id": row["id"], "key": raw_key, "key_prefix": key_prefix, "environment": body.environment}


@router.post("/admin/keys/{key_id}/revoke")
async def revoke_key(key_id: str, x_admin_secret: str | None = Header(default=None)):
    _require_admin(x_admin_secret)
    sb = get_supabase()
    sb.table("data_api_keys").update(
        {"revoked_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", key_id).execute()
    return {"ok": True}


class GrantCallsRequest(BaseModel):
    customer_id: str
    amount: int
    reason: str = "manual_grant"


@router.post("/admin/credits/grant")
async def grant_calls(body: GrantCallsRequest, x_admin_secret: str | None = Header(default=None)):
    """Adds to a customer's pay-per-call balance — the manual-onboarding
    equivalent of credits.py's /grant, until Stripe checkout is wired."""
    _require_admin(x_admin_secret)
    sb = get_supabase()
    row = _get_or_create_credit_row(sb, body.customer_id)
    new_balance = max(0, (row.get("balance", 0) or 0) + body.amount)
    sb.table("data_api_credits").update({"balance": new_balance}).eq("customer_id", body.customer_id).execute()
    sb.table("data_api_credit_ledger").insert({
        "customer_id": body.customer_id, "delta": body.amount, "reason": body.reason, "balance_after": new_balance,
    }).execute()
    return {"balance": new_balance}


class SetPlanRequest(BaseModel):
    customer_id: str
    plan: str
    monthly_quota: int
    period_days: int = 30


@router.post("/admin/plan/set")
async def set_plan(body: SetPlanRequest, x_admin_secret: str | None = Header(default=None)):
    """Manually attaches/refreshes a monthly plan quota — the manual
    equivalent of what the Stripe invoice.paid webhook will do once billing
    is wired (task: 'Wire billing + checkout for Data API customers')."""
    _require_admin(x_admin_secret)
    from datetime import timedelta
    sb = get_supabase()
    _get_or_create_credit_row(sb, body.customer_id)
    now = datetime.now(timezone.utc)
    sb.table("data_api_credits").update({
        "plan": body.plan,
        "plan_quota": body.monthly_quota,
        "plan_used": 0,
        "plan_period_start": now.isoformat(),
        "plan_period_end": (now + timedelta(days=body.period_days)).isoformat(),
    }).eq("customer_id", body.customer_id).execute()
    return {"ok": True}
