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


def _clean_source(source: str | None) -> str | None:
    """Normalize a ?src= channel tag to a safe, bounded slug so the
    per-channel rollups don't fracture on casing/whitespace and can't be
    abused to inject junk. Lowercased, non-alphanumerics collapsed to '-',
    capped at 32 chars. Returns None for empty/garbage input (treated as
    'untagged' downstream)."""
    if not source:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", source.strip().lower()).strip("-")
    return slug[:32] or None


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


def record_conversion(
    referred_user_id: str,
    source: str,
    amount: float,
    note: str | None = None,
    external_ref: str | None = None,
):
    """Called from subscriptions.py's webhook (and available for manual
    admin use) whenever money that's actually FASS Flow's lands, for a
    user who has profiles.referred_by_code set. No-op if the user wasn't
    referred, if the code doesn't map to an affiliate, or if the affiliate
    has paused their account.

    `external_ref` is the Stripe invoice id for subscription renewals —
    subscriptions.py now calls this on EVERY invoice.payment_succeeded
    (first invoice and renewals alike) to support recurring commission, and
    Stripe's webhook delivery is at-least-once, so this dedupes against
    affiliate_conversions' partial unique index instead of trusting the
    caller not to double-fire.

    `source == "subscription"` is additionally gated by the affiliate's
    commission_window_months, measured from profiles.referred_at — this is
    what makes recurring commission "for 12 months" rather than for life.
    Other sources (wallet_pass/masterclass/bd_partner/other) are one-time
    and not time-gated."""
    if source not in CONVERSION_SOURCES:
        raise ValueError(f"Unknown conversion source: {source}")

    sb = get_supabase()
    profile = single_data(
        sb.table("profiles").select("referred_by_code, referred_at, referred_source").eq("id", referred_user_id).maybe_single().execute()
    )
    code = profile.get("referred_by_code") if profile else None
    if not code:
        return None
    referred_source = profile.get("referred_source") if profile else None

    affiliate = single_data(
        sb.table("affiliates").select("*").eq("code", code).maybe_single().execute()
    )
    if not affiliate or affiliate["status"] != "active":
        return None

    if source == "subscription":
        referred_at_raw = profile.get("referred_at") if profile else None
        if referred_at_raw:
            try:
                referred_at = datetime.fromisoformat(str(referred_at_raw).replace("Z", "+00:00"))
                window_months = int(affiliate.get("commission_window_months") or 12)
                elapsed_days = (datetime.now(timezone.utc) - referred_at).days
                if elapsed_days > window_months * 30:
                    return None
            except (ValueError, TypeError):
                pass  # malformed timestamp — don't let a parse error block a real commission

    commission = round(amount * float(affiliate["commission_rate"]), 2)
    row = {
        "affiliate_user_id": affiliate["user_id"],
        "referred_user_id": referred_user_id,
        "source": source,
        "referred_source": referred_source,
        "amount": amount,
        "commission_amount": commission,
        "note": note,
        "external_ref": external_ref,
    }
    try:
        created = single_data(sb.table("affiliate_conversions").insert(row).execute())
    except Exception as exc:
        if "duplicate key" in str(exc).lower():
            # Same Stripe invoice already commissioned — webhook redelivery.
            return None
        raise
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


class ApplyRequest(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    platform: str | None = None       # instagram / youtube / tiktok / blog / newsletter / other
    channel_url: str | None = None
    audience_size: str | None = None  # free-text band, informational only
    why_join: str | None = None
    how_promote: str | None = None
    handle: str | None = None
    ref_code: str | None = None       # if the applicant themselves was referred in


@router.post("/apply")
async def apply(body: ApplyRequest):
    """External affiliate application — no existing FASS Flow account
    required. This is deliberately the front door for the "separate but
    linked through" partner program: it provisions a real Supabase Auth
    account (via the Admin API, pre-confirmed — no email-verification
    wait), flags profiles.is_affiliate_only so the app shell renders a
    stripped creator-only experience instead of the GovCon product, and
    generates a referral code in the same request. Per spec, the applicant
    can start sharing their link and earning the moment they apply — admin
    review (see /admin/applications/review below) is for curation/visibility
    only, it never blocks the ability to promote."""
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    sb = get_supabase()
    try:
        created = sb.auth.admin.create_user({
            "email": email,
            "password": body.password,
            "email_confirm": True,
            "user_metadata": {"full_name": body.full_name, "is_affiliate_only": True},
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

    profile_row = {
        "id": user_id,
        "full_name": body.full_name,
        "is_affiliate_only": True,
    }
    recruited_by_user_id = None
    if body.ref_code:
        ref_affiliate = single_data(
            sb.table("affiliates").select("user_id").eq("code", body.ref_code).maybe_single().execute()
        )
        if ref_affiliate and ref_affiliate["user_id"] != user_id:
            profile_row["referred_by_code"] = body.ref_code
            profile_row["referred_at"] = datetime.now(timezone.utc).isoformat()
            recruited_by_user_id = ref_affiliate["user_id"]
    sb.table("profiles").upsert(profile_row).execute()

    sb.table("affiliate_applications").insert({
        "user_id": user_id,
        "full_name": body.full_name,
        "email": email,
        "platform": body.platform,
        "channel_url": body.channel_url,
        "audience_size": body.audience_size,
        "why_join": body.why_join,
        "how_promote": body.how_promote,
    }).execute()

    affiliate = None
    for _ in range(5):
        code = _generate_code(body.handle or body.full_name)
        try:
            insert_row = {"user_id": user_id, "code": code, "source": "application"}
            if recruited_by_user_id:
                insert_row["recruited_by_user_id"] = recruited_by_user_id
            affiliate = single_data(sb.table("affiliates").insert(insert_row).execute())
            break
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                continue
            raise
    if not affiliate:
        raise HTTPException(status_code=500, detail="Account created but could not generate a referral code — contact support")

    _award_xp(sb, user_id, "join", XP_VALUES["join"], "Applied to the affiliate program")
    if recruited_by_user_id:
        _award_xp(sb, recruited_by_user_id, f"recruit_{user_id}", XP_VALUES["recruit"], "A creator joined under your link")

    # Sign them in immediately so the frontend gets a real session and can
    # land straight on the dashboard — no separate login step after applying.
    session = None
    try:
        signed_in = sb.auth.sign_in_with_password({"email": email, "password": body.password})
        if signed_in.session:
            session = {
                "access_token": signed_in.session.access_token,
                "refresh_token": signed_in.session.refresh_token,
            }
    except Exception:
        pass  # account + affiliate row still succeeded; frontend can fall back to /signin

    return {"affiliate": affiliate, "user_id": user_id, "session": session}


class ApplyOAuthRequest(BaseModel):
    full_name: str | None = None
    platform: str | None = None
    channel_url: str | None = None
    audience_size: str | None = None
    why_join: str | None = None
    how_promote: str | None = None
    handle: str | None = None
    ref_code: str | None = None


@router.post("/apply-oauth")
async def apply_oauth(body: ApplyOAuthRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Same destination as /apply (provision an affiliate, flag
    is_affiliate_only, log the application) but for someone who already
    has a real Supabase Auth session because they just signed up via
    Google on /affiliates/apply — Supabase creates auth.users itself on
    the OAuth round-trip, so there's no password step and nothing to
    create here, just the affiliate-specific provisioning. Called from
    AuthCallback.jsx right after that round-trip completes.

    Google sign-in is shared with the regular product, so the email that
    just authenticated might belong to an EXISTING FASS Flow customer who
    happened to start from the apply page instead of /affiliates inside
    the app. Walling a real customer into is_affiliate_only would cut them
    off from the product they pay for — so anyone with a business_profiles
    row is treated like the in-app self-serve join() above: they get an
    affiliate row, but keep full product access. Only a genuinely new
    signup (no business yet) gets the curated application entry."""
    sb = get_supabase()
    user_id = current_user.id

    existing_affiliate = single_data(
        sb.table("affiliates").select("*").eq("user_id", user_id).maybe_single().execute()
    )
    if existing_affiliate:
        return {"affiliate": existing_affiliate, "user_id": user_id}

    is_existing_customer = single_data(
        sb.table("business_profiles").select("user_id").eq("user_id", user_id).maybe_single().execute()
    ) is not None

    recruited_by_user_id = None
    if body.ref_code:
        ref_affiliate = single_data(
            sb.table("affiliates").select("user_id").eq("code", body.ref_code).maybe_single().execute()
        )
        if ref_affiliate and ref_affiliate["user_id"] != user_id:
            recruited_by_user_id = ref_affiliate["user_id"]

    if not is_existing_customer:
        profile_row = {"id": user_id, "is_affiliate_only": True}
        if body.full_name:
            profile_row["full_name"] = body.full_name
        if recruited_by_user_id:
            profile_row["referred_by_code"] = body.ref_code
            profile_row["referred_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("profiles").upsert(profile_row).execute()

        sb.table("affiliate_applications").insert({
            "user_id": user_id,
            "full_name": body.full_name,
            "email": current_user.email or "",
            "platform": body.platform,
            "channel_url": body.channel_url,
            "audience_size": body.audience_size,
            "why_join": body.why_join,
            "how_promote": body.how_promote,
        }).execute()

    affiliate = None
    for _ in range(5):
        code = _generate_code(body.handle or body.full_name)
        try:
            insert_row = {
                "user_id": user_id,
                "code": code,
                "source": "self_serve" if is_existing_customer else "application",
            }
            if recruited_by_user_id:
                insert_row["recruited_by_user_id"] = recruited_by_user_id
            affiliate = single_data(sb.table("affiliates").insert(insert_row).execute())
            break
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                continue
            raise
    if not affiliate:
        raise HTTPException(status_code=500, detail="Signed in but could not generate a referral code — contact support")

    note = "Joined the affiliate program" if is_existing_customer else "Applied to the affiliate program"
    _award_xp(sb, user_id, "join", XP_VALUES["join"], note)
    if recruited_by_user_id:
        _award_xp(sb, recruited_by_user_id, f"recruit_{user_id}", XP_VALUES["recruit"], "A creator joined under your link")

    return {"affiliate": affiliate, "user_id": user_id, "is_affiliate_only": not is_existing_customer}


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
        .select("created_at, source")
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

    # Per-channel rollup — "where did it come from." Clicks are bucketed by
    # their own ?src= tag; conversions by the referred_source captured at
    # attribution time (the channel of the FIRST click that brought the
    # customer in). Override earnings are excluded — they're a cut of a
    # recruit's sales, not tied to any click channel of this affiliate.
    channel_map: dict[str, dict] = {}

    def _bucket(name: str | None) -> dict:
        key = name or "untagged"
        if key not in channel_map:
            channel_map[key] = {"source": key, "clicks": 0, "conversions": 0, "earned": 0.0}
        return channel_map[key]

    for c in clicks:
        _bucket(c.get("source"))["clicks"] += 1
    for c in conversions:
        if c.get("source") == OVERRIDE_SOURCE:
            continue
        b = _bucket(c.get("referred_source"))
        b["conversions"] += 1
        b["earned"] += c["commission_amount"]
    channels = sorted(channel_map.values(), key=lambda x: (x["earned"], x["clicks"]), reverse=True)
    for ch in channels:
        ch["earned"] = round(ch["earned"], 2)

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
        "channels": channels,
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
    source: str | None = None   # ?src= channel tag (discord/reddit/tiktok/…)


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
        "source": _clean_source(body.source),
    }).execute()
    return {"tracked": True}


class AttributeRequest(BaseModel):
    user_id: str
    code: str
    source: str | None = None   # channel the first attributing click came from


@router.post("/attribute")
async def attribute(body: AttributeRequest, current_user: CurrentUser = Depends(get_current_user)):
    """First click wins — only ever sets referred_by_code if it isn't
    already set, so a user clicking a second creator's link later doesn't
    steal credit from whoever actually brought them in. The channel
    (referred_source) is captured in the same first-click-wins move, so a
    conversion can later be traced back to the exact placement (Discord vs.
    TikTok vs. newsletter) that originally brought this user in."""
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

    sb.table("profiles").upsert({
        "id": body.user_id,
        "referred_by_code": body.code,
        "referred_source": _clean_source(body.source),
    }).execute()
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

    # Application status, for affiliates whose source is 'application' —
    # self_serve affiliates never have a row here, so this stays None for them.
    applications = (
        sb.table("affiliate_applications")
        .select("user_id, status, created_at, platform, channel_url")
        .in_("user_id", user_ids)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    application_by_user = {}
    for app_row in applications:
        application_by_user.setdefault(app_row["user_id"], app_row)  # most recent only

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
        a["application"] = application_by_user.get(a["user_id"])

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
    application = single_data(
        sb.table("affiliate_applications")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    return {"conversions": conversions, "payouts": payouts, "recruits": recruits, "application": application}


class ApplicationReviewRequest(BaseModel):
    user_id: str
    status: str  # 'approved' | 'rejected'
    notes: str = ""


@router.post("/admin/applications/review")
async def admin_review_application(body: ApplicationReviewRequest, x_admin_secret: str = Header(None)):
    """Approve/reject is curation-only — it does NOT pause the affiliate's
    ability to earn or share their link (that's controlled separately via
    affiliates.status, same as any self-serve affiliate). This just lets
    admin track who's been vetted, e.g. for featuring in marketing or
    deciding who gets a higher commission tier later."""
    _check_admin_secret(x_admin_secret)
    if body.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'approved' or 'rejected'")

    sb = get_supabase()
    application = single_data(
        sb.table("affiliate_applications")
        .select("*")
        .eq("user_id", body.user_id)
        .order("created_at", desc=True)
        .limit(1)
        .maybe_single()
        .execute()
    )
    if not application:
        raise HTTPException(status_code=404, detail="No application found for that user_id")

    updated = single_data(
        sb.table("affiliate_applications")
        .update({
            "status": body.status,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "notes": body.notes or None,
        })
        .eq("id", application["id"])
        .execute()
    )
    return {"application": updated}


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
