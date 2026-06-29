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

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.config import settings
from app.database import get_supabase, single_data

router = APIRouter(prefix="/affiliates", tags=["affiliates"])

CONVERSION_SOURCES = {"subscription", "wallet_pass", "masterclass", "bd_partner", "other"}
# System-generated only — never offered in the admin console's manual-entry
# dropdown, only ever inserted by _credit_recruiter_override below.
OVERRIDE_SOURCE = "override"

# --- Gamification shell (FASS Creator OS, phase 1) ------------------------
# Simple action-based XP: a fixed amount per discrete action, recorded once
# each in affiliate_xp_events (unique per affiliate+action) so nothing can
# be double-awarded even if an endpoint gets called twice. Leveling is a
# flat curve (500 XP per level) — easy to reason about, easy to retune
# later without a migration since it's computed, not stored.
XP_VALUES = {
    "join": 100,             # generated your referral link
    "complete_profile": 100, # Day-1 onboarding mission item
    "watch_onboarding": 100, # Day-1 onboarding mission item
    "assignment_done": 25,   # today's assignment, once per calendar day
    "conversion": 50,        # a referral of yours converts
    "recruit": 100,          # someone joins as an affiliate under your link
}
LEVEL_XP_STEP = 500
RANK_TITLES = [
    (1, "New Creator"),
    (3, "Rising Creator"),
    (6, "Regional Partner"),
    (10, "National Partner"),
    (15, "Enterprise Partner"),
]


def _level_for_xp(xp: int) -> int:
    return int(xp // LEVEL_XP_STEP) + 1


def _rank_for_level(level: int) -> str:
    title = RANK_TITLES[0][1]
    for min_level, name in RANK_TITLES:
        if level >= min_level:
            title = name
    return title


def _next_rank_title(level: int) -> str | None:
    for min_level, name in RANK_TITLES:
        if level < min_level:
            return name
    return None


def _gamification_snapshot(xp: int) -> dict:
    xp = int(xp or 0)
    level = _level_for_xp(xp)
    xp_into_level = xp % LEVEL_XP_STEP
    return {
        "xp": xp,
        "level": level,
        "rank": _rank_for_level(level),
        "next_rank": _next_rank_title(level),
        "xp_into_level": xp_into_level,
        "xp_to_next_level": LEVEL_XP_STEP - xp_into_level,
        "level_progress_pct": round(xp_into_level / LEVEL_XP_STEP * 100, 1),
    }


def _award_xp(sb, affiliate_user_id: str, action: str, xp_amount: int, note: str | None = None):
    """Idempotent per (affiliate_user_id, action) — relies on the unique
    constraint in affiliate_xp_events. Repeatable actions (conversions,
    recruits, daily assignments) encode their own uniqueness into `action`
    (e.g. f"conversion_{conversion_id}", f"assignment_{date}")."""
    try:
        sb.table("affiliate_xp_events").insert({
            "affiliate_user_id": affiliate_user_id,
            "action": action,
            "xp_amount": xp_amount,
            "note": note,
        }).execute()
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            return False
        raise

    current = single_data(
        sb.table("affiliates").select("xp").eq("user_id", affiliate_user_id).maybe_single().execute()
    )
    new_xp = (current.get("xp") if current else 0) or 0
    sb.table("affiliates").update({"xp": new_xp + xp_amount}).eq("user_id", affiliate_user_id).execute()
    return True


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


def _credit_recruiter_override(sb, affiliate: dict, base_commission: float, referred_user_id: str | None, note: str | None):
    """Sub-affiliate override — one level deep, no chaining further up.
    If `affiliate` was recruited by someone (recruited_by_user_id set at
    join time, see join() below), that recruiter earns a percentage of
    THIS commission on top of their own 30%. No-op if there's no
    recruiter, the recruiter's account is paused, or the rate is zero."""
    recruiter_id = affiliate.get("recruited_by_user_id")
    if not recruiter_id:
        return None

    recruiter = single_data(
        sb.table("affiliates").select("*").eq("user_id", recruiter_id).maybe_single().execute()
    )
    if not recruiter or recruiter["status"] != "active":
        return None

    override_rate = float(recruiter.get("override_rate") or 0)
    if override_rate <= 0:
        return None

    override_commission = round(base_commission * override_rate, 2)
    if override_commission <= 0:
        return None

    row = {
        "affiliate_user_id": recruiter_id,
        "referred_user_id": referred_user_id,
        "source": OVERRIDE_SOURCE,
        "amount": base_commission,
        "commission_amount": override_commission,
        "note": f"Override on {affiliate['code']}'s commission" + (f" — {note}" if note else ""),
    }
    return single_data(sb.table("affiliate_conversions").insert(row).execute())


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
    created = single_data(sb.table("affiliate_conversions").insert(row).execute())
    _credit_recruiter_override(sb, affiliate, commission, referred_user_id, note)
    if created:
        _award_xp(sb, affiliate["user_id"], f"conversion_{created['id']}", XP_VALUES["conversion"], "Referral converted")
    return created


class JoinRequest(BaseModel):
    user_id: str
    handle: str | None = None


@router.post("/join")
async def join(body: JoinRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Self-serve — any signed-in user can become an affiliate. No admin
    gate here on purpose: this is the growth lever, the fewer steps between
    "I want to promote this" and "here's your link" the better. Abuse (fake
    signups, self-referral) is caught at payout time, not signup time —
    the admin console shows every conversion before any money moves."""
    require_owner(current_user, body.user_id, detail="You can only set up your own affiliate account")
    sb = get_supabase()
    existing = single_data(
        sb.table("affiliates").select("*").eq("user_id", body.user_id).maybe_single().execute()
    )
    if existing:
        return {"affiliate": existing}

    # Sub-affiliate recruiting reuses the exact same attribution already
    # captured for customer referrals: whichever affiliate's link this
    # person clicked (profiles.referred_by_code, first-click-wins) becomes
    # their recruiter the moment they themselves join as an affiliate. No
    # separate "recruiting link" — one link does both jobs.
    recruited_by_user_id = None
    profile = single_data(
        sb.table("profiles").select("referred_by_code").eq("id", body.user_id).maybe_single().execute()
    )
    ref_code = profile.get("referred_by_code") if profile else None
    if ref_code:
        recruiter = single_data(
            sb.table("affiliates").select("user_id").eq("code", ref_code).maybe_single().execute()
        )
        if recruiter and recruiter["user_id"] != body.user_id:
            recruited_by_user_id = recruiter["user_id"]

    for _ in range(5):
        code = _generate_code(body.handle)
        try:
            insert_row = {"user_id": body.user_id, "code": code}
            if recruited_by_user_id:
                insert_row["recruited_by_user_id"] = recruited_by_user_id
            result = sb.table("affiliates").insert(insert_row).execute()
            created = single_data(result)
            _award_xp(sb, body.user_id, "join", XP_VALUES["join"], "Generated your referral link")
            if recruited_by_user_id:
                _award_xp(sb, recruited_by_user_id, f"recruit_{body.user_id}", XP_VALUES["recruit"], "A creator joined under your link")
            return {"affiliate": created}
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                continue
            raise
    raise HTTPException(status_code=500, detail="Could not generate a unique code, try again")


@router.get("/me")
async def get_me(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="You can only view your own affiliate dashboard")
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
    override_earned = sum(c["commission_amount"] for c in conversions if c.get("source") == OVERRIDE_SOURCE)

    recruits = (
        sb.table("affiliates")
        .select("user_id, code, status, created_at")
        .eq("recruited_by_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )

    xp_events = (
        sb.table("affiliate_xp_events").select("action").eq("affiliate_user_id", user_id).execute().data or []
    )
    done_actions = {e["action"] for e in xp_events}
    today_key = datetime.now(timezone.utc).date().isoformat()

    return {
        "affiliate": affiliate,
        "clicks": clicks,
        "conversions": conversions,
        "payouts": payouts,
        "recruits": recruits,
        "stats": {
            "click_count": len(clicks),
            "conversion_count": len(conversions),
            "total_earned": round(total_earned, 2),
            "total_paid": round(total_paid, 2),
            "balance_due": round(total_earned - total_paid, 2),
            "override_earned": round(override_earned, 2),
            "recruit_count": len(recruits),
        },
        "gamification": {
            **_gamification_snapshot(affiliate.get("xp") or 0),
            "onboarding": {
                "join": "join" in done_actions,
                "complete_profile": "complete_profile" in done_actions,
                "watch_onboarding": "watch_onboarding" in done_actions,
            },
            "assignment_done_today": f"assignment_{today_key}" in done_actions,
        },
    }


class XpClaimRequest(BaseModel):
    user_id: str
    action: str


CLAIMABLE_ACTIONS = {"complete_profile", "watch_onboarding"}


@router.post("/xp/claim")
async def claim_xp(body: XpClaimRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Manual, self-reported Day-1 onboarding mission items — there's no
    backend signal for "read your profile" or "watched the video", so the
    person just checks it off. Idempotent either way, can't be farmed."""
    require_owner(current_user, body.user_id, detail="You can only claim your own XP")
    if body.action not in CLAIMABLE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(CLAIMABLE_ACTIONS)}")
    sb = get_supabase()
    affiliate = single_data(
        sb.table("affiliates").select("user_id").eq("user_id", body.user_id).maybe_single().execute()
    )
    if not affiliate:
        raise HTTPException(status_code=404, detail="Not an affiliate yet")
    awarded = _award_xp(sb, body.user_id, body.action, XP_VALUES[body.action])
    return {"awarded": awarded, "xp_amount": XP_VALUES[body.action] if awarded else 0}


class AssignmentDoneRequest(BaseModel):
    user_id: str


@router.post("/assignment/done")
async def mark_assignment_done(body: AssignmentDoneRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Today's directive action item — +25 XP, once per calendar day
    (UTC), tracked via affiliate_xp_events so it survives across devices
    instead of living only in localStorage."""
    require_owner(current_user, body.user_id, detail="You can only mark your own assignment done")
    sb = get_supabase()
    affiliate = single_data(
        sb.table("affiliates").select("user_id").eq("user_id", body.user_id).maybe_single().execute()
    )
    if not affiliate:
        raise HTTPException(status_code=404, detail="Not an affiliate yet")
    today_key = datetime.now(timezone.utc).date().isoformat()
    awarded = _award_xp(sb, body.user_id, f"assignment_{today_key}", XP_VALUES["assignment_done"], "Completed today's assignment")
    return {"awarded": awarded, "xp_amount": XP_VALUES["assignment_done"] if awarded else 0}


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
async def attribute(body: AttributeRequest, current_user: CurrentUser = Depends(get_current_user)):
    """First click wins — only ever sets referred_by_code if it isn't
    already set, so a user clicking a second creator's link later doesn't
    steal credit from whoever actually brought them in."""
    require_owner(current_user, body.user_id, detail="You can only attribute your own account")
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
    override_by_user = {}
    for c in conversions:
        earned_by_user[c["affiliate_user_id"]] = earned_by_user.get(c["affiliate_user_id"], 0) + c["commission_amount"]
        if c.get("source") == OVERRIDE_SOURCE:
            override_by_user[c["affiliate_user_id"]] = override_by_user.get(c["affiliate_user_id"], 0) + c["commission_amount"]
    paid_by_user = {}
    for p in payouts:
        paid_by_user[p["affiliate_user_id"]] = paid_by_user.get(p["affiliate_user_id"], 0) + p["amount"]

    recruit_counts = {}
    for a in affiliates:
        rid = a.get("recruited_by_user_id")
        if rid:
            recruit_counts[rid] = recruit_counts.get(rid, 0) + 1

    for a in affiliates:
        p = profiles_by_id.get(a["user_id"])
        a["full_name"] = p.get("full_name") if p else None
        a["company_name"] = p.get("company_name") if p else None
        earned = round(earned_by_user.get(a["user_id"], 0), 2)
        paid = round(paid_by_user.get(a["user_id"], 0), 2)
        a["total_earned"] = earned
        a["total_paid"] = paid
        a["balance_due"] = round(earned - paid, 2)
        a["override_earned"] = round(override_by_user.get(a["user_id"], 0), 2)
        a["recruit_count"] = recruit_counts.get(a["user_id"], 0)
        recruiter_p = profiles_by_id.get(a.get("recruited_by_user_id"))
        a["recruited_by_name"] = (recruiter_p.get("company_name") or recruiter_p.get("full_name")) if recruiter_p else None

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
    recruits = (
        sb.table("affiliates")
        .select("user_id, code, status, created_at")
        .eq("recruited_by_user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    return {"conversions": conversions, "payouts": payouts, "recruits": recruits}


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
    _credit_recruiter_override(sb, affiliate, commission, None, body.note or None)
    if created:
        _award_xp(sb, body.affiliate_user_id, f"conversion_{created['id']}", XP_VALUES["conversion"], "Referral converted")
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
