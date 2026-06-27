"""Affiliate program — 30% commission for content creators who promote
FASS Flow + FASS Wallet.

Money-reality note (read before touching commission math): only two
payment paths today actually land revenue in FASS Flow's own Stripe
account — plan subscriptions and the one-time Wallet pass unlock. Gift
card / campaign purchases are destination charges straight to the
*referred business's* own Stripe Connect account (see gift_cards.py) — FASS
takes $0 of that, so there's nothing real to pay 30% of there. Masterclass
and BD Partner are still raw out-of-band Stripe Payment Links with no
webhook at all. So:
  - subscription + wallet_pass conversions are recorded automatically from
    subscriptions.py's webhook via record_conversion() below.
  - masterclass / bd_partner / anything else gets logged by hand in the
    admin console, same manual pattern as bd_partner_activity.

Attribution: the frontend stores whichever ?ref=<code> code it last saw in
localStorage (30-day window) and calls POST /affiliates/attribute once a
session exists. That sets profiles.referred_by_code — first click wins,
never overwritten — which is what record_conversion() and the manual
admin form both key off of.
"""
import random
import re
import string
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from app.config import settings
from app.database import get_supabase, single_data

router = APIRouter(prefix="/affiliates", tags=["affiliates"])

CONVERSION_SOURCES = {"subscription", "wallet_pass", "masterclass", "bd_partner", "other"}


def _check_admin_secret(x_admin_secret: str | None):
    if not settings.admin_secret:
        raise HTTPException(status_code=503, detail="Admin tools not configured")
    if not x_admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


def _generate_code(handle: str | None) -> str:
    base = re.sub(r"[^a-z0-9]", "", (handle or "").lower())[:12]
    if len(base) < 3:
        base = "fass"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{base}{suffix}"


def record_conversion(referred_user_id: str, source: str, amount: float, note: str | None = None):
    """Called from subscriptions.py's webhook (and available for manual
    admin use) whenever money that's actually FASS Flow's lands, for a
    user who has profiles.referred_by_code set. No-op if the user wasn't
    referred, if the code doesn't map to an affiliate, or if the affiliate
    has paused their account."""
    if source not in CONVERSION_SOURCES:
        raise ValueError(f"Unknown conversion source: {source}")

    sb = get_supabase()
    profile = single_data(
        sb.table("profiles").select("referred_by_code").eq("id", referred_user_id).maybe_single().execute()
    )
    code = profile.get("referred_by_code") if profile else None
    if not code:
        return None

    affiliate = single_data(
        sb.table("affiliates").select("*").eq("code", code).maybe_single().execute()
    )
    if not affiliate or affiliate["status"] != "active":
        return None

    commission = round(amount * float(affiliate["commission_rate"]), 2)
    row = {
        "affiliate_user_id": affiliate["user_id"],
        "referred_user_id": referred_user_id,
        "source": source,
        "amount": amount,
        "commission_amount": commission,
        "note": note,
    }
    return single_data(sb.table("affiliate_conversions").insert(row).execute())


class JoinRequest(BaseModel):
    user_id: str
    handle: str | None = None


@router.post("/join")
async def join(body: JoinRequest):
    """Self-serve — any signed-in user can become an affiliate. No admin
    gate here on purpose: this is the growth lever, the fewer steps between
    "I want to promote this" and "here's your link" the better. Abuse (fake
    signups, self-referral) is caught at payout time, not signup time —
    the admin console shows every conversion before any money moves."""
    sb = get_supabase()
    existing = single_data(
        sb.table("affiliates").select("*").eq("user_id", body.user_id).maybe_single().execute()
    )
    if existing:
        return {"affiliate": existing}

    for _ in range(5):
        code = _generate_code(body.handle)
        try:
            result = sb.table("affiliates").insert({"user_id": body.user_id, "code": code}).execute()
            return {"affiliate": single_data(result)}
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                continue
            raise
    raise HTTPException(status_code=500, detail="Could not generate a unique code, try again")


@router.get("/me")
async def get_me(user_id: str):
    sb = get_supabase()
    affiliate = single_data(
        sb.table("affiliates").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    if not affiliate:
        return {"affiliate": None}

    clicks = (
        sb.table("affiliate_clicks")
        .select("created_at")
        .eq("code", affiliate["code"])
        .order("created_at", desc=True)
        .limit(2000)
        .execute()
        .data
        or []
    )
    conversions = (
        sb.table("affiliate_conversions")
        .select("*")
        .eq("affiliate_user_id", user_id)
        .order("created_at", desc=True)
        .limit(500)
        .execute()
        .data
        or []
    )
    payouts = (
        sb.table("affiliate_payouts")
        .select("*")
        .eq("affiliate_user_id", user_id)
        .order("paid_at", desc=True)
        .execute()
        .data
        or []
    )

    total_earned = sum(c["commission_amount"] for c in conversions)
    total_paid = sum(p["amount"] for p in payouts)

    return {
        "affiliate": affiliate,
        "clicks": clicks,
        "conversions": conversions,
        "payouts": payouts,
        "stats": {
            "click_count": len(clicks),
            "conversion_count": len(conversions),
            "total_earned": round(total_earned, 2),
            "total_paid": round(total_paid, 2),
            "balance_due": round(total_earned - total_paid, 2),
        },
    }


class ClickRequest(BaseModel):
    code: str
    landing_path: str | None = None


@router.post("/track-click")
async def track_click(body: ClickRequest):
    sb = get_supabase()
    affiliate = single_data(
        sb.table("affiliates").select("user_id").eq("code", body.code).maybe_single().execute()
    )
    if not affiliate:
        # Don't error on a bad/expired code — the visitor's experience
        # shouldn't depend on it, this just silently doesn't get tracked.
        return {"tracked": False}
    sb.table("affiliate_clicks").insert({
        "code": body.code,
        "landing_path": body.landing_path,
    }).execute()
    return {"tracked": True}


class AttributeRequest(BaseModel):
    user_id: str
    code: str


@router.post("/attribute")
async def attribute(body: AttributeRequest):
    """First click wins — only ever sets referred_by_code if it isn't
    already set, so a user clicking a second creator's link later doesn't
    steal credit from whoever actually brought them in."""
    sb = get_supabase()
    affiliate = single_data(
        sb.table("affiliates").select("user_id").eq("code", body.code).maybe_single().execute()
    )
    if not affiliate:
        return {"attributed": False}
    if affiliate["user_id"] == body.user_id:
        # Can't refer yourself.
        return {"attributed": False}

    profile = single_data(
        sb.table("profiles").select("referred_by_code").eq("id", body.user_id).maybe_single().execute()
    )
    if profile and profile.get("referred_by_code"):
        return {"attributed": False, "reason": "already attributed"}

    sb.table("profiles").upsert({"id": body.user_id, "referred_by_code": body.code}).execute()
    return {"attributed": True}


@router.get("/admin/list")
async def admin_list(x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    sb = get_supabase()
    affiliates = (
        sb.table("affiliates").select("*").order("created_at", desc=True).execute().data or []
    )
    if not affiliates:
        return {"affiliates": []}

    user_ids = [a["user_id"] for a in affiliates]
    profiles = (
        sb.table("profiles").select("id, full_name, company_name").in_("id", user_ids).execute().data or []
    )
    profiles_by_id = {p["id"]: p for p in profiles}

    conversions = (
        sb.table("affiliate_conversions").select("affiliate_user_id, commission_amount").execute().data or []
    )
    payouts = (
        sb.table("affiliate_payouts").select("affiliate_user_id, amount").execute().data or []
    )
    earned_by_user = {}
    for c in conversions:
        earned_by_user[c["affiliate_user_id"]] = earned_by_user.get(c["affiliate_user_id"], 0) + c["commission_amount"]
    paid_by_user = {}
    for p in payouts:
        paid_by_user[p["affiliate_user_id"]] = paid_by_user.get(p["affiliate_user_id"], 0) + p["amount"]

    for a in affiliates:
        p = profiles_by_id.get(a["user_id"])
        a["full_name"] = p.get("full_name") if p else None
        a["company_name"] = p.get("company_name") if p else None
        earned = round(earned_by_user.get(a["user_id"], 0), 2)
        paid = round(paid_by_user.get(a["user_id"], 0), 2)
        a["total_earned"] = earned
        a["total_paid"] = paid
        a["balance_due"] = round(earned - paid, 2)

    return {"affiliates": affiliates}


@router.get("/admin/detail")
async def admin_detail(user_id: str, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    sb = get_supabase()
    conversions = (
        sb.table("affiliate_conversions")
        .select("*")
        .eq("affiliate_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    payouts = (
        sb.table("affiliate_payouts")
        .select("*")
        .eq("affiliate_user_id", user_id)
        .order("paid_at", desc=True)
        .execute()
        .data
        or []
    )
    return {"conversions": conversions, "payouts": payouts}


class ManualConversionRequest(BaseModel):
    affiliate_user_id: str
    source: str
    amount: float
    note: str = ""


@router.post("/admin/conversion")
async def admin_log_conversion(body: ManualConversionRequest, x_admin_secret: str = Header(None)):
    """For sources with no webhook (Masterclass, BD Partner) — you saw the
    payment land (Stripe dashboard / bank), you log it here, same trust
    model as bd_partner_activity."""
    _check_admin_secret(x_admin_secret)
    if body.source not in CONVERSION_SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {sorted(CONVERSION_SOURCES)}")

    sb = get_supabase()
    affiliate = single_data(
        sb.table("affiliates").select("*").eq("user_id", body.affiliate_user_id).maybe_single().execute()
    )
    if not affiliate:
        raise HTTPException(status_code=404, detail="No affiliate with that user_id")

    commission = round(body.amount * float(affiliate["commission_rate"]), 2)
    row = {
        "affiliate_user_id": body.affiliate_user_id,
        "source": body.source,
        "amount": body.amount,
        "commission_amount": commission,
        "note": body.note or None,
    }
    created = single_data(sb.table("affiliate_conversions").insert(row).execute())
    return created


class PayoutRequest(BaseModel):
    affiliate_user_id: str
    amount: float
    note: str = ""


@router.post("/admin/payout")
async def admin_log_payout(body: PayoutRequest, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    sb = get_supabase()
    row = {
        "affiliate_user_id": body.affiliate_user_id,
        "amount": body.amount,
        "note": body.note or None,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    }
    created = single_data(sb.table("affiliate_payouts").insert(row).execute())
    return created
